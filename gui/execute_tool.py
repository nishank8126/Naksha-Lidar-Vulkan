TOOL_MAP = {
    "AboveLine": "above_line",
    "BelowLine": "below_line",
    "ParallelLine": "parallel_line",
    "Rectangle": "rectangle",
    "Circle": "circle",
    "Polygon": "polygon",
    "Freehand": "freehand",
    "Brush": "brush",
    "Point": "point",
    "TempFence": "temp_fence",
    "CrossSectionRect": "cross_section",
    "CutSectionRect": "cut_section",
    "CutFromCross": "CutFromCross",  
    "CutFromCut": "CutFromCut",  
    "TopView": "top_view",
    "MeasureLine": "measure_line",
    "MeasurePath": "measure_path",
    "ClearMeasurements": "clear_measurements",
    "Pan": "pan",
    "DisplayMode": "display_mode",
    "ShadingMode": "shading_mode",
    "Surface": "surface",
    "SyncViews":   "sync_views",    
    "Save": "save",
    "SaveAs": "save_as",
    "AttachSNT": "attach_snt",
}

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


def _get_live_class_picker(app_window):
    """Return a live ClassPicker or None, and clear stale wrappers eagerly."""
    picker = getattr(app_window, "class_picker", None)
    if picker is None:
        return None
    try:
        if not _qt_object_is_valid(picker):
            app_window.class_picker = None
            return None
        # Touching a QObject method catches deleted C++ object wrappers.
        picker.objectName()
        return picker
    except (RuntimeError, ReferenceError, AttributeError):
        try:
            app_window.class_picker = None
        except Exception:
            pass
        return None
    except Exception:
        try:
            app_window.class_picker = None
        except Exception:
            pass
        return None


def _class_picker_is_visible(picker) -> bool:
    if picker is None:
        return False
    try:
        return bool(picker.isVisible())
    except (RuntimeError, ReferenceError, AttributeError):
        return False
    except Exception:
        return False


def _apply_display_visibility_preset(app_window, preset) -> int:
    """Apply a saved class-visibility map without replacing live metadata."""
    if not isinstance(preset, dict):
        return 0
    raw_classes = preset.get("classes", {}) or {}
    visibility = {}
    for raw_code, info in raw_classes.items():
        if not isinstance(info, dict):
            continue
        try:
            visibility[int(raw_code)] = bool(info.get("show", True))
        except (TypeError, ValueError):
            continue
    if not visibility:
        return 0

    palettes = []
    class_palette = getattr(app_window, "class_palette", None)
    if isinstance(class_palette, dict):
        palettes.append(class_palette)

    app_view_palettes = getattr(app_window, "view_palettes", None)
    if isinstance(app_view_palettes, dict):
        slot_zero = app_view_palettes.get(0)
        if isinstance(slot_zero, dict) and slot_zero is not class_palette:
            palettes.append(slot_zero)

    dialog = (
        getattr(app_window, "display_mode_dialog", None)
        or getattr(app_window, "display_dialog", None)
    )
    dialog_palettes = getattr(dialog, "view_palettes", None) if dialog else None
    if isinstance(dialog_palettes, dict):
        slot_zero = dialog_palettes.get(0)
        if isinstance(slot_zero, dict) and all(slot_zero is not p for p in palettes):
            palettes.append(slot_zero)

    for palette in palettes:
        for code, is_visible in visibility.items():
            entry = palette.get(code)
            if isinstance(entry, dict):
                entry["show"] = is_visible

    current_slot = int(getattr(dialog, "current_slot", 0) or 0) if dialog else 0
    table = getattr(dialog, "table", None) if dialog and current_slot == 0 else None
    if table is not None:
        for row in range(table.rowCount()):
            code_item = table.item(row, 1)
            checkbox = table.cellWidget(row, 0)
            if code_item is None or checkbox is None:
                continue
            try:
                code = int(str(code_item.text()).strip())
            except (TypeError, ValueError):
                continue
            if code not in visibility or not hasattr(checkbox, "setChecked"):
                continue
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(visibility[code])
            finally:
                checkbox.blockSignals(False)

    return sum(1 for value in visibility.values() if value)


def _sync_display_visibility_to_renderer(
    app_window, preset, border_percent=0.0, render=True
) -> bool:
    """Push shortcut visibility to the unified actor without replacing mode colors."""
    if not isinstance(preset, dict) or not (preset.get("classes") or {}):
        return False

    palette = getattr(app_window, "class_palette", None)
    if not isinstance(palette, dict) or not palette:
        return False

    try:
        from gui.unified_actor_manager import sync_palette_to_gpu

        return bool(sync_palette_to_gpu(
            app_window,
            slot_idx=0,
            palette=palette,
            border=float(border_percent),
            render=bool(render),
            rewrite_rgb=False,
        ))
    except Exception as exc:
        print(f"   Warning: display visibility renderer sync failed: {exc}")
        return False


def _schedule_curve_tool_resume(app_window, reason="mode switch"):
    """
    Auto-resume the curve tool on the next Qt event-loop tick, once the
    mode switch that triggered the suspend() has fully completed.

    Why this exists: suspend() alone leaves the curve paused indefinitely.
    Nothing was calling resume() back automatically, so in practice the
    curve only "woke up" as an accidental side effect of some unrelated
    interaction (e.g. a middle-click pan) passing through digitizer code
    that happened to re-arm the last-selected tool. From the user's POV
    that reads as "I have to reactivate the curve tool" after every
    Intensity/Depth/RGB/Elevation switch — unlike ShadingMode, which never
    touches curve_tool at all and therefore never interrupts it.

    QTimer.singleShot(0, ...) guarantees this runs only AFTER the current
    call stack (the mode switch itself) has fully returned to the Qt event
    loop, so we never resume mid-actor-rebuild.
    """
    from PySide6.QtCore import QTimer

    def _do_resume():
        ct = getattr(app_window, 'curve_tool', None)
        if ct is None:
            return
        # Guard against double-resume: something else may have already
        # resumed (or the user cancelled) in the meantime.
        if getattr(ct, 'active', False):
            return
        if not getattr(ct, '_suspended_state', None):
            return
        # Only auto-resume if curve is still the tool the user intends to
        # keep using — don't yank them back into curve mode if they
        # explicitly switched to a different draw tool since we suspended.
        if not getattr(app_window, '_draw_curve_context_active', False):
            return
        if hasattr(ct, 'resume'):
            ct.resume()
            print(f"   🔮 Curve tool auto-resumed after {reason}")

    QTimer.singleShot(0, _do_resume)


