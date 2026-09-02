from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QElapsedTimer
from PySide6.QtGui import QKeySequence
from flask import views
from .execute_tool import execute_tool
from .shading_preset_quality import normalize_shading_preset_quality, shading_quality_label
# ✅ ADD this class in the same file as GlobalShortcutFilter, BEFORE it

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


class ShortcutExecutionGuard(QObject):
    """Prevents concurrent shortcut execution that crashes VTK/OpenGL."""

    COOLDOWN_NORMAL  = 150   # ms — simple tools
    COOLDOWN_DISPLAY = 400   # ms — DisplayMode (actor rebuild)
    COOLDOWN_SHADING = 800   # ms — ShadingMode (mesh + GPU heavy)
    COOLDOWN_DRAW    = 200   # ms — DrawSettings

    def __init__(self, parent=None):
        super().__init__(parent)
        self._locked = False
        self._cooldown_timer = QTimer(self)
        self._cooldown_timer.setSingleShot(True)
        self._cooldown_timer.timeout.connect(self._unlock)
        self._elapsed = QElapsedTimer()
        self._last_tool = None

    def try_acquire(self, tool_name: str = "") -> bool:
        if self._locked:
            elapsed_ms = self._elapsed.elapsed() if self._elapsed.isValid() else 0
            print(f"⚠️ SHORTCUT BLOCKED: '{tool_name}' rejected "
                  f"(locked by '{self._last_tool}', {elapsed_ms}ms ago)")
            return False
        self._locked = True
        self._last_tool = tool_name
        self._elapsed.start()

        if tool_name == "ShadingMode":
            cooldown = self.COOLDOWN_SHADING
        elif tool_name in ("DisplayMode", "Surface"):
            cooldown = self.COOLDOWN_DISPLAY
        elif tool_name == "DrawSettings":
            cooldown = self.COOLDOWN_DRAW
        else:
            cooldown = self.COOLDOWN_NORMAL

        self._cooldown_timer.start(cooldown)
        return True

    def _unlock(self):
        self._locked = False
        self._last_tool = None

    def force_unlock(self):
        self._locked = False
        self._last_tool = None
        self._cooldown_timer.stop()