def _deactivate_curve_tool_safely(app_window, reason="switching tools"):
    """
    Safely deactivate curve tool in all modes.
    Called when switching to any other tool context.
    """
    if not hasattr(app_window, 'curve_tool') or app_window.curve_tool is None:
        return
    
    ct = app_window.curve_tool
    
    # Check if curve tool has any active mode
    is_active = ct.active
    is_select_mode = getattr(ct, '_select_mode', False)
    
    if not is_active and not is_select_mode:
        return
    
    print(f"   🎨 Deactivating curve tool ({reason})")
    
    # Cancel active drawing if in progress
    if is_active:
        if hasattr(ct, "suspend"):
            ct.suspend()
            # ✅ FIX: don't leave it suspended — re-arm it once this mode
            # switch finishes, so the very next canvas click continues the
            # curve instead of requiring the user to reselect the tool.
            _schedule_curve_tool_resume(app_window, reason)
        else:
            ct._cancel_curve()
    
    # Exit select mode if active
    if is_select_mode:
        ct.deactivate_select_mode()


def _deactivate_measurement_tool_safely(app_window, reason="switching tools"):
    """
    Safely deactivate measurement tool.
    Called when switching to classification or section tools.
    """
    if not hasattr(app_window, 'measurement_tool') or app_window.measurement_tool is None:
        return
    
    mt = app_window.measurement_tool
    
    if not getattr(mt, 'active', False) and not getattr(mt, 'is_measuring', False):
        return
    
    print(f"   📏 Deactivating measurement tool ({reason})")
    
    if hasattr(mt, 'deactivate'):
        try:
            mt.deactivate()
        except Exception as e:
            print(f"      ⚠️ Measurement deactivate failed: {e}")
    
    # Force flags even if deactivate() didn't clear them
    mt.active = False
    mt.is_measuring = False
    if hasattr(mt, 'is_drawing'):
        mt.is_drawing = False


def _deactivate_temp_fence_safely(app_window, reason="switching tools"):
    """
    Safely stand down the temp fence tool.
    Called when switching to cross-section / cut-section (or any tool that
    bypasses set_classify_tool and would otherwise leave the fence's VTK
    observers capturing clicks).
    """
    tft = getattr(app_window, 'temp_fence_tool', None)
    if tft is None:
        return

    if not getattr(tft, 'active', False):
        return

    print(f"   🚧 Deactivating temp fence tool ({reason})")

    if hasattr(tft, 'deactivate'):
        try:
            tft.deactivate()
        except Exception as e:
            print(f"      ⚠️ Temp fence deactivate failed: {e}")

    # Force flags even if deactivate() didn't clear them
    tft.active = False
    if getattr(app_window, 'active_classify_tool', None) == 'temp_fence':
        app_window.active_classify_tool = None


def execute_tool(app_window, tool, from_cls=None, to_cls=None, preset=None, key_label=""):
    """
    Execute a classification or cross-section tool.
    ✅ FIXED: Uses open_next_cross_section_view() for direct activation (no dialog)
    ✅ FIXED: Preserves classification parameters across view changes
    ✅ FIXED: Returns focus to correct view for immediate use
    ✅ NEW: Handles DisplayMode and ShadingMode presets
    ✅ FIXED: Properly deactivates curve/measurement tools when switching contexts
    ✅ NEW: ``key_label`` is prepended to status-bar feedback so the user sees the exact pressed key.
    """
    _kp = f"[{key_label}] " if key_label else ""
    
    print(f"🔧 execute_tool called: tool={tool}, from_cls={from_cls}, to_cls={to_cls}, preset={preset is not None}")
    
    tool_name = TOOL_MAP.get(tool, tool.lower())

    if tool_name != "element_selection":
        deactivate_selection = getattr(app_window, "_deactivate_selection_tools", None)
        if callable(deactivate_selection):
            deactivate_selection(f"switching to {tool_name} via shortcut")

    if tool_name not in ("cut_section", "CutFromCross", "CutFromCut"):
        cleanup_cut = getattr(app_window, "_deactivate_pending_cut_section_tool", None)
        if cleanup_cut:
            cleanup_cut(f"switching to {tool_name} via shortcut")
    
    # ✅ NEW: If switching AWAY from cross-section while it was active, deactivate it properly 
    if tool_name != "cross_section" and getattr(app_window, "cross_section_active", False):
        print(f"🛑 Deactivating cross-section (switching to {tool_name} via shortcut)")
        if hasattr(app_window, "_cancel_cross_section_tool_only"):
            app_window._cancel_cross_section_tool_only()

    # Handle empty list case
    if from_cls is not None and isinstance(from_cls, list) and len(from_cls) == 0:
        from_cls = None

    # ========================================================================
    # PAN TOOL (mutually exclusive navigation shortcut)
    # ========================================================================
    if tool_name == "pan":
        print("Switching to Pan - deactivating tools that own left click")

        deactivate_identification = getattr(
            app_window, "_deactivate_active_identification_tools_for_escape", None
        )
        if callable(deactivate_identification):
            deactivate_identification()

        class_picker = _get_live_class_picker(app_window)
        classification_active = bool(
            getattr(app_window, "active_classify_tool", None)
            or _class_picker_is_visible(class_picker)
            or getattr(app_window, "classify_interactor", None)
            or getattr(app_window, "classify_interactors", None)
            or getattr(app_window, "cut_classify_interactor", None)
        )
        if classification_active:
            deactivate_classification = getattr(
                app_window, "deactivate_classification_tool", None
            )
            if callable(deactivate_classification):
                deactivate_classification()
            app_window.active_classify_tool = None

        _deactivate_measurement_tool_safely(app_window, "switching to Pan")

        deactivate_digitize = getattr(app_window, "_deactivate_digitize_tool", None)
        if callable(deactivate_digitize):
            deactivate_digitize()
        else:
            digitizer = getattr(app_window, "digitizer", None)
            deactivate_draw = getattr(
                digitizer, "_deactivate_active_tool_keep_drawings", None
            )
            if callable(deactivate_draw):
                deactivate_draw()

        curve_tool = getattr(app_window, "curve_tool", None)
        if curve_tool is not None:
            app_window._draw_curve_context_active = False
            if getattr(curve_tool, "active", False):
                suspend = getattr(curve_tool, "suspend", None)
                cancel = getattr(curve_tool, "_cancel_curve", None)
                if callable(suspend):
                    suspend()
                elif callable(cancel):
                    cancel()
            deactivate_select = getattr(curve_tool, "deactivate_select_mode", None)
            if getattr(curve_tool, "_select_mode", False) and callable(deactivate_select):
                deactivate_select()

        # Do not close completed cross/cut docks; only their active placement
        # tools are cancelled by the generic cleanup above.
        _return_focus_to_main_view(app_window)
        print("Pan tool cleanup complete - all left-click tools deactivated")
        return

    # ========================================================================
    # ✅ NEW: DISPLAY MODE PRESET (Non-classification)
    # ========================================================================
    if tool_name == "display_mode":
        print("🎨 Applying DisplayMode preset from shortcut")
        
        # ✅ Deactivate curve tool when switching to display mode
        _deactivate_curve_tool_safely(app_window, "switching to DisplayMode")
        
        if preset is None:
            print("   ⚠️ No preset provided for DisplayMode")
            return
        
        try:
            # ✅ ADD THIS: Clear shading actors when switching to DisplayMode
            print(f"   🧹 Clearing shading mode actors...")
            if hasattr(app_window, '_shaded_mesh_actor') and app_window._shaded_mesh_actor:
                app_window.vtk_widget.remove_actor('shaded_mesh', render=False)
                app_window._shaded_mesh_actor = None
                print(f"      ✅ Removed shading mesh actor")
            
            if hasattr(app_window, '_shaded_mesh_polydata'):
                app_window._shaded_mesh_polydata = None
            
            # Clear shading cache
            from gui.shading_display import clear_shading_cache
            clear_shading_cache("switching to DisplayMode")
            
# ── REBASE: discard stale description/color/lvl/draw
            #            and refill from the currently loaded PTC ──
            from gui.shortcut_manager import rebase_display_preset_to_current_ptc
            preset = rebase_display_preset_to_current_ptc(preset, app_window)

            # ✅ Extract multi-view preset data
            views = preset.get("views", {})
            border_percent = preset.get("border_percent", 0)
            
            if not views:
                print("   ⚠️ No views configured in preset")
                return
            
            print(f"\n{'='*60}")
            print(f"🎨 APPLYING DISPLAYMODE PRESET")
            print(f"{'='*60}")
            
            # ✅ CRITICAL: Initialize app_window.view_palettes with CORRECT WEIGHTS
            if not hasattr(app_window, 'view_palettes'):
                app_window.view_palettes = {}
            
            # Set default weights based on view type
            for view_idx in range(6):  # 0=Main, 1-4=Cross-sections, 5=Cut
                if view_idx == 0:
                    default_weight = 1.0  # Main View
                else:
                    default_weight = 0.5  # All others
                
                app_window.view_palettes[view_idx] = {}
            
            # ✅ Now apply preset values, overriding defaults only where preset has data
            for view_idx_str, classes in views.items():
                view_idx = int(view_idx_str)
                
                print(f"\n   Processing View {view_idx}:")
                
                # Get default weight for this view type
                default_weight = 1.0 if view_idx == 0 else 0.5
                
                # Copy all class info from preset
                for code_str, info in classes.items():
                    code_int = int(code_str)
                    
                    # ✅ Use weight from preset if available, otherwise use default
                    preset_weight = info.get("weight", default_weight)
                    
                    app_window.view_palettes[view_idx][code_int] = {
                        "show": info.get("show", False),
                        "description": info.get("description", ""),
                        "color": info.get("color", (128, 128, 128)),
                        "weight": preset_weight,
                        "draw": info.get("draw", ""),
                        "lvl": info.get("lvl", "")
                    }
                
                # Print summary
                visible = sum(1 for c in app_window.view_palettes[view_idx].values() if c.get("show"))
                weights = set(c.get("weight") for c in app_window.view_palettes[view_idx].values())
                view_name = "Main" if view_idx == 0 else f"View {view_idx}"
                print(f"      ✅ {view_name}: {len(app_window.view_palettes[view_idx])} classes")
                print(f"      📊 Visible: {visible}")
                print(f"      ⚖️ Weights: {weights}")
            
            # ✅ Also update class_palette from view 0
            if 0 in views:
                app_window.class_palette = {}
                for code_str, info in views[0].items():
                    code_int = int(code_str)
                    app_window.class_palette[code_int] = {
                        "show": info.get("show", False),
                        "description": info.get("description", ""),
                        "color": info.get("color", (128, 128, 128)),
                        "weight": info.get("weight", 1.0),
                        "draw": info.get("draw", ""),
                        "lvl": info.get("lvl", "")
                    }
            
            # Trigger refresh
            from gui.class_display import update_class_mode
            app_window._preserve_view = True
            update_class_mode(app_window, force_refresh=True)
            
            if hasattr(app_window, "statusBar"):
                app_window.statusBar().showMessage(
                    f"{_kp}✅ DisplayMode: Main=1.0, Views 1-5=0.5",
                    2000
                )
            
            print(f"{'='*60}")
            print(f"✅ Weights set: Main=1.0, Views 1-5=0.5")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"⚠️ Failed: {e}")
            import traceback
            traceback.print_exc()
        
        return

    # ========================================================================
    # ✅ NEW: SHADING MODE PRESET (Non-classification)
    # ========================================================================
    if tool_name == "shading_mode":
        print("🌗 Applying ShadingMode preset from shortcut")
        
        # ✅ Deactivate curve tool when switching to shading mode
        _deactivate_curve_tool_safely(app_window, "switching to ShadingMode")
        
        if preset is None:
            print("   ⚠️ No preset provided for ShadingMode")
            return
        
        try:
            # Extract shading parameters
            azimuth = preset.get("azimuth", 45.0)
            angle = preset.get("angle", 45.0)
            ambient = preset.get("ambient", 0.1)
            quality = preset.get("quality", 100.0)
            speed = preset.get("speed", 1)
            classes = preset.get("classes", {})
            
            print(f"   🌗 Shading: az={azimuth}°, angle={angle}°, ambient={ambient}")
            print(f"   📋 Classes: {len(classes)} configured")
            
            # Apply shading parameters to panel
            if hasattr(app_window, 'shading_panel'):
                panel = app_window.shading_panel
                
                if hasattr(panel, 'az_spin'):
                    panel.az_spin.setValue(azimuth)
                if hasattr(panel, 'el_spin'):
                    panel.el_spin.setValue(angle)
                if hasattr(panel, 'quality_spin'):
                    panel.quality_spin.setValue(quality)
                if hasattr(panel, 'speed_spin'):
                    panel.speed_spin.setValue(speed)
                
                print("   ✅ Shading parameters set in panel")
            
            # Apply ambient
            app_window.shade_ambient = ambient
            
            # ✅ CRITICAL: Only update visibility for classes in the preset
            if classes and hasattr(app_window, 'class_palette'):
                for code, info in classes.items():
                    code_int = int(code)
                    if code_int in app_window.class_palette:
                        app_window.class_palette[code_int]["show"] = info.get("show", False)
                
                print(f"   ✅ Updated visibility for {len(classes)} classes")
            
            # Trigger the actual shaded-class pipeline, not the generic alias.
            if hasattr(app_window, 'set_display_mode'):
                app_window.set_display_mode("shaded_class")
                print("   ✅ Set shaded_class mode")
            elif hasattr(app_window, 'on_display_changed'):
                app_window.on_display_changed("shaded_class", force_refresh=True)
                print("   ✅ Triggered shaded_class mode")
            
            # Clear any active classification tool
            app_window.active_classify_tool = None
            
            # Return focus
            _return_focus_to_main_view(app_window)
            
            visible_count = sum(1 for c in classes.values() if c.get("show")) if classes else 0
            
            if hasattr(app_window, "statusBar"):
                app_window.statusBar().showMessage(
                    f"{_kp}🌗 Shading preset applied: {azimuth}°/{angle}°, {visible_count} classes visible",
                    2000
                )
            
            print(f"   ✅ ShadingMode preset applied successfully")
            
        except Exception as e:
            print(f"⚠️ ShadingMode preset application failed: {e}")
            import traceback
            traceback.print_exc()
        
        return

    # ========================================================================
    # VIEW NAVIGATION TOOLS (Non-classification)
    # ========================================================================
    # ========================================================================
    # SYNC VIEWS PRESET
    # ========================================================================
    if tool_name == "sync_views":
        print("🔗 Applying SyncViews preset from shortcut")
        if preset is None:
            print("   ⚠️ No preset provided for SyncViews")
            return
        try:
            rows = preset.get("rows", [])
            if not rows:
                print("   ⚠️ Empty SyncViews preset — nothing to apply")
                return

            # Build desired mapping: target_idx → source_idx
            existing = dict(getattr(app_window, "view_sync_map", {}) or {})
            desired  = {}
            sources_no_sync = set()

            for r in rows:
                source_view_num = int(r.get("view", 1))
                mode            = int(r.get("mode", 0))
                source_idx      = source_view_num - 1

                if mode == 0:
                    sources_no_sync.add(source_idx)
                    continue

                target_view_num = int(r.get("source", 1))
                target_idx      = target_view_num - 1
                if target_idx == source_idx:
                    continue
                desired[target_idx] = source_idx

            # Clear mappings no longer wanted
            for target_idx, src_idx in existing.items():
                if target_idx in desired and desired[target_idx] != src_idx:
                    app_window.set_view_sync(target_idx + 1, None)
                elif target_idx not in desired and src_idx in sources_no_sync:
                    app_window.set_view_sync(target_idx + 1, None)

            # Apply new mappings
            for target_idx, source_idx in desired.items():
                app_window.set_view_sync(target_idx + 1, source_idx + 1)

            if hasattr(app_window, "statusBar"):
                if desired:
                    parts = [f"V{s+1}→V{t+1}" for t, s in desired.items()]
                    app_window.statusBar().showMessage(
                        f"{_kp}🔗 Sync applied: {', '.join(parts)}", 2000
                    )
                else:
                    app_window.statusBar().showMessage(f"{_kp}🔕 All syncs cleared", 2000)

            print(f"   ✅ SyncViews applied: {len(desired)} active sync(s)")
        except Exception as e:
            print(f"⚠️ SyncViews preset application failed: {e}")
            import traceback
            traceback.print_exc()
        return

    if tool == "Surface" and preset is not None:
        print("🏔️ Applying Surface preset from shortcut")
        try:
            azimuth = float(preset.get("azimuth", getattr(app_window, "last_shade_azimuth", 45.0)))
            angle = float(preset.get("angle", getattr(app_window, "last_shade_angle", 45.0)))
            ambient = float(preset.get("ambient", getattr(app_window, "shade_ambient", 0.1)))
            max_edge = float(preset.get("max_edge", getattr(app_window, "surface_max_edge", 0.0) or 0.0))
            classes = preset.get("classes", {}) or {}

            # Surface should always resolve classes against the live palette so
            # shortcut presets never pin stale metadata from an older PTC.
            # ✅ Same rule as ShadingMode's preset handler: only overwrite
            # visibility for classes the saved preset actually mentions.
            # Classes absent from the preset (e.g. new since the shortcut was
            # last configured, or a palette that drifted mid-classification)
            # keep their current live "show" state instead of being forced
            # hidden — that forced-hidden default was what could zero out
            # every point and blank the main view.
            live_palette = getattr(app_window, "class_palette", {}) or {}
            visible_codes = set()
            surface_slot_palette = {}
            for code, live in live_palette.items():
                try:
                    key = int(code)
                except Exception:
                    continue
                cloned = dict(live) if isinstance(live, dict) else {}
                incoming = classes.get(key, classes.get(str(key)))
                if incoming is not None:
                    cloned["show"] = bool(incoming.get("show", False))
                is_visible = bool(cloned.get("show", True))
                surface_slot_palette[key] = cloned
                if is_visible:
                    visible_codes.add(key)

            app_window.surface_max_edge = max_edge
            app_window.last_shade_max_edge = max_edge
            app_window.last_shade_azimuth = azimuth
            app_window.last_shade_angle = angle
            app_window.shade_ambient = ambient

            dlg = getattr(app_window, "display_mode_dialog", None) or getattr(app_window, "display_dialog", None)
            app_view_palettes = getattr(app_window, "view_palettes", None)
            if isinstance(app_view_palettes, dict):
                app_view_palettes[0] = {code: dict(info) for code, info in surface_slot_palette.items()}
            if dlg is not None and hasattr(dlg, "view_palettes"):
                dlg.view_palettes[0] = {code: dict(info) for code, info in surface_slot_palette.items()}
                if hasattr(dlg, "slot_shows"):
                    dlg.slot_shows.setdefault(0, {})
                    dlg.slot_shows[0] = {code: bool(info.get("show", False)) for code, info in surface_slot_palette.items()}
                if hasattr(dlg, "view_configs"):
                    dlg.view_configs[0] = {int(code): dict(info) for code, info in surface_slot_palette.items()}

            if dlg is not None:
                try:
                    if hasattr(dlg, "slot_box") and dlg.current_slot != 0:
                        dlg.slot_box.blockSignals(True)
                        dlg.slot_box.setCurrentIndex(0)
                        dlg.slot_box.blockSignals(False)
                    # NOTE: do NOT call dlg.on_slot_changed(0) / sync_with_app_state()
                    # here. on_slot_changed() does save-then-load: when
                    # dlg.current_slot is already 0 (the common case for Main
                    # View), its "save outgoing slot" step (_save_slot_state)
                    # reads the table's STALE checkbox widgets and writes them
                    # straight into dlg.view_palettes[0], clobbering the
                    # single-class surface_slot_palette we just assigned above.
                    # We only want to PAINT the table from the palette we
                    # already set — never re-save the table into it first.
                    dlg.current_slot = 0
                    if hasattr(dlg, "_refresh_classes"):
                        dlg._refresh_classes()
                    if hasattr(dlg, "_load_slot_checkboxes"):
                        dlg._load_slot_checkboxes(0)
                    elif hasattr(dlg, "_load_slot_state"):
                        dlg._load_slot_state(0)
                    if hasattr(dlg, "update_border_display"):
                        dlg.update_border_display()
                    if hasattr(dlg, "_sync_color_mode_state"):
                        dlg._sync_color_mode_state()
                except Exception as sync_err:
                    print(f"⚠️ Surface dialog sync failed: {sync_err}")
                    
        except Exception as e:
            print(f"⚠️ Surface preset application failed before mode switch: {e}")
            import traceback
            traceback.print_exc()

    if tool_name in ("measure_line", "measure_path"):
        print(f"📏 Activating measurement shortcut: {tool_name}")

        # ✅ Deactivate curve tool when switching to measurement
        _deactivate_curve_tool_safely(app_window, "switching to measurement")

        try:
            if hasattr(app_window, "_sync_tools_for_ribbon_tab"):
                app_window._sync_tools_for_ribbon_tab("measure")
            elif hasattr(app_window, "_enter_measure_tab_mode"):
                app_window._enter_measure_tab_mode()

            if hasattr(app_window, "activate_measurement_tool"):
                app_window.activate_measurement_tool(tool_name)
                _return_focus_to_main_view(app_window)
                print(f"   ✅ Measurement tool activated: {tool_name}")
            else:
                print("   ⚠️ Measurement activation handler not available")
        except Exception as e:
            print(f"⚠️ Measurement shortcut failed: {e}")
            import traceback
            traceback.print_exc()
        return
    
    if tool_name in ("save", "save_as"):
        print(f"💾 {'Save' if tool_name == 'save' else 'Save As'} triggered via shortcut")
        try:
            if tool_name == "save":
                if hasattr(app_window, "_save_quick_no_dialog"):
                    app_window._save_quick_no_dialog()
                else:
                    from .save_pointcloud import save_pointcloud
                    save_pointcloud(app_window, path=None, show_dialog=False)
            else:
                from .save_pointcloud import save_pointcloud
                save_pointcloud(app_window, path=None, show_dialog=True)
            print(f"   ✅ {'Save' if tool_name == 'save' else 'Save As'} complete")
        except Exception as e:
            print(f"⚠️ {'Save' if tool_name == 'save' else 'Save As'} shortcut failed: {e}")
            import traceback
            traceback.print_exc()
        return

    if tool_name == "attach_snt":
        print("📎 Opening Attach SNT dialog via shortcut")
        try:
            from .snt_attachment import show_snt_attachment_dialog
            app_window.snt_dialog = show_snt_attachment_dialog(app_window)
        except Exception as e:
            print(f"⚠️ Attach SNT shortcut failed: {e}")
            import traceback
            traceback.print_exc()
        return

    if tool_name == "clear_measurements":
        print("🗑️ Clearing measurements from shortcut")

        try:
            if hasattr(app_window, "clear_all_measurements"):
                app_window.clear_all_measurements()
            elif hasattr(app_window, "measurement_tool") and app_window.measurement_tool:
                app_window.measurement_tool.clear_all_measurements()
            else:
                print("   ⚠️ No measurement tool available to clear")
                return

            _return_focus_to_main_view(app_window)
            print("   ✅ Measurements cleared")
        except Exception as e:
            print(f"⚠️ Clear measurements shortcut failed: {e}")
            import traceback
            traceback.print_exc()
        return

    if tool_name == "top_view":
        print("🔝 Activating Top View")
        
        # ✅ Deactivate curve tool when switching to top view
        _deactivate_curve_tool_safely(app_window, "switching to TopView")
        
        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
            
            cam = app_window.vtk_widget.renderer.GetActiveCamera()
            cam.ParallelProjectionOn()
            cam.SetViewUp(0, 1, 0)
            cam.SetPosition(0, 0, 1)
            cam.SetFocalPoint(0, 0, 0)
            
            app_window.vtk_widget.renderer.ResetCamera()
            app_window.vtk_widget.render()
            print("   ✅ Camera configured (top view + orthographic + reset)")
            
            interactor = app_window.vtk_widget.interactor
            style = vtkInteractorStyleImage()
            interactor.SetInteractorStyle(style)
            print("   🔒 Interactor LOCKED to 2D (pan/zoom only, no rotation)")
            
            app_window.current_view = "top"
            app_window.active_classify_tool = None
            
            _return_focus_to_main_view(app_window)
            
            if hasattr(app_window, "statusBar"):
                app_window.statusBar().showMessage(f"{_kp}🔝 Top View LOCKED", 2000)
            
            print("   ✅ Top View switched successfully (LOCKED)")
        except Exception as e:
            print(f"⚠️ Top View activation failed: {e}")
            import traceback
            traceback.print_exc()
        return
    
    # ✅ DISPLAY MODE SHORTCUTS (Non-classification)
    # ========================================================================
    if tool in ("Depth", "RGB", "Intensity", "Elevation", "Line", "Class", "Surface"):
        
        print(f"🎨 Switching to {tool} display mode")
        
        # ✅ Deactivate curve tool when switching display modes
        _deactivate_curve_tool_safely(app_window, f"switching to {tool} mode")
        
        # Clear shading actors
        if hasattr(app_window, '_shaded_mesh_actor') and app_window._shaded_mesh_actor:
            app_window.vtk_widget.remove_actor('shaded_mesh', render=False)
            app_window._shaded_mesh_actor = None
        
        if hasattr(app_window, '_shaded_mesh_polydata'):
            app_window._shaded_mesh_polydata = None
        
        from gui.shading_display import clear_shading_cache
        clear_shading_cache(f"switching to {tool} mode")
        
        mode_map = {
            "Depth": "depth",
            "RGB": "rgb",
            "Intensity": "intensity",
            "Elevation": "elevation",
            "Line": "line",
            "Class": "class",
            "Surface": "surface",
        }
        
        mode = mode_map[tool]

        # Line shortcut can change three independent GPU states: flight-line
        # visibility, class visibility, and RGB presentation. Batch the first
        # two without rendering so set_display_mode("line") performs the one
        # final repaint. This avoids a 13M+ point actor rebuild and duplicate
        # renders while preserving all existing preset semantics.
        _line_flight_prepatched = False
        _line_visibility_presynced = False

        if tool in ("Depth", "RGB", "Intensity", "Elevation", "Line"):
            visible_count = _apply_display_visibility_preset(app_window, preset)
            if isinstance(preset, dict) and preset.get("classes"):
                print(f"   Applied {tool} visibility preset: {visible_count} classes visible")

        if tool == "Line" and isinstance(preset, dict):
            saved_lines = preset.get("flight_lines", {}) or {}
            if saved_lines:
                line_visibility = {
                    int(line_id): bool(shown)
                    for line_id, shown in saved_lines.items()
                }
                by_slot = getattr(
                    app_window, "flight_line_visibility_by_slot", None
                )
                if not isinstance(by_slot, dict):
                    by_slot = {}
                    app_window.flight_line_visibility_by_slot = by_slot
                by_slot[0] = dict(line_visibility)
                app_window.flight_line_visibility = dict(line_visibility)
                cache = getattr(
                    app_window, "_flight_line_mask_cache_by_slot", None
                )
                if isinstance(cache, dict):
                    cache.pop(0, None)
                app_window._flight_line_mask_cache_key = None
                app_window._flight_line_mask_cache = None
                print(
                    "   Applied Line flight-line preset: "
                    f"{sum(line_visibility.values())}/{len(line_visibility)} visible"
                )

                # IMPORTANT: do this BEFORE set_display_mode(). The old order
                # made _get_unified_actor() see a new flight-line signature and
                # incorrectly rebuild the entire point-cloud actor.
                try:
                    from gui.unified_actor_manager import (
                        fast_main_flight_line_visibility_update,
                    )
                    _line_flight_prepatched = bool(
                        fast_main_flight_line_visibility_update(
                            app_window, render=False
                        )
                    )
                    if _line_flight_prepatched:
                        print("   ⚡ Line flight visibility prepatched (no render)")
                except Exception as _line_patch_exc:
                    print(f"   ⚠️ Line prepatch skipped: {_line_patch_exc}")
            app_window._last_display_shortcut_id = None

            # Push class visibility/border uniforms before the RGB mode switch,
            # also without rendering. set_display_mode('line') will repaint once.
            try:
                _line_visibility_presynced = bool(
                    _sync_display_visibility_to_renderer(
                        app_window,
                        preset,
                        border_percent=float(
                            getattr(app_window, "point_border_percent", 0.0) or 0.0
                        ),
                        render=False,
                    )
                )
                if _line_visibility_presynced:
                    print("   ⚡ Line class visibility pre-synced (no render)")
            except Exception as _line_vis_exc:
                print(f"   ⚠️ Line visibility pre-sync skipped: {_line_vis_exc}")
        
        if mode in ['depth', 'rgb', 'intensity', 'elevation', 'surface']:
            print(f"   🔳 Clearing borders for {mode} mode")
            
            app_window.point_border_percent = 0
            app_window._main_view_borders_active = False
            
            if hasattr(app_window, 'display_mode_dialog') and app_window.display_mode_dialog:
                dlg = app_window.display_mode_dialog
                dlg.view_borders[0] = 0
                
                if dlg.isVisible():
                    if hasattr(dlg, 'border_slider'):
                        dlg.border_slider.blockSignals(True)
                        dlg.border_slider.setValue(0)
                        dlg.border_slider.blockSignals(False)
                    if hasattr(dlg, 'border_value_display'):
                        dlg.border_value_display.setText("0%")
                    if hasattr(dlg, 'border_label'):
                        dlg.border_label.setText("🔳 Border: 0%")
            
            print(f"   ✅ Borders set to 0%")
        
        elif mode == 'class':
            if hasattr(app_window, 'shading_class_visibility'):
                print(f"   🗑️ Clearing shading_class_visibility")
                app_window.shading_class_visibility = {}

            if hasattr(app_window, 'display_mode_dialog') and app_window.display_mode_dialog:
                dlg = app_window.display_mode_dialog
                saved_border = dlg.view_borders.get(0, 0)

                if saved_border > 0:
                    app_window.point_border_percent = saved_border
                    app_window._main_view_borders_active = True
                    print(f"   ✅ Restored borders to {saved_border}%")

            palette = getattr(app_window, 'class_palette', {})
            if palette and not any(v.get('show', True) for v in palette.values()):
                for code in palette:
                    palette[code]['show'] = True
                print(f"   ⚠️ All classes were hidden — reset to visible")

        try:
            if hasattr(app_window, 'on_display_changed'):
                app_window.on_display_changed(mode)
                print(f"   ✅ Called on_display_changed({mode})")
            elif hasattr(app_window, 'set_display_mode'):
                app_window.set_display_mode(mode)
                print(f"   ✅ Called set_display_mode({mode})")
            elif hasattr(app_window, 'change_display_mode'):
                app_window.change_display_mode(mode)
                print(f"   ✅ Called change_display_mode({mode})")
            else:
                if hasattr(app_window, 'view_ribbon'):
                    app_window.view_ribbon.display_changed.emit(mode)
                    print(f"   ✅ Emitted display_changed signal: {mode}")
                else:
                    print(f"   ⚠️ No display mode handler found!")
                    return

            if tool in ("Depth", "RGB", "Intensity", "Elevation", "Line"):
                sync_border = (
                    float(getattr(app_window, "point_border_percent", 0.0) or 0.0)
                    if tool == "Line" else 0.0
                )
                if tool == "Line" and _line_visibility_presynced:
                    print("   ⚡ Line visibility already synchronized before final render")
                elif _sync_display_visibility_to_renderer(
                    app_window, preset, border_percent=sync_border
                ):
                    print(f"   ✅ Synced {tool} class visibility to renderer")
            
            app_window.active_classify_tool = None
            if tool == "Line" and isinstance(preset, dict) and preset.get(
                "flight_lines"
            ):
                if _line_flight_prepatched:
                    print("   ⚡ Line flight visibility already patched before mode switch")
                else:
                    from gui.unified_actor_manager import (
                        fast_main_flight_line_visibility_update,
                    )
                    fast_main_flight_line_visibility_update(app_window)
            _return_focus_to_main_view(app_window)
            
            icon_map = {"depth": "🧱", "rgb": "🌈", "intensity": "💡", "elevation": "📊", "line": "✈", "class": "🏷️", "surface": "🏔️"}
            icon = icon_map.get(mode, "🎨")
            
            if hasattr(app_window, "statusBar"):
                app_window.statusBar().showMessage(f"{_kp}{icon} {tool} mode activated", 1500)
            
            print(f"   ✅ {tool} display mode activated")
            
        except Exception as e:
            print(f"⚠️ Failed to switch to {tool} mode: {e}")
            import traceback
            traceback.print_exc()
        
        return

    # ========================================================================
    # CROSS-SECTION TOOL (Non-classification)
    # ========================================================================
    if tool_name == "cross_section":
        print("🔧 Activating Cross Section tool - SHOWING POPUP DIALOG")

        # Curve deactivation is handled inside the cross-section entry point.
        # Keeping it there covers both shortcuts and the Tools ribbon, and
        # avoids suspending Curve when cross-section activation is rejected
        # because the main viewer is not in Top View.
        
        # ✅ Deactivate measurement tool
        _deactivate_measurement_tool_safely(app_window, "switching to cross-section")

        # ✅ Stand down temp fence tool (its VTK observers would fight the
        # cross-section line drawing)
        _deactivate_temp_fence_safely(app_window, "switching to cross-section")

        # Element/rectangle selection tools install their own VTK observers;
        # clear them before the cross-section interactor takes over.
        deactivate_selection = getattr(app_window, "_deactivate_selection_tools", None)
        if callable(deactivate_selection):
            deactivate_selection("switching to cross-section")
        
        # Clear any active classification session
        class_picker = _get_live_class_picker(app_window)
        classification_active = bool(
            getattr(app_window, "active_classify_tool", None)
            or _class_picker_is_visible(class_picker)
            or getattr(app_window, "classify_interactor", None)
            or getattr(app_window, "classify_interactors", None)
            or getattr(app_window, "cut_classify_interactor", None)
        )

        if classification_active and hasattr(app_window, "deactivate_classification_tool"):
            has_cross_action = (
                hasattr(app_window, 'cross_action') 
                and app_window.cross_action is not None
            )
            app_window.deactivate_classification_tool(
                preserve_cross_section=has_cross_action
            )
            app_window.active_classify_tool = None
        
        # ✅ Deactivate cut section pending state
        if hasattr(app_window, "cut_section_controller") and app_window.cut_section_controller:
            if hasattr(app_window.cut_section_controller, "_force_deactivate_pending_state"):
                print("   🛑 Deactivating pending cut tool state...")
                app_window.cut_section_controller._force_deactivate_pending_state()

        # ✅ CRITICAL: Use enable_cross_section_mode() to show popup dialog FIRST
        if hasattr(app_window, "enable_cross_section_mode"):
            app_window.enable_cross_section_mode()
            print("   ✅ Used enable_cross_section_mode() - SHOWS POPUP DIALOG!")
        elif hasattr(app_window, "open_next_cross_section_view"):
            app_window.open_next_cross_section_view()
            print("   ⚠️ Fell back to open_next_cross_section_view() - auto-select view")
        
        _return_focus_to_main_view(app_window)
        
        if hasattr(app_window, "statusBar"):
            app_window.statusBar().showMessage(
                f"{_kp}✅ Select view from popup - Draw line on main view", 3000
            )
        return

    # ========================================================================
    # CUT SECTION TOOL (Non-classification)
    # ========================================================================
    if tool_name == "cut_section":
        print("🔧 Activating Cut Section tool")
        
        # ✅ Deactivate curve tool when switching to cut-section
        _deactivate_curve_tool_safely(app_window, "switching to cut-section")
        
        # ✅ Deactivate measurement tool
        _deactivate_measurement_tool_safely(app_window, "switching to cut-section")

        # ✅ Stand down temp fence tool (its VTK observers would fight the
        # cut-section rectangle drawing)
        _deactivate_temp_fence_safely(app_window, "switching to cut-section")
        
        try:
            app_window.active_classify_tool = None
            app_window.cut_section_controller.activate()
            _return_focus_to_main_view(app_window)
            
            if hasattr(app_window, "statusBar"):
                app_window.statusBar().showMessage(
                    f"{_kp}✅ Cut Section ready - Click center, drag, then finalize", 3000
                )
        except Exception as e:
            print(f"⚠️ Cut Section activation failed: {e}")
        return
    
    # ========================================================================
    # CutFromCross/CutFromCut
    # ========================================================================
    if tool_name == "CutFromCross":
        # ✅ Deactivate curve tool
        _deactivate_curve_tool_safely(app_window, "switching to CutFromCross")

        # ✅ Stand down temp fence tool
        _deactivate_temp_fence_safely(app_window, "switching to CutFromCross")
        
        if hasattr(app_window, "cut_section_controller"):
            app_window.cut_section_controller.activate_from_cross_shortcut()
        return

    if tool_name == "CutFromCut":
        # ✅ Deactivate curve tool
        _deactivate_curve_tool_safely(app_window, "switching to CutFromCut")

        # ✅ Stand down temp fence tool
        _deactivate_temp_fence_safely(app_window, "switching to CutFromCut")
        
        if hasattr(app_window, "cut_section_controller"):
            app_window.cut_section_controller.activate_from_cut_shortcut()
        return

    # ========================================================================
    # TEMP FENCE (standalone fence tool — no ClassPicker, no classification)
    # ========================================================================
    if tool_name == "temp_fence":
        _deactivate_curve_tool_safely(app_window, "switching to temp fence")
        _deactivate_measurement_tool_safely(app_window, "switching to temp fence")
        if hasattr(app_window, "set_classify_tool"):
            app_window.set_classify_tool("temp_fence")
        return

    # ========================================================================
    # CLASSIFICATION TOOLS
    # ========================================================================

    # ── Guard: block classify tools when non-class display mode is active ──
    _CLASSIFY_RESTRICTED_MODES = {"depth", "rgb", "intensity", "elevation"}
    _current_display_mode = getattr(app_window, "display_mode", "class")


    if _current_display_mode in _CLASSIFY_RESTRICTED_MODES:
        _mode_label = {
            "depth": "Depth", "rgb": "RGB",
            "intensity": "Intensity", "elevation": "Elevation",
        }.get(_current_display_mode, _current_display_mode.capitalize())

        print(f"ℹ️ Classification tool activated in '{_current_display_mode}' display mode (colors will be hidden until switching to Class/Shading)")

        if hasattr(app_window, "statusBar"):
            app_window.statusBar().showMessage(
                f"⚠️ Classification active in {_mode_label} mode (colors show when switched to Class/Shading)", 
                5000
            )

    # ── All clear — proceed with classification tool activation ──────────────
    print(f"🎯 Activating classification tool: {tool_name}")
    
    # ✅ CRITICAL: Deactivate curve tool when switching to classification
    _deactivate_curve_tool_safely(app_window, "switching to classification")
    
    # ✅ CRITICAL: Deactivate measurement tool when switching to classification
    _deactivate_measurement_tool_safely(app_window, "switching to classification")
    
    # ✅ Determine classification target (main/cross/cut)
    classification_target = _determine_classification_target(app_window)
    
    print(f"   📍 Classification target: {classification_target}")

    # Set classification parameters
    app_window.from_classes = from_cls
    app_window.to_class = to_cls
    app_window.active_classify_tool = tool_name
    
    # ✅ CRITICAL: Set the classification target
    if hasattr(app_window, "active_classify_target"):
        app_window.active_classify_target = classification_target
    
    # ✅ Use set_classify_tool if available (better integration)
    if hasattr(app_window, "set_classify_tool"):
        app_window.set_classify_tool(tool_name)
    
    # Build display message
    if isinstance(from_cls, list) and len(from_cls) > 0:
        from_display = ", ".join(str(c) for c in from_cls)
    else:
        from_display = "Any"
    
    to_display = str(to_cls) if to_cls is not None else "Any"
    
    print(f"✅ Tool armed: {tool_name} [{from_display} → {to_display}]")
    print(f"   Target view: {classification_target}")

    # ✅ CRITICAL: Update ClassPicker BUT keep focus on main view
    class_picker = _get_live_class_picker(app_window)
    if class_picker is not None:
        try:
            class_picker.sync_with_app()
            if not _class_picker_is_visible(class_picker):
                class_picker.show()
        except (RuntimeError, ReferenceError, AttributeError):
            try:
                app_window.class_picker = None
            except Exception:
                pass
        except Exception:
            pass

    # ✅ CRITICAL: Return focus to appropriate view
    if classification_target == "cross":
        _return_focus_to_cross_section(app_window)
    elif classification_target == "cut":
        _return_focus_to_cut_section(app_window)
    else:
        _return_focus_to_main_view(app_window)
    
    # Status bar feedback
    if hasattr(app_window, "statusBar"):
        target_name = {
            "main": "Main View",
            "cross": "Cross-Section",
            "cut": "Cut Section"
        }.get(classification_target, "View")
        
        msg = f"{_kp}✅ {tool_name.title()} ready in {target_name} [{from_display} → {to_display}]"
        app_window.statusBar().showMessage(msg, 3000)