class GlobalShortcutFilter(QObject):
    def __init__(self, app_window):
        super().__init__()
        self.app_window = app_window
        self._shortcut_guard = ShortcutExecutionGuard(self)  # ✅ ADD THIS

    def _set_pan_shortcut_active(self, active):
        """Publish the latched left-pan tool state used by every viewport."""
        active = bool(active)
        self.app_window._left_pan_shortcut_active = active
        if not active:
            release = getattr(self.app_window, "_handle_fast_main_pan_release", None)
            if callable(release) and getattr(self.app_window, "_qt_main_pan_active", False):
                release()

    def _get_live_qt_attr(self, attr_name):
        """Return a live QObject attribute from app_window or None if stale."""
        obj = getattr(self.app_window, attr_name, None)
        if obj is None:
            return None
        try:
            if not _qt_object_is_valid(obj):
                setattr(self.app_window, attr_name, None)
                return None
            obj.objectName()
            return obj
        except (RuntimeError, ReferenceError, AttributeError):
            try:
                setattr(self.app_window, attr_name, None)
            except Exception:
                pass
            return None
        except Exception:
            return None

    def _is_classification_active(self) -> bool:
        """Robust classification-active check across main/cross/cut contexts."""
        try:
            class_picker = getattr(self.app_window, "class_picker", None)
            if class_picker is not None:
                try:
                    if not _qt_object_is_valid(class_picker):
                        self.app_window.class_picker = None
                    elif class_picker.isVisible():
                        return True
                except (RuntimeError, ReferenceError, AttributeError):
                    self.app_window.class_picker = None
                except Exception:
                    self.app_window.class_picker = None

            if getattr(self.app_window, "active_classify_tool", None):
                return True

            if getattr(self.app_window, "classify_interactor", None) is not None:
                return True

            classify_interactors = getattr(self.app_window, "classify_interactors", None)
            if isinstance(classify_interactors, dict):
                if any(v is not None for v in classify_interactors.values()):
                    return True

            if getattr(self.app_window, "cut_classify_interactor", None) is not None:
                return True
        except Exception:
            return False

        return False

    def _is_shortcut_editor_active(self) -> bool:
        """Block runtime shortcuts while Shortcut Manager (or its pickers) is open."""
        try:
            from PySide6.QtWidgets import QApplication

            focus_widget = QApplication.focusWidget()
            active_window = QApplication.activeWindow()

            shortcut_mgr = getattr(self.app_window, "_shortcut_manager", None)
            if shortcut_mgr is None:
                try:
                    from gui.shortcut_manager import ShortcutManager
                    shortcut_mgr = getattr(ShortcutManager, "instance", None)
                except Exception:
                    shortcut_mgr = None

            config_windows = []
            if shortcut_mgr is not None and hasattr(shortcut_mgr, "isVisible") and shortcut_mgr.isVisible():
                config_windows.append(shortcut_mgr)
                for attr_name in ("_display_picker", "_shading_picker", "_draw_settings_dialog"):
                    child = getattr(shortcut_mgr, attr_name, None)
                    if child is not None and hasattr(child, "isVisible") and child.isVisible():
                        config_windows.append(child)

            # NOTE: ClassPicker is intentionally NOT added to config_windows.
            # It is a classification companion widget (shows current from/to classes),
            # not a shortcut editor. Including it here caused classification shortcuts
            # (e.g. pressing Q after A) to be silently swallowed whenever the ClassPicker
            # was the active window — which happens transiently after each tool arm.
            # Classification shortcuts must always fire regardless of ClassPicker visibility.

            if not config_windows:
                return False

            for win in config_windows:
                try:
                    if active_window is win or focus_widget is win:
                        return True
                    if focus_widget is not None and hasattr(win, "isAncestorOf") and win.isAncestorOf(focus_widget):
                        return True
                except Exception:
                    continue



        except Exception:
            return False

        return False

    def _is_input_popup_open(self) -> bool:
        """Generic guard: suppress runtime tool shortcuts while ANY input popup
        (block creation, custom backup, PRJ/Display sub-dialogs, …) is open.

        This is focus-independent: dialogs set app._input_popup_open via
        gui.popup_guard (InputPopupMixin or set_input_popup_open) so typed
        letters become text instead of arming tools, regardless of where focus
        is forced.
        """
        try:
            from gui.popup_guard import is_input_popup_open
            if is_input_popup_open(self.app_window):
                return True
        except Exception:
            pass

        # Fallback: a modal dialog owned by the app is open (covers any dialog
        # we forgot to opt into the mixin).
        try:
            from PySide6.QtWidgets import QApplication
            modal = QApplication.activeModalWidget()
            if modal is not None:
                p = modal
                while p is not None:
                    if p is self.app_window:
                        return True
                    p = p.parent()
        except Exception:
            pass

        return False

    def _is_display_mode_dialog_active(self) -> bool:
        """Suppress runtime tool shortcuts while the Display Mode dialog (or any
        of its sub-dialogs, e.g. Edit Class) is open — so alphabet keys typed
        into the description / code / level / draw fields don't arm tools.
        """
        try:
            from PySide6.QtWidgets import QApplication, QLineEdit

            dm_dlg = getattr(self.app_window, "display_mode_dialog", None)
            if dm_dlg is None or not hasattr(dm_dlg, "isVisible") or not dm_dlg.isVisible():
                return False

            focus_widget = QApplication.focusWidget()
            if focus_widget is None:
                # Dialog visible but nothing focused inside it — keep shortcuts
                # suppressed so typed class names can't trigger viewport tools.
                # A visible non-modal dialog is not a shortcut suspension state.
                return False

            if isinstance(focus_widget, QLineEdit):
                try:
                    if focus_widget is dm_dlg or dm_dlg.isAncestorOf(focus_widget):
                        return True
                except Exception:
                    return True

            # Non-text Display Mode controls must not disable global shortcuts.
            return False
        except Exception:
            return False

        return False

    def _is_prj_dialog_active(self) -> bool:
        """Suppress runtime tool shortcuts while the PRJ Block Identifier dialog
        (or any of its sub-dialogs, e.g. Add Blocks by Boundaries) is open, or
        while the user is typing into the PRJ block-label search box.

        Alphabet keys (E, W, L, …) are mapped to drawing tools, so without this
        guard they would fire while the user types block names / searches — the
        PRJ dialog must keep those key presses for text entry.
        """
        try:
            from PySide6.QtWidgets import QApplication, QLineEdit

            prj_dlg = getattr(self.app_window, "block_identifier_dialog", None)
            if prj_dlg is None or not hasattr(prj_dlg, "isVisible") or not prj_dlg.isVisible():
                return False

            focus_widget = QApplication.focusWidget()
            if focus_widget is None:
                # Dialog visible but nothing focused inside it → be conservative
                # and still suspend, since the user is interacting with PRJ UI.
                # (A visible modal/non-modal PRJ dialog means PRJ text input is
                #  a real possibility; don't fire viewport tool shortcuts.)
                return True

            # Typing into the PRJ block-label search box, or any QLineEdit owned
            # by the PRJ dialog (e.g. Add-by-Boundaries prefix / project info).
            if isinstance(focus_widget, QLineEdit):
                try:
                    if focus_widget is prj_dlg or prj_dlg.isAncestorOf(focus_widget):
                        return True
                except Exception:
                    return True

            # The Add Blocks by Boundaries popup is modal child of the PRJ dialog;
            # any focus inside it must not trigger runtime shortcuts.
            if prj_dlg.isAncestorOf(focus_widget):
                return True

            # Explicit per-feature flags set by the PRJ dialog itself.
            if bool(getattr(prj_dlg, "_addb_dialog_active", False)):
                return True
            if bool(getattr(prj_dlg, "_prj_search_focus", False)):
                return True
        except Exception:
            return False

        return False

    def _is_runtime_shortcut_suspended(self) -> bool:
        """
        Temporarily suppress runtime shortcuts while the user is editing
        sensitive text fields (e.g. SNT layer rename/add-level inputs).
        """
        try:
            if bool(getattr(self.app_window, "_snt_layer_rename_edit_active", False)):
                return True

            # Fallback guard: if an SNT layer-name input dialog is modal/active,
            # avoid firing runtime shortcuts for typed characters.
            from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit
            modal = QApplication.activeModalWidget()
            if isinstance(modal, QInputDialog):
                title = str(modal.windowTitle() or "").strip().lower()
                if "rename layer" in title or "add level" in title:
                    fw = QApplication.focusWidget()
                    if fw is None or isinstance(fw, QLineEdit) or modal.isAncestorOf(fw):
                        return True
        except Exception:
            return False
        return False

    @staticmethod
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

    @staticmethod
    def _format_key_label(source) -> str:
        """Build a human-readable key label like 'Ctrl+Shift+F1' from a QKeyEvent or (mod, keyname) tuple."""
        try:
            if isinstance(source, tuple) and len(source) == 2:
                mod, keyname = source
            else:
                ev = source
                mods = ev.modifiers()
                parts = []
                if mods & Qt.ControlModifier:
                    parts.append("ctrl")
                if mods & Qt.AltModifier:
                    parts.append("alt")
                if mods & Qt.ShiftModifier:
                    parts.append("shift")
                mod = "+".join(parts) if parts else "none"

                k = ev.key()
                if Qt.Key_F1 <= k <= Qt.Key_F12:
                    keyname = f"F{k - Qt.Key_F1 + 1}"
                elif Qt.Key_0 <= k <= Qt.Key_9:
                    keyname = chr(ord('0') + (k - Qt.Key_0))
                else:
                    keyname = QKeySequence(k).toString().upper() or (ev.text() or "").upper()

            pieces = []
            if mod and mod != "none":
                pieces.extend(p.capitalize() for p in str(mod).split("+") if p)
            keyname_str = str(keyname).strip()
            if keyname_str:
                pieces.append(keyname_str.upper() if len(keyname_str) == 1 else keyname_str)
            return "+".join(pieces) if pieces else "?"
        except Exception:
            return "?"
        
    def _enforce_shading_only_visibility(self, render=False):
        """
        Keep ShadingMode visually stable when the same shortcut is pressed again.

        Shading shortcut must be idempotent:
        pressing the same shading shortcut twice should NOT reveal the main
        unified/class point actor over the shaded mesh.
        """
        app = self.app_window

        try:
            # Remove stale Surface actors if any exist.
            from gui.surface_mode import detach_surface_before_non_surface_mode
            detach_surface_before_non_surface_mode(
                app,
                requested_mode="shaded_class",
            )
        except Exception as _surface_cleanup_err:
            print(f"   ⚠️ Surface cleanup before shading visibility enforce skipped: {_surface_cleanup_err}")

        try:
            # Hide unified/main point actor.
            from gui.unified_actor_manager import _get_unified_actor
            unified_actor = _get_unified_actor(app)
            if unified_actor is not None:
                unified_actor.SetVisibility(False)
                print("   🙈 Unified point actor hidden for ShadingMode")
        except Exception as _unified_hide_err:
            print(f"   ⚠️ Unified actor hide for ShadingMode skipped: {_unified_hide_err}")

        try:
            vtk_widget = getattr(app, "vtk_widget", None)
            actors = getattr(vtk_widget, "actors", {}) if vtk_widget is not None else {}

            hidden_count = 0
            for name, actor in list(actors.items()):
                if str(name).startswith("class_") and actor is not None:
                    actor.SetVisibility(False)
                    hidden_count += 1

            if hidden_count:
                print(f"   🙈 Hidden {hidden_count} class_ actors for ShadingMode")

            shaded_actor = getattr(app, "_shaded_mesh_actor", None)
            if shaded_actor is None and "shaded_mesh" in actors:
                shaded_actor = actors.get("shaded_mesh")

            if shaded_actor is not None:
                shaded_actor.SetVisibility(True)

            app.display_mode = "shaded_class"
            if hasattr(app, "current_display_mode"):
                app.current_display_mode = "shaded_class"

            if render and vtk_widget is not None:
                vtk_widget.render()

        except Exception as _actor_state_err:
            print(f"   ⚠️ Shading visibility enforce failed: {_actor_state_err}")

    def eventFilter(self, obj, event):
        _QTimer = QTimer  # noqa — intentional alias

        # Block Alt+F4 unless the user has explicitly mapped it to a shortcut.
        # The native-event guard in app_window.nativeEvent handles OS-level close
        # only when Alt+F4 is not a mapped shortcut.
        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_F4 and (event.modifiers() & Qt.AltModifier):
                shortcuts = getattr(self.app_window, "shortcuts", {})
                if ("alt", "F4") not in shortcuts:
                    return True


        if event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape and getattr(
                self.app_window, "_left_pan_shortcut_active", False
            ):
                self._set_pan_shortcut_active(False)

            # Build a readable label for the pressed key (e.g. "Ctrl+Shift+F1") so
            # status-bar feedback can show the exact key the user pressed.
            key_label = self._format_key_label(event)

            if self._is_runtime_shortcut_suspended():
                return False

            # Generic guard: any input popup (block creation, custom backup,
            # PRJ/Display sub-dialogs, …) owns the key presses while open.
            if self._is_input_popup_open():
                return False

            # PRJ Block Identifier dialog (incl. Add-by-Boundaries popup and its
            # block-label search box) owns the key presses while it is open.
            if self._is_prj_dialog_active():
                return False

            # Display Mode dialog (incl. its Edit Class sub-dialog) owns the key
            # presses while open — description/code/level text entry must not
            # trigger alphabet tool shortcuts.
            if self._is_display_mode_dialog_active():
                return False

            # Shortcut editor has priority over runtime tool shortcuts.
            if self._is_shortcut_editor_active():
                return False

            # If the Digitizer text dialog is open, block all runtime shortcuts
            # so typed characters always go to text editing.
            try:
                digitizer = getattr(self.app_window, "digitizer", None)
                if (
                    digitizer is not None
                    and hasattr(digitizer, "is_text_ui_editing_active")
                    and digitizer.is_text_ui_editing_active()
                ):
                    return False
            except Exception:
                pass
            
            # ====================================================================
            # BYPASS SHORTCUTS IF USER IS TYPING IN AN INPUT FIELD
            # ====================================================================
            try:
                from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox
                focus_widget = QApplication.focusWidget()
                focus_in_cross_selector = False
                view_selector = getattr(self.app_window, "_view_selector_dialog", None)
                if view_selector is not None and focus_widget is not None:
                    try:
                        focus_in_cross_selector = (
                            view_selector.isVisible()
                            and (
                                focus_widget is view_selector
                                or view_selector.isAncestorOf(focus_widget)
                            )
                        )
                    except Exception:
                        focus_in_cross_selector = False
                if (
                    not focus_in_cross_selector
                    and isinstance(focus_widget, (QLineEdit, QTextEdit, QPlainTextEdit, QAbstractSpinBox, QComboBox))
                ):
                    # Some search fields must always own their keystrokes, even
                    # when a typed letter is also configured as a runtime tool
                    # shortcut. This opt-in keeps unrelated editors unchanged.
                    try:
                        if bool(focus_widget.property("nakshaStrictTextInput")):
                            return False
                    except Exception:
                        pass

                    # If this key maps to a configured runtime shortcut, do NOT suppress it
                    # just because focus sits in a combo/spinbox/list editor.
                    try:
                        _mods = []
                        if event.modifiers() & Qt.ControlModifier:
                            _mods.append("ctrl")
                        if event.modifiers() & Qt.AltModifier:
                            _mods.append("alt")
                        if event.modifiers() & Qt.ShiftModifier:
                            _mods.append("shift")
                        _mod = "+".join(_mods) if _mods else "none"

                        _k = event.key()
                        if Qt.Key_F1 <= _k <= Qt.Key_F12:
                            _keyname = f"F{_k - Qt.Key_F1 + 1}"
                        elif Qt.Key_0 <= _k <= Qt.Key_9:
                            _keyname = chr(ord('0') + (_k - Qt.Key_0))
                        else:
                            _keyname = QKeySequence(_k).toString().upper() or event.text().upper()

                        _combo = (_mod.lower(), _keyname.upper())
                        _mapped_shortcuts = getattr(self.app_window, "shortcuts", {})
                        if _combo in _mapped_shortcuts:
                            # Let global shortcut handling continue below.
                            pass
                        else:
                            if not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
                                return False
                            if event.modifiers() & Qt.ControlModifier and event.key() in (Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_Z, Qt.Key_Y, Qt.Key_A):
                                return False
                    except Exception:
                        # Fall back to conservative behavior if combo detection fails.
                        if not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
                            return False
                        if event.modifiers() & Qt.ControlModifier and event.key() in (Qt.Key_C, Qt.Key_V, Qt.Key_X, Qt.Key_Z, Qt.Key_Y, Qt.Key_A):
                            return False
            except Exception:
                pass

            # ====================================================================
            # PRIORITY -2: Shift+ESC - Exit active digitize tool, keep drawings
            # ====================================================================
            if event.key() == Qt.Key_Escape and (event.modifiers() & Qt.ShiftModifier):
                digitizer = getattr(self.app_window, 'digitizer', None)
                if digitizer and getattr(digitizer, 'active_tool', None):
                    print("🛑 Shift+ESC - deactivating digitizer tool")
                    try:
                        if hasattr(digitizer, '_deactivate_active_tool_keep_drawings') and \
                        digitizer._deactivate_active_tool_keep_drawings():
                            return True
                    except Exception as e:
                        print(f"⚠️ Shift+ESC digitizer deactivation failed: {e}")

            # ====================================================================
            # PRIORITY -1: Curve Tool Shortcuts (highest priority)
            # ====================================================================
            if hasattr(self.app_window, 'curve_tool'):
                curve_tool = self.app_window.curve_tool
                
                if event.key() == Qt.Key_E and (event.modifiers() & Qt.ShiftModifier):
                    if curve_tool.selected_curve_data:
                        print("🎨 Shift+E → Edit Curve Color (curve tool)")
                        try:
                            curve_tool._edit_selected_curve_color()
                        except Exception as e:
                            print(f"⚠️ Curve color edit failed: {e}")
                        return True
                
                if (event.key() == Qt.Key_Delete or event.key() == Qt.Key_Backspace):
                    if curve_tool.selected_curve_data and not curve_tool.active:
                        print("🗑️ Delete → Delete Curve (curve tool)")
                        try:
                            curve_tool._delete_selected_curve()
                        except Exception as e:
                            print(f"⚠️ Curve delete failed: {e}")
                        return True
            
            # ====================================================================
            # PRIORITY 0: ESC key - Cancel active modes
            # ====================================================================
            if event.key() == Qt.Key_Escape:
                cut_controller = getattr(
                    self.app_window, 'cut_section_controller', None
                )
                cancel_nested_cut = getattr(
                    cut_controller, 'cancel_persistent_cut_in_cut', None
                )
                if callable(cancel_nested_cut) and cancel_nested_cut():
                    print("🛑 ESC - persistent Cut-in-Cut deactivated; left pan restored")
                    return True

                deactivate_identification = getattr(
                    self.app_window,
                    "_deactivate_active_identification_tools_for_escape",
                    None,
                )
                if callable(deactivate_identification) and deactivate_identification():
                    print("🛑 ESC - identification tool deactivated; left pan restored")
                    return True

                # Deactivate temp fence tool on Escape
                _tft = getattr(self.app_window, 'temp_fence_tool', None)
                if _tft is not None and getattr(_tft, 'active', False):
                    try:
                        _tft.deactivate()
                    except Exception:
                        pass
                    _tft.active = False
                    if getattr(self.app_window, 'active_classify_tool', None) == 'temp_fence':
                        self.app_window.active_classify_tool = None
                    print("🛑 ESC - temp fence tool deactivated")
                    return True

                cross_action = getattr(self.app_window, 'cross_action', None)
                cross_checked = bool(cross_action is not None and cross_action.isChecked())
                cross_interactor_active = bool(getattr(self.app_window, 'cross_interactor', None) is not None)
                cross_mode_flag = bool(getattr(self.app_window, 'cross_section_active', False))

                if cross_checked or cross_interactor_active or cross_mode_flag:
                    print("🛑 ESC - deactivating cross-section")

                    try:
                        # Route ESC through the app-level helper so preview overlays
                        # and VTK actors are cleaned in one consistent path.
                        if hasattr(self.app_window, "_cancel_cross_section_tool_only"):
                            self.app_window._cancel_cross_section_tool_only()
                        elif hasattr(self.app_window, "deactivate_cross_section_tool"):
                            self.app_window.deactivate_cross_section_tool()

                        if hasattr(self.app_window, "set_cross_cursor_active"):
                            self.app_window.set_cross_cursor_active(False, "cross_section")

                        if hasattr(self.app_window, "vtk_widget") and self.app_window.vtk_widget:
                            self.app_window.vtk_widget.render()
                        print("✅ Cross-section mode deactivated (existing sections preserved)")
                        
                    except Exception as e:
                        print(f"⚠️ Error deactivating cross-section: {e}")
                    
                    return True
                
                if self._is_classification_active():
                    print("🛑 ESC - deactivating classification")
                    self.app_window.deactivate_classification_tool()
                    return True
                
                return False

            # ====================================================================
            # PRIORITY 1: Undo/Redo (Ctrl+Z/Y)
            # 
            # ✅ TOOL EXCLUSIVITY RULES:
            # 1. Measurement Tool Active → Measurement undo/redo ONLY
            # 2. Curve Tool ACTIVELY Drawing → Curve point undo/redo ONLY
            # 3. ANY other tool active (classification, cross-section, cut-section) 
            #    → That tool's undo/redo (classification is default)
            # 4. Curve tool NOT drawing + Ribbon='curve' + NO other tool 
            #    → Completed curve undo/redo
            # 5. Digitizer active + Ribbon='draw' + NO other tool 
            #    → Digitizer undo/redo
            # 6. Default (no tool owns it) → Classification undo/redo
            # ====================================================================
            if event.modifiers() & Qt.ControlModifier:

                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 1: MEASUREMENT TOOL (HIGHEST PRIORITY)
                # When measurement tool is active OR has measurements,
                # Ctrl+Z/Y ONLY affects measurements
                # ═══════════════════════════════════════════════════════════════════
                if hasattr(self.app_window, 'measurement_tool') and \
                   self.app_window.measurement_tool is not None:
                   
                    mt = self.app_window.measurement_tool
                    # ✅ FIX: ONLY check mt.active - NOT mt.measurements
                    # Once tool is deactivated, even if measurements exist on screen,
                    # Ctrl+Z/Y should go to classification
                    measurement_active = getattr(mt, 'active', False)
                    
                    if measurement_active:
                        if event.key() == Qt.Key_Z:
                            print("📏 Ctrl+Z → Measurement Undo (EXCLUSIVE)")
                            try:
                                mt.undo()
                            except Exception as e:
                                print(f"⚠️ Measurement undo failed: {e}")
                            return True

                        elif event.key() == Qt.Key_Y:
                            print("📏 Ctrl+Y → Measurement Redo (EXCLUSIVE)")
                            try:
                                mt.redo()
                            except Exception as e:
                                print(f"⚠️ Measurement redo failed: {e}")
                            return True
                        
                        if event.key() in (Qt.Key_Z, Qt.Key_Y):
                            return True

                # =================================================================
                # LEVEL 1.5: TEMP FENCE TOOL (ACTIVE DRAWING)
                # While the temp fence tool is active and drawing (or has redo
                # history), Ctrl+Z/Y exclusively undo/redo fence vertices.
                # =================================================================
                _tft = getattr(self.app_window, 'temp_fence_tool', None)
                if _tft is not None and getattr(_tft, 'active', False) and (
                        getattr(_tft, '_drawing', False)
                        or bool(getattr(_tft, '_redo_stack', None))):
                    if event.key() == Qt.Key_Z:
                        print("Ctrl+Z -> Temp Fence Undo Vertex (EXCLUSIVE)")
                        try:
                            _tft.undo_vertex()
                        except Exception as e:
                            print(f"Temp fence undo failed: {e}")
                        return True
                    elif event.key() == Qt.Key_Y:
                        print("Ctrl+Y -> Temp Fence Redo Vertex (EXCLUSIVE)")
                        try:
                            _tft.redo_vertex()
                        except Exception as e:
                            print(f"Temp fence redo failed: {e}")
                        return True

                # =================================================================
                # PRE-CHECK: Is ANY "other" tool active that owns undo/redo?
                # If yes, curve/digitizer completed undo should be BLOCKED
                # =================================================================
                classification_active = self._is_classification_active()
                cross_section_active = getattr(self.app_window, 'cross_section_active', False)
                cut_section_waiting = False
                if hasattr(self.app_window, 'cut_section_controller'):
                    cut_ctrl = self.app_window.cut_section_controller
                    _cut_state = getattr(cut_ctrl, '_state', 0)
                    # Only block when actively drawing the cut line (WAITING_CENTER=1 or WAITING_DEPTH=2)
                    # FINALIZED=3 means cut dock is open but user may have moved to Draw tab
                    cut_section_waiting = _cut_state in (1, 2)  # WAITING_CENTER or WAITING_DEPTH only
 
                # If ANY other tool is active, curve/digitizer completed undo is blocked
                other_tool_active = classification_active or cross_section_active or cut_section_waiting
 

                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 2: CURVE TOOL - ACTIVE DRAWING ONLY
                # Point-by-point undo while user is actively drawing (points exist).
                # When the tool is active but self.points is empty the user has just
                # finalized a curve — fall through to LEVEL 3 for whole-curve undo.
                # ═══════════════════════════════════════════════════════════════════
                _curve_tool = getattr(self.app_window, 'curve_tool', None)
                _curve_mid_draw = (
                    _curve_tool is not None
                    and getattr(_curve_tool, 'active', False)
                    and bool(getattr(_curve_tool, 'points', None))   # ← points in progress
                )

                if _curve_mid_draw:
                    if event.key() == Qt.Key_Z:
                        print("🎨 Ctrl+Z → Curve Tool Undo Point (ACTIVE DRAWING)")
                        try:
                            _curve_tool._undo_last_point()
                        except Exception as e:
                            print(f"⚠️ Curve undo failed: {e}")
                        return True
                    
                    elif event.key() == Qt.Key_Y:
                        print("🎨 Ctrl+Y → Curve Tool Redo Point (ACTIVE DRAWING)")
                        try:
                            _curve_tool._redo_last_point()
                        except Exception as e:
                            print(f"⚠️ Curve redo failed: {e}")
                        return True


                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 3: CURVE TOOL - COMPLETED CURVE UNDO/REDO
                # Fires when:
                #   • curve ribbon is active  AND
                #   • no points currently in progress (idle or just finalized)  AND
                #   • no other tool (classification / cross-section / cut) is active
                # The `curve_tool.active` flag is intentionally NOT checked here —
                # the tool stays active between curves so the user can keep drawing,
                # and we must still be able to undo the last finished curve.
                # ═══════════════════════════════════════════════════════════════════
                elif not other_tool_active and \
                     _curve_tool is not None and \
                     not bool(getattr(_curve_tool, 'points', None)) and \
                     (
                         # Primary: curve ribbon is active
                         getattr(getattr(self.app_window, 'ribbon_manager', None), 'current_ribbon', None) == 'curve'
                         # Fallback: curve tool has undo/redo history even if ribbon switched away.
                         # Without this, switching to the draw ribbon after drawing curves makes
                         # Ctrl+Z/Y fall through to the digitizer and never reach undo_curve/redo_curve.
                         or bool(getattr(_curve_tool, 'history_stack', None))
                         or bool(getattr(_curve_tool, 'history_redo_stack', None))
                     ):

                    if event.key() == Qt.Key_Z:
                        print("🎨 Ctrl+Z → Curve Undo (completed curve)")
                        try:
                            _curve_tool.undo_curve()
                        except Exception as e:
                            print(f"⚠️ Curve undo failed: {e}")
                        return True

                    elif event.key() == Qt.Key_Y:
                        print("🎨 Ctrl+Y → Curve Redo (completed curve)")
                        try:
                            _curve_tool.redo_curve()
                        except Exception as e:
                            print(f"⚠️ Curve redo failed: {e}")
                        return True

                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 3.5: ELEMENT SELECT TOOL UNDO
                # When element selection tool is active, Ctrl+Z/Y → digitizer undo.
                # digitizer.enabled is False during selection so LEVEL 4 misses it.
                # The tool lives on digitizer._element_select_tool, not on app_window.
                #
                # Classification-first guard: if the classification undo stack on
                # app_window has entries, undo classification first — per user rule
                # "always give undo priority to classification, then digitize".
                # Without this guard, Element Select would unconditionally steal
                # Ctrl+Z and never let classification undo run.
                # ═══════════════════════════════════════════════════════════════════
                elif getattr(getattr(getattr(self.app_window, 'digitizer', None),
                                     '_element_select_tool', None), '_active', False):
                    _digitizer = self.app_window.digitizer
                    _element_tool = getattr(_digitizer, '_element_select_tool', None)
                    _in_intersect_mode = getattr(_element_tool, '_intersect_mode', False)
                    
                    # Intersect owns its in-session history. Handle it at the
                    # Qt boundary and consume the key before QtInteractor turns
                    # the same key press into VTK KeyPressEvent + CharEvent.
                    if _in_intersect_mode:
                        if event.key() == Qt.Key_Z:
                            if event.modifiers() & Qt.ShiftModifier:
                                print("🎯 Ctrl+Shift+Z → Intersect Redo (consumed before VTK)")
                                _element_tool._intersect_redo_last_delete()
                            else:
                                print("🎯 Ctrl+Z → Intersect Undo (consumed before VTK)")
                                _element_tool._intersect_undo_last_delete()
                            event.accept()
                            return True
                        if event.key() == Qt.Key_Y:
                            print("🎯 Ctrl+Y → Intersect Redo (consumed before VTK)")
                            _element_tool._intersect_redo_last_delete()
                            event.accept()
                            return True
                        return False
                    
                    _classify_undo_stack = getattr(self.app_window, 'undo_stack', None)
                    _classify_redo_stack = getattr(self.app_window, 'redo_stack', None)
                    # Defensive: stacks are lists today, but a try-wrap survives
                    # any future swap to deque / lazy structure without crashing
                    # the global Ctrl+Z dispatcher.
                    try:
                        _has_classify_undo = bool(_classify_undo_stack) and len(_classify_undo_stack) > 0
                    except Exception:
                        _has_classify_undo = False
                    try:
                        _has_classify_redo = bool(_classify_redo_stack) and len(_classify_redo_stack) > 0
                    except Exception:
                        _has_classify_redo = False

                    if event.key() == Qt.Key_Z:
                        if _has_classify_undo:
                            print("🟢 Ctrl+Z → Classification Undo (priority over Element Select)")
                            try:
                                self.app_window.undo_classification()
                            except Exception as e:
                                print(f"⚠️ Classification undo failed: {e}")
                            return True
                        print("🎯 Ctrl+Z → Element Select Undo (digitizer)")
                        try:
                            _digitizer.undo()
                        except Exception as e:
                            print(f"⚠️ Element Select undo failed: {e}")
                        return True
                    elif event.key() == Qt.Key_Y:
                        if _has_classify_redo:
                            print("🟢 Ctrl+Y → Classification Redo (priority over Element Select)")
                            try:
                                self.app_window.redo_classification()
                            except Exception as e:
                                print(f"⚠️ Classification redo failed: {e}")
                            return True
                        print("🎯 Ctrl+Y → Element Select Redo (digitizer)")
                        try:
                            _digitizer.redo()
                        except Exception as e:
                            print(f"⚠️ Element Select redo failed: {e}")
                        return True

                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 4: DIGITIZER UNDO (STRICT CONDITIONS)
                # ONLY if: digitizer enabled + ribbon='draw' + NO other tool active
                # Also handles curve context for unified undo/redo
                #
                # Reaches here only when other_tool_active=False (classification NOT
                # active). The draw tool owns undo/redo exclusively in this branch —
                # stale classification stack history must not divert to classification
                # because the user has explicitly switched to the draw context.
                # ═══════════════════════════════════════════════════════════════════
                elif not other_tool_active and \
                     hasattr(self.app_window, 'digitizer') and \
                     (self.app_window.digitizer.enabled or
                      bool(getattr(self.app_window, '_draw_curve_context_active', False))) and \
                     (getattr(self.app_window.digitizer, 'active_tool', None) or
                      getattr(getattr(self.app_window, 'ribbon_manager', None), 'current_ribbon', None) == 'draw' or
                      bool(getattr(self.app_window, '_draw_curve_context_active', False))):

                    digitizer = self.app_window.digitizer

                    if event.key() == Qt.Key_Z:
                        print("🎨 Ctrl+Z → Digitizer Undo")
                        try:
                            digitizer.undo()
                        except Exception as e:
                            print(f"⚠️ Digitizer undo failed: {e}")
                        return True

                    elif event.key() == Qt.Key_Y:
                        print("🎨 Ctrl+Y → Digitizer Redo")
                        try:
                            digitizer.redo()
                        except Exception as e:
                            print(f"⚠️ Digitizer redo failed: {e}")
                        return True
                
                # ═══════════════════════════════════════════════════════════════════
                # ✅ LEVEL 5: CLASSIFICATION UNDO/REDO (DEFAULT)
                # This is the fallback when no other tool owns undo/redo
                # ═══════════════════════════════════════════════════════════════════
                else:
                    if event.key() == Qt.Key_Z:
                        print("🟢 Ctrl+Z → Undo Classification")
                        try:
                            self.app_window.undo_classification()
                        except Exception as e:
                            print(f"⚠️ Undo failed: {e}")
                        return True

                    elif event.key() == Qt.Key_Y:
                        print("🟢 Ctrl+Y → Redo Classification")
                        try:
                            self.app_window.redo_classification()
                        except Exception as e:
                            print(f"⚠️ Redo failed: {e}")
                        return True

                if (event.modifiers() & Qt.ShiftModifier):
                    if event.key() == Qt.Key_D:
                        if not self._shortcut_guard.try_acquire("Ctrl+Shift+D"):
                            return True
                        print("🎛️ Ctrl+Shift+D → Apply Display Settings")
                        try:
                            if not (hasattr(self.app_window, 'data') and self.app_window.data is not None):
                                print("   ⚠️ No point cloud data loaded")
                                if hasattr(self.app_window, 'statusBar'):
                                    self.app_window.statusBar().showMessage(
                                        f"[{key_label}] ⚠️ Please load a point cloud file first (File → Open)",
                                        3000
                                    )
                                return True
                            
                            saved_slot = None
                            if hasattr(self.app_window, 'display_mode_dialog') and \
                            self.app_window.display_mode_dialog is not None:
                                saved_slot = self.app_window.display_mode_dialog.current_slot
                                print(f"   💾 Current view: {saved_slot}")
                            
                            if not (hasattr(self.app_window, 'display_mode_dialog') and
                                    self.app_window.display_mode_dialog is not None):
                                print("   🔧 Creating Display Mode dialog silently...")
                                from gui.display_mode import DisplayModeDialog
                                prev_block = bool(getattr(self.app_window, '_block_ptc_autoload', False))
                                self.app_window._block_ptc_autoload = True
                                try:
                                    self.app_window.display_mode_dialog = DisplayModeDialog(self.app_window)
                                    try:
                                        _dlg = self.app_window.display_mode_dialog
                                        if hasattr(_dlg, '_load_slot_state'):
                                            _dlg._load_slot_state(int(getattr(_dlg, 'current_slot', 0)))
                                    except Exception:
                                        pass
                                finally:
                                    self.app_window._block_ptc_autoload = prev_block

                            if hasattr(self.app_window, 'ensure_display_mode_dialog'):
                                if not self.app_window.ensure_display_mode_dialog():
                                    return True
                            dialog = self.app_window.display_mode_dialog
                            if dialog is not None and hasattr(dialog, '_select_border_mode'):
                                dialog._select_border_mode(1, push_gpu=False)
 
                            print("   📋 Ctrl+Shift+D: GPU-poke all active views with current PTC...")
                            from gui.unified_actor_manager import sync_palette_to_gpu

                            app = self.app_window
                            views_synced = []

                            main_palette = getattr(app, 'class_palette', {})
                            border0 = float(getattr(app, 'point_border_percent', 0) or 0.0)
                            if sync_palette_to_gpu(app, 0, main_palette, border0, render=False):
                                views_synced.append("Main")

                            section_views = getattr(app, 'section_vtks', None)
                            if isinstance(section_views, dict):
                                for view_idx, vtk_w in section_views.items():
                                    if vtk_w is None:
                                        continue
                                    slot_idx = view_idx + 1
                                    from gui.unified_actor_manager import _get_slot_palette
                                    sec_palette = _get_slot_palette(app, slot_idx)
                                    if not sec_palette:
                                        if hasattr(app, "_get_cross_section_palette"):
                                            sec_palette = app._get_cross_section_palette(
                                                slot_idx,
                                                allow_default_seed=True,
                                                persist_seed=False,
                                            )
                                    if not sec_palette:
                                        continue
                                    border_s = float(
                                        getattr(dialog, 'view_borders', {}).get(slot_idx, 0) or 0
                                    )
                                    if sync_palette_to_gpu(app, slot_idx, sec_palette, border_s, render=False):
                                        views_synced.append(f"V{slot_idx}")

                            self._safe_widget_render(getattr(app, "vtk_widget", None))
                            if isinstance(section_views, dict):
                                for vtk_w in section_views.values():
                                    self._safe_widget_render(vtk_w)

                            msg = f"✅ Ctrl+Shift+D applied to: {', '.join(views_synced)}" \
                                if views_synced else "⚠️ No active views to sync"
                            if hasattr(app, 'statusBar'):
                                app.statusBar().showMessage(f"[{key_label}] {msg}", 2500)
                            print(f"   {msg}")
                            
                        except Exception as e:
                            print(f"⚠️ Display shortcut failed: {e}")
                            import traceback
                            traceback.print_exc()
                            if hasattr(self.app_window, 'statusBar'):
                                self.app_window.statusBar().showMessage(
                                    f"[{key_label}] ❌ Failed to apply display settings: {e}",
                                    3000
                                )
                        finally:
                            self._shortcut_guard.force_unlock()
                        return True
                    
                    if event.key() == Qt.Key_S:
                        print("⚡ Ctrl+Shift+S → Apply Shortcuts")
                        try:
                            from gui.shortcut_manager import ShortcutManager
                            sm = ShortcutManager.instance
                            if (sm is not None and hasattr(sm, "isVisible") and sm.isVisible()
                                    and hasattr(sm, "on_apply")):
                                print("   📋 ShortcutManager open — calling on_apply()")
                                sm.on_apply()
                            else:
                                print("   📋 ShortcutManager closed — loading from QSettings")
                            ShortcutManager.apply_shortcuts_from_settings(self.app_window)
                            
                        except Exception as e:
                            print(f"⚠️ Apply shortcuts failed: {e}")
                            import traceback
                            traceback.print_exc()
                            if hasattr(self.app_window, 'statusBar'):
                                self.app_window.statusBar().showMessage(
                                    f"[{key_label}] ❌ Failed to apply shortcuts: {e}",
                                    3000
                                )
                        return True

            # ====================================================================
            # Unlock views shortcut (Shift+P)
            # ====================================================================
            if (event.modifiers() & Qt.ShiftModifier) and event.key() == Qt.Key_P:
                print("🔓 Shift+P → Unlock Focused View")
                try:
                    from PySide6.QtWidgets import QApplication
                    focused = QApplication.focusWidget()
                    print(f"   🔍 Focused widget: {type(focused).__name__}")
                    
                    unlocked_view = None
                    if hasattr(self.app_window, 'section_vtks') and self.app_window.section_vtks:
                        for view_idx, vtk_widget in self.app_window.section_vtks.items():
                            try:
                                if (focused == vtk_widget.interactor or
                                    vtk_widget.interactor.isAncestorOf(focused)):
                                    self._unlock_cross_section_view(view_idx, vtk_widget)
                                    unlocked_view = f"Cross-Section {view_idx + 1}"
                                    break
                            except Exception as e:
                                print(f"   ⚠️ Error checking view {view_idx}: {e}")
                    
                    if unlocked_view is None:
                        self._unlock_main_view()
                        unlocked_view = "Main View"
                    
                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] 🔓 {unlocked_view} unlocked - 3D rotation enabled",
                            1500
                        )
                except Exception as e:
                    print(f"⚠️ Unlock failed: {e}")
                return True

            # ====================================================================
            # Fit View shortcut (Shift+F)
            # ====================================================================
            if (event.modifiers() & Qt.ShiftModifier) and event.key() == Qt.Key_F:
                print("🧲 Shift+F → Context-Aware Fit View")
                try:
                    cut_ctrl = getattr(self.app_window, "cut_section_controller", None)
                    focused = QApplication.focusWidget()
                    cut_vtk = getattr(cut_ctrl, "cut_vtk", None) if cut_ctrl else None
                    if (
                        cut_ctrl is not None
                        and getattr(cut_ctrl, "is_cut_view_active", False)
                        and cut_vtk is not None
                        and focused is not None
                    ):
                        try:
                            if focused == cut_vtk.interactor or cut_vtk.interactor.isAncestorOf(focused):
                                if hasattr(cut_ctrl, "fit_cut_section_view"):
                                    print("   🎯 Shift+F detected in cut section - fitting cut view")
                                    if cut_ctrl.fit_cut_section_view():
                                        return True
                        except Exception:
                            pass
                    self._handle_context_fit_view(key_label=key_label)
                except Exception as e:
                    print(f"⚠️ Fit view failed: {e}")
                return True

            # ====================================================================
            # PRIORITY 2: Tool Shortcuts (F1–F12, digits, etc.)
            # ====================================================================

            _digitizer = getattr(self.app_window, 'digitizer', None)
            if (_digitizer and
                    getattr(_digitizer, 'active_tool', None) == 'orthopolygon' and
                    event.key() == Qt.Key_Space):
                return False

            key = event.key()

            mod_parts = []
            if event.modifiers() & Qt.ControlModifier:
                mod_parts.append("ctrl")
            if event.modifiers() & Qt.AltModifier:
                mod_parts.append("alt")
            if event.modifiers() & Qt.ShiftModifier:
                mod_parts.append("shift")
            mod = "+".join(mod_parts) if mod_parts else "none"

            if Qt.Key_F1 <= key <= Qt.Key_F12:
                keyname = f"F{key - Qt.Key_F1 + 1}"
            elif Qt.Key_0 <= key <= Qt.Key_9:
                keyname = chr(ord('0') + (key - Qt.Key_0))
            else:
                keyname = QKeySequence(key).toString().upper() or event.text().upper()

            # Plain "x" is reserved for text entry and should not act as a
            # runtime shortcut. This prevents accidental point/view jumps when
            # the user presses x in the main viewport.
            if key == Qt.Key_X and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier | Qt.ShiftModifier)):
                return False

            combo = (mod.lower(), keyname.upper())

            shortcuts = getattr(self.app_window, 'shortcuts', {})
            shortcut = shortcuts.get(combo)

            tool = None
            if shortcut:
                tool = shortcut.get("tool")

            if shortcut and tool != "Pan" and getattr(
                self.app_window, "_left_pan_shortcut_active", False
            ):
                self._set_pan_shortcut_active(False)

            # Pan behaves like the other tool shortcuts: one press arms it and
            # it stays armed until Escape or another tool is selected. Mouse
            # ownership guards still prevent it from stealing an active tool's
            # left click.
            if tool == "Pan":
                if not event.isAutoRepeat():
                    execute_tool(
                        self.app_window,
                        "Pan",
                        key_label=key_label,
                    )
                    self._set_pan_shortcut_active(True)
                    print(f"Pan shortcut activated: {combo} (left-drag to pan)")
                    status_bar = getattr(self.app_window, "statusBar", None)
                    if callable(status_bar):
                        status_bar().showMessage(
                            f"[{key_label}] Pan active - left-drag to pan", 2500
                        )
                return True

            if shortcut and tool not in (
                "CutSectionRect",
                "cut_section",
                "cut_section_nested",
                "CutFromCross",
                "CutFromCut",
            ):
                cleanup_cut = getattr(self.app_window, "_deactivate_pending_cut_section_tool", None)
                if cleanup_cut:
                    cleanup_cut(f"switching to {tool} via shortcut")

            # ====================================================================
            # Surface shortcut handler
            # ====================================================================
            if tool == "Surface":
                print(f"✅ SHORTCUT MATCH: {combo} → Surface")

                if not self._shortcut_guard.try_acquire("Surface"):
                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] ⚠️ Please wait — previous display switch is still processing", 1500
                        )
                    return True

                try:
                    preset = shortcut.get("preset")
                    execute_tool(self.app_window, tool, preset=preset, key_label=key_label)
                    print("✅ Surface shortcut complete")

                except Exception as e:
                    print(f"⚠️ Surface shortcut failed: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    _QTimer.singleShot(
                        ShortcutExecutionGuard.COOLDOWN_DISPLAY,
                        self._shortcut_guard.force_unlock
                    )

                return True

            # ====================================================================
            # Dedicated flight-line shortcut. This intentionally bypasses the
            # class-preset rebase used by DisplayMode.
            if tool == "LineMode":
                preset = shortcut.get("preset") or {}
                lines = {
                    int(line_id): bool(shown)
                    for line_id, shown in dict(
                        preset.get("lines", {}) or {}
                    ).items()
                }
                if not lines:
                    print("LineMode shortcut has no flight-line preset")
                    return True
                if not self._shortcut_guard.try_acquire("DisplayMode"):
                    return True
                try:
                    app = self.app_window
                    by_slot = getattr(
                        app, "flight_line_visibility_by_slot", None
                    )
                    if not isinstance(by_slot, dict):
                        by_slot = {}
                    by_slot[0] = dict(lines)
                    app.flight_line_visibility_by_slot = by_slot
                    app.flight_line_visibility = dict(lines)

                    cache = getattr(
                        app, "_flight_line_mask_cache_by_slot", None
                    )
                    if isinstance(cache, dict):
                        cache.pop(0, None)
                    app._flight_line_mask_cache_key = None
                    app._flight_line_mask_cache = None

                    from gui.unified_actor_manager import (
                        fast_main_flight_line_visibility_update,
                    )
                    fast_done = fast_main_flight_line_visibility_update(app)
                    if fast_done:
                        app.set_display_mode("line")
                    else:
                        # Do not block a first Line shortcut on constructing a
                        # new 13M-point unified actor. The established Line
                        # pipeline is substantially faster when no actor exists.
                        from gui.pointcloud_display import update_pointcloud
                        app.display_mode = "line"
                        update_pointcloud(app, "line")

                    dlg = self._get_live_qt_attr("display_mode_dialog")
                    if dlg is not None:
                        dlg.current_slot = 0
                        if hasattr(dlg, "slot_box"):
                            dlg.slot_box.blockSignals(True)
                            dlg.slot_box.setCurrentIndex(0)
                            dlg.slot_box.blockSignals(False)
                        if hasattr(dlg, "color_mode"):
                            dlg.color_mode.blockSignals(True)
                            dlg.color_mode.setCurrentIndex(7)
                            dlg.color_mode.blockSignals(False)
                        if hasattr(dlg, "_line_visibility"):
                            dlg._line_visibility = dict(lines)

                    visible = sum(1 for shown in lines.values() if shown)
                    if hasattr(app, "statusBar"):
                        app.statusBar().showMessage(
                            f"[{key_label}] Line Mode: "
                            f"{visible}/{len(lines)} flight lines",
                            2500,
                        )
                    print(
                        f"Line shortcut applied: "
                        f"{visible}/{len(lines)} flight lines"
                    )
                except Exception as exc:
                    print(f"Line shortcut failed: {exc}")
                    import traceback
                    traceback.print_exc()
                finally:
                    _QTimer.singleShot(
                        ShortcutExecutionGuard.COOLDOWN_DISPLAY,
                        self._shortcut_guard.force_unlock,
                    )
                return True

            # DisplayMode shortcut handler
            # ====================================================================
            if tool == "DisplayMode":
                preset = shortcut.get("preset")
                print(f"✅ SHORTCUT MATCH: {combo} → DisplayMode")

                if self._is_classification_active():
                    print("🔄 Classification is active — deactivating before DisplayMode apply")
                    if hasattr(self.app_window, "deactivate_classification_tool"):
                        try:
                            has_cross_action = (
                                hasattr(self.app_window, "cross_action")
                                and self.app_window.cross_action is not None
                                and self.app_window.cross_action.isChecked()
                            )
                            self.app_window.deactivate_classification_tool(
                                preserve_cross_section=has_cross_action
                            )
                            self.app_window.active_classify_tool = None
                            print("   ✅ Classification deactivated for DisplayMode shortcut")
                        except Exception as e:
                            print(f"   ❌ Failed to deactivate classification: {e}")
                            if hasattr(self.app_window, 'statusBar'):
                                self.app_window.statusBar().showMessage(
                                    f"[{key_label}] ❌ Could not switch from classification to DisplayMode",
                                    2500
                                )
                            return True
                    else:
                        print("   ❌ No deactivate_classification_tool handler available")
                        if hasattr(self.app_window, 'statusBar'):
                            self.app_window.statusBar().showMessage(
                                f"[{key_label}] ❌ Cannot deactivate classification for DisplayMode switch",
                                2500
                            )
                        return True

                if not preset:
                    print("⚠️ DisplayMode shortcut has no preset")
                    return True

                if not self._shortcut_guard.try_acquire("DisplayMode"):
                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] ⚠️ Please wait — previous shortcut still processing", 1500
                        )
                    return True

                try:
                    print(f"\n{'='*60}")
                    print(f"🎨 APPLYING DISPLAYMODE PRESET FROM SHORTCUT")
                    print(f"{'='*60}")
                    
                    # ── REBASE: discard stale description/color/lvl/draw
                    #            and refill from the currently loaded PTC ──
                    from .shortcut_manager import rebase_display_preset_to_current_ptc
                    preset = rebase_display_preset_to_current_ptc(preset, self.app_window)

                    views = preset.get("views", {})
                    border_percent = preset.get("border_percent", 0.0)
                    target_display_mode = str(
                        preset.get("display_mode", "class") or "class"
                    ).lower()
                    target_quality_mode = str(
                        preset.get("quality_mode", "normal") or "normal"
                    ).lower()
                    if target_quality_mode not in ("fast", "normal", "slow"):
                        target_quality_mode = "normal"

                    # Shaded Classification / Surface both read their quality
                    # (Fast/Normal/Slow) from a global app attribute -- the
                    # same one the live Display Mode dialog's own "Speed"
                    # combo writes to (gui/display_mode.py:2681-2686). Set it
                    # from THIS shortcut's own saved quality before applying,
                    # so a shortcut with a different Speed than whatever was
                    # last used actually takes effect (previously the
                    # shortcut editor had no quality control at all, so this
                    # was always whatever the global setting happened to be).
                    if target_display_mode == "shaded_class":
                        self.app_window.shading_quality = target_quality_mode
                    elif target_display_mode == "surface":
                        self.app_window.surface_quality = target_quality_mode

                    target_view = int(list(views.keys())[0]) if views else 0
                    print(f"   🎯 TARGET VIEW FROM SHORTCUT: {target_view} "
                        f"mode={target_display_mode} quality={target_quality_mode}")

                    # ============================================================
                    # STEP 0: CHECK IF SAME SHORTCUT ALREADY APPLIED — skip rebuild
                    # ============================================================
                    # Identity includes target_display_mode/target_view/
                    # target_quality_mode, not just the key combo -- otherwise
                    # editing an already-"last applied" shortcut's mode (e.g.
                    # Class -> Depth) OR its Speed (e.g. Normal -> Fast, same
                    # mode) and pressing the same key again matched the old
                    # identity and was skipped as a no-op, silently keeping
                    # the stale mode/quality applied.
                    _current_shortcut_id = (
                        combo, target_display_mode, target_view, target_quality_mode
                    )
                    _last_applied_id = getattr(
                        self.app_window, '_last_display_shortcut_id', None
                    )

                    # lines 540–565 — REPLACE the entire if block with this:
                    
                    if _last_applied_id == _current_shortcut_id:
                        _state_ok = True
                        try:
                            if target_view == 0 and str(
                                    getattr(self.app_window, 'display_mode', None) or ""
                            ).lower() != target_display_mode:
                                _state_ok = False
                            if _state_ok and target_view == 0:
                                _cp = getattr(self.app_window, 'class_palette', None)
                                if _cp:
                                    view_key = str(target_view) if str(target_view) in views \
                                        else target_view
                                    _pclasses = views.get(
                                        view_key, views.get(str(view_key), {})
                                    )
                                    for _pc, _pi in _pclasses.items():
                                        _ci = _cp.get(int(_pc))
                                        if _ci is None or \
                                                _ci.get('show', True) != _pi.get('show', False):
                                            _state_ok = False
                                            break
                                        if abs(float(_ci.get('weight', 1.0)) -
                                            float(_pi.get('weight', 1.0))) > 0.001:
                                            _state_ok = False
                                            break
                            elif _state_ok and target_view != 0:
                                # Same gap as the target_view == 0 branch above
                                # used to have: verify the section's own
                                # CURRENTLY APPLIED mode still matches this
                                # shortcut's mode, not just class visibility/
                                # weights. Without this, manually switching
                                # that section's mode via the Display Mode
                                # dialog (or a different shortcut) and then
                                # re-pressing THIS shortcut could see
                                # unchanged classes/weights, wrongly treat it
                                # as "already applied", and skip -- leaving
                                # the section stuck on the wrong mode.
                                _dlg_for_check = getattr(
                                    self.app_window, 'display_mode_dialog', None
                                )
                                _view_modes = getattr(
                                    _dlg_for_check, 'view_color_modes', {}
                                ) if _dlg_for_check else {}
                                _MODE_TO_IDX_CHECK = {
                                    'class': 0, 'shaded_class': 1,
                                    'depth': 2, 'intensity': 3,
                                    'rgb': 4, 'elevation': 5,
                                    'surface': 6, 'line': 7,
                                }
                                if _view_modes.get(target_view, 0) != \
                                        _MODE_TO_IDX_CHECK.get(target_display_mode, 0):
                                    _state_ok = False

                                _vp = getattr(self.app_window, 'view_palettes', {}).get(target_view)
                                if not _state_ok:
                                    pass
                                elif not _vp:
                                    _state_ok = False
                                else:
                                    view_key = str(target_view) if str(target_view) in views \
                                        else target_view
                                    _pclasses = views.get(
                                        view_key, views.get(str(view_key), {})
                                    )
                                    for _pc, _pi in _pclasses.items():
                                        _ci = _vp.get(int(_pc))
                                        if _ci is None or \
                                                _ci.get('show', True) != _pi.get('show', False):
                                            _state_ok = False
                                            break
                                        if abs(float(_ci.get('weight', 1.0)) -
                                            float(_pi.get('weight', 1.0))) > 0.001:
                                            _state_ok = False
                                            break
                        except Exception:
                            _state_ok = False

                        if _state_ok:
                            print(f"   ⏭️  SKIPPING — same shortcut already applied (no-op)")
                            print(f"{'='*60}\n")
                            if hasattr(self.app_window, 'statusBar'):
                                self.app_window.statusBar().showMessage(
                                    f"[{key_label}] ✅ DisplayMode already applied (no rebuild needed)", 2000
                                )
                            self._shortcut_guard.force_unlock()
                            return True

                    print(f"   🔄 Applying preset "
                        f"(last={_last_applied_id}, current={_current_shortcut_id})...")

                    # ============================================================
                    # STEP 0A: Reset class_palette visibility + clear shading
                    # ✅ FIX: Properly transition from shading to classification
                    # ============================================================
                    print(f"   🔄 Checking display state (target={target_view})...")

                    if target_view == 0:

                        # ✅ FIX STEP 1: Record previous display mode BEFORE changing
                        previous_display_mode = getattr(
                            self.app_window, 'display_mode', 'class'
                        )

                        previous_display_mode_l = str(previous_display_mode or "").lower()

                        was_shading = previous_display_mode_l in (
                            'shaded_class', 'shading', 'hillshade'
                        )

                        was_surface = previous_display_mode_l == "surface"

                        print(f"   🔍 Previous display mode: '{previous_display_mode}' "
                            f"| was_shading={was_shading} | was_surface={was_surface}")

                        # Surface transitions must behave like manual Display Mode.
                        # Remove Surface mesh before class actor/GPU sync is made visible.
                        if was_surface:
                            try:
                                from gui.surface_mode import detach_surface_before_non_surface_mode
                                detach_surface_before_non_surface_mode(
                                    self.app_window,
                                    requested_mode=target_display_mode,
                                )
                                print("      ✅ Surface actor removed before DisplayMode shortcut class view")
                            except Exception as _surface_cleanup_err:
                                print(f"      ⚠️ Surface cleanup before DisplayMode shortcut skipped: {_surface_cleanup_err}")

                        # ✅ FIX STEP 2: Reset class_palette visibility from preset
                        print(f"   🔄 Resetting class_palette visibility from preset...")
                        if hasattr(self.app_window, 'class_palette'):
                            for code in self.app_window.class_palette:
                                self.app_window.class_palette[code]["show"] = False

                        view_key = str(target_view) if str(target_view) in views \
                            else target_view
                        preset_classes = views.get(
                            view_key, views.get(str(view_key), {})
                        )
                        for code, info in preset_classes.items():
                            code_int = int(code)
                            if code_int in self.app_window.class_palette:
                                self.app_window.class_palette[code_int]["show"] = \
                                    info.get("show", False)
                                self.app_window.class_palette[code_int]["weight"] = \
                                    info.get("weight", 1.0)
                                print(f"      Class {code_int}: "
                                    f"show={info.get('show', False)}")
                        print(f"   ✅ class_palette reset complete")

                        # ✅ FIX STEP 3: Remove shading mesh actor
                        print(f"   🧹 Clearing shading actors...")
                        if hasattr(self.app_window, '_shaded_mesh_actor') and \
                                self.app_window._shaded_mesh_actor:
                            try:
                                self.app_window.vtk_widget.remove_actor(
                                    'shaded_mesh', render=False
                                )
                                self.app_window._shaded_mesh_actor = None
                                print(f"      ✅ Removed shading mesh actor")
                            except Exception as e:
                                print(f"      ⚠️ Could not remove shading actor: {e}")

                        if hasattr(self.app_window, '_shaded_mesh_polydata'):
                            self.app_window._shaded_mesh_polydata = None

                        try:
                            from gui.shading_display import clear_shading_cache
                            clear_shading_cache("Main View switching to DisplayMode")
                            print(f"      ✅ Cleared shading cache")
                        except Exception as e:
                            print(f"      ⚠️ Could not clear shading cache: {e}")

                        # ✅ FIX STEP 4: Clear shading override flag
                        self.app_window._shading_visibility_override = None

                        print(
                            f"      Target display mode from shortcut: "
                            f"'{target_display_mode}'"
                        )

                        # ✅ FIX STEP 6: Restore class_ actor visibility
                        # CRITICAL — ShadingMode hides all class_ actors via
                        # SetVisibility(False). Without this restore, the view
                        # stays blank even after GPU sync rebuilds the palette.
                        if was_shading:
                            print(f"      🔧 Restoring class_ actors hidden by shading...")
                            _hidden_actors = getattr(
                                self.app_window, '_actors_hidden_by_shading', None
                            )
                            if _hidden_actors:
                                # Precise restore — only actors we specifically hid
                                _restored = 0
                                for name in _hidden_actors:
                                    actor = self.app_window.vtk_widget.actors.get(name)
                                    if actor:
                                        actor.SetVisibility(True)
                                        _restored += 1
                                print(f"      ✅ Precisely restored {_restored}/"
                                    f"{len(_hidden_actors)} hidden actors")
                                self.app_window._actors_hidden_by_shading = []
                            else:
                                # Broad fallback — restore all class_ actors
                                _restored = 0
                                for name, actor in \
                                        self.app_window.vtk_widget.actors.items():
                                    if str(name).startswith("class_"):
                                        actor.SetVisibility(True)
                                        _restored += 1
                                print(f"      ✅ Broad restore: {_restored} class_ actors")
                        else:
                            print(f"      ℹ️ No shading transition — skip actor restore")

                        # ✅ FIX STEP 7: Ensure unified actor is visible
                        try:
                            from gui.unified_actor_manager import _get_unified_actor
                            _ua = _get_unified_actor(self.app_window)
                            if _ua is not None:
                                _ua.SetVisibility(True)
                                print(f"      ✅ Unified actor made visible")
                            else:
                                print(f"      ℹ️ No unified actor yet — will be built below")
                        except Exception as e:
                            print(f"      ⚠️ Could not check unified actor: {e}")

                        # ✅ FIX STEP 8: GPU sync / full rebuild
                        try:
                            from gui.unified_actor_manager import (
                                sync_palette_to_gpu, _get_unified_actor
                            )
                            _actor = _get_unified_actor(self.app_window)
                            print(f"      🔍 Unified actor for GPU sync: "
                                f"{'exists' if _actor else 'None — full rebuild'}")
                            if _actor is not None:
                                _actor.SetVisibility(True)  # ensure visible before sync
                                sync_palette_to_gpu(
                                    self.app_window, 0,
                                    self.app_window.class_palette,
                                    border_percent, render=True
                                )
                                print(f"      ⚡ Fast GPU sync complete")
                            else:
                                from gui.class_display import update_class_mode
                                update_class_mode(self.app_window, force_refresh=True)
                                print(f"      ✅ Full classification rebuild complete")
                        except Exception as e:
                            print(f"      ⚠️ GPU sync failed, falling back: {e}")
                            import traceback
                            traceback.print_exc()
                            try:
                                from gui.class_display import update_class_mode
                                self.app_window._preserve_view = True
                                update_class_mode(self.app_window, force_refresh=True)
                            except Exception as _fb_err:
                                print(f"      ❌ Fallback rebuild also failed: {_fb_err}")

                    else:
                        print(f"   ⏭️  Target is View {target_view}, NOT Main View")
                        _prev_mode = getattr(self.app_window, 'display_mode', 'class')
                        print(f"   ✅ Main View stays in '{_prev_mode}' — no change")

                    view_names = [
                        "Main View", "View 1", "View 2",
                        "View 3", "View 4",
                    ]
                    print(f"   📋 Border: {border_percent}%")
                    print(f"   📋 Views configured: {list(views.keys())}")

                    # ============================================================
                    # PRE-STEP: Write preset to app.view_palettes
                    # ============================================================
                    if not hasattr(self.app_window, 'view_palettes'):
                        self.app_window.view_palettes = {}

                    for view_idx_str, classes in views.items():
                        view_idx = int(view_idx_str)
                        self.app_window.view_palettes[view_idx] = {}
                        for code, info in classes.items():
                            code_int = int(code)
                            self.app_window.view_palettes[view_idx][code_int] = {
                                "show":        bool(info.get("show", False)),
                                "description": str(info.get("description", "")),
                                "color":       tuple(info.get("color", (128, 128, 128))),
                                "weight":      float(info.get("weight", 1.0)),
                                "draw":        info.get("draw", ""),
                                "lvl":         info.get("lvl", "")
                            }
                    print(f"   ✅ PRE-STEP: app.view_palettes seeded "
                        f"({len(views)} views)")

                    if not hasattr(self.app_window, 'view_borders'):
                        self.app_window.view_borders = {}
                    self.app_window.view_borders[target_view] = border_percent

                    # ============================================================
                    # Initialize display_mode_dialog if needed
                    # ============================================================
                    dlg = self._get_live_qt_attr("display_mode_dialog")
                    if dlg is None:
                        self.app_window._block_ptc_autoload = True
                        try:
                            from gui.display_mode import DisplayModeDialog
                            dlg = DisplayModeDialog(self.app_window)
                            self.app_window.display_mode_dialog = dlg
                            print("   🔧 Created DisplayModeDialog")
                        finally:
                            self.app_window._block_ptc_autoload = False

                    if dlg is None:
                        print("   ⚠️ DisplayModeDialog unavailable")
                        return True

                    if not hasattr(dlg, 'view_palettes'):
                        dlg.view_palettes = {}
                    if not hasattr(dlg, 'view_borders'):
                        dlg.view_borders = {}
                    if not hasattr(dlg, 'slot_shows'):
                        dlg.slot_shows = {}

                    # ============================================================
                    # STEP 1: Update slot dropdown to target view
                    # ============================================================
                    print(f"\n   🔄 SWITCHING DIALOG TO VIEW {target_view}")
                    dlg.current_slot = target_view

                    slot_widget = None
                    for attr_name in ['slot_box', 'slot_combo',
                                    'view_combo', 'view_selector']:
                        if hasattr(dlg, attr_name):
                            slot_widget = getattr(dlg, attr_name)
                            if slot_widget is not None:
                                slot_widget.blockSignals(True)
                                slot_widget.setCurrentIndex(target_view)
                                slot_widget.blockSignals(False)
                                print(f"   ✅ Updated {attr_name} → index {target_view}")
                                break

                    if slot_widget is None:
                        print(f"   ⚠️ Could not find slot dropdown widget")

                    # ============================================================
                    # STEP 2: Process preset data into dialog state
                    # ============================================================
                    for view_idx_str, classes in views.items():
                        view_idx = int(view_idx_str)
                        print(f"   🔍 Processing View {view_idx} "
                            f"({len(classes)} classes)")

                        dlg.view_palettes[view_idx] = {}
                        if view_idx not in dlg.slot_shows:
                            dlg.slot_shows[view_idx] = {}

                        for code, info in classes.items():
                            code_int = int(code)
                            dlg.view_palettes[view_idx][code_int] = {
                                "show":        info.get("show", False),
                                "description": info.get("description", ""),
                                "color":       tuple(info.get("color", (128, 128, 128))),
                                "weight":      float(info.get("weight", 1.0)),
                                "draw":        info.get("draw", ""),
                                "lvl":         info.get("lvl", "")
                            }
                            dlg.slot_shows[view_idx][code_int] = \
                                info.get("show", False)

                        dlg.view_borders[view_idx] = border_percent
                        
                        border_type = preset.get("border_type", 0)
                        if hasattr(dlg, 'border_logic_hybrid') and hasattr(dlg, 'border_logic_object') and hasattr(dlg, 'border_logic_point'):
                            if border_type == 2:
                                dlg.border_logic_hybrid.setChecked(True)
                            elif border_type == 1:
                                dlg.border_logic_object.setChecked(True)
                            else:
                                dlg.border_logic_point.setChecked(True)

                        visible = [
                            c for c, i in dlg.view_palettes[view_idx].items()
                            if i.get("show")
                        ]
                        print(f"   ✅ View {view_idx}: {len(visible)} visible classes, border={border_percent}%, type={border_type}")

                    # ============================================================
                    # STEP 3: Sync class_palette for Main View
                    # ============================================================
                    for view_idx_str, classes in views.items():
                        view_idx = int(view_idx_str)
                        for code, info in classes.items():
                            code_int = int(code)
                            preset_weight = float(info.get("weight", 1.0))
                            if view_idx == 0 and \
                                    hasattr(self.app_window, 'class_palette') and \
                                    code_int in self.app_window.class_palette:
                                self.app_window.class_palette[code_int]["show"] = \
                                    bool(info.get("show", False))
                                self.app_window.class_palette[code_int]["weight"] = \
                                    preset_weight

                    for view_idx in views.keys():
                        view_idx = int(view_idx)
                        if view_idx == 0:
                            print(f"   ✅ Synced view_palettes[0] AND class_palette")
                        else:
                            print(f"   ✅ Synced view_palettes[{view_idx}] "
                                f"(Main View untouched)")

                    # ============================================================
                    # STEP 4: Refresh dialog UI for target view
                    # Always update widget state regardless of visibility so the
                    # dialog reflects the shortcut when it is next opened.
                    # ============================================================
                    print(f"\n   🔄 REFRESHING DIALOG UI → VIEW {target_view}")
                    dlg.blockSignals(True)
                    try:
                        if hasattr(dlg, '_load_slot_checkboxes'):
                            dlg._load_slot_checkboxes(target_view)
                            print(f"   ✅ Checkboxes reloaded")

                        if hasattr(dlg, '_load_slot_weights'):
                            dlg._load_slot_weights(target_view)
                            print(f"   ✅ Weights reloaded")

                        if hasattr(dlg, 'load_view_border'):
                            dlg.load_view_border(target_view)

                        if hasattr(dlg, 'border_slider'):
                            dlg.border_slider.blockSignals(True)
                            dlg.border_slider.setValue(int(border_percent))
                            dlg.border_slider.blockSignals(False)

                        view_name = view_names[target_view] \
                            if target_view < len(view_names) \
                            else f"View {target_view}"
                        dlg.setWindowTitle(f"Display Mode - {view_name} ✓")
                        print(f"   ✅ Title → {view_name}")

                    finally:
                        dlg.blockSignals(False)

                    # ============================================================
                    # STEP 4C: Sync color_mode combo to match actual display mode.
                    # For Main View presets: force "By Classification" (index 0)
                    # because display_mode was set to 'class' above.
                    # For View 1-4 presets: keep the combo reflecting the current
                    # Main View display mode so the user's Depth/RGB/Intensity/
                    # Elevation selection is not silently discarded.
                    # ============================================================
                    if hasattr(dlg, 'color_mode'):
                        # Same fix as STEP 5 below: the combo must reflect
                        # THIS preset's own target_display_mode regardless of
                        # target_view -- it previously borrowed Main View's
                        # live mode for any section target, so opening the
                        # Display Mode dialog after a section-targeted
                        # Elevation/Depth/... shortcut still showed
                        # "By Classification" instead of the mode actually
                        # applied.
                        _MODE_TO_IDX_LOCAL = {
                            'class': 0, 'shaded_class': 1,
                            'depth': 2, 'intensity': 3,
                            'rgb': 4, 'elevation': 5,
                            'surface': 6, 'line': 7,
                        }
                        _combo_idx = _MODE_TO_IDX_LOCAL.get(
                            target_display_mode, 0
                        )
                        dlg.color_mode.blockSignals(True)
                        dlg.color_mode.setCurrentIndex(_combo_idx)
                        dlg.color_mode.blockSignals(False)
                        print(f"   ✅ color_mode combo set to index {_combo_idx}")

                        # Remember this view's mode the same way the Display
                        # Mode dialog's own Apply button does (display_mode.py
                        # on_apply/on_slot_changed), so drawing a NEW section
                        # afterward re-applies the mode this shortcut actually
                        # set instead of whatever mode was last applied via
                        # the dialog's Apply button (e.g. Shaded Classification
                        # set manually before this shortcut ran).
                        if not hasattr(dlg, 'view_color_modes'):
                            dlg.view_color_modes = {}
                        dlg.view_color_modes[target_view] = _combo_idx

                        # Same gap, one more control: the live dialog's own
                        # "Speed" combo (dlg.shading_quality) wasn't synced
                        # either, so opening Display Mode after a
                        # shortcut-applied Shaded/Surface with a different
                        # quality than whatever the combo last showed would
                        # display a stale Speed value.
                        if hasattr(dlg, 'shading_quality'):
                            _quality_idx = dlg.shading_quality.findData(
                                target_quality_mode
                            )
                            if _quality_idx >= 0:
                                dlg.shading_quality.blockSignals(True)
                                dlg.shading_quality.setCurrentIndex(_quality_idx)
                                dlg.shading_quality.blockSignals(False)
                                if target_display_mode == "shaded_class":
                                    dlg._shading_quality_value = target_quality_mode
                                elif target_display_mode == "surface":
                                    dlg._surface_quality_value = target_quality_mode

                    # ============================================================
                    # STEP 4B: Sync class_palette — Main View only
                    # ============================================================
                    print(f"\n   🔄 CLASS_PALETTE SYNC CHECK (target={target_view})")
                    if target_view == 0:
                        if target_view in self.app_window.view_palettes:
                            for code, info in \
                                    self.app_window.view_palettes[target_view].items():
                                if code in self.app_window.class_palette:
                                    self.app_window.class_palette[code]["show"] = \
                                        info.get("show", False)
                                    self.app_window.class_palette[code]["weight"] = \
                                        info.get("weight", 1.0)
                            print(f"   ✅ class_palette synced from view_palettes[0]")
                    else:
                        print(f"   ⏭️  Skipped (View {target_view}, not Main View)")

                    # ============================================================
                    # STEP 5: Check current display mode / adjust borders
                    # ============================================================
                    # The preset's own chosen mode (target_display_mode) applies
                    # regardless of target view -- previously a section target
                    # (target_view != 0) silently discarded the mode picked in
                    # the editor's dropdown and borrowed Main View's current live
                    # mode instead, so every section-targeted DisplayMode
                    # shortcut behaved like whatever Main View happened to be
                    # showing (usually Class), never the mode actually saved.
                    current_display_mode = target_display_mode
                    print(f"\n   🎨 DISPLAY MODE NOW: {current_display_mode}")

                    if current_display_mode in ['depth', 'rgb', 'intensity']:
                        print(f"   🔳 Forcing border=0 for {current_display_mode}")
                        border_percent = 0
                        self.app_window.point_border_percent = 0
                        self.app_window._main_view_borders_active = False
                        dlg.view_borders[target_view] = 0
                        if hasattr(dlg, 'load_view_border'):
                            dlg.load_view_border(target_view)
                        if hasattr(dlg, 'border_slider'):
                            dlg.border_slider.blockSignals(True)
                            dlg.border_slider.setValue(0)
                            dlg.border_slider.blockSignals(False)
                    else:
                        print(f"   🔳 Using preset border {border_percent}%")
                        if target_view == 0:
                            self.app_window.point_border_percent = border_percent
                            self.app_window._main_view_borders_active = \
                                (border_percent > 0)

                    # ============================================================
                    # STEP 6: Refresh views
                    # ============================================================
                    self.app_window._preserve_shortcut_visibility = True

                    if target_view == 0 and 0 in views:
                        dlg.view_borders[0] = border_percent
                        try:
                            if target_display_mode != "class":
                                self.app_window._preserve_view = True
                                self.app_window.set_display_mode(target_display_mode)
                                print(
                                    f"   STEP 6: Main view switched live to "
                                    f"{target_display_mode}"
                                )
                            else:
                                # Palette sync alone does not leave Line mode.
                                if str(getattr(
                                    self.app_window, "display_mode", "class"
                                ) or "class").lower() != "class":
                                    self.app_window._preserve_view = True
                                    self.app_window.set_display_mode("class")
                                    print("   STEP 6: Main view switched live to class")
                                from gui.unified_actor_manager import (
                                    sync_palette_to_gpu, _get_unified_actor
                                )
                                _actor = _get_unified_actor(self.app_window)
                                if _actor is not None:
                                    _actor.SetVisibility(True)
                                    self.app_window._preserve_view = True
                                    sync_palette_to_gpu(
                                        self.app_window, 0,
                                        self.app_window.class_palette,
                                        border_percent, render=True
                                    )
                                    print(f"   STEP 6: Main view GPU sync "
                                        f"(border={border_percent}%)")
                                else:
                                    from gui.class_display import update_class_mode
                                    self.app_window._preserve_view = True
                                    update_class_mode(self.app_window, force_refresh=True)
                                    print(f"   STEP 6: Main view rebuilt (no actor)")
                        except Exception as _sync_err:
                            print(f"   ⚠️ STEP 6 GPU sync failed: {_sync_err}")
                            import traceback
                            traceback.print_exc()
                            try:
                                from gui.class_display import update_class_mode
                                self.app_window._preserve_view = True
                                update_class_mode(self.app_window, force_refresh=True)
                            except Exception as _fb_err:
                                print(f"   ❌ STEP 6 fallback failed: {_fb_err}")

                    # Cross-section views
                    all_section_views = [v for v in views.keys() if 1 <= int(v) <= 5]
                    if all_section_views:
                        print(f"\n   🔄 SYNCING SECTION VIEWS: {all_section_views}")
                        for view_idx_str in all_section_views:
                            view_idx = int(view_idx_str)

                            if view_idx == 5:
                                if hasattr(self.app_window, 'cut_vtk') and \
                                        self.app_window.cut_vtk is not None:
                                    try:
                                        print(f"\n      🔪 REFRESHING CUT SECTION")
                                        self._refresh_cut_section(view_idx)
                                        print(f"      ✅ Cut Section refreshed")
                                    except Exception as e:
                                        print(f"      ⚠️ Cut Section failed: {e}")
                                        import traceback
                                        traceback.print_exc()
                                else:
                                    print(f"      ℹ️ Cut Section not open — "
                                        f"preset stored, applies on open")
                                continue

                            view_index = view_idx - 1

                            section_vtks = getattr(
                                self.app_window, 'section_vtks', {}
                            )
                            if view_index not in section_vtks:
                                print(f"      ℹ️ View {view_idx} not open — "
                                    f"preset in view_palettes[{view_idx}]")
                                continue

                            try:
                                from gui.unified_actor_manager import \
                                    build_section_unified_actor
                                try:
                                    from gui.cross_section.section_shaded_surface import \
                                        remove_section_shaded_surface_actor
                                    remove_section_shaded_surface_actor(
                                        self.app_window, view_index
                                    )
                                except Exception as _cs_mesh_clear_err:
                                    print(f"      ⚠️ Shaded/Surface mesh cleanup "
                                        f"skipped for View {view_idx}: {_cs_mesh_clear_err}")

                                if view_idx not in dlg.view_palettes or \
                                        not dlg.view_palettes[view_idx]:
                                    dlg.view_palettes[view_idx] = dict(
                                        self.app_window.view_palettes.get(
                                            view_idx, {}
                                        )
                                    )
                                    print(f"      🔄 dlg.view_palettes[{view_idx}] "
                                        f"synced from app (fallback)")

                                actor = build_section_unified_actor(
                                    self.app_window,
                                    view_index,
                                    border_percent=border_percent,
                                )

                                if actor is not None:
                                    visible = [
                                        c for c, info in
                                        self.app_window.view_palettes.get(
                                            view_idx, {}
                                        ).items()
                                        if info.get("show", False)
                                    ]
                                    print(f"      ✅ View {view_idx}: "
                                        f"{len(visible)} classes visible")
                                else:
                                    print(f"      ⚠️ View {view_idx}: "
                                        f"build returned None")

                                # Apply the preset's own chosen display mode to
                                # this section. Previously only the class-color
                                # point actor above was ever built here, so a
                                # section-targeted Shaded/Surface/Depth/
                                # Intensity/RGB/Elevation shortcut silently
                                # behaved like Class.
                                _SECTION_WEIGHT_MODE = {
                                    "depth": "depth", "intensity": "intensity",
                                    "rgb": "rgb", "elevation": "elevation",
                                    "line": "line",
                                }
                                if target_display_mode in ("shaded_class", "surface"):
                                    from gui.cross_section.section_shaded_surface import \
                                        build_section_shaded_surface_actor
                                    _mesh_mode = (
                                        "shaded" if target_display_mode == "shaded_class"
                                        else "surface"
                                    )
                                    _mesh_ok = build_section_shaded_surface_actor(
                                        self.app_window, view_index, _mesh_mode
                                    )
                                    print(f"      🎨 View {view_idx}: "
                                        f"{target_display_mode} "
                                        f"{'applied' if _mesh_ok else 'unavailable'} "
                                        f"(mesh cut)")
                                elif target_display_mode in _SECTION_WEIGHT_MODE:
                                    from gui.cross_section.section_shaded_surface import \
                                        remove_section_shaded_surface_actor
                                    remove_section_shaded_surface_actor(
                                        self.app_window, view_index
                                    )
                                    from gui.unified_actor_manager import \
                                        refresh_section_after_weight_change
                                    _palette = dlg.view_palettes.get(view_idx, {})
                                    _mode_ok = refresh_section_after_weight_change(
                                        self.app_window, view_index, _palette,
                                        0.0, _SECTION_WEIGHT_MODE[target_display_mode],
                                    )
                                    print(f"      🎨 View {view_idx}: "
                                        f"{target_display_mode} "
                                        f"{'applied' if _mode_ok else 'unavailable'}")
                                else:
                                    # Class -- clear any leftover Shaded/Surface
                                    # mesh actor from a previously-applied mode
                                    # on this section.
                                    from gui.cross_section.section_shaded_surface import \
                                        remove_section_shaded_surface_actor
                                    remove_section_shaded_surface_actor(
                                        self.app_window, view_index
                                    )

                            except Exception as e:
                                print(f"      ⚠️ View {view_idx} sync failed: {e}")
                                import traceback
                                traceback.print_exc()

                    # Track last applied shortcut
                    self.app_window._last_display_shortcut_id = _current_shortcut_id

                    total_visible = sum(
                        sum(1 for c in classes.values() if c.get("show"))
                        for classes in views.values()
                    )

                    if hasattr(self.app_window, 'statusBar'):
                        view_names_short = []
                        for v in sorted([int(x) for x in views.keys()]):
                            if v == 0:
                                view_names_short.append("Main")
                            elif v == 5:
                                view_names_short.append("Cut")
                            else:
                                view_names_short.append(f"V{v}")

                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] ✅ DisplayMode: "
                            f"{target_display_mode}, {view_names_short[0]} active, "
                            f"{total_visible} classes visible",
                            2500
                        )

                    print(f"   ✅ DisplayMode shortcut complete — "
                        f"{view_names[target_view]}")
                    print(f"{'='*60}\n")

                except Exception as e:
                    print(f"⚠️ Failed to apply DisplayMode preset: {e}")
                    import traceback
                    traceback.print_exc()

                finally:
                    _QTimer.singleShot(
                        1000,
                        lambda: setattr(
                            self.app_window,
                            '_preserve_shortcut_visibility',
                            False
                        )
                    )
                    _QTimer.singleShot(
                        ShortcutExecutionGuard.COOLDOWN_DISPLAY,
                        self._shortcut_guard.force_unlock
                    )

                return True

            # ====================================================================
            # ShadingMode shortcut handler
            # ====================================================================
            if tool == "ShadingMode":
                preset = shortcut.get("preset")
                print(f"✅ SHORTCUT MATCH: {combo} → ShadingMode")

                if not preset:
                    print("⚠️ ShadingMode shortcut has no preset")
                    return True

                if not self._shortcut_guard.try_acquire("ShadingMode"):
                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] ⚠️ Please wait — previous shortcut still processing", 1500
                        )
                    return True

                try:
                    print(f"\n{'='*60}")
                    print(f"🌗 APPLYING SHADINGMODE PRESET FROM SHORTCUT")
                    print(f"{'='*60}")

                    quality_mode = normalize_shading_preset_quality(
                        preset.get("quality_mode"),
                        preset.get("speed"),
                    )
                    previous_quality = normalize_shading_preset_quality(
                        getattr(self.app_window, "shading_quality", "normal")
                    )
                    previous_display_mode = str(
                        getattr(self.app_window, "display_mode", "") or ""
                    ).lower()

                    # ============================================================
                    # GUARD: Skip if same shading already active
                    # ============================================================
                    _skip_shading = False
                    try:
                        if getattr(self.app_window, 'display_mode', None) == \
                                'shaded_class' and \
                                hasattr(self.app_window, '_shaded_mesh_actor') and \
                                self.app_window._shaded_mesh_actor is not None:
                            _az_match = abs(
                                getattr(self.app_window, 'last_shade_azimuth', -1) -
                                preset.get('azimuth', 45.0)
                            ) < 0.01
                            _an_match = abs(
                                getattr(
                                    self.app_window, 'shading_sharpness_angle',
                                    getattr(self.app_window, 'last_shade_angle', -1)
                                ) - preset.get('angle', 45.0)
                            ) < 0.01
                            _am_match = abs(
                                getattr(self.app_window, 'shade_ambient', -1) -
                                preset.get('ambient', 0.2)
                            ) < 0.01
                            _quality_match = previous_quality == quality_mode
                            if _az_match and _an_match and _am_match and _quality_match:
                                _p_vis = set(
                                    int(c) for c, i in
                                    preset.get("classes", {}).items()
                                    if i.get("show", False)
                                )
                                _c_vis = getattr(
                                    self.app_window,
                                    '_shading_visibility_override',
                                    None
                                )
                                if _c_vis is None:
                                    _c_vis = getattr(
                                        self.app_window,
                                        '_shading_visible_classes',
                                        None
                                    )
                                if _c_vis is not None and \
                                        isinstance(_c_vis, set) and \
                                        _c_vis == _p_vis:
                                    _skip_shading = True
                    except Exception:
                        _skip_shading = False

                    # ============================================================
                    # STEP 0: Remove Surface before Shading shortcut
                    # Must run BEFORE _skip_shading, otherwise Surface can remain
                    # when shortcut thinks Shading is already active.
                    # ============================================================
                    try:
                        from gui.surface_mode import detach_surface_before_non_surface_mode
                        detach_surface_before_non_surface_mode(
                            self.app_window,
                            requested_mode="shaded_class",
                        )
                    except Exception as _surface_cleanup_err:
                        print(f"   ⚠️ Surface cleanup before Shading shortcut skipped: {_surface_cleanup_err}")

                    if _skip_shading:
                        # Same shortcut pressed again.
                        # Do NOT rebuild mesh, but enforce correct actor visibility.
                        # This prevents point/class layer from appearing over Shading.
                        self._enforce_shading_only_visibility(render=True)

                        print(f"   ⏭️  SKIPPING — same shading already active (visibility enforced)")
                        print(f"{'='*60}\n")
                        if hasattr(self.app_window, 'statusBar'):
                            self.app_window.statusBar().showMessage(
                                f"[{key_label}] ✅ ShadingMode already applied",
                                2000
                            )
                        self._shortcut_guard.force_unlock()
                        return True

                    # ============================================================
                    # STEP 1: Hide classification actors + SAVE which ones we hide
                    # ✅ FIX: Track hidden actors so DisplayMode can restore them
                    # ============================================================
                    if hasattr(self.app_window, 'vtk_widget'):
                        _hidden_by_shading = []
                        for name in list(self.app_window.vtk_widget.actors.keys()):
                            if str(name).startswith("class_"):
                                self.app_window.vtk_widget.actors[name]\
                                    .SetVisibility(False)
                                _hidden_by_shading.append(name)
                        # ✅ FIX: Persist the list for DisplayMode to restore
                        self.app_window._actors_hidden_by_shading = _hidden_by_shading
                        print(f"   🙈 Hidden {len(_hidden_by_shading)} class_ actors "
                            f"(saved for restore)")

                    # STEP 2: Set display mode
                    self.app_window.display_mode = "shaded_class"
                    if hasattr(self.app_window, 'current_display_mode'):
                        self.app_window.current_display_mode = "shaded_class"

                    # STEP 3: Reset ALL classes hidden, apply preset visibility
                    classes = preset.get("classes", {})

                    if hasattr(self.app_window, 'class_palette'):
                        for code in self.app_window.class_palette:
                            self.app_window.class_palette[code]["show"] = False

                    visible_set = set()
                    if classes:
                        for code, info in classes.items():
                            code_int = int(code)
                            if code_int in self.app_window.class_palette:
                                is_visible = info.get("show", False)
                                self.app_window.class_palette[code_int]["show"] = \
                                    is_visible
                                if is_visible:
                                    visible_set.add(code_int)
                    else:
                        if hasattr(self.app_window, 'class_palette'):
                            for code in self.app_window.class_palette:
                                self.app_window.class_palette[code]["show"] = True
                                visible_set.add(int(code))

                    visible_count = len(visible_set)
                    print(f"   ✅ Visible classes: {sorted(visible_set)}")

                    self.app_window._shading_visibility_override = visible_set

                    # STEP 4: Sync to display_mode_dialog
                    if hasattr(self.app_window, 'display_mode_dialog') and \
                            self.app_window.display_mode_dialog is not None:
                        dlg = self.app_window.display_mode_dialog

                        # Always switch dialog to Main View before syncing —
                        # mode changes belong to Main View only.
                        if getattr(dlg, 'current_slot', 0) != 0:
                            dlg.slot_box.blockSignals(True)
                            dlg.slot_box.setCurrentIndex(0)
                            dlg.slot_box.blockSignals(False)
                            dlg.on_slot_changed(0)

                        if not hasattr(dlg, 'view_palettes'):
                            dlg.view_palettes = {}
                        if 0 not in dlg.view_palettes:
                            dlg.view_palettes[0] = {}

                        for code, entry in self.app_window.class_palette.items():
                            dlg.view_palettes[0][int(code)] = {
                                "show":   entry.get("show", False),
                                "description": entry.get("description", ""),
                                "lvl": entry.get("lvl", ""),
                                "color":  entry.get("color", (128, 128, 128)),
                                "weight": entry.get("weight", 1.0),
                            }

                        if hasattr(dlg, 'table') and dlg.table is not None:
                            for row in range(dlg.table.rowCount()):
                                try:
                                    code_item = dlg.table.item(row, 1)
                                    if not code_item:
                                        continue
                                    code = int(code_item.text())
                                    chk = dlg.table.cellWidget(row, 0)
                                    if chk:
                                        chk.blockSignals(True)
                                        chk.setChecked(code in visible_set)
                                        chk.blockSignals(False)
                                except Exception:
                                    continue

                        if not hasattr(dlg, 'slot_shows'):
                            dlg.slot_shows = {}
                        if 0 not in dlg.slot_shows:
                            dlg.slot_shows[0] = {}

                        for code in self.app_window.class_palette:
                            dlg.slot_shows[0][int(code)] = (int(code) in visible_set)

                        if hasattr(dlg, 'color_mode') and dlg.color_mode.currentIndex() != 1:
                            dlg.color_mode.blockSignals(True)
                            dlg.color_mode.setCurrentIndex(1)
                            dlg.color_mode.blockSignals(False)

                    # STEP 5: Store shading params
                    azimuth = preset.get("azimuth", 45.0)
                    angle   = preset.get("angle",   45.0)
                    ambient = preset.get("ambient",  0.2)

                    self.app_window.last_shade_azimuth = azimuth
                    self.app_window.shading_sharpness_angle = angle
                    self.app_window.shade_ambient      = ambient
                    self.app_window.shading_quality   = quality_mode

                    try:
                        dlg = getattr(self.app_window, "display_mode_dialog", None)
                        if dlg is not None:
                            dlg._shading_quality_value = quality_mode
                            combo = getattr(dlg, "shading_quality", None)
                            if combo is not None:
                                quality_index = combo.findData(quality_mode)
                                if quality_index >= 0:
                                    combo.blockSignals(True)
                                    combo.setCurrentIndex(quality_index)
                                    combo.blockSignals(False)
                    except Exception as _quality_ui_err:
                        print(f"   ⚠️ Could not sync shading speed selector: {_quality_ui_err}")

                    try:
                        panel = getattr(self.app_window, 'shading_panel', None)
                        if panel is not None and hasattr(panel, 'refresh_from_app'):
                            panel.refresh_from_app()
                    except Exception as _sp_err:
                        print(f"   ⚠️ Could not refresh shading panel: {_sp_err}")

                    # STEP 6: Apply shading
                    from gui.shading_display import update_shaded_class
                    single_class_max_edge = None
                    try:
                        panel = getattr(self.app_window, "shading_panel", None)
                        if panel is not None and hasattr(panel, "max_edge") and panel.max_edge is not None:
                            single_class_max_edge = panel.max_edge.value()
                    except Exception:
                        single_class_max_edge = None
                    # Do not discard valid shaded geometry on every shortcut
                    # press. Classification commits update the live mesh
                    # incrementally; forcing Delaunay for all 13M points here
                    # caused the 10–15 second refresh seen in the audit log.
                    # Quality is part of shading_display's cache key. Let it
                    # restore/build the requested Fast/Normal/Slow geometry
                    # instead of discarding a valid quality-specific cache.
                    _shading_force_rebuild = previous_display_mode != "shaded_class"
                    update_shaded_class(
                        self.app_window,
                        azimuth=azimuth,
                        angle=angle,
                        ambient=ambient,
                        force_rebuild=_shading_force_rebuild,
                        single_class_max_edge=single_class_max_edge
                    )

                    # Ensure final Shading result does not show point/class actor on top.
                    self._enforce_shading_only_visibility(render=True)

                    _QTimer.singleShot(
                        200,
                        lambda: setattr(
                            self.app_window,
                            '_shading_visibility_override',
                            None
                        )
                    )

                    print(f"   ✅ Shading done: az={azimuth}° angle={angle}° "
                        f"| {visible_count} classes | Speed={shading_quality_label(quality_mode)}")
                    print(f"{'='*60}\n")

                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] 🌗 ShadingMode: {azimuth}°/{angle}°, "
                            f"{visible_count} classes visible",
                            2500
                        )

                except Exception as e:
                    try:
                        self.app_window._shading_visibility_override = None
                    except Exception:
                        pass
                    print(f"⚠️ Failed to apply ShadingMode preset: {e}")
                    import traceback
                    traceback.print_exc()

                finally:
                    _QTimer.singleShot(
                        ShortcutExecutionGuard.COOLDOWN_SHADING,
                        self._shortcut_guard.force_unlock
                    )

                return True

            # ====================================================================
            # DrawSettings shortcut handler
            # ====================================================================
            if tool == "DrawSettings":
                preset = shortcut.get("preset")
                print(f"✅ SHORTCUT MATCH: {combo} → DrawSettings")

                if not preset:
                    print("⚠️ DrawSettings shortcut has no preset")
                    return True

                try:
                    print(f"\n{'='*60}")
                    print(f"🎨 APPLYING DRAW SETTINGS PRESET FROM SHORTCUT")
                    print(f"{'='*60}")

                    tools = preset.get("tools", {})

                    if hasattr(self.app_window, 'digitizer') and \
                            hasattr(self.app_window.digitizer, 'draw_tool_styles'):
                        for tool_key, style in tools.items():
                            self.app_window.digitizer.draw_tool_styles[tool_key] = \
                                dict(style)
                        print(f"   ✅ Applied {len(tools)} tool styles to digitizer")
                    else:
                        print("   ⚠️ Digitizer not available")

                    from gui.draw_settings_dialog import save_draw_settings
                    save_draw_settings(tools)
                    print(f"   ✅ Saved to QSettings")

                    active_tool = preset.get("active_tool", "smartline")
                    if hasattr(self.app_window, 'digitizer') and \
                            self.app_window.digitizer:
                        digi = self.app_window.digitizer
                        if hasattr(digi, 'enable'):
                            digi.enable(True)
                        elif hasattr(digi, 'enabled'):
                            digi.enabled = True

                        if hasattr(digi, 'set_tool'):
                            digi.set_tool(active_tool)
                            print(f"   ✅ Activated digitizer: {active_tool}")
                        elif hasattr(digi, 'active_tool'):
                            digi.active_tool = active_tool
                            print(f"   ✅ Set active_tool: {active_tool}")
                    else:
                        print("   ⚠️ Digitizer not found")

                    draw_settings_dialog = self._get_live_qt_attr("draw_settings_dialog")
                    if draw_settings_dialog is not None and draw_settings_dialog.isVisible():
                        try:
                            draw_settings_dialog._load_styles()
                            print(f"   ✅ Refreshed Draw Settings dialog")
                        except Exception:
                            pass

                    if hasattr(self.app_window, 'statusBar'):
                        self.app_window.statusBar().showMessage(
                            f"[{key_label}] 🎨 DrawSettings: {active_tool} activated "
                            f"({len(tools)} tools updated)",
                            2500
                        )

                    print(f"   ✅ DrawSettings complete")
                    print(f"{'='*60}\n")

                except Exception as e:
                    print(f"⚠️ Failed to apply DrawSettings preset: {e}")
                    import traceback
                    traceback.print_exc()
                finally:
                    _QTimer.singleShot(
                        ShortcutExecutionGuard.COOLDOWN_DRAW,
                        self._shortcut_guard.force_unlock
                    )

                return True
            # ====================================================================
            # SyncViews shortcut handler
            # ====================================================================
            if tool == "SyncViews":
                preset = shortcut.get("preset")
                print(f"✅ SHORTCUT MATCH: {combo} → SyncViews")
                try:
                    execute_tool(self.app_window, tool, preset=preset, key_label=key_label)
                except Exception as e:
                    print(f"⚠️ SyncViews execute_tool failed: {e}")
                    import traceback
                    traceback.print_exc()
                return True
            # ====================================================================
            # No shortcut match — pass through
            # ====================================================================
            if not shortcut:
                return False

            if tool in ("ShadingMode", "DisplayMode", "DrawSettings", "Surface"):
                print(f"⚠️ {tool} reached fallback — already handled inline")
                return True

            # ====================================================================
            # Classification tools
            # ====================================================================
            from_cls = shortcut.get("from")
            to_cls   = shortcut.get("to")
            preset   = shortcut.get("preset")

            print(f"✅ SHORTCUT MATCH: {combo} → {tool}, "
                f"from={from_cls}, to={to_cls}")
            try:
                execute_tool(
                    self.app_window,
                    tool,
                    from_cls,
                    to_cls,
                    preset=preset,
                    key_label=key_label,
                )
            except Exception as e:
                print(f"⚠️ execute_tool failed for {tool}: {e}")
                import traceback
                traceback.print_exc()
            return True

        return False
             
    def _refresh_cut_section(self, view_idx):
        """
        ✅ Refresh Cut Section (View 5) with current classifications
        """
        import numpy as np
        import pyvista as pv
        
        print(f"\n{'='*60}")
        print(f"🔪 REFRESHING CUT SECTION")
        print(f"{'='*60}")
        
        try:
            vtk_widget = self.app_window.cut_vtk
            
            # Get cut section data
            cut_points = getattr(self.app_window, 'cut_section_points', None)
            cut_mask = getattr(self.app_window, 'cut_section_mask', None)
            
            if cut_points is None or cut_mask is None or len(cut_points) == 0:
                print("   ⚠️ No cut section data")
                return
            
            # Save camera
            try:
                cam_pos = vtk_widget.camera_position
            except:
                cam_pos = None
            
            # Clear existing actors
            actors_to_remove = [name for name in list(vtk_widget.actors.keys()) 
                              if name.startswith('class_')]
            for name in actors_to_remove:
                vtk_widget.remove_actor(name, render=False)
            
            print(f"   🗑️ Cleared {len(actors_to_remove)} existing actors")
            
            # Get View 5 palette
            view_palette = self.app_window.view_palettes.get(5, {})
            visible = [c for c, info in view_palette.items() if info.get("show", False)]
            
            print(f"   👁️ Visible classes in Cut Section: {visible}")
            
            # Get current classifications
            current_classes = self.app_window.data["classification"]
            cut_cls = current_classes[cut_mask]
            
            # Rebuild with current data
            for cls_val in visible:
                cls_mask = (cut_cls == cls_val)
                cls_pts = cut_points[cls_mask]
                
                if len(cls_pts) == 0:
                    continue
                
                entry = view_palette.get(int(cls_val), {})
                color = entry.get("color", (128, 128, 128))
                point_size = 5.0
                
                cls_colors = np.array([color] * len(cls_pts), dtype=np.uint8)
                cls_cloud = pv.PolyData(cls_pts)
                cls_cloud["RGB"] = cls_colors
                
                vtk_widget.add_points(
                    cls_cloud,
                    scalars="RGB",
                    rgb=True,
                    point_size=point_size,
                    render_points_as_spheres=True,
                    reset_camera=False,
                    name=f"class_{cls_val}",
                    render=False
                )
                
                print(f"      ✅ Added class {cls_val}: {len(cls_pts)} points")
            
            # Restore camera
            if cam_pos:
                try:
                    vtk_widget.camera_position = cam_pos
                except:
                    pass
            
            vtk_widget.render()
            print(f"   ✅ Cut Section refreshed with {len(visible)} visible classes")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"⚠️ Cut section refresh failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")
 

    def _handle_context_fit_view(self, key_label: str = "Shift+F"):
        """
        ✅ Context-aware fit view - fits the ACTIVE window

        Priority:
        0. Cross-section view (if focused)
        1. If SNT is loaded and no cross-section is focused, fit MAIN view
        2. Main view (default)
        3. Cut section (future support)
        """
    
        # Get the currently focused widget
        from PySide6.QtWidgets import QApplication
        focused = QApplication.focusWidget()
    
        print(f"   🔍 Focused widget: {type(focused).__name__}")

        # ====================================================================
        # PRIORITY 0: Check if a cross-section view is focused
        # ====================================================================
        if hasattr(self.app_window, 'section_vtks') and self.app_window.section_vtks:
            for view_idx, vtk_widget in self.app_window.section_vtks.items():
                try:
                    # Check if THIS cross-section view has focus
                    if (focused == vtk_widget.interactor or
                        vtk_widget.interactor.isAncestorOf(focused)):
                    
                        print(f"   🎯 Fitting Cross-Section View {view_idx + 1}")
                        self._fit_cross_section_view(view_idx, vtk_widget)
                    
                        if hasattr(self.app_window, 'statusBar'):
                            self.app_window.statusBar().showMessage(
                                f"[{key_label}] 🧲 Cross-Section {view_idx + 1} fitted & locked",
                                1500
                            )
                        return
                except Exception as e:
                    print(f"   ⚠️ Error checking view {view_idx}: {e}")

        has_snt_loaded = bool(
            getattr(self.app_window, "snt_actors", None) or
            getattr(self.app_window, "snt_attachments", None)
        )
        if has_snt_loaded:
            print("   🗂️ SNT loaded — no cross-section focus, fitting MAIN view")
            self._fit_main_view_with_2d_lock()
            if hasattr(self.app_window, 'statusBar'):
                self.app_window.statusBar().showMessage(
                    f"[{key_label}] 🧲 Main View fitted (SNT workflow)",
                    1500
                )
            return
    
        # ====================================================================
        # PRIORITY 2: Main view (default) with 2D lock
        # ====================================================================
        print(f"   🎯 Fitting Main View with 2D lock")
        self._fit_main_view_with_2d_lock()
    
        if hasattr(self.app_window, 'statusBar'):
            self.app_window.statusBar().showMessage(
                f"[{key_label}] 🧲 Main View fitted & locked",
                1500
            )

    def _clear_main_camera_lock_observer(self, camera=None):
        """Remove only the main-view camera observer installed by this filter."""
        tracked_camera = getattr(self.app_window, "_main_camera_lock_camera", None)
        tracked_id = getattr(self.app_window, "_main_camera_lock_observer_id", None)
        target_camera = camera if camera is not None else tracked_camera
        if target_camera is not None and tracked_id is not None:
            try:
                target_camera.RemoveObserver(tracked_id)
            except Exception:
                pass
        self.app_window._main_camera_lock_camera = None
        self.app_window._main_camera_lock_observer_id = None

    def _install_main_camera_lock_observer(self, camera, callback):
        """Install a single owned main-view camera observer."""
        self._clear_main_camera_lock_observer(camera)
        obs_id = camera.AddObserver('ModifiedEvent', callback)
        self.app_window._main_camera_lock_camera = camera
        self.app_window._main_camera_lock_observer_id = obs_id
        return obs_id

    def _clear_cross_view_camera_lock_observer(self, view_idx):
        """Remove only the cross-section observer owned by this filter for one view."""
        state_map = getattr(self.app_window, "_cross_section_2d_mode", None)
        if not isinstance(state_map, dict):
            return
        view_state = state_map.get(view_idx, {})
        camera = view_state.get("camera")
        obs_id = view_state.get("camera_lock_observer_id")
        if camera is not None and obs_id is not None:
            try:
                camera.RemoveObserver(obs_id)
            except Exception:
                pass
            
            
    def _fit_cross_section_view(self, view_idx, vtk_widget):
        """
        ✅ Restore cross-section view to ORIGINAL STATE (like "Side" button)
        ✅ Reset camera to original coordinates & 2D projection
        ✅ COMPLETELY DISABLE 3D rotation - LOCKED in 2D (SAFE MODE)
        ✅ PRESERVE classification tools - they stay active after Shift+F
        ✅ DEBOUNCED - Prevents crashes from multiple rapid presses

        Args:
            view_idx: View index (0, 1, 2, 3...)
            vtk_widget: The VTK widget to restore
        """
        import numpy as np
        import time

        # ====================================================================
        # DEBOUNCE CHECK - Prevent multiple rapid calls
        # ====================================================================
        if not hasattr(self, '_fit_cross_section_last_call'):
            self._fit_cross_section_last_call = {}
        
        current_time = time.time()
        last_call_time = self._fit_cross_section_last_call.get(view_idx, 0)
        
        # Ignore calls within 500ms of previous call
        if current_time - last_call_time < 0.5:
            print(f"   ⏭️ Ignoring rapid Shift+F press (debounce)")
            return
        
        # Update last call time
        self._fit_cross_section_last_call[view_idx] = current_time
        
        # ====================================================================
        # EXECUTION LOCK - Prevent concurrent execution
        # ====================================================================
        if not hasattr(self, '_fit_cross_section_executing'):
            self._fit_cross_section_executing = {}
        
        if self._fit_cross_section_executing.get(view_idx, False):
            print(f"   ⏭️ Already executing for view {view_idx}, ignoring...")
            return
        
        # Set execution flag
        self._fit_cross_section_executing[view_idx] = True

        print(f"\n{'='*60}")
        print(f"🔄 RESET CROSS-SECTION VIEW {view_idx + 1} TO ORIGINAL STATE")
        print(f"{'='*60}")

        try:
            # Get section data
            core_points = getattr(self.app_window, f'section_{view_idx}_core_points', None)
            core_mask   = getattr(self.app_window, f'section_{view_idx}_core_mask', None)
            core_indices = getattr(self.app_window, f'section_{view_idx}_core_indices', None)  # ✅ needed for visibility filter
        
            if core_points is None or len(core_points) == 0:
                print(f"   ⚠️ No data in this view")
                self._fit_cross_section_executing[view_idx] = False
                return

            # ====================================================================
            # Get ORIGINAL section plane info
            # ====================================================================
            section_axis = getattr(self.app_window, f'section_{view_idx}_axis', 'X')
            section_position = getattr(self.app_window, f'section_{view_idx}_position', 0.0)
        
            print(f"   📋 Original plane: {section_axis}-axis at {section_position}")

            # ====================================================================
            # Calculate bounds from ORIGINAL section data
            # ====================================================================
            xmin, xmax = core_points[:, 0].min(), core_points[:, 0].max()
            ymin, ymax = core_points[:, 1].min(), core_points[:, 1].max()
            zmin, zmax = core_points[:, 2].min(), core_points[:, 2].max()

            center_x = (xmin + xmax) / 2.0
            center_y = (ymin + ymax) / 2.0
            center_z = (zmin + zmax) / 2.0

            width = (xmax - xmin) * 1.1
            height = (ymax - ymin) * 1.1
            depth = (zmax - zmin) * 1.1

            print(f"   📏 Section bounds:")
            print(f"      X: {xmin:.2f} → {xmax:.2f}")
            print(f"      Y: {ymin:.2f} → {ymax:.2f}")
            print(f"      Z: {zmin:.2f} → {zmax:.2f}")

            # ====================================================================
            # SAVE CURRENT INTERACTOR STYLE (classification tool) BEFORE changes
            # ====================================================================
            interactor = vtk_widget.interactor
            saved_interactor_style = None
            has_classification_tool = False
            active_tool_name = None
            
            if interactor is not None:
                current_style = interactor.GetInteractorStyle()
                if current_style is not None:
                    # Save the current style
                    saved_interactor_style = current_style
                    style_class_name = current_style.GetClassName()
                    
                    # Check if it's a classification tool
                    if 'PointPicker' in style_class_name or 'AreaSelector' in style_class_name:
                        has_classification_tool = True
                        active_tool_name = style_class_name
                        print(f"   💾 SAVED classification tool: {style_class_name}")

            # ====================================================================
            # Setup camera for ORIGINAL view (like the "Side" button)
            # ====================================================================
            renderer = vtk_widget.renderer
            camera = renderer.GetActiveCamera()

            # Enforce orthographic (2D) projection
            camera.ParallelProjectionOn()
            print(f"   🔒 Orthographic projection ENABLED")

            ## ====================================================================
            # ✅ CHECK: If already in 2D locked mode, just re-fit WITHOUT changing orientation
            # ====================================================================
            if (hasattr(self.app_window, '_cross_section_2d_mode') and 
                view_idx in self.app_window._cross_section_2d_mode and
                self.app_window._cross_section_2d_mode[view_idx].get('is_2d_locked', False)):
                
                print(f"   ℹ️ Already in 2D mode - re-fitting using section_controller exact fit")
                
                # ✅ FIX: Use the SAME fit method as initial draw (aspect-ratio correct)
                try:
                    sc = getattr(self.app_window, 'section_controller', None)
                    if sc is not None and hasattr(sc, '_fit_camera_to_section_points'):
                        # ✅ Try visibility filter first (same as initial draw)
                        pts_to_fit = core_points  # safe default
                        if core_indices is not None and hasattr(sc, '_filter_points_by_visibility'):
                            try:
                                filtered = sc._filter_points_by_visibility(
                                    core_points, core_indices, view_idx
                                )
                                if filtered is not None and len(filtered) > 0:
                                    pts_to_fit = filtered
                            except Exception as _fe:
                                print(f"   ⚠️ Visibility filter failed, using all points: {_fe}")
                        sc._fit_camera_to_section_points(vtk_widget, pts_to_fit)
                        print(f"   ✅ Re-fitted using section_controller._fit_camera_to_section_points ({len(pts_to_fit)} pts)")
                    else:
                        # Fallback: simple scale (old behaviour)
                        camera = renderer.GetActiveCamera()
                        if section_axis == 'X':
                            scale = max(height, depth) / 2.0
                        elif section_axis == 'Y':
                            scale = max(width, depth) / 2.0
                        elif section_axis == 'Z':
                            scale = max(width, height) / 2.0
                        else:
                            scale = max(width, height, depth) / 2.0
                        camera.SetParallelScale(scale)
                        renderer.ResetCameraClippingRange()
                        vtk_widget.render()
                except Exception as _refit_e:
                    print(f"   ⚠️ Re-fit failed: {_refit_e}")
                    import traceback
                    traceback.print_exc()
                
                self._fit_cross_section_executing[view_idx] = False
                return  # ✅ EXIT - orientation already correct

            # ====================================================================
            # Setup camera for ORIGINAL view (only runs on FIRST Shift+F)
            # ====================================================================
            # ✅ FIX: Delegate to the SAME fit method used during initial draw.
            # This respects cross_view_mode ('side'/'front'), uses proper aspect-ratio
            # correction and 5% padding — exactly matching what the user saw on draw.
            try:
                sc = getattr(self.app_window, 'section_controller', None)
                if sc is not None and hasattr(sc, '_fit_camera_to_section_points'):
                    # ✅ Try visibility filter first (same as initial draw)
                    pts_to_fit = core_points  # safe default
                    if core_indices is not None and hasattr(sc, '_filter_points_by_visibility'):
                        try:
                            filtered = sc._filter_points_by_visibility(
                                core_points, core_indices, view_idx
                            )
                            if filtered is not None and len(filtered) > 0:
                                pts_to_fit = filtered
                        except Exception as _fe:
                            print(f"   ⚠️ Visibility filter failed, using all points: {_fe}")
                    sc._fit_camera_to_section_points(vtk_widget, pts_to_fit)
                    # Re-read camera state after the delegate fit so locked_params captures it
                    camera = renderer.GetActiveCamera()
                    print(f"   📐 Fit via section_controller._fit_camera_to_section_points ({len(pts_to_fit)} pts)")
                else:
                    # Fallback: old axis-based logic
                    if section_axis == 'X':
                        max_dim = max(height, depth)
                        camera.SetPosition(center_x, center_y - max_dim * 2, center_z)
                        camera.SetFocalPoint(center_x, center_y, center_z)
                        camera.SetViewUp(0, 0, 1)
                        camera.SetParallelScale(max(height, depth) / 2.0)
                        print(f"   📐 Fallback: X-axis slice view (Y-Z plane)")
                    elif section_axis == 'Y':
                        max_dim = max(width, depth)
                        camera.SetPosition(center_x + max_dim * 2, center_y, center_z)
                        camera.SetFocalPoint(center_x, center_y, center_z)
                        camera.SetViewUp(0, 0, 1)
                        camera.SetParallelScale(max(width, depth) / 2.0)
                        print(f"   📐 Fallback: Y-axis slice view (X-Z plane)")
                    elif section_axis == 'Z':
                        max_dim = max(width, height)
                        camera.SetPosition(center_x, center_y, center_z + max_dim * 2)
                        camera.SetFocalPoint(center_x, center_y, center_z)
                        camera.SetViewUp(0, 1, 0)
                        camera.SetParallelScale(max(width, height) / 2.0)
                        print(f"   📐 Fallback: Z-axis slice view (X-Y plane)")
            except Exception as _init_fit_e:
                print(f"   ⚠️ Initial fit failed: {_init_fit_e}")
                import traceback
                traceback.print_exc()

            # Store LOCKED camera parameters
            locked_params = {
                'position': camera.GetPosition(),
                'focal_point': camera.GetFocalPoint(),
                'view_up': camera.GetViewUp(),
                'parallel_scale': camera.GetParallelScale(),
                'view_angle': camera.GetViewAngle()
            }

            # Update clipping range
            renderer.ResetCameraClippingRange()
            # ====================================================================
            # ✅ SAFE 2D LOCK - DISABLE ROTATION WITHOUT CRASHING
            # ====================================================================
            if interactor is not None:
                try:
                    # Remove only our previously-installed camera lock observer.
                    self._clear_cross_view_camera_lock_observer(view_idx)
                    
                    # ================================================================
                    # ✅ CRITICAL FIX: Re-entry guard and throttling
                    # ================================================================
                    # Create closure variables
                    _enforcing = [False]
                    _last_enforce = [0.0]
                    
                    def enforce_camera_lock(obj, event):
                        """SAFELY lock camera - prevent rotation without crashing"""
                        
                        # ✅ RE-ENTRY GUARD - Prevent recursive calls
                        if _enforcing[0]:
                            return
                        
                        # ✅ THROTTLE - Max 30fps to prevent event cascade
                        import time
                        current_time = time.time()
                        if current_time - _last_enforce[0] < 0.033:  # 30fps
                            return
                        _last_enforce[0] = current_time
                        
                        _enforcing[0] = True
                        try:
                            cam = renderer.GetActiveCamera()
                            
                            # Force parallel projection
                            cam.ParallelProjectionOn()
                            
                            # Get current values
                            current_pos = np.array(cam.GetPosition())
                            current_focal = np.array(cam.GetFocalPoint())
                            current_up = np.array(cam.GetViewUp())
                            
                            locked_pos = np.array(locked_params['position'])
                            locked_focal = np.array(locked_params['focal_point'])
                            locked_up = np.array(locked_params['view_up'])
                            
                            # Calculate view directions
                            current_dir = current_focal - current_pos
                            locked_dir = locked_focal - locked_pos
                            
                            # Normalize
                            current_dir_norm = current_dir / (np.linalg.norm(current_dir) + 1e-10)
                            locked_dir_norm = locked_dir / (np.linalg.norm(locked_dir) + 1e-10)
                            
                            # Check if direction has changed (rotation)
                            direction_dot = np.dot(current_dir_norm, locked_dir_norm)
                            
                            # Check if view up has changed
                            up_dot = np.dot(current_up, locked_up)
                            
                            # If ANY rotation detected, FORCE restore
                            if direction_dot < 0.9999 or up_dot < 0.9999:
                                # Block rotation by restoring RELATIVE geometry
                                # Keep the OFFSET between focal and position (preserves zoom)
                                # But enforce locked direction
                                
                                current_distance = np.linalg.norm(current_dir)
                                
                                # Calculate how much the focal point has moved (pan delta)
                                focal_delta = current_focal - locked_focal
                                
                                # Apply same delta to position (keep them moving together)
                                new_position = locked_pos + focal_delta - (locked_dir_norm * current_distance) + (locked_dir_norm * np.linalg.norm(locked_dir))
                                new_focal = locked_focal + focal_delta
                                
                                cam.SetPosition(*new_position)
                                cam.SetFocalPoint(*new_focal)
                                cam.SetViewUp(*locked_up)
                                renderer.ResetCameraClippingRange()
                                # ✅ NO RENDER - Let normal render cycle handle it
                                # vtk_widget.render()  # ❌ This causes cascade!
                        
                        finally:
                            _enforcing[0] = False
                    
                    # ✅ Use ONLY ONE observer to minimize event firing
                    camera_lock_observer_id = camera.AddObserver('ModifiedEvent', enforce_camera_lock)
                    
                    print(f"   🔒 SAFE camera lock installed")
                    print(f"   ✓ Single observer with re-entry guard")
                    print(f"   ✓ Throttled to 30fps")
                    
                    # ================================================================
                    # RESTORE SAVED INTERACTOR STYLE (classification tool)
                    # ================================================================
                    # ✅ FORCE 2D-only interactor style (no rotation allowed)
                    # ✅ Use Image style which allows BOTH horizontal AND vertical panning
                    if hasattr(self.app_window, '_apply_plain_section_2d_style'):
                        self.app_window._apply_plain_section_2d_style(interactor, vtk_widget)
                    else:
                        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                        style_2d = vtkInteractorStyleImage()
                        try:
                            style_2d.SetInteractionModeToImage2D()
                        except Exception:
                            pass
                        interactor.SetInteractorStyle(style_2d)

                    if has_classification_tool:
                        print(f"   ⚠️ Classification tool was: {active_tool_name} (replaced with 2D pan/zoom)")
                    
                    # Store state
                    if not hasattr(self.app_window, '_cross_section_2d_mode'):
                        self.app_window._cross_section_2d_mode = {}
                    
                    self.app_window._cross_section_2d_mode[view_idx] = {
                        'axis': section_axis,
                        'position': section_position,
                        'locked_params': locked_params,
                        'is_2d_locked': True,
                        'renderer': renderer,
                        'camera': camera,
                        'camera_lock_observer_id': camera_lock_observer_id,
                        'has_classification_tool': has_classification_tool,
                        'saved_interactor_style': saved_interactor_style,
                        'classification_tool_name': active_tool_name
                    }
                
                    print(f"   🔒 2D MODE SAFELY LOCKED")
                    print(f"   ✓ Rotation: DISABLED")
                    print(f"   ✓ Pan/Zoom: ENABLED with locked orientation")
                    if has_classification_tool:
                        print(f"   ✓ Classification tool: STILL ACTIVE")
                
                except Exception as e:
                    print(f"   ⚠️ Could not set 2D lock: {e}")
                    import traceback
                    traceback.print_exc()

            # Final render
            vtk_widget.render()

            print(f"   ✅ View reset to original state ({section_axis}-axis)")
            print(f"   🔒 3D rotation DISABLED (SAFE MODE)")
            print(f"{'='*60}\n")

        except Exception as e:
            print(f"⚠️ Reset cross-section view failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")
        
        finally:
            # ✅ ALWAYS clear execution flag
            self._fit_cross_section_executing[view_idx] = False    
        
    def _fit_main_view_with_2d_lock(self):
        """
        ✅ Fit main view and lock to 2D mode
        ✅ IDEMPOTENT - Safe to press Shift+F multiple times without 3D flash
        ✅ Uses app_window.fit_view() which includes Point Cloud + DXF + SNT bounds
        """
        import numpy as np

        already_locked = getattr(self.app_window, '_main_view_2d_locked', False)

        print(f"\n{'='*60}")
        print(f"🧲 FITTING MAIN VIEW (with SNT/DXF support)")
        print(f"   Already 2D locked: {already_locked}")
        print(f"{'='*60}")

        vtk_widget = self.app_window.vtk_widget
        renderer = vtk_widget.renderer
        camera = renderer.GetActiveCamera()
        interactor = vtk_widget.interactor

        # ====================================================================
        # STEP 1: ALWAYS remove old camera observers FIRST
        #         Prevents old closures from fighting new camera state
        # ====================================================================
        # try:
        #     camera.RemoveObservers('ModifiedEvent')
        #     renderer.RemoveObservers('StartEvent')
        #     renderer.RemoveObservers('EndEvent')
        #     renderer.RemoveObservers('ModifiedEvent')
        # except Exception:
        #     pass
        # print(f"   🧹 Cleared old camera observers")

        measurement_active = (
            hasattr(self.app_window, 'measurement_tool') and
            getattr(self.app_window.measurement_tool, 'active', False)
        )

        if measurement_active:
            # ✅ Measurement active — fit view then re-capture lock params and reinstall observer
            # ⚠️ Must NOT early-return before observer reinstall — old stale observer fights the new camera state
            print(f"   🔒 Measurement active — fitting then refreshing camera lock")

            # Remove only our previously-installed main-view camera lock observer.
            self._clear_main_camera_lock_observer(camera)
            print(f"   🧹 Cleared owned camera lock observer")

            # Save measurement interactor style so fit_view() can't clobber it
            saved_meas_style = None
            try:
                saved_meas_style = interactor.GetInteractorStyle()
            except Exception:
                pass

            # Run fit_view
            if hasattr(self.app_window, 'fit_view'):
                print(f"   📐 Calling fit_view (Point Cloud + DXF + SNT)...")
                self.app_window.fit_view()
                print(f"   ✅ fit_view complete")

            # Restore measurement interactor style
            if saved_meas_style is not None:
                try:
                    interactor.SetInteractorStyle(saved_meas_style)
                    print(f"   ✅ Measurement interactor style restored")
                except Exception as e:
                    print(f"   ⚠️ Could not restore interactor: {e}")

            # Re-capture camera state AFTER fit (new lock target)
            camera.ParallelProjectionOn()
            locked_params = {
                'position': camera.GetPosition(),
                'focal_point': camera.GetFocalPoint(),
                'view_up': camera.GetViewUp(),
                'parallel_scale': camera.GetParallelScale(),
                'view_angle': camera.GetViewAngle()
            }
            self.app_window._main_view_locked_params = locked_params
            print(f"   📸 Captured new lock target after fit")

            # Reinstall fresh camera lock observer with new params
            _enforcing_m = [False]
            _last_enforce_m = [0.0]

            def enforce_camera_lock_measurement(obj, event):
                if _enforcing_m[0]:
                    return
                import time
                current_time = time.time()
                if current_time - _last_enforce_m[0] < 0.033:
                    return
                _last_enforce_m[0] = current_time
                _enforcing_m[0] = True
                try:
                    cam = renderer.GetActiveCamera()
                    cam.ParallelProjectionOn()
                    current_pos = np.array(cam.GetPosition())
                    current_focal = np.array(cam.GetFocalPoint())
                    current_up = np.array(cam.GetViewUp())
                    locked_pos = np.array(locked_params['position'])
                    locked_focal = np.array(locked_params['focal_point'])
                    locked_up = np.array(locked_params['view_up'])
                    current_dir = current_focal - current_pos
                    locked_dir = locked_focal - locked_pos
                    current_dir_norm = current_dir / (np.linalg.norm(current_dir) + 1e-10)
                    locked_dir_norm = locked_dir / (np.linalg.norm(locked_dir) + 1e-10)
                    direction_dot = np.dot(current_dir_norm, locked_dir_norm)
                    up_dot = np.dot(current_up, locked_up)
                    if direction_dot < 0.9999 or up_dot < 0.9999:
                        current_distance = np.linalg.norm(current_dir)
                        new_position = current_focal - (locked_dir_norm * current_distance)
                        cam.SetPosition(*new_position)
                        cam.SetViewUp(*locked_up)
                finally:
                    _enforcing_m[0] = False

            self._install_main_camera_lock_observer(camera, enforce_camera_lock_measurement)
            print(f"   🔒 Fresh camera lock observer installed (measurement)")

            self.app_window._main_view_2d_locked = True
            renderer.ResetCameraClippingRange()
            vtk_widget.render()
            print(f"   🔒 Main view 2D lock REFRESHED")
            print(f"   ✓ Rotation: DISABLED")
            print(f"   ✓ Pan/Zoom: ENABLED with locked orientation")
            print(f"{'='*60}\n")
            return  # ✅ Early exit — interactor style already restored above
        self._clear_main_camera_lock_observer(camera)
        print(f"   🧹 Cleared owned camera lock observer")
        # ====================================================================
        # STEP 2: Set top view ONLY on FIRST call
        #         Skip if already locked → prevents interactor style reset → no 3D flash
        # ====================================================================
        if not already_locked:
            print(f"   🔄 Forcing TOP view (first lock)...")
            from gui.views import set_view
            self.app_window._preserve_view = False
            set_view(self.app_window, "top")
            print(f"   ✅ Switched to TOP view")
        else:
            # Already in 2D mode — just ensure parallel projection stays on
            camera.ParallelProjectionOn()
            print(f"   ℹ️ Already 2D locked — re-fitting bounds only (no view reset)")

        # ====================================================================
        # STEP 3: Fit view (Point Cloud + DXF + SNT)
        #         Camera observers are REMOVED so fit_view won't be fought
        # ====================================================================
        if hasattr(self.app_window, 'fit_view'):
            print(f"   📐 Calling fit_view (Point Cloud + DXF + SNT)...")
            self.app_window.fit_view()
            print(f"   ✅ fit_view complete")
        else:
            print(f"   ⚠️ fit_view not found, using fallback bounds...")
            self._fit_main_view_fallback()

        # ====================================================================
        # STEP 4: Capture camera state AFTER fit (this becomes the new lock target)
        # ====================================================================
        locked_params = {
            'position': camera.GetPosition(),
            'focal_point': camera.GetFocalPoint(),
            'view_up': camera.GetViewUp(),
            'parallel_scale': camera.GetParallelScale(),
            'view_angle': camera.GetViewAngle()
        }
        self.app_window._main_view_locked_params = locked_params
        print(f"   📸 Captured new lock target")

        # ====================================================================
        # STEP 5: Save interactor style ONLY on first call
        # ====================================================================
        saved_interactor_style = None
        has_classification_tool = False
        active_tool_name = None

        if not already_locked and interactor is not None:
            current_style = interactor.GetInteractorStyle()
            if current_style is not None:
                saved_interactor_style = current_style
                style_class_name = current_style.GetClassName()
                if 'PointPicker' in style_class_name or 'AreaSelector' in style_class_name:
                    has_classification_tool = True
                    active_tool_name = style_class_name
                    print(f"   💾 SAVED classification tool: {style_class_name}")

        # ====================================================================
        # STEP 6: Install FRESH camera lock observer (with new locked_params)
        # ====================================================================
        _enforcing = [False]
        _last_enforce = [0.0]

        def enforce_camera_lock_main(obj, event):
            """SAFELY lock main view camera — prevent rotation without crashing"""
            if _enforcing[0]:
                return

            import time
            current_time = time.time()
            if current_time - _last_enforce[0] < 0.033:  # 30fps throttle
                return
            _last_enforce[0] = current_time

            _enforcing[0] = True
            try:
                cam = renderer.GetActiveCamera()
                cam.ParallelProjectionOn()

                current_pos = np.array(cam.GetPosition())
                current_focal = np.array(cam.GetFocalPoint())
                current_up = np.array(cam.GetViewUp())

                locked_pos = np.array(locked_params['position'])
                locked_focal = np.array(locked_params['focal_point'])
                locked_up = np.array(locked_params['view_up'])

                current_dir = current_focal - current_pos
                locked_dir = locked_focal - locked_pos

                current_dir_norm = current_dir / (np.linalg.norm(current_dir) + 1e-10)
                locked_dir_norm = locked_dir / (np.linalg.norm(locked_dir) + 1e-10)

                direction_dot = np.dot(current_dir_norm, locked_dir_norm)
                up_dot = np.dot(current_up, locked_up)

                if direction_dot < 0.9999 or up_dot < 0.9999:
                    current_distance = np.linalg.norm(current_dir)
                    new_position = current_focal - (locked_dir_norm * current_distance)
                    cam.SetPosition(*new_position)
                    cam.SetViewUp(*locked_up)
            finally:
                _enforcing[0] = False

        self._install_main_camera_lock_observer(camera, enforce_camera_lock_main)
        print(f"   🔒 Fresh camera lock observer installed")

        # ====================================================================
        # STEP 7: Disable digitize manager picker (only on first call)
        # ====================================================================
        if not already_locked:
            if hasattr(self.app_window, 'digitize_manager'):
                try:
                    if not hasattr(self.app_window, '_digitize_picker_was_enabled'):
                        self.app_window._digitize_picker_was_enabled = True
                    if hasattr(self.app_window.digitize_manager, 'picker_enabled'):
                        self.app_window._digitize_picker_was_enabled = \
                            self.app_window.digitize_manager.picker_enabled
                        self.app_window.digitize_manager.picker_enabled = False
                    if hasattr(self.app_window.digitize_manager, 'disconnect_picker'):
                        self.app_window.digitize_manager.disconnect_picker()
                    print(f"   🔇 DigitizeManager picker DISABLED")
                except Exception as e:
                    print(f"   ⚠️ Could not disable digitize picker: {e}")

        # ====================================================================
        # STEP 8: Set interactor style (only on first call)
        #         On repeat calls the style is ALREADY correct — don't touch it
        # ====================================================================
        if not already_locked:
            if has_classification_tool and saved_interactor_style is not None:
                interactor.SetInteractorStyle(saved_interactor_style)
                print(f"   ✅ Classification tool PRESERVED: {active_tool_name}")
            else:
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                style_2d = vtkInteractorStyleImage()
                style_2d.SetInteractionModeToImageSlicing()
                interactor.SetInteractorStyle(style_2d)
                print(f"   🔒 2D pan/zoom enabled, rotation locked")

        # ====================================================================
        # STEP 9: Mark as locked
        # ====================================================================
        self.app_window._main_view_2d_locked = True

        # Final render
        renderer.ResetCameraClippingRange()
        vtk_widget.render()

        print(f"   🔒 Main view 2D lock {'REFRESHED' if already_locked else 'ENABLED'}")
        print(f"   ✓ Rotation: DISABLED")
        print(f"   ✓ Pan/Zoom: ENABLED with locked orientation")
        print(f"{'='*60}\n")

    def _fit_main_view_fallback(self):
        """
        Fallback bounds calculation when fit_view() is not available.
        Handles point cloud + DWG bounds.
        """
        import numpy as np

        def _get_point_cloud_bounds():
            pts = getattr(self.app_window, 'points', None)
            if pts is not None and len(pts) > 0:
                return [pts[:,0].min(), pts[:,0].max(),
                        pts[:,1].min(), pts[:,1].max(),
                        pts[:,2].min(), pts[:,2].max()]
            data = getattr(self.app_window, 'data', None)
            if data is not None:
                xyz = data.get('xyz') if hasattr(data, 'get') else getattr(data, 'xyz', None)
                if xyz is not None and len(xyz) > 0:
                    arr = np.asarray(xyz)
                    return [float(arr[:,0].min()), float(arr[:,0].max()),
                            float(arr[:,1].min()), float(arr[:,1].max()),
                            float(arr[:,2].min()), float(arr[:,2].max())]
            return None

        def _get_dwg_bounds():
            try:
                dwg_actors = getattr(self.app_window, 'dwg_actors', [])
                if not dwg_actors:
                    return None
                all_actors = [a for entry in dwg_actors for a in entry.get('actors', [])]
                if not all_actors:
                    return None
                xmin = ymin = zmin = 1e18
                xmax = ymax = zmax = -1e18
                for actor in all_actors:
                    if not actor.GetVisibility():
                        continue
                    b = actor.GetBounds()
                    if b[0] > b[1]:
                        continue
                    xmin = min(xmin, b[0]); xmax = max(xmax, b[1])
                    ymin = min(ymin, b[2]); ymax = max(ymax, b[3])
                    zmin = min(zmin, b[4]); zmax = max(zmax, b[5])
                if xmin > xmax:
                    return None
                return [xmin, xmax, ymin, ymax, zmin, zmax]
            except Exception:
                return None

        renderer = self.app_window.vtk_widget.renderer
        pc_bounds = _get_point_cloud_bounds()
        dwg_bounds = _get_dwg_bounds()

        if pc_bounds is not None:
            renderer.ResetCamera(pc_bounds)
            print(f"   ✅ Fitted to point cloud bounds")
        elif dwg_bounds is not None:
            renderer.ResetCamera(dwg_bounds)
            print(f"   ✅ Fitted to DWG bounds")
        elif hasattr(self.app_window, 'view_ribbon') and self.app_window.view_ribbon:
            self.app_window.view_ribbon._fit_view()
            print(f"   ✅ Fitted via view_ribbon")
        else:
            print(f"   ⚠️ No data to fit")
        
    def _unlock_cross_section_view(self, view_idx, vtk_widget):
        """Unlock a SINGLE cross-section view"""
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
        
        try:
            renderer = vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            
            # Remove only our owned camera lock observer for this cross view.
            self._clear_cross_view_camera_lock_observer(view_idx)
            
            # Restore 3D trackball camera
            vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
            camera.ParallelProjectionOff()
            vtk_widget.render()
            
            # Clear state
            if hasattr(self.app_window, '_cross_section_2d_mode'):
                if view_idx in self.app_window._cross_section_2d_mode:
                    del self.app_window._cross_section_2d_mode[view_idx]
            
            print(f"   ✅ Cross-section view {view_idx + 1} unlocked")
        except Exception as e:
            print(f"   ⚠️ Error unlocking view {view_idx}: {e}")

    def _unlock_main_view(self):
        """Unlock ONLY the main view"""
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
        
        try:
            vtk_widget = self.app_window.vtk_widget
            renderer = vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            
            # Remove only our owned main-view camera lock observer.
            self._clear_main_camera_lock_observer(camera)
            
            # Restore 3D trackball camera
            vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
            camera.ParallelProjectionOff()
            vtk_widget.render()
            
            self.app_window._main_view_2d_locked = False
            
            # Re-enable digitize manager picker
            if hasattr(self.app_window, 'digitize_manager'):
                try:
                    if hasattr(self.app_window, '_digitize_picker_was_enabled'):
                        if self.app_window._digitize_picker_was_enabled:
                            if hasattr(self.app_window.digitize_manager, 'picker_enabled'):
                                self.app_window.digitize_manager.picker_enabled = True
                            
                            if hasattr(self.app_window.digitize_manager, 'connect_picker'):
                                self.app_window.digitize_manager.connect_picker()
                            
                            print(f"   🔊 DigitizeManager picker RE-ENABLED")
                except Exception as e:
                    print(f"   ⚠️ Could not re-enable digitize picker: {e}")
            
            print(f"   ✅ Main view unlocked")
        except Exception as e:
            print(f"   ⚠️ Error unlocking main view: {e}")