def _determine_classification_target(app_window):
    """
    ✅ Determine which view should receive classification actions.
    Priority: Cut Section > Cross Section > Main View
    
    Returns:
        "cut", "cross", or "main"
    """
    
    # Check if cut section is active and locked
    if hasattr(app_window, 'cut_section_controller') and app_window.cut_section_controller:
        ctrl = app_window.cut_section_controller
        if getattr(ctrl, 'is_locked', False) and getattr(ctrl, 'is_cut_view_active', False):
            print("   🔒 Cut section is locked and active")
            return "cut"
    
    # Check if cross-section view is active
    if hasattr(app_window, 'section_controller') and app_window.section_controller:
        active_view = getattr(app_window.section_controller, 'active_view', None)
        
        if active_view is not None:
            if hasattr(app_window, 'section_vtks') and active_view in app_window.section_vtks:
                vtk_widget = app_window.section_vtks[active_view]
                
                if vtk_widget and hasattr(vtk_widget, 'isVisible') and vtk_widget.isVisible():
                    print(f"   📊 Cross-section view {active_view} is active")
                    return "cross"
    
    # Default to main view
    print("   🏠 Using main view")
    return "main"


def _return_focus_to_main_view(app_window):
    """
    ✅ CRITICAL: Return keyboard focus to main VTK viewer
    This allows next shortcut key to work immediately without clicking
    """
    try:
        if hasattr(app_window, 'vtk_widget') and app_window.vtk_widget:
            app_window.vtk_widget.setFocus()
            print("   🎯 Focus returned to main VTK widget")
            return
        
        if hasattr(app_window, 'setFocus'):
            app_window.setFocus()
            print("   🎯 Focus returned to main window")
            return
        
        print("   ⚠️ Could not return focus (no vtk_widget found)")
        
    except Exception as e:
        print(f"   ⚠️ Focus return failed: {e}")


def _return_focus_to_cross_section(app_window):
    """
    ✅ Return keyboard focus to active cross-section view
    """
    try:
        if not hasattr(app_window, 'section_controller') or not app_window.section_controller:
            print("   ⚠️ No section_controller found")
            _return_focus_to_main_view(app_window)
            return
        
        active_view = getattr(app_window.section_controller, 'active_view', None)
        
        if active_view is None:
            print("   ⚠️ No active cross-section view")
            _return_focus_to_main_view(app_window)
            return
        
        if hasattr(app_window, 'section_vtks') and active_view in app_window.section_vtks:
            vtk_widget = app_window.section_vtks[active_view]
            
            if vtk_widget and hasattr(vtk_widget, 'setFocus'):
                vtk_widget.setFocus()
                print(f"   🎯 Focus returned to cross-section view {active_view}")
                return
        
        print("   ⚠️ Could not find cross-section VTK widget")
        _return_focus_to_main_view(app_window)
        
    except Exception as e:
        print(f"   ⚠️ Cross-section focus return failed: {e}")
        _return_focus_to_main_view(app_window)

def _return_focus_to_cut_section(app_window):
    """
    ✅ Return keyboard focus to cut section view
    """
    try:
        if not hasattr(app_window, 'cut_section_controller') or not app_window.cut_section_controller:
            print("   ⚠️ No cut_section_controller found")
            _return_focus_to_main_view(app_window)
            return
        
        ctrl = app_window.cut_section_controller
        
        if hasattr(ctrl, 'cut_vtk') and ctrl.cut_vtk:
            if hasattr(ctrl.cut_vtk, 'setFocus'):
                ctrl.cut_vtk.setFocus()
                print("   🎯 Focus returned to cut section view")
                return
        
        print("   ⚠️ Could not find cut section VTK widget")
        _return_focus_to_main_view(app_window)
        
    except Exception as e:
        print(f"   ⚠️ Cut section focus return failed: {e}")
        _return_focus_to_main_view(app_window)
