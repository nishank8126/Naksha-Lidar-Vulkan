import importlib as _importlib
import sys as _sys
import os as _os
import copy as _copy

from PySide6.QtGui import (
    QColor, QFont, QIcon, QAction, QActionGroup, QPainter, QPainterPath, QPen
)
from PySide6.QtCore import (
    Qt, Signal, QSettings, QMutex, QMutexLocker, QEvent, QRectF
)
import os
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QHeaderView, QCheckBox, QColorDialog, QLabel, QLineEdit,
    QFileDialog, QComboBox, QWidget, QFrame, QAbstractItemView, QToolButton, QMenu,
    QMessageBox
)
from gui.class_display import update_class_mode
from gui.theme_manager import get_dialog_stylesheet
from gui.popup_guard import InputPopupMixin

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


def _get_live_class_picker(app):
    picker = getattr(app, "class_picker", None)
    if picker is None:
        return None
    try:
        if not _qt_object_is_valid(picker):
            app.class_picker = None
            return None
        picker.objectName()
        return picker
    except (RuntimeError, ReferenceError, AttributeError):
        app.class_picker = None
        return None
    except Exception:
        app.class_picker = None
        return None

def _register_uam_aliases(module):
    """Keep legacy and package import paths pointing at the same module."""
    _sys.modules.setdefault('gui.unified_actor_manager', module)
    _sys.modules.setdefault('unified_actor_manager', module)
    return module
 
def _resolve_uam():
    for _module_name in ('gui.unified_actor_manager', 'unified_actor_manager'):
        try:
            return _register_uam_aliases(
                _importlib.import_module(_module_name)
            )
        except ModuleNotFoundError:
            pass
    _this_dir = _os.path.dirname(_os.path.abspath(__file__))
    _candidates = [
        _this_dir,
        _os.path.dirname(_this_dir),
        _os.path.join(_this_dir, '..', 'core'),
        _os.path.join(_this_dir, 'core'),
    ]
    for _path in _candidates:
        _path = _os.path.normpath(_path)
        _target = _os.path.join(_path, 'unified_actor_manager.py')
        if _os.path.exists(_target):
            if _path not in _sys.path:
                _sys.path.insert(0, _path)
            return _register_uam_aliases(
                _importlib.import_module('unified_actor_manager')
            )
    raise ModuleNotFoundError(
        "unified_actor_manager.py not found. "
        "Expected gui/unified_actor_manager.py or a legacy top-level copy."
    )
 
_uam = _resolve_uam()

# --- Helper Aliases at the top of display_mode.py ---

def _uam_sync(app, slot_idx, palette=None, border=None, render=True):
    """General sync for any slot."""
    return _uam.sync_palette_to_gpu(app, slot_idx, palette, border, render)

def _uam_refresh_section(app, view_idx, palette=None, border=0.0, mode="class"):
    """Used for Cross-Sections (Slots 1-4)."""
    return _uam.refresh_section_after_weight_change(app, view_idx, palette, border, mode)

def _uam_fast_refresh(app, palette=None, border=0.0):
    """Used for Main View (Slot 0)."""
    # ðŸš€ This must call sync_palette_to_gpu to trigger the new weight logic
    return _uam.sync_palette_to_gpu(app, 0, palette, border, True)

def _uam_connect(app):
    """Wires the palette_changed signal to the GPU sync function."""
    return _uam.connect_palette_signal(app)

def clone_palette(palette):
    """Return an independent deep copy of a palette dict.

    Every slot that *owns* a palette must hold its own object so that
    in-place mutations (``palette[code]['show'] = False``) never leak
    into another slot or into ``app.class_palette``.
    """
    if palette is None:
        return {}
    return _copy.deepcopy(palette)


def validate_palette_isolation(view_palettes, cut_section_palette=None,
                               context=""):
    """Detect shared object-identity between palette slots at runtime.

    Call after any operation that creates or re-assigns palette slots.
    Prints a ``[PALETTE-LEAK]`` warning if two slots share the same
    inner dict object â€” which would cause silent cross-slot mutations.
    """
    seen = {}
    for slot, palette in view_palettes.items():
        pid = id(palette)
        if pid in seen:
            print(
                f"[PALETTE-LEAK] context={context} "
                f"slots {seen[pid]} and {slot} share palette object id={pid}"
            )
        seen[pid] = slot

    if cut_section_palette is not None:
        cut_id = id(cut_section_palette)
        for slot, palette in view_palettes.items():
            if id(palette) == cut_id:
                print(
                    f"[PALETTE-LEAK] context={context} "
                    f"cut_section_palette shares object with slot {slot}"
                )


def sync_palette_to_gpu_safe(app, slot, palette, reason=""):
    """Wrapper around ``_uam_sync`` with slot/palette assertions and logging."""
    assert slot in range(6), f"Invalid palette slot: {slot}"
    assert palette is not None, f"Missing palette for slot {slot}"

    visible = [cls for cls, cfg in palette.items() if cfg.get("show", True)]
    hidden  = [cls for cls, cfg in palette.items() if not cfg.get("show", True)]

    print(f"[PALETTE-SYNC] reason={reason} slot={slot} "
          f"visible={len(visible)} hidden={len(hidden)}")

    return _uam_sync(app, slot, palette)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# restore_display_settings_for_file
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def restore_display_settings_for_file(app, filepath, refresh=True):
    """
    Restore display settings for a file.

    Precedence (most specific first):
      1) file-specific settings (for this loaded file)
      2) global fallback settings
    """
    try:
        from PySide6.QtCore import QSettings
        import os

        file_key = os.path.abspath(filepath)
        settings = QSettings("NakshaAI", "LidarApp")

        print("=" * 60)
        print(f"ðŸ”„ RESTORING DISPLAY SETTINGS FOR {os.path.basename(filepath)}")
        print("=" * 60)

        dialog = None
        if hasattr(app, 'display_mode_dialog') and app.display_mode_dialog:
            dialog = app.display_mode_dialog
        elif hasattr(app, 'display_dialog') and app.display_dialog:
            dialog = app.display_dialog

        def _to_bool(value, default=True):
            """Robust bool parser for QSettings payloads (bool/int/str)."""
            if isinstance(value, bool):
                return value
            if value is None:
                return default
            if isinstance(value, (int, float)):
                return value != 0
            if isinstance(value, str):
                token = value.strip().lower()
                if token in {"1", "true", "yes", "on", "checked"}:
                    return True
                if token in {"0", "false", "no", "off", "unchecked", ""}:
                    return False
            return bool(value)

        def _parse_palette_blob(raw_blob):
            if not raw_blob or not isinstance(raw_blob, dict):
                return None
            parsed = {}
            for view_idx_key, palette_dict in raw_blob.items():
                try:
                    view_idx = int(view_idx_key)
                except Exception:
                    continue
                if not isinstance(palette_dict, dict):
                    continue
                parsed[view_idx] = {}
                for code_key, info in palette_dict.items():
                    try:
                        code = int(code_key)
                    except Exception:
                        continue
                    info = info if isinstance(info, dict) else {}
                    lvl_text = str(info.get('lvl', '') or '').strip()
                    if not lvl_text:
                        desc_text = str(info.get('description', '') or '').strip()
                        if desc_text and desc_text != str(code):
                            lvl_text = desc_text
                    parsed[view_idx][code] = {
                        'show':        _to_bool(info.get('show', True), True),
                        'description': info.get('description', ''),
                        'lvl':         lvl_text,
                        'color':       tuple(info.get('color', (128, 128, 128))),
                        'weight':      float(info.get('weight', 1.0))
                    }
            return parsed or None

        def _parse_show_blob(raw_blob):
            if not raw_blob or not isinstance(raw_blob, dict):
                return None
            parsed = {}
            for slot_idx_key, show_dict in raw_blob.items():
                try:
                    slot_idx = int(slot_idx_key)
                except Exception:
                    continue
                if not isinstance(show_dict, dict):
                    continue
                parsed[slot_idx] = {}
                for code_key, checked in show_dict.items():
                    try:
                        code = int(code_key)
                    except Exception:
                        continue
                    parsed[slot_idx][code] = _to_bool(checked, True)
            return parsed or None

        # Mid-session guard: once the user has an active palette in this
        # session, do not fall back to GLOBAL QSettings keys. Those globals
        # are written by save_global_settings on every Apply and persist
        # across sessions, so they leak a prior PTC (e.g. Building.ptc) into
        # the current PowerLine.ptc session whenever a new LAS tile is loaded.
        # File-specific keys (file_palettes/, file_ptc/, ...) are still
        # honoured â€” those are deterministic per-LAS.
        session_active = (
            dialog is not None
            and bool(getattr(dialog, 'current_ptc_path', None))
            and bool(getattr(app, 'class_palette', None))
            and bool(getattr(app, 'view_palettes', None))
        )
        if session_active:
            print(f"ðŸ›¡ï¸ Mid-session restore â€” GLOBAL fallback disabled "
                  f"(active PTC: {os.path.basename(dialog.current_ptc_path)})")

        temp_palettes = None
        temp_shows = None
        global_weight_source = None
        global_weight_source_name = None

        # Weights are intentionally global across files.
        # Prefer live runtime weights (latest in-session state), then
        # QSettings global palette as fallback.
        has_active_loaded_file = bool(getattr(app, 'loaded_file', None))
        if (
            has_active_loaded_file
            and
            hasattr(app, 'view_palettes')
            and isinstance(app.view_palettes, dict)
            and bool(app.view_palettes)
        ):
            global_weight_source = app.view_palettes
            global_weight_source_name = "RUNTIME"
        else:
            _global_palette_blob = settings.value("global_view_palettes")
            _global_palettes = _parse_palette_blob(_global_palette_blob)
            if _global_palettes:
                global_weight_source = _global_palettes
                global_weight_source_name = "GLOBAL"

        # Prefer per-file palette/visibility first, then global fallback.
        # Grid-label loads can force session restore to avoid stale per-file
        # visibility snapshots overriding the user's current preset.
        palette_source = "GLOBAL"
        shows_source = "GLOBAL"
        prefer_session_restore = bool(getattr(app, "_prefer_session_display_restore", False))
        if (
            prefer_session_restore
            and dialog is not None
            and isinstance(getattr(dialog, "view_palettes", None), dict)
            and bool(getattr(dialog, "view_palettes", None))
        ):
            temp_palettes = {
                int(_slot): clone_palette(_palette)
                for _slot, _palette in dialog.view_palettes.items()
                if isinstance(_palette, dict)
            } or None
            temp_shows = _parse_show_blob(getattr(dialog, "slot_shows", None))
            palette_source = "SESSION"
            shows_source = "SESSION" if temp_shows else "NONE"
            print("🧷 Session restore preference active - using live dialog palettes")
        else:
            if not session_active:
                temp_palettes = _parse_palette_blob(settings.value("global_view_palettes"))
                temp_shows = _parse_show_blob(settings.value("global_slot_shows"))

        if temp_palettes:
            print(f"ðŸ’¾ Loaded {len(temp_palettes)} {palette_source} view palettes")
            if global_weight_source:
                overrides = 0
                for _slot_idx, _slot_palette in temp_palettes.items():
                    if not isinstance(_slot_palette, dict):
                        continue
                    _weight_slot = global_weight_source.get(_slot_idx)
                    if not isinstance(_weight_slot, dict):
                        _weight_slot = global_weight_source.get(0, {})
                    if not isinstance(_weight_slot, dict):
                        continue
                    for _code, _info in _slot_palette.items():
                        _src = _weight_slot.get(_code)
                        if not isinstance(_src, dict):
                            continue
                        _info["weight"] = float(_src.get("weight", _info.get("weight", 1.0)))
                        overrides += 1
                print(
                    f"ðŸŒ Applied {global_weight_source_name} global weights "
                    f"to {overrides} restored class entries"
                )
        if temp_shows:
            print(f"ðŸ’¾ Loaded {len(temp_shows)} {shows_source} checkbox-state slots")
            if temp_palettes:
                merged = 0
                for _slot_idx, _show_map in temp_shows.items():
                    _slot_palette = temp_palettes.get(_slot_idx)
                    if not isinstance(_slot_palette, dict):
                        continue
                    for _code, _is_visible in _show_map.items():
                        if _code in _slot_palette and isinstance(_slot_palette[_code], dict):
                            _slot_palette[_code]["show"] = _to_bool(_is_visible, True)
                            merged += 1
                print(f"🔁 Applied checkbox visibility to {merged} palette entries")

        # PTC is GLOBAL state — never restored from file or global QSettings
        # here. PTC changes only when the user manually loads a .ptc file from
        # the Display Mode dialog. saved_ptc is set to None so the RUNTIME-CHECK
        # log at the end of this function still has a valid reference.
        saved_ptc = None
        ptc_source = "NONE"
        # app.class_palette, app.view_palettes, and dialog.view_palettes are
        # GLOBAL PTC state — not overwritten from file/session blobs here.
        #
        # Only checkbox visibility (slot_shows / show flags) is restored
        # because which classes are shown can legitimately differ per file.
        if temp_shows:
            if dialog:
                dialog.slot_shows = temp_shows
                if hasattr(dialog, '_load_slot_checkboxes'):
                    try:
                        dialog._load_slot_checkboxes(dialog.current_slot)
                        print(f"✅ Applied {shows_source} checkbox states to UI")
                    except Exception as e:
                        print(f"⚠️ Checkbox UI update failed: {e}")
            else:
                app._pending_checkbox_states = temp_shows
                app.pending_checkbox_states = temp_shows
                print(f"💤 Stored {shows_source} checkbox states for later")

        mode_source = "DISABLED"
        color_mode_source = "DISABLED"
        print("ðŸš« Display mode restore skipped for file load")
        print("ðŸš« Color mode restore skipped for file load")

        saved_borders = settings.value("global_view_borders")
        if saved_borders and isinstance(saved_borders, dict):
            parsed_borders = {int(k): v for k, v in saved_borders.items()}
            if 0 in parsed_borders:
                app.point_border_percent = float(parsed_borders[0])
                is_class_mode = (app.display_mode == "class")
                app._main_view_borders_active = (app.point_border_percent > 0) and is_class_mode

            # â”€â”€ FIX 2: always write app.view_borders for ALL slots â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # The old code wrote app.view_borders only in the `else` branch (no dialog).
            # Because a dialog is always present at load time, app.view_borders was
            # never updated, so every new cross-section built after a file-open
            # received border=0.0% instead of the saved value.
            if not hasattr(app, 'view_borders') or app.view_borders is None:
                app.view_borders = {}
            app.view_borders.update(parsed_borders)
            # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

            if dialog:
                dialog.view_borders = parsed_borders
                if hasattr(dialog, 'load_view_border'):
                    try:
                        dialog.load_view_border(dialog.current_slot)
                    except Exception:
                        pass
            else:
                app.pending_border_values = parsed_borders
            # (no else needed â€” app.view_borders is already updated above)

            print(f"âœ… Restored GLOBAL border values (Main View: {app.point_border_percent}%)")

        print("=" * 60)
        print(f"âœ… DISPLAY SETTINGS APPLIED TO {os.path.basename(filepath)}")
        print("=" * 60 + "\n")

        print("\n" + "=" * 60)
        print("ðŸ”„ SYNCING WEIGHTS TO app.class_palette...")
        print("=" * 60)

        if hasattr(app, 'view_palettes') and 0 in app.view_palettes:
            main_palette = app.view_palettes[0]
            app.class_palette = {}
            for code, info in main_palette.items():
                app.class_palette[code] = {
                    'show':        info.get('show', True),
                    'description': info.get('description', ''),
                    'lvl':         str(info.get('lvl', '')),
                    'color':       tuple(info.get('color', (128, 128, 128))),
                    'weight':      float(info.get('weight', 1.0))
                }
                print(f"      Class {code}: weight={info.get('weight', 1.0):.1f}x")
            print(f"   âœ… Synced {len(app.class_palette)} classes from view_palettes[0]")
        else:
            print("   âš ï¸ No view_palettes[0] available - skipping weight sync")

        if not refresh:
            print('   Classification refresh deferred to the load pipeline')
        elif hasattr(app, 'data') and app.data is not None:
            print(f"\n   ðŸ”„ FORCING CLASSIFICATION REFRESH with restored weights...")
            try:
                from gui.class_display import update_class_mode
                update_class_mode(app, force_refresh=True)
                print("   âœ… Classification refresh complete - weights are now visible!")
            except Exception as e:
                print(f"   âš ï¸ Classification refresh failed: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("   â„¹ï¸ No data loaded yet - weights will apply when classification starts")
        print("=" * 60 + "\n")

        if hasattr(app, 'statusBar'):
            app.statusBar().showMessage(
                f"âœ¨ Display settings applied & refreshed", 3000
            )

        try:
            ptc_name = os.path.basename(saved_ptc) if (saved_ptc and os.path.exists(saved_ptc)) else "None"
            slot0_count = len(app.view_palettes.get(0, {})) if hasattr(app, 'view_palettes') and isinstance(app.view_palettes, dict) else 0
            print(
                f"[RUNTIME-CHECK] restore file={os.path.basename(filepath)} "
                f"palette_src={palette_source} shows_src={shows_source} "
                f"weight_src={global_weight_source_name if global_weight_source_name else 'NONE'} "
                f"ptc_src={ptc_source} ptc={ptc_name} "
                f"mode_src={mode_source if 'mode_source' in locals() else 'NA'} "
                f"color_src={color_mode_source if 'color_mode_source' in locals() else 'NA'} "
                f"slot0_classes={slot0_count}"
            )
        except Exception:
            pass

        if dialog and hasattr(dialog, 'sync_with_app_state'):
            try:
                dialog.sync_with_app_state()
            except Exception as e:
                print(f"⚠️ Dialog sync failed during restore: {e}")

        return True

    except Exception as e:
        print(f"âŒ Failed to restore display settings: {e}")
        import traceback
        traceback.print_exc()
        return False


def restore_global_display_settings(app):
    """Restore global display settings that apply to ANY file."""
    try:
        from PySide6.QtCore import QSettings
        settings = QSettings("NakshaAI", "LidarApp")

        print("=" * 60)
        print(f"ðŸŒ RESTORING GLOBAL DISPLAY SETTINGS")
        print("=" * 60)

        restored_anything = False

        dialog = None
        if hasattr(app, 'display_mode_dialog') and app.display_mode_dialog:
            dialog = app.display_mode_dialog
        elif hasattr(app, 'display_dialog') and app.display_dialog:
            dialog = app.display_dialog

        if not dialog:
            print("âš ï¸ No display dialog found - will restore when dialog opens")
            return False

        saved_palettes = settings.value("global_view_palettes")
        if saved_palettes and isinstance(saved_palettes, dict):
            if not hasattr(app, 'view_palettes'):
                app.view_palettes = {}
            for view_idx_str, palette_dict in saved_palettes.items():
                view_idx = int(view_idx_str)
                app.view_palettes[view_idx] = {}
                for code_str, info in palette_dict.items():
                    code = int(code_str)
                    lvl_text = str(info.get('lvl', '') or '').strip()
                    if not lvl_text:
                        desc_text = str(info.get('description', '') or '').strip()
                        if desc_text and desc_text != str(code):
                            lvl_text = desc_text
                    app.view_palettes[view_idx][code] = {
                        'show':        info.get('show', True),
                        'description': info.get('description', ''),
                        'lvl':         lvl_text,
                        'color':       tuple(info.get('color', (128, 128, 128))),
                        'weight':      info.get('weight', 1.0)
                    }
            if hasattr(dialog, 'view_palettes'):
                dialog.view_palettes = {
                    int(vi): clone_palette(pal)
                    for vi, pal in app.view_palettes.items()
                }
            if 0 in app.view_palettes:
                app.class_palette = clone_palette(app.view_palettes[0])
            print(f"âœ… Restored {len(saved_palettes)} GLOBAL view palettes")
            restored_anything = True

        saved_shows = settings.value("global_slot_shows")
        if saved_shows and isinstance(saved_shows, dict):
            if hasattr(dialog, 'slot_shows'):
                dialog.slot_shows = {}
                for slot_idx_str, show_dict in saved_shows.items():
                    slot_idx = int(slot_idx_str)
                    dialog.slot_shows[slot_idx] = {
                        int(code): checked
                        for code, checked in show_dict.items()
                    }
                print(f"âœ… Restored GLOBAL checkbox states for {len(saved_shows)} views")
                restored_anything = True

        saved_mode = settings.value("global_display_mode")
        if saved_mode:
            app.display_mode = saved_mode
            print(f"âœ… Restored GLOBAL display mode: {saved_mode}")
            restored_anything = True

        saved_color_mode = settings.value("global_color_mode")
        if saved_color_mode is not None:
            try:
                idx_to_restore = int(saved_color_mode)
                if idx_to_restore < dialog.color_mode.count():
                    dialog.color_mode.setCurrentIndex(idx_to_restore)
                print(f"âœ… Restored GLOBAL color mode: {saved_color_mode}")
                restored_anything = True
            except Exception:
                pass

        saved_borders = settings.value("global_view_borders")
        if saved_borders and isinstance(saved_borders, dict):
            dialog.view_borders = {int(k): v for k, v in saved_borders.items()}
            print(f"âœ… Restored GLOBAL border values: {dialog.view_borders}")
            restored_anything = True

        saved_structured_border = settings.value("global_structured_border")
        saved_logic_mode = settings.value("global_border_logic_mode")

        if saved_logic_mode is not None:
            try:
                dialog._pending_border_logic_mode = int(saved_logic_mode)
            except (ValueError, TypeError):
                dialog._pending_border_logic_mode = 0
            print(f"âœ… Stashed border logic mode for deferred apply: {dialog._pending_border_logic_mode}")
            restored_anything = True
        elif saved_structured_border is not None:
            is_structured = str(saved_structured_border).lower() == 'true'
            dialog._pending_border_logic_mode = 1 if is_structured else 0
            print(f"âœ… Stashed structured border mode for deferred apply: {is_structured}")
            restored_anything = True

        if restored_anything:
            if hasattr(dialog, '_load_slot_checkboxes'):
                dialog._load_slot_checkboxes(dialog.current_slot)
            if hasattr(dialog, 'load_view_border'):
                dialog.load_view_border(dialog.current_slot)
            # Do NOT call on_apply here â€” restoring settings only populates
            # the dialog UI. GPU push happens only when user clicks Apply.
            print("=" * 60)
            print("âœ… GLOBAL DISPLAY SETTINGS RESTORED (click Apply to apply to view)")
            print("=" * 60 + "\n")
            if hasattr(app, 'statusBar'):
                app.statusBar().showMessage(f"âœ¨ Display settings restored â€” click Apply", 3000)

        return restored_anything

    except Exception as e:
        print(f"âŒ Failed to restore global display settings: {e}")
        import traceback
        traceback.print_exc()
        return False


def apply_global_settings_on_dialog_open(dialog, app):
    """Apply pending global settings when Display Mode dialog is first opened."""
    if not hasattr(app, 'pending_global_restore') or not app.pending_global_restore:
        return

    print("\n" + "=" * 60)
    print("ðŸ”„ APPLYING PENDING GLOBAL SETTINGS TO NEW DIALOG")
    print("=" * 60)

    try:
        restore_global_display_settings(app)
        app.pending_global_restore = False
        print("âœ… Pending global settings applied")
        print("=" * 60 + "\n")
    except Exception as e:
        print(f"âš ï¸ Failed to apply pending settings: {e}")
        import traceback
        traceback.print_exc()

class _CloseLabel(QWidget):
    """Tiny × widget drawn via QPainter — immune to any global theme stylesheet."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(18, 18)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Close")
        self._hovered = False

    def enterEvent(self, event):
        self._hovered = True
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QPen, QColor
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        color = QColor("#ff4444") if self._hovered else QColor("#ffffff")
        pen = QPen(color, 2.0, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        m = 5
        w, h = self.width(), self.height()
        p.drawLine(m, m, w - m, h - m)
        p.drawLine(w - m, m, m, h - m)
        p.end()


class _WeightBulkPopup(QDialog):
    """Floating mini-window for applying a weight to all listed classes at once."""

    def __init__(self, parent_dialog):
        super().__init__(parent_dialog,
                         Qt.Tool | Qt.FramelessWindowHint)
        self.setObjectName("weightBulkPopup")
        self._dialog = parent_dialog
        self._anchor_global = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        frame = QFrame(self)
        frame.setObjectName("weightBulkFrame")
        frame.setFrameShape(QFrame.StyledPanel)
        frame.setStyleSheet(
            "#weightBulkFrame {"
            "  background-color: #111111;"
            "  border: 1px solid #555555;"
            "  border-radius: 4px;"
            "}"
        )
        outer.addWidget(frame)

        frame_vbox = QVBoxLayout(frame)
        frame_vbox.setContentsMargins(0, 0, 0, 0)
        frame_vbox.setSpacing(0)

        # ── Thin grey title bar with × on the right ──────────────────────
        title_bar = QFrame(frame)
        title_bar.setObjectName("weightBulkTitleBar")
        title_bar.setFixedHeight(18)
        title_bar.setStyleSheet(
            "#weightBulkTitleBar {"
            "  background-color: #3a3a3a;"
            "  border: none;"
            "  border-bottom: 1px solid #555555;"
            "  border-top-left-radius: 3px;"
            "  border-top-right-radius: 3px;"
            "}"
        )
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(0, 0, 2, 0)
        title_layout.setSpacing(0)
        title_layout.addStretch()

        close_lbl = _CloseLabel(title_bar)
        close_lbl.clicked.connect(self.hide)
        title_layout.addWidget(close_lbl)
        frame_vbox.addWidget(title_bar)

        # ── Content row ───────────────────────────────────────────────────
        content_row = QHBoxLayout()
        content_row.setContentsMargins(10, 7, 10, 7)
        content_row.setSpacing(6)

        label = QLabel("Apply weights to all the listed classes")
        label.setStyleSheet("color: #cccccc; font-size: 11px; background: transparent;")

        self._spin = QLineEdit("1.0")
        self._spin.setObjectName("weightBulkInput")
        self._spin.setFixedSize(54, 24)
        self._spin.setAlignment(Qt.AlignCenter)
        self._spin.setStyleSheet(
            "QLineEdit {"
            "  font-size: 11px;"
            "  padding: 0px 3px;"
            "  background: #1e1e1e;"
            "  color: #e0e0e0;"
            "  border: 1px solid #555;"
            "  border-radius: 3px;"
            "}"
        )
        self._spin.setToolTip("Decimal weight value (0.1 – 12.0)")
        self._spin.mouseDoubleClickEvent = lambda e: (
            self._spin.selectAll(),
            super(QLineEdit, self._spin).mouseDoubleClickEvent(e)
        )

        apply_btn = QPushButton("Apply")
        apply_btn.setObjectName("weightBulkApplyBtn")
        apply_btn.setFixedHeight(24)
        apply_btn.setMinimumWidth(60)
        apply_btn.setFocusPolicy(Qt.NoFocus)
        apply_btn.setStyleSheet(
            "QPushButton {"
            "  font-size: 11px;"
            "  padding: 0px 12px;"
            "  background: #3c3f41;"
            "  color: #e0e0e0;"
            "  border: 1px solid #666;"
            "  border-radius: 3px;"
            "}"
            "QPushButton:hover   {"
            "  background: #1a3a1a;"
            "  color: #39ff14;"
            "  border: 1px solid #39ff14;"
            "}"
            "QPushButton:pressed { background: #0d1f0d; color: #39ff14; }"
        )
        apply_btn.clicked.connect(self._apply)
        self._spin.returnPressed.connect(self._apply)

        content_row.addWidget(label)
        content_row.addWidget(self._spin)
        content_row.addWidget(apply_btn)
        frame_vbox.addLayout(content_row)

        self.adjustSize()
        self.setFixedSize(self.sizeHint())

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show_at(self, global_pos):
        self._anchor_global = global_pos
        self.move(global_pos)
        self.show()
        self.raise_()
        self._spin.selectAll()
        self._spin.setFocus()

    def reposition(self, delta):
        if self.isVisible() and self._anchor_global is not None:
            self._anchor_global = self._anchor_global + delta
            self.move(self._anchor_global)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _apply(self):
        try:
            value = float(self._spin.text().strip())
        except ValueError:
            self._spin.setText("1.0")
            self._spin.selectAll()
            return

        value = max(0.1, min(value, 12.0))
        self._spin.setText(f"{value:.2f}")

        # Only update the Weight column cells in the table.
        # The user must click Apply in the Display Mode dialog to push changes live.
        table = self._dialog.table
        for row in range(table.rowCount()):
            item = table.item(row, 6)
            if item is not None:
                item.setText(f"{value:.2f}")

        self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

class DisplayModeDialog(QDialog):
    applied         = Signal(dict)
    view_switched   = Signal(int)
    classes_loaded  = Signal()
    palette_changed = Signal(int)   # emits slot_idx for GPU uniform sync
    border_changed  = Signal(int, float, int)  # (slot_idx, border_percent, logic_mode) — one-way to shortcut manager

    def _get_app_window(self):
        """
        Return the owning AppWindow reference.

        Kept as a helper because this dialog is intentionally parentless at
        the Qt level (to get native taskbar/minimize behavior on Windows),
        while still needing access to the main app object.
        """
        return getattr(self, "_app_window", None)

    def _show_front_message(
        self,
        icon,
        title,
        text,
        buttons=QMessageBox.Ok,
        default_button=QMessageBox.Ok,
    ):
        """
        Show a popup above the always-on-top Display Mode window.
        """
        restore_enabled = True
        restore_topmost = False

        try:
            restore_enabled = bool(self.isEnabled())
            restore_topmost = bool(self.windowFlags() & Qt.WindowStaysOnTopHint)
            self.setEnabled(False)
            self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            self.show()
            self.lower()
        except Exception:
            restore_enabled = True
            restore_topmost = False

        msg_box = QMessageBox(None)
        try:
            msg_box.setIcon(icon)
            msg_box.setWindowTitle(title)
            msg_box.setText(text)
            msg_box.setStandardButtons(buttons)

            if default_button is not None:
                try:
                    msg_box.setDefaultButton(default_button)
                except Exception:
                    pass

            msg_box.setWindowModality(Qt.ApplicationModal)
            msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)
            msg_box.show()
            msg_box.raise_()
            msg_box.activateWindow()
            return msg_box.exec()
        finally:
            try:
                self.setEnabled(restore_enabled)
                self.setWindowFlag(Qt.WindowStaysOnTopHint, restore_topmost)
                self.show()
                self.raise_()
                self.activateWindow()
            except Exception:
                pass

    def _display_ptc_name(self) -> str:
        path = getattr(self, "current_ptc_path", None)
        return os.path.basename(path) if path else "No PTC loaded"

    def _update_window_title(self) -> None:
        ptc_name = self._display_ptc_name()
        if ptc_name == "No PTC loaded":
            self.setWindowTitle("Display Mode")
        else:
            self.setWindowTitle(f"Display Mode - {ptc_name}")

    def _refresh_ptc_label(self) -> None:
        return

    def __init__(self, parent):
        self._app_window = parent
        super().__init__(None)
        self.setProperty("themeStyledDialog", True)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setWindowTitle("Display Mode")
        try:
            logo_path = os.path.join(os.path.dirname(__file__), "icons", "logo.png")
            if os.path.exists(logo_path):
                icon = QIcon(logo_path)
                from PySide6.QtCore import QSize
                for size in [16, 20, 24, 32, 48]:
                    icon.addFile(logo_path, QSize(size, size))
                self.setWindowIcon(icon)
        except Exception:
            pass
        self.resize(520, 680)
        self.setMinimumSize(420, 400)
        self.current_ptc_path = None
        ##
        self.view_borders = {i: 0 for i in range(6)}
        ##
        # Display Mode is a normal non-modal NakshaAI utility window.
        # It must NEVER be globally always-on-top: clicking the main NakshaAI
        # window or another application should naturally move that window in
        # front.  We keep it as a real top-level Qt.Window (rather than
        # Qt.Tool/owned-window) so it is also allowed to move behind the main
        # window when the user changes focus.
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WA_QuitOnClose, False)

        # Owner-state synchronization is observational only.  The dialog stays
        # open after Apply and after focus changes.  It is minimized/restored
        # with NakshaAI without stealing focus on restore.
        self._owner_state_sync = False
        self._owner_minimized_me = False
        self._was_visible_before_owner_minimize = False
        self._user_minimized = False
        if parent is not None:
            try:
                parent.installEventFilter(self)
            except Exception:
                pass


        from PySide6.QtCore import QSettings
        settings = QSettings("NakshaAI", "LidarApp")

        saved_geo = settings.value("display_mode_dialog_geometry")
        if saved_geo:
            try:
                self.restoreGeometry(saved_geo)
            except Exception as e:
                print(f"âš ï¸ Failed to restore geometry: {e}")

        saved_palettes = settings.value("global_view_palettes")
        if saved_palettes:
            self.view_palettes = {}
            for view_idx_str, palette_dict in saved_palettes.items():
                view_idx = int(view_idx_str)
                self.view_palettes[view_idx] = {}
                for code_str, info in palette_dict.items():
                    code = int(code_str)
                    lvl_text = str(info.get('lvl', '') or '').strip()
                    if not lvl_text:
                        desc_text = str(info.get('description', '') or '').strip()
                        if desc_text and desc_text != str(code):
                            lvl_text = desc_text
                    self.view_palettes[view_idx][code] = {
                        'show':        info.get('show', True),
                        'description': info.get('description', ''),
                        'lvl':         lvl_text,
                        'color':       tuple(info.get('color', (128, 128, 128))),
                        'weight':      info.get('weight', 1.0)
                    }

        saved_shows = settings.value("global_slot_shows")
        if saved_shows:
            self.slot_shows = {}
            for slot_idx_str, show_dict in saved_shows.items():
                slot_idx = int(slot_idx_str)
                self.slot_shows[slot_idx] = {
                    int(code): checked
                    for code, checked in show_dict.items()
                }

        # Stash border logic mode to restore after UI widgets are built
        self._pending_border_logic_mode = None
        saved_logic_mode = settings.value("global_border_logic_mode")
        if saved_logic_mode is not None:
            try:
                self._pending_border_logic_mode = int(saved_logic_mode)
            except (ValueError, TypeError):
                pass

        # Clear stale file-specific display state from older builds so fresh
        # loads do not revive per-file UI state.
        settings.remove("global_last_ptc_path")
        for key in settings.allKeys():
            if (
                key.startswith("file_ptc/")
                or key.startswith("file_palettes/")
                or key.startswith("file_slot_shows/")
                or key.startswith("file_display_mode/")
                or key.startswith("file_color_mode/")
            ):
                settings.remove(key)


        # Bug-8 fix: single-shot debounce timer for QSettings registry flush.
        # Fires 2 s after the last Apply/border click; harmlessly restarts on each
        # new click so rapid interactions never block the main thread.
        from PySide6.QtCore import QTimer
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self.save_global_settings)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 10)
        main_layout.setSpacing(6)

        self.setStyleSheet(get_dialog_stylesheet())

        self.file_menu = QMenu(self)
        file_menu = self.file_menu
        load_action  = file_menu.addAction("Open...")
        save_action  = file_menu.addAction("Save...")
        save_as_action = file_menu.addAction("Save As...")
        file_menu.addSeparator()
        exit_action  = file_menu.addAction("Close")
        load_action.triggered.connect(self.load_classes)
        save_action.triggered.connect(self.save_classes)
        save_as_action.triggered.connect(lambda: self.save_classes_as(update_active=False))
        exit_action.triggered.connect(self.close)

        controls_card = QFrame()
        controls_card.setObjectName("displayControlsCard")
        controls_layout = QHBoxLayout(controls_card)
        controls_layout.setContentsMargins(10, 8, 10, 8)
        controls_layout.setSpacing(8)

        self.file_button = QToolButton()
        self.file_button.setObjectName("displayFileButton")
        self.file_button.setText("File")
        self.file_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.file_button.setPopupMode(QToolButton.InstantPopup)
        self.file_button.setMenu(self.file_menu)
        self.file_button.setFocusPolicy(Qt.NoFocus)
        controls_layout.addWidget(self.file_button)

        self.slot_box = QComboBox()
        self.slot_box.setMinimumWidth(110)
        self.slot_box.addItems([
            "Main View",
            "View 1", "View 2", "View 3", "View 4",
        ])
        self.slot_box.currentIndexChanged.connect(self.on_slot_changed)
        self.slot_box.currentIndexChanged.connect(self.on_view_selection_changed)
        controls_layout.addWidget(self.slot_box, stretch=2)

        self.color_mode = QComboBox()
        self.color_mode.setMinimumWidth(110)
        self.color_mode.addItems([
            "By Classification",      # idx 0
            "Shaded Classification",  # idx 1
            "Depth",                  # idx 2
            "Intensity",              # idx 3
            "RGB",                    # idx 4
            "Elevation",              # idx 5
            "Surface",                # idx 6
            "Line",                   # idx 7 - LAS flight lines / Point Source ID
        ])
        controls_layout.addWidget(self.color_mode, stretch=2)
        self.shading_quality_label = QLabel("Speed")
        self.shading_quality_label.setToolTip(
            "Shading density: Fast uses 1M representatives, Normal uses 3M, "
            "and Slow triangulates every eligible point."
        )
        controls_layout.addWidget(self.shading_quality_label)
        self.shading_quality = QComboBox()
        self.shading_quality.setObjectName("displayShadingQuality")
        self.shading_quality.addItem("Fast", "fast")
        self.shading_quality.addItem("Normal", "normal")
        self.shading_quality.addItem("Slow – all points", "slow")
        self.shading_quality.setMinimumWidth(135)
        self._shading_quality_value = str(
            settings.value("global_shading_quality", "normal") or "normal"
        ).lower()
        self._surface_quality_value = str(
            settings.value("global_surface_quality", "normal") or "normal"
        ).lower()
        self._quality_mode_context = int(self.color_mode.currentIndex())
        quality_index = self.shading_quality.findData(self._shading_quality_value)
        self.shading_quality.setCurrentIndex(quality_index if quality_index >= 0 else 1)
        if parent is not None:
            parent.shading_quality = self._shading_quality_value
            parent.surface_quality = self._surface_quality_value
        self.shading_quality.setToolTip(
            "Fast: up to 1,000,000 mesh points. Normal: up to 3,000,000. "
            "Slow: every eligible finite point; may require very large RAM and several minutes."
        )
        controls_layout.addWidget(self.shading_quality)
        self.color_mode.currentIndexChanged.connect(self._sync_color_mode_state)

        # MicroStation-style persistent flight-line selector.  A QMenu closes
        # after every click, so this button opens a proper dialog instead.
        self.lines_button = QToolButton()
        self.lines_button.setObjectName("displayLinesButton")
        self.lines_button.setText("Lines")
        self.lines_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.lines_button.setFocusPolicy(Qt.NoFocus)
        self.lines_button.setToolTip("Choose flight lines shown in Line mode")
        controls_layout.addWidget(self.lines_button)
        self.lines_button.clicked.connect(self._open_lines_dialog)
        # Never scan a potentially multi-million-entry source-ID array merely
        # to construct Display Mode. Discovery runs when Lines is requested.
        self._rebuild_lines_menu(allow_recovery=False)

        main_layout.addWidget(controls_card)

        # ── Border widgets — created here, added to bottom row later ──────
        self.border_label = QLabel("Border")
        self.border_label.setObjectName("displayBorderLabel")
        self.border_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)

        self.border_minus_btn = QPushButton("-")
        self.border_minus_btn.setObjectName("displayBorderButton")
        self.border_minus_btn.setFixedWidth(26)
        self.border_minus_btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.border_minus_btn.setAutoDefault(False)
        self.border_minus_btn.setDefault(False)
        self.border_minus_btn.setFocusPolicy(Qt.NoFocus)
        self.border_minus_btn.clicked.connect(self.decrease_border)

        self.border_value_display = QLabel("0%")
        self.border_value_display.setObjectName("displayBorderValuePill")
        self.border_value_display.setFixedWidth(42)
        self.border_value_display.setAlignment(Qt.AlignCenter)
        self.border_value_display.setFont(QFont("Segoe UI", 9))

        self.border_plus_btn = QPushButton("+")
        self.border_plus_btn.setObjectName("displayBorderButton")
        self.border_plus_btn.setFixedWidth(26)
        self.border_plus_btn.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.border_plus_btn.setAutoDefault(False)
        self.border_plus_btn.setDefault(False)
        self.border_plus_btn.setFocusPolicy(Qt.NoFocus)
        self.border_plus_btn.clicked.connect(self.increase_border)

        self.border_setting_btn = QPushButton()
        self.border_setting_btn.setObjectName("displayBorderButton")
        from gui.icon_provider import get_icon
        self.border_setting_btn.setIcon(get_icon("settings_gear", size=14))
        from PySide6.QtCore import QSize
        self.border_setting_btn.setIconSize(QSize(14, 14))
        self.border_setting_btn.setFixedWidth(26)
        self.border_setting_btn.setFocusPolicy(Qt.NoFocus)

        self.border_setting_menu = QMenu(self)
        self.border_logic_point = QAction("Per-Point", self.border_setting_menu)
        self.border_logic_point.setCheckable(True)
        self.border_logic_point.setChecked(False)

        self.border_logic_object = QAction("Structured", self.border_setting_menu)
        self.border_logic_object.setCheckable(True)
        self.border_logic_object.setChecked(True)

        self.border_logic_hybrid = QAction("Hybrid", self.border_setting_menu)
        self.border_logic_hybrid.setCheckable(True)

        self.border_action_group = QActionGroup(self)
        self.border_action_group.addAction(self.border_logic_point)
        self.border_action_group.addAction(self.border_logic_object)
        self.border_action_group.addAction(self.border_logic_hybrid)
        self.border_action_group.setExclusive(False)

        self.border_setting_menu.addAction(self.border_logic_point)
        self.border_setting_menu.addAction(self.border_logic_object)
        self.border_setting_menu.addAction(self.border_logic_hybrid)

        self.border_logic_point.triggered.connect(lambda _=False: self._select_border_mode(0))
        self.border_logic_object.triggered.connect(lambda _=False: self._select_border_mode(1))
        self.border_logic_hybrid.triggered.connect(lambda _=False: self._select_border_mode(2))

        self.border_setting_btn.clicked.connect(
            lambda: self.border_setting_menu.exec(
                self.border_setting_btn.mapToGlobal(
                    self.border_setting_btn.rect().bottomLeft()
                )
            )
        )

        table_card = QFrame()
        table_card.setObjectName("displayTableCard")
        table_layout = QVBoxLayout(table_card)
        table_layout.setContentsMargins(12, 12, 12, 12)
        table_layout.setSpacing(12)
        self.table   = QTableWidget(0, 7)
        self.table.setObjectName("displayClassTable")
        self.table.setHorizontalHeaderLabels(
            ["Show", "Code", "Description", "Draw", "Lvl", "Color", "Weight"]
        )
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setWordWrap(False)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignCenter)
        self.table.horizontalHeader().setHighlightSections(False)
        hdr = self.table.horizontalHeader()
        # All columns Interactive — user can drag-resize any column freely.
        # setStretchLastSection fills remaining space so there is never a gap.
        for i in range(7):
            hdr.setSectionResizeMode(i, QHeaderView.Interactive)
        hdr.setStretchLastSection(True)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 60)
        self.table.setColumnWidth(2, 140)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 56)
        self.table.setColumnWidth(5, 75)
        self.table.setColumnWidth(6, 65)
        self.table.setShowGrid(False)

        header = self.table.horizontalHeader()
        header.moveSection(4, 2)  # Move Lvl to 3rd position
        header.moveSection(5, 3)  # Move Color to 4th position
        header.moveSection(6, 4)  # Move Weight to 5th position

        header.sectionDoubleClicked.connect(self._on_header_double_clicked)
        self._weight_popup = _WeightBulkPopup(self)

        saved_col_widths = settings.value("display_mode_table_column_widths")

        if saved_col_widths:
            try:
                widths = [int(w) for w in saved_col_widths]
                if len(widths) == self.table.columnCount():
                    for i, w in enumerate(widths):
                        self.table.setColumnWidth(i, w)
            except Exception as e:
                print(f"Failed to restore column widths: {e}")

        table_layout.addWidget(self.table)

        action_rail = QFrame()
        action_rail.setObjectName("displayActionRail")
        bottom_buttons_layout = QHBoxLayout(action_rail)
        bottom_buttons_layout.setContentsMargins(0, 0, 0, 0)
        bottom_buttons_layout.setSpacing(8)
        
        self.add_btn    = QPushButton("Add")
        self.edit_btn   = QPushButton("Edit")
        self.del_btn    = QPushButton("Delete")
        self.select_btn = QPushButton("Select All")
        self.clear_btn  = QPushButton("Clear All")
        
        for b in [self.add_btn, self.edit_btn, self.del_btn, self.select_btn, self.clear_btn]:
            b.setObjectName("displayActionButton")
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setFocusPolicy(Qt.NoFocus)
            bottom_buttons_layout.addWidget(b, stretch=1)
            
        table_layout.addWidget(action_rail)
        main_layout.addWidget(table_card)

        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        bottom.addStretch()
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.setObjectName("displayApplyBtn")
        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("displayCloseBtn")
        self.apply_btn.setAutoDefault(False)
        self.apply_btn.setDefault(False)
        self.apply_btn.setFocusPolicy(Qt.NoFocus)
        self.close_btn.setAutoDefault(False)
        self.close_btn.setDefault(False)
        self.close_btn.setFocusPolicy(Qt.NoFocus)
        bottom.addWidget(self.border_label)
        bottom.addWidget(self.border_minus_btn)
        bottom.addWidget(self.border_value_display)
        bottom.addWidget(self.border_plus_btn)
        bottom.addWidget(self.border_setting_btn)
        bottom.addSpacing(8)
        bottom.addWidget(self.apply_btn)
        bottom.addWidget(self.close_btn)
        main_layout.addLayout(bottom)

        self.close_btn.clicked.connect(self._handle_close)
        self.apply_btn.clicked.connect(self.on_apply)
        self.add_btn.clicked.connect(self.on_add)
        self.edit_btn.clicked.connect(self.on_edit)
        self.del_btn.clicked.connect(self.on_delete)
        self.select_btn.clicked.connect(self.on_select_all)
        self.clear_btn.clicked.connect(self.on_clear_all)
        self.table.itemChanged.connect(self._on_weight_cell_changed)

        # Preserve any palettes/shows restored from QSettings before creating
        # default slot containers.
        existing_view_palettes = getattr(self, "view_palettes", {}) \
            if isinstance(getattr(self, "view_palettes", None), dict) else {}
        existing_slot_shows = getattr(self, "slot_shows", {}) \
            if isinstance(getattr(self, "slot_shows", None), dict) else {}

        self.current_slot      = 0
        self.slot_shows        = {i: {} for i in range(6)}
        app_modes = getattr(parent, "_naksha_border_logic_modes", None)
        self.view_border_modes = {
            i: int(app_modes.get(i, 1)) if isinstance(app_modes, dict) else 1
            for i in range(6)
        }   # 0=Per-Point, 1=Structured, 2=Hybrid
        self.point_border_percent = 0
        self.view_palettes     = {i: {} for i in range(6)}

        # Rehydrate restored entries into the fixed slot range.
        for slot_idx, show_map in existing_slot_shows.items():
            try:
                si = int(slot_idx)
            except Exception:
                continue
            if 0 <= si <= 5 and isinstance(show_map, dict):
                self.slot_shows[si] = {
                    int(code): bool(val) for code, val in show_map.items()
                }

        for slot_idx, palette_map in existing_view_palettes.items():
            try:
                si = int(slot_idx)
            except Exception:
                continue
            if 0 <= si <= 5 and isinstance(palette_map, dict):
                normalized = {}
                for code, info in palette_map.items():
                    try:
                        ci = int(code)
                    except Exception:
                        continue
                    info = info if isinstance(info, dict) else {}
                    normalized[ci] = {
                        "show":        bool(info.get("show", True)),
                        "description": str(info.get("description", "")),
                        "lvl":         str(info.get("lvl", "")),
                        "color":       tuple(info.get("color", (128, 128, 128))),
                        "weight":      float(info.get("weight", 1.0)),
                    }
                self.view_palettes[si] = normalized

        if parent and hasattr(parent, 'view_palettes'):
            print(f"ðŸ“¥ Syncing view_palettes from app to dialog...")
            for view_idx, palette in parent.view_palettes.items():
                if view_idx not in self.view_palettes:
                    self.view_palettes[view_idx] = {}
                for code, info in palette.items():
                    self.view_palettes[view_idx][code] = {
                        "show":        bool(info.get("show", True)),
                        "color":       tuple(info.get("color", (128, 128, 128))),
                        "weight":      float(info.get("weight", 1.0)),
                        "description": str(info.get("description", "")),
                        "lvl":         str(info.get("lvl", "")),
                        "draw":        info.get("draw", "")
                    }
            print(f"âœ… Synced {len(parent.view_palettes)} view palettes from app")

        ptc_loaded = False
        settings   = QSettings("NakshaAI", "LidarApp")

        pending_ptc = None
        if parent:
            pending_ptc = getattr(parent, '_pending_ptc_restore', None) or \
                          getattr(parent, 'pending_ptc_restore', None)

        if pending_ptc:
            if getattr(parent, '_block_ptc_autoload', False):
                print("   Pending PTC restore blocked (shortcut in progress)")
                pending_ptc = None
                if hasattr(parent, '_pending_ptc_restore'):
                    del parent._pending_ptc_restore
                if hasattr(parent, 'pending_ptc_restore'):
                    del parent.pending_ptc_restore
            ptc_path = pending_ptc
            if ptc_path and os.path.exists(ptc_path):
                print(f"\n{'=' * 60}")
                print(f"ðŸ“‚ RESTORING PTC FILE (Pending): {os.path.basename(ptc_path)}")
                self.load_classes_from_path(ptc_path)
                ptc_loaded = True
                if hasattr(parent, '_pending_ptc_restore'):
                    del parent._pending_ptc_restore
                if hasattr(parent, 'pending_ptc_restore'):
                    del parent.pending_ptc_restore

        if not ptc_loaded:
            if getattr(parent, '_block_ptc_autoload', False):
                print("   â­ï¸ Global PTC auto-load blocked (shortcut in progress)")
            else:

                global_last_ptc = settings.value("global_last_ptc_path")
                if global_last_ptc and os.path.exists(global_last_ptc):
                    print(f"ðŸ“‚ Restoring LAST USED PTC (Global): {global_last_ptc}")
                    self.load_classes_from_path(global_last_ptc)
                    ptc_loaded = True

        if not ptc_loaded:
            print(f"\n{'=' * 60}")
            print(f"ðŸ”§ INITIALIZING DISPLAY MODE WITH DEFAULT CLASSES")
            existing_weights = {}
            if parent and hasattr(parent, 'view_palettes') and 0 in parent.view_palettes:
                for code, info in parent.view_palettes[0].items():
                    existing_weights[code] = info.get('weight', 1.0)
            print(f"   âœ… Added {self.table.rowCount()} default classes")

        default_palette_template = {}
        for row in range(self.table.rowCount()):
            code        = int(self.table.item(row, 1).text())
            desc        = self.table.item(row, 2).text()
            color       = self.table.item(row, 5).background().color().getRgb()[:3]
            weight_item = self.table.item(row, 6)
            weight      = float(weight_item.text()) if weight_item else 1.0
            chk         = self.table.cellWidget(row, 0)
            is_visible  = chk.isChecked() if chk else True
            default_palette_template[code] = {
                "show":        is_visible,
                "description": desc,
                "color":       color,
                "weight":      weight
            }

        for view_idx in range(6):
            if view_idx not in self.view_palettes:
                self.view_palettes[view_idx] = {}
            if view_idx not in self.slot_shows:
                self.slot_shows[view_idx] = {}
            for code, info in default_palette_template.items():
                if code not in self.view_palettes[view_idx]:
                    self.view_palettes[view_idx][code] = {
                        "show":        info["show"],
                        "description": str(info["description"]),
                        "lvl":         str(info.get("lvl", "")),
                        "color":       tuple(info["color"]),
                        "weight":      info.get("weight", 1.0)
                    }
                self.slot_shows[view_idx][code] = self.view_palettes[view_idx][code].get("show", info["show"])

        self._check_pending_restores()

        print(f"\n{'=' * 60}")
        print(f"ðŸ” VERIFYING DISPLAY MODE SETUP")
        print(f"{'=' * 60}")

        has_signal = hasattr(self, 'applied')
        print(f"   Applied signal exists: {has_signal}")
        has_button = hasattr(self, 'apply_btn') and self.apply_btn is not None
        print(f"   Apply button exists: {has_button}")

        if parent:
            has_handler = hasattr(parent, 'apply_class_map')
            print(f"   Parent has apply_class_map: {has_handler}")
            if has_handler:
                try:
                    import warnings
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        try:
                            self.applied.disconnect(parent.apply_class_map)
                        except (TypeError, RuntimeError):
                            pass
                    self.applied.connect(parent.apply_class_map)
                    print(f"   âœ… Connected 'applied' signal to parent.apply_class_map")
                except Exception as e:
                    print(f"   âŒ Connection failed: {e}")
                    import traceback
                    traceback.print_exc()
            else:
                print(f"   âŒ ERROR: Parent does not have apply_class_map method!")
        else:
            print(f"   âš ï¸ No parent provided - signal not connected")

        print(f"{'=' * 60}\n")
        self.connect_existing_checkboxes()
        if parent:
            try:
                self.wire_palette_signal(parent)
                print("   âœ… palette_changed signal auto-wired to GPU sync")
            except Exception as _wire_err:
                print(f"   âš ï¸ palette_changed wirezfailed: {_wire_err}")
        print(f"ðŸŽ¯ DisplayModeDialog initializatioz complete")

        # Apply stashed border logic mode now that all UI widgets exist
        if isinstance(getattr(parent, "_naksha_border_logic_modes", None), dict):
            mode_val = self.view_border_modes.get(self.current_slot, 1)
            self._select_border_mode(mode_val, push_gpu=False)
            self._pending_border_logic_mode = None
        elif getattr(self, '_pending_border_logic_mode', None) is not None:
            mode_val = self._pending_border_logic_mode
            try:
                self._select_border_mode(mode_val, push_gpu=False)
                print(f"âœ… Restored border logic mode from QSettings: {mode_val}")
            except Exception as _e:
                print(f"âš ï¸ Could not restore border logic mode: {_e}")
            self._pending_border_logic_mode = None

        self._sync_color_mode_state()

    def _sync_color_mode_state(self) -> None:
        """Keep mode controls visible and bind Speed independently to Shading/Surface."""
        try:
            if not hasattr(self, "color_mode") or self.color_mode is None:
                return

            # Save the outgoing mesh-quality choice before changing context.
            previous = int(getattr(self, "_quality_mode_context", self.color_mode.currentIndex()))
            if hasattr(self, "shading_quality"):
                current_value = str(self.shading_quality.currentData() or "normal")
                if previous == 1:
                    self._shading_quality_value = current_value
                elif previous == 6:
                    self._surface_quality_value = current_value

            self.color_mode.setVisible(True)
            # Cross-sections (slots 1-4) support Class/Depth/Intensity/RGB/
            # Elevation; Main View (slot 0) supports everything; Cut Section
            # (slot 5) stays classification-only, unchanged.
            self.color_mode.setEnabled(self.current_slot in (0, 1, 2, 3, 4))
            mode_idx = int(self.color_mode.currentIndex())
            mesh_speed_visible = self.current_slot in (0, 1, 2, 3, 4) and mode_idx in (1, 6)

            if hasattr(self, "shading_quality_label"):
                self.shading_quality_label.setVisible(mesh_speed_visible)
            if hasattr(self, "shading_quality"):
                self.shading_quality.setVisible(mesh_speed_visible)
                self.shading_quality.setEnabled(mesh_speed_visible)
                if mesh_speed_visible:
                    wanted = (
                        getattr(self, "_shading_quality_value", "normal")
                        if mode_idx == 1
                        else getattr(self, "_surface_quality_value", "normal")
                    )
                    qidx = self.shading_quality.findData(str(wanted).lower())
                    if qidx < 0:
                        qidx = self.shading_quality.findData("normal")
                    self.shading_quality.blockSignals(True)
                    self.shading_quality.setCurrentIndex(qidx if qidx >= 0 else 1)
                    self.shading_quality.blockSignals(False)
                    if mode_idx == 1:
                        self.shading_quality.setToolTip(
                            "Shading — Fast: 1M representatives; Normal: 3M; Slow: all eligible points."
                        )
                    else:
                        self.shading_quality.setToolTip(
                            "Surface — Fast: 1M representatives; Normal: 3M; Slow: all eligible points."
                        )

            self._quality_mode_context = mode_idx

            # Class(0) / Shaded(1) / Depth(2) / Intensity(3) / RGB(4) / Elevation(5) /
            # Surface(6) / Line(7) are wired up for cross-section views (View 1-4).
            # Cut Section (slot 5) keeps its original classification-only restriction.
            _SECTION_ALLOWED_MODES = (0, 1, 2, 3, 4, 5, 6, 7)
            if self.current_slot == 5 and self.color_mode.currentIndex() != 0:
                self.color_mode.blockSignals(True)
                self.color_mode.setCurrentIndex(0)
                self.color_mode.blockSignals(False)
            elif (
                1 <= self.current_slot <= 4
                and self.color_mode.currentIndex() not in _SECTION_ALLOWED_MODES
            ):
                self.color_mode.blockSignals(True)
                self.color_mode.setCurrentIndex(0)
                self.color_mode.blockSignals(False)
        except Exception:
            pass

    @staticmethod
    def wire_palette_signal(app) -> bool:
        return _uam_connect(app)

    def _save_slot_state(self, slot_idx: int) -> None:
        """
        Bug-10 fix: single-pass save of checkboxes AND weights.
        Replaces the two separate _save_slot_checkboxes + _save_slot_weights
        passes that each iterated all table rows independently (2Ã— scan â†’ 1Ã—).
        """
        if not hasattr(self, 'slot_shows'):
            self.slot_shows = {}
        self.slot_shows.setdefault(slot_idx, {})
        if not hasattr(self, 'view_palettes'):
            self.view_palettes = {}
        slot_pal = self.view_palettes.setdefault(slot_idx, {})

        for row in range(self.table.rowCount()):
            try:
                code_item = self.table.item(row, 1)
                if not code_item:
                    continue
                code = int(code_item.text())
                chk  = self.table.cellWidget(row, 0)
                show = chk.isChecked() if chk else True
                wt_item = self.table.item(row, 6)
                weight  = float(wt_item.text()) if wt_item else 1.0

                self.slot_shows[slot_idx][code] = show
                slot_pal.setdefault(code, {}).update({'show': show, 'weight': weight})
            except Exception:
                continue

    def _select_border_mode(self, mode_val: int, push_gpu: bool = True) -> None:
        """
        Enforce exactly ONE border action checked. Never rely on QActionGroup
        internal exclusivity â€” toggling setExclusive() corrupts its 'current'
        pointer causing two items to appear checked simultaneously on next click.
        """
        actions = [self.border_logic_point, self.border_logic_object, self.border_logic_hybrid]
        # Block signals so setChecked() calls don't trigger _select_border_mode recursively
        for a in actions:
            a.blockSignals(True)
        try:
            self.border_logic_point.setChecked(mode_val == 0)
            self.border_logic_object.setChecked(mode_val == 1)
            self.border_logic_hybrid.setChecked(mode_val == 2)
        finally:
            for a in actions:
                a.blockSignals(False)
        # â”€â”€ Per-slot border mode save â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if hasattr(self, 'view_border_modes'):
            self.view_border_modes[self.current_slot] = mode_val
        if push_gpu:
            self._on_border_mode_changed()

    def _save_border_mode(self, slot_idx: int) -> None:
        """Save the current border type selection for this slot."""
        if not hasattr(self, 'view_border_modes'):
            self.view_border_modes = {i: 1 for i in range(6)}
        self.view_border_modes[slot_idx] = int(self.get_border_mode())

    def _load_border_mode(self, slot_idx: int) -> None:
        """Restore the border type selection for this slot (no GPU push)."""
        if not hasattr(self, 'view_border_modes'):
            self.view_border_modes = {i: 1 for i in range(6)}
        mode_val = self.view_border_modes.get(slot_idx, 1)
        self._select_border_mode(mode_val, push_gpu=False)


    def _load_slot_state(self, slot_idx: int) -> None:
        """
        Bug-10 fix: single-pass load of checkboxes AND weights with table-level
        blockSignals â€” suppresses all stateChanged callbacks during bulk restore.

        Previously: _load_slot_checkboxes + _load_slot_weights = 2 full scans
        + per-checkbox blockSignals (still fires on_checkbox_toggled n times).
        Now: 1 scan, table-level signal block â†’ zero spurious callbacks â†’ O(n).
        """
        palette        = self.view_palettes.get(slot_idx, {}) if hasattr(self, 'view_palettes') else {}
        saved_shows    = self.slot_shows.get(slot_idx, {})     if hasattr(self, 'slot_shows')    else {}
        default_weight = 1.0 if slot_idx == 0 else 0.5

        # table.blockSignals suppresses ALL stateChanged during bulk setChecked â€”
        # eliminates the O(nÂ²) callback storm caused by per-row blockSignals.
        self.table.blockSignals(True)
        try:
            for row in range(self.table.rowCount()):
                try:
                    code_item = self.table.item(row, 1)
                    if not code_item:
                        continue
                    code = int(code_item.text())
                    info = palette.get(code, {})

                    chk = self.table.cellWidget(row, 0)
                    if chk:
                        chk.setChecked(saved_shows.get(code, info.get('show', True)))

                    wt_item = self.table.item(row, 6)
                    if wt_item:
                        wt_item.setText(f"{info.get('weight', default_weight):.1f}")
                except Exception:
                    continue
        finally:
            self.table.blockSignals(False)

    # â”€â”€ Backward-compat shims so existing callers don't break â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _load_slot_checkboxes(self, slot_idx: int) -> None:
        self._load_slot_state(slot_idx)

    def _save_slot_checkboxes(self, slot_idx: int) -> None:
        self._save_slot_state(slot_idx)

    def _on_weight_cell_changed(self, item: "QTableWidgetItem") -> None:
        if item.column() != 6:
            return
        try:
            val = float(item.text())
        except ValueError:
            item.setText("1.00")
            return
        clamped = max(0.1, min(val, 12.0))
        if clamped != val:
            self.table.blockSignals(True)
            item.setText(f"{clamped:.2f}")
            self.table.blockSignals(False)

    def on_slot_changed(self, idx: int) -> None:
        # The flight-line selector belongs to the slot that was active when it
        # was opened. Close it before changing slots so it cannot appear to
        # control the newly selected Main/Cross Section view. The user can
        # reopen it from the Lines button after the switch.
        self._close_lines_dialog()

        # Bug-10 fix: single save + single load (was 4 separate table scans).
        # Bug-3/Signal: table.blockSignals handled inside _load_slot_state.
        self._save_slot_state(self.current_slot)   # 1 pass: checks + weights
        self._save_border_mode(self.current_slot)  # save border type for outgoing slot

        # Remember the outgoing slot's own color-mode choice (View 1-4 can
        # each stay on their own mode independently, same pattern as palettes).
        if not hasattr(self, 'view_color_modes'):
            self.view_color_modes = {}
        self.view_color_modes[self.current_slot] = int(self.color_mode.currentIndex())

        self.current_slot = idx
        self._load_slot_state(idx)                 # 1 pass: checks + weights, signals blocked
        self.update_border_display()
        self.load_view_border(idx)
        self._load_border_mode(idx)
        if idx == 5:
            self.on_view_switched_to_cut_section()

        # Restore this slot's own remembered color mode.
        #   Slot 0 (Main View): any mode.
        #   Slots 1-4 (cross-sections): Class/Shaded/Depth/Intensity/RGB/Elevation/Surface/Line.
        #   Slot 5 (Cut Section): classification-only, unchanged.
        _SECTION_ALLOWED_MODES = (0, 1, 2, 3, 4, 5, 6, 7)
        restore_idx = int(self.view_color_modes.get(idx, 0))
        if idx == 0:
            self.color_mode.setEnabled(True)
        elif 1 <= idx <= 4:
            if restore_idx not in _SECTION_ALLOWED_MODES:
                restore_idx = 0
            self.color_mode.setEnabled(True)
        else:
            restore_idx = 0
            self.color_mode.setEnabled(False)

        self.color_mode.blockSignals(True)
        self.color_mode.setCurrentIndex(restore_idx)
        self.color_mode.blockSignals(False)
        self._sync_color_mode_state()

    def on_view_selection_changed(self, idx):
        self.view_switched.emit(idx)

    def add_class(self, code, desc, draw, lvl, color, show=False, weight=2.0):
        row = self.table.rowCount()
        self.table.insertRow(row)

        chk = QCheckBox()
        chk.setChecked(show)
        chk.setFocusPolicy(Qt.NoFocus)
        chk.setCursor(Qt.PointingHandCursor)
        chk.setStyleSheet(
            "QCheckBox { background: transparent; margin-left: 16px; padding: 0px; }"
        )
        # Bug-9 fix: closure captures `row` at connect time â†’ O(1) handler,
        # no sender() search loop needed.
        chk.stateChanged.connect(
            lambda state, r=row: self._on_checkbox_toggled_fast(r, state)
        )
        self.table.setCellWidget(row, 0, chk)

        self.table.setItem(row, 1, QTableWidgetItem(str(code)))
        self.table.setItem(row, 2, QTableWidgetItem(desc))
        self.table.setItem(row, 3, QTableWidgetItem(draw))
        self.table.setItem(row, 4, QTableWidgetItem(str(lvl)))

        self._set_color_cell(row, color)

        try:
            weight_text = f"{float(weight):.2f}"
        except Exception:
            weight_text = str(weight)
        self.table.setItem(row, 6, QTableWidgetItem(weight_text))
        self._format_table_row(row)

    def _set_color_cell(self, row, color):
        qcolor = QColor(color)
        color_item = self.table.item(row, 5)
        if color_item is None:
            color_item = QTableWidgetItem()
            self.table.setItem(row, 5, color_item)
        color_item.setText("")
        color_item.setBackground(qcolor)

        border_color = qcolor.darker(145)
        swatch = QFrame()
        swatch.setFixedSize(52, 18)
        swatch.setStyleSheet(
            f"background-color: rgb({qcolor.red()}, {qcolor.green()}, {qcolor.blue()});"
            f"border: 1px solid {border_color.name()};"
            "border-radius: 5px;"
        )
        swatch.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        holder.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignCenter)
        holder_layout.addWidget(swatch)
        self.table.setCellWidget(row, 5, holder)

    def _format_table_row(self, row):
        alignments = {
            1: Qt.AlignCenter,
            4: Qt.AlignCenter,
            5: Qt.AlignCenter,
            6: Qt.AlignCenter,
        }
        for column in range(1, self.table.columnCount()):
            item = self.table.item(row, column)
            if item is None:
                continue
            item.setTextAlignment(alignments.get(column, Qt.AlignVCenter | Qt.AlignLeft))
            if column == 2 or column == 3 or column == 4:
                item.setToolTip(item.text())
            elif column == 5:
                rgb = item.background().color().getRgb()[:3]
                item.setToolTip(f"RGB: {rgb[0]}, {rgb[1]}, {rgb[2]}")

    def save_classes(self):
        if not self.current_ptc_path:
            return self.save_classes_as(update_active=True)
        self._write_ptc(self.current_ptc_path)
        print(f"ðŸ’¾ Saved class table to {self.current_ptc_path}")

    def save_classes_as(self, update_active=False):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Class Table As", "", "Point Class Table (*.ptc)"
        )
        if not path:
            return
        self._write_ptc(path)
        if update_active:
            self.current_ptc_path = path
            self._update_window_title()
            self._refresh_ptc_label()
            settings = QSettings("NakshaAI", "LidarApp")
            settings.setValue("last_ptc_path", path)

    def _write_ptc(self, path):
        with open(path, "w") as f:
            for row in range(self.table.rowCount()):
                code       = int(self.table.item(row, 1).text())
                desc       = self.table.item(row, 2).text()
                draw       = self.table.item(row, 3).text()
                lvl_item   = self.table.item(row, 4)
                lvl        = lvl_item.text() if lvl_item else ""
                color_item = self.table.item(row, 5)
                qcolor     = color_item.background().color()
                rgb        = f"{qcolor.red()},{qcolor.green()},{qcolor.blue()}"
                show       = int(self.table.cellWidget(row, 0).isChecked())
                weight_item = self.table.item(row, 6)
                weight     = self.table.item(row, 6).text() if weight_item else "2.0"
                try:
                    weight_float = max(0.5, min(float(weight), 3.0))
                    weight = f"{weight_float:.2f}"
                except Exception:
                    weight = "1.0"
                f.write(f"{code}\t{desc}\t{lvl}\n")
                f.write(f"*\t{draw}\t{code}\t{rgb}\t{show}\t{weight}\n\n")

    def load_classes(self):
        if self.current_slot != 0:
            self._show_front_message(
                QMessageBox.Warning,
                "Restricted Action",
                "âš ï¸ <b>Cannot load PTC file in Cross-Section View!</b><br><br>"
                "Please switch to the <b>Main View</b> to load a PTC file."
            )
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Class Table", "", "Point Class Table (*.ptc)"
        )
        if not path:
            return
        self.load_classes_from_path(path, force_colors=True)

    def load_classes_from_path(self, path, force_colors=False):
        """Load a PTC file and rebuild view palettes.

        Parameters
        ----------
        path : str
            Path to the .ptc file.
        force_colors : bool, optional
            When *True* (explicit user "Load PTC" action), the PTC colours
            overwrite every view slot unconditionally.
            When *False* (auto-reload during dialog open / grid switch),
            existing per-view colour, show and weight customisations set
            by DisplayMode presets are preserved for classes that already
            exist in the view palette.
        """
        if getattr(self, "_ptc_load_in_progress", False):
            print("â­ï¸ PTC load already in progress - skipping re-entrant call")
            return

        table_updates_were_enabled = True
        try:
            self._ptc_load_in_progress = True
            self._bulk_ptc_loading = True

            table_updates_were_enabled = self.table.updatesEnabled()
            self.table.setUpdatesEnabled(False)
            self.table.blockSignals(True)

            self.current_ptc_path = path
            self._update_window_title()
            self._refresh_ptc_label()
            self.table.setRowCount(0)

            with open(path, "r") as f:
                lines = [ln.strip() for ln in f if ln.strip()]

            class_0_found      = False
            class_0_was_hidden = False

            print(f"\n{'=' * 60}")
            print(f"ðŸ“‚ LOADING PTC: {os.path.basename(path)}")
            print(f"{'=' * 60}")

            for i in range(0, max(0, len(lines) - 1), 2):
                header = lines[i].split("\t")
                detail = lines[i + 1].split("\t")
                if len(header) < 2 or len(detail) < 5:
                    continue

                code   = int(header[0])
                desc   = header[1]
                lvl    = header[2] if len(header) > 2 else ""
                draw   = detail[1] if len(detail) > 1 else ""
                rgb_raw = [int(c) for c in detail[3].split(",")] if len(detail) > 3 else [128, 128, 128]
                if len(rgb_raw) < 3:
                    rgb_raw = (rgb_raw + [128, 128, 128])[:3]
                rgb    = rgb_raw[:3]
                show   = (len(detail) > 4 and detail[4] == "1")
                weight = float(detail[5]) if len(detail) > 5 else 1.0
                color  = QColor(*rgb)

                if code == 0:
                    class_0_found      = True
                    class_0_was_hidden = not show

                self.add_class(code, desc, draw, lvl, color, show, weight)
                print(f"   Class {code:3d}: weight={weight:.1f}, show={show}")

            print(f"   âœ… All classes loaded")

            app = self._get_app_window()
            if force_colors and app:
                # Loading a PTC through the explicit user action is a fresh
                # display configuration.  Initialize every view consistently;
                # later manual border choices remain per-slot as before.
                from .unified_actor_manager import reset_border_logic_to_structured
                reset_border_logic_to_structured(app)
            needs_class_0_fix = False

            if app and hasattr(app, 'data') and app.data is not None:
                if 'classification' in app.data and app.data['classification'] is not None:
                    import numpy as np
                    unique_classes = np.unique(app.data['classification'])
                    if len(unique_classes) == 1 and unique_classes[0] == 0:
                        if class_0_was_hidden:
                            needs_class_0_fix = True

            if needs_class_0_fix:
                for row in range(self.table.rowCount()):
                    if int(self.table.item(row, 1).text()) == 0:
                        chk = self.table.cellWidget(row, 0)
                        if chk:
                            chk.setChecked(True)
                        color_item = self.table.item(row, 5)
                        if color_item:
                            current_color = color_item.background().color()
                            r, g, b = (current_color.red(),
                                       current_color.green(),
                                       current_color.blue())
                            if r + g + b < 30:
                                self._set_color_cell(row, QColor(200, 200, 200))
                        break

            print(f"\n{'=' * 60}")
            print(f"ðŸ”„ REBUILDING ALL VIEW PALETTES AFTER PTC LOAD")
            print(f"{'=' * 60}")

            master_palette = {}
            for row in range(self.table.rowCount()):
                code        = int(self.table.item(row, 1).text())
                desc        = self.table.item(row, 2).text()
                lvl_item    = self.table.item(row, 4)
                lvl         = lvl_item.text() if lvl_item else ""
                color       = self.table.item(row, 5).background().color().getRgb()[:3]
                weight_item = self.table.item(row, 6)
                weight      = float(weight_item.text()) if weight_item else 1.0
                chk         = self.table.cellWidget(row, 0)
                show        = chk.isChecked() if chk else False
                master_palette[code] = {
                    "show":        show,
                    "description": desc,
                    "lvl":         lvl,
                    "color":       color,
                    "weight":      weight
                }

            if not hasattr(self, 'view_palettes'):
                self.view_palettes = {}

            # Keep a snapshot of the pre-load per-slot palettes so auto-reload
            # can preserve existing user customizations (show/color/weight).
            # Prefer app.view_palettes as baseline when available because it is
            # the canonical runtime source across grid-switch restores.
            previous_view_palettes = {}
            if not force_colors:
                baseline_palettes = {}
                if app and hasattr(app, 'view_palettes') and isinstance(app.view_palettes, dict) and app.view_palettes:
                    baseline_palettes = app.view_palettes
                else:
                    baseline_palettes = self.view_palettes or {}
                for _slot in range(6):
                    slot_pal = (baseline_palettes or {}).get(_slot, {})
                    if isinstance(slot_pal, dict):
                        previous_view_palettes[_slot] = clone_palette(slot_pal)
                    else:
                        previous_view_palettes[_slot] = {}

            # âœ… FIX: Clear view_palettes before rebuilding to prevent stale classes 
            # from previous PTC files from bleeding into the new one.
            self.view_palettes = {i: {} for i in range(6)}
            
            for view_idx in range(6):
                for code, info in master_palette.items():
                    existing = previous_view_palettes.get(view_idx, {}).get(code)

                    if force_colors or existing is None:
                        # â”€â”€ Explicit user PTC load OR brand-new class entry â”€â”€
                        # Take everything from the PTC.
                        weight_to_use = float(info.get("weight", 1.0))
                        show_to_use   = bool(info["show"])
                        color_to_use  = tuple(info["color"])
                    else:
                        # â”€â”€ Auto-reload (dialog open / grid switch) AND
                        #    this class already exists in the per-view palette â”€â”€
                        # Preserve per-view customisations (colour, visibility,
                        # weight) so DisplayMode preset colours are never
                        # silently overwritten by PTC auto-reload.
                        color_to_use  = tuple(existing.get('color', info["color"]))
                        show_to_use   = bool(existing.get('show', info["show"]))
                        weight_to_use = float(existing.get('weight', info.get("weight", 1.0)))

                    self.view_palettes[view_idx][code] = {
                        "show":        show_to_use,
                        "description": str(info["description"]),
                        "lvl":         str(info.get("lvl", "")),
                        "color":       color_to_use,
                        "weight":      weight_to_use
                    }

            if app:
                if not hasattr(app, 'view_palettes') or app.view_palettes is None:
                    app.view_palettes = {}

                # Deep-copy each slot so app and dialog never share inner dicts
                app.view_palettes = {
                    int(view_idx): clone_palette(palette)
                    for view_idx, palette in self.view_palettes.items()
                }

                if 0 in app.view_palettes and app.view_palettes[0]:
                    app.class_palette = clone_palette(app.view_palettes[0])
                else:
                    app.class_palette = clone_palette(master_palette)

                print(f"   âœ… Updated app.view_palettes ({len(app.view_palettes)} slots) and app.class_palette ({len(app.class_palette)} classes)")
                validate_palette_isolation(app.view_palettes, context="load_classes_from_path")

            print(f"{'=' * 60}\n")

            self.classes_loaded.emit()

            # Auto-rebase DisplayMode shortcut presets to the newly loaded
            # PTC's class_palette, so shortcuts work immediately without
            # reopening Shortcut Configuration / pressing Apply / Ctrl+Shift+S.
            if app and not getattr(app, "_block_ptc_autoload", False):
                from .shortcut_manager import rebase_display_preset_to_current_ptc
                for combo, entry in getattr(app, "shortcuts", {}).items():
                    if entry.get("tool") == "DisplayMode" and entry.get("preset"):
                        entry["preset"] = rebase_display_preset_to_current_ptc(
                            entry["preset"], app
                        )

            # Do NOT call update_class_mode here â€” that triggers a full GPU
            # repaint on every ribbon click / PTC load before Apply is pressed.
            # The class_picker UI update below is safe (no GPU work).
            if app:
                picker = _get_live_class_picker(app)
                if picker is not None:
                    try:
                        if hasattr(picker, 'on_classes_changed'):
                            picker.on_classes_changed()
                    except Exception as e:
                        print(f"âš ï¸ Failed to update Class Picker: {e}")

            self.current_ptc_path = path
            settings = QSettings("NakshaAI", "LidarApp")
            settings.setValue("global_last_ptc_path", path)
            settings.sync()
            print(f"âœ… Loaded {self.table.rowCount()} classes from {os.path.basename(path)}")
            print(f"{'=' * 60}\n")
            self.connect_existing_checkboxes()

        except Exception as e:
            print(f"Failed to load PTC: {e}")
            import traceback
            traceback.print_exc()
        finally:
            try:
                self.table.blockSignals(False)
                self.table.setUpdatesEnabled(table_updates_were_enabled)
                self.table.viewport().update()
            except Exception:
                pass
            self._bulk_ptc_loading = False
            self._ptc_load_in_progress = False

    def on_add(self):
        dlg = EditClassDialog(parent=self)
        if dlg.exec() == QDialog.Accepted:
            # EditClassDialog stores the selected QColor as an attribute.
            # Calling it raises: TypeError: 'QColor' object is not callable.
            self.add_class(dlg.code(), dlg.desc(), dlg.draw(), dlg.lvl(), dlg.color)

    def on_edit(self):
        row = self.table.currentRow()
        if row < 0:
            return
        code   = int(self.table.item(row, 1).text())
        desc   = self.table.item(row, 2).text()
        draw   = self.table.item(row, 3).text()
        lvl    = self.table.item(row, 4).text()
        color  = self.table.item(row, 5).background().color()
        weight = self.table.item(row, 6).text() if self.table.columnCount() > 6 else "2.0"

        dlg = EditClassDialog(code, desc, color, self, draw, lvl, weight)
        if dlg.exec() == QDialog.Accepted:
            self.table.setItem(row, 1, QTableWidgetItem(str(dlg.code())))
            self.table.setItem(row, 2, QTableWidgetItem(dlg.desc()))
            self.table.setItem(row, 3, QTableWidgetItem(dlg.draw()))
            self.table.setItem(row, 4, QTableWidgetItem(dlg.lvl()))
            self._set_color_cell(row, dlg.color)
            self.table.setItem(row, 6, QTableWidgetItem(f"{float(dlg.weight()):.2f}"))
            self._format_table_row(row)

    def on_delete(self):
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)

    def _on_color_mode_changed(self, idx):
        """
        When user switches between display modes, preserve existing checkbox states.
        The checked classes in the table determine which classes are visible for
        ALL modes (depth, intensity, rgb, elevation, classification).
        """
        # Do not auto-check or modify checkboxes â€” user controls visibility for all modes.
        pass

    def _rebuild_lines_menu(self, allow_recovery=True):
        """Refresh flight-line IDs/colors from the active point cloud."""
        if not hasattr(self, "lines_button"):
            return
        if not allow_recovery:
            # Opening/synchronizing Display Mode must remain O(1) even when
            # the loaded point-source array contains tens of millions of IDs.
            self.lines_button.setEnabled(True)
            return
        app = self._get_app_window()
        source_ids = None
        if app is not None and isinstance(getattr(app, "data", None), dict):
            source_ids = app.data.get("point_source_id")
            if source_ids is None and allow_recovery:
                source_ids = self._recover_flight_line_ids(app)
        slot = int(getattr(self, "current_slot", 0))
        by_slot = getattr(app, "flight_line_visibility_by_slot", None) if app else None
        if not isinstance(by_slot, dict):
            by_slot = {}
        existing = by_slot.get(slot)
        if not isinstance(existing, dict):
            # Migrate the former global selection into every slot once, then
            # each slot becomes independent.
            existing = dict(getattr(app, "flight_line_visibility", {}) or {}) if app else {}
        if source_ids is None:
            self._line_ids = []
            self.lines_button.setEnabled(True)
            return
        import numpy as np
        unique_ids = [int(v) for v in np.unique(source_ids)]
        self._line_ids = unique_ids
        self.lines_button.setEnabled(bool(unique_ids))
        if app is not None:
            slot_visibility = {
                lid: bool(existing.get(lid, True)) for lid in unique_ids
            }
            by_slot[slot] = slot_visibility
            app.flight_line_visibility_by_slot = by_slot
            if slot == 0:
                app.flight_line_visibility = dict(slot_visibility)
            colors = dict(getattr(app, "flight_line_colors", {}) or {})
            for lid in unique_ids:
                colors.setdefault(lid, self._flight_line_color(lid))
            app.flight_line_colors = colors

    @staticmethod
    def _flight_line_color(line_id):
        """Return the same stable high-contrast RGB used by Line rendering."""
        lid = int(line_id)
        return ((lid * 67 + 53) % 256,
                (lid * 131 + 97) % 256,
                (lid * 193 + 181) % 256)

    def _close_lines_dialog(self):
        """Close the flight-line selector and forget its stale slot context."""
        popup = getattr(self, "_lines_popup", None)
        self._lines_popup = None
        if popup is None:
            return
        try:
            popup.close()
        except RuntimeError:
            # Qt may already have deleted a WA_DeleteOnClose popup while a
            # queued slot-change signal is still being delivered.
            pass

    def _open_lines_dialog(self):
        """Open a persistent selector; changes commit only when OK is clicked."""
        existing_popup = getattr(self, "_lines_popup", None)
        if existing_popup is not None:
            try:
                if existing_popup.isVisible():
                    existing_popup.raise_()
                    existing_popup.activateWindow()
                    return
            except RuntimeError:
                self._lines_popup = None

        self._rebuild_lines_menu()
        app = self._get_app_window()
        line_ids = list(getattr(self, "_line_ids", []) or [])
        if not line_ids:
            self._show_front_message(
                QMessageBox.Information,
                "Display Lines",
                "No flight-line data is available in the active point cloud.",
            )
            return

        class LinesDialog(QDialog):
            """Movable frameless selector with resize handles on every edge."""

            RESIZE_MARGIN = 10

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setMouseTracking(True)
                self._drag_offset = None
                self._resize_edges_active = Qt.Edges()
                self._resize_start_position = None
                self._resize_start_geometry = None

            def _resize_edges(self, position):
                edges = Qt.Edges()
                margin = self.RESIZE_MARGIN
                if position.x() <= margin:
                    edges |= Qt.LeftEdge
                elif position.x() >= self.width() - margin:
                    edges |= Qt.RightEdge
                if position.y() <= margin:
                    edges |= Qt.TopEdge
                elif position.y() >= self.height() - margin:
                    edges |= Qt.BottomEdge
                return edges

            def _update_resize_cursor(self, position):
                edges = self._resize_edges(position)
                if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeFDiagCursor)
                elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeBDiagCursor)
                elif edges & (Qt.LeftEdge | Qt.RightEdge):
                    self.setCursor(Qt.SizeHorCursor)
                elif edges & (Qt.TopEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeVerCursor)
                else:
                    self.unsetCursor()

            def mousePressEvent(self, event):
                if event.button() == Qt.LeftButton:
                    edges = self._resize_edges(event.position().toPoint())
                    if edges:
                        handle = self.windowHandle()
                        if handle is not None and handle.startSystemResize(edges):
                            event.accept()
                            return
                        # Some window managers do not implement native resize
                        # for frameless windows, so retain a manual fallback.
                        self._resize_edges_active = edges
                        self._resize_start_position = event.globalPosition().toPoint()
                        self._resize_start_geometry = self.geometry()
                        event.accept()
                        return
                    self._drag_offset = event.globalPosition().toPoint() - self.pos()
                    handle = self.windowHandle()
                    if handle is not None and handle.startSystemMove():
                        self._drag_offset = None
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(self, event):
                if self._resize_edges_active and self._resize_start_geometry is not None:
                    delta = (
                        event.globalPosition().toPoint()
                        - self._resize_start_position
                    )
                    geometry = self._resize_start_geometry
                    left, top = geometry.left(), geometry.top()
                    right, bottom = geometry.right(), geometry.bottom()
                    minimum_width = self.minimumWidth()
                    minimum_height = self.minimumHeight()
                    edges = self._resize_edges_active
                    if edges & Qt.LeftEdge:
                        left = min(left + delta.x(), right - minimum_width + 1)
                    if edges & Qt.RightEdge:
                        right = max(right + delta.x(), left + minimum_width - 1)
                    if edges & Qt.TopEdge:
                        top = min(top + delta.y(), bottom - minimum_height + 1)
                    if edges & Qt.BottomEdge:
                        bottom = max(bottom + delta.y(), top + minimum_height - 1)
                    self.setGeometry(left, top, right - left + 1, bottom - top + 1)
                    event.accept()
                    return
                if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    event.accept()
                    return
                self._update_resize_cursor(event.position().toPoint())
                super().mouseMoveEvent(event)

            def mouseReleaseEvent(self, event):
                self._drag_offset = None
                self._resize_edges_active = Qt.Edges()
                self._resize_start_position = None
                self._resize_start_geometry = None
                self._update_resize_cursor(event.position().toPoint())
                super().mouseReleaseEvent(event)

            def leaveEvent(self, event):
                if not self._resize_edges_active:
                    self.unsetCursor()
                super().leaveEvent(event)

            def resizeEvent(self, event):
                super().resizeEvent(event)
                buttons = getattr(self, "_responsive_buttons", ())
                if not buttons:
                    return
                margins = self.layout().contentsMargins()
                spacing = self._responsive_button_spacing
                usable = (
                    self.contentsRect().width()
                    - margins.left() - margins.right()
                    - spacing * (len(buttons) - 1)
                )
                equal_width = max(56, usable // len(buttons))
                for button in buttons:
                    button.setFixedWidth(equal_width)

        class VisibilityCheckBox(QCheckBox):
            """High-contrast visibility checkbox with an explicit check mark."""

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setFixedSize(22, 22)
                self.setCursor(Qt.PointingHandCursor)

            def paintEvent(self, event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                box = QRectF(2.0, 2.0, 18.0, 18.0)
                if self.isChecked():
                    painter.setPen(QPen(QColor("#69b8ff"), 1.5))
                    painter.setBrush(QColor("#087ff5"))
                    painter.drawRoundedRect(box, 4.0, 4.0)
                    tick = QPainterPath()
                    tick.moveTo(6.0, 11.0)
                    tick.lineTo(9.5, 14.5)
                    tick.lineTo(16.5, 7.0)
                    painter.setPen(QPen(QColor("#ffffff"), 2.2, Qt.SolidLine,
                                        Qt.RoundCap, Qt.RoundJoin))
                    painter.drawPath(tick)
                else:
                    painter.setPen(QPen(QColor("#7b8793"), 1.5))
                    painter.setBrush(QColor("#303840"))
                    painter.drawRoundedRect(box, 4.0, 4.0)
        popup = LinesDialog(self)
        popup.setWindowFlag(Qt.FramelessWindowHint, True)
        popup.setObjectName("flightLinesPopup")
        popup.setWindowTitle("Display Lines")
        popup.setModal(False)
        popup.setWindowModality(Qt.NonModal)
        popup.setAttribute(Qt.WA_DeleteOnClose, True)
        # Keep this as an owned dialog so the application's existing window-
        # state handler hides/restores it with NakshaAI. The size grip makes
        # the frameless popup resizable without adding a native title bar.
        popup.setSizeGripEnabled(True)
        popup.setMinimumSize(344, 250)
        popup.setStyleSheet("""
            QDialog#flightLinesPopup {
                background: #191c1f; border: 1px solid #30363c;
                border-radius: 8px;
            }
            QTableWidget {
                background: #141719; color: #ebebeb;
                border: 1px solid #30363c; border-radius: 5px;
                font-family: 'Segoe UI'; font-size: 14px;
            }
            QTableWidget::item { padding-left: 12px; }
            QHeaderView { background: #202428; }
            QHeaderView::section {
                background: #202428; color: #ebebeb;
                border: none; border-right: 1px solid #30363c;
                border-bottom: 1px solid #30363c;
                padding: 8px; font-family: 'Segoe UI'; font-size: 14px;
            }
            QCheckBox { background: transparent; }
            QScrollBar:vertical {
                background: #191c1f; width: 10px; margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #59636f; border-radius: 3px; min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QPushButton {
                background: #252a30; color: #ebebeb;
                border: 1px solid #363e47; border-radius: 6px;
                padding: 0px; font-family: 'Segoe UI'; font-size: 14px;
            }
            QPushButton:hover { background: #303740; }
            QPushButton:pressed { background: #1c2127; }
            QPushButton:focus { border: 1px solid #8794a2; }
            QPushButton#applyFlightLines {
                background: #0067df; border-color: #2388ff; color: white;
            }
            QPushButton#applyFlightLines:hover { background: #0877ef; }
            QPushButton#applyFlightLines:pressed { background: #0058c4; }
            QPushButton#applyFlightLines:focus { border-color: #b6d8ff; }
        """)
        root = QVBoxLayout(popup)
        root.setContentsMargins(16, 20, 16, 16)
        root.setSpacing(16)

        table = QTableWidget(len(line_ids), 2, popup)
        table.setHorizontalHeaderLabels(["Show", "Flight line / Color"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setFocusPolicy(Qt.NoFocus)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.horizontalHeader().setFixedHeight(40)
        table.horizontalHeader().setMinimumSectionSize(64)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        table.setColumnWidth(0, 80)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setMinimumHeight(40 + min(len(line_ids), 4) * 40 + 2)

        slot = int(getattr(self, "current_slot", 0))
        visibility = dict(
            getattr(app, "flight_line_visibility_by_slot", {}).get(slot, {}) or {}
        )
        colors = dict(getattr(app, "flight_line_colors", {}) or {})
        checks = {}
        for row, lid in enumerate(line_ids):
            check_host = QWidget()
            check_layout = QHBoxLayout(check_host)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignCenter)
            check = VisibilityCheckBox()
            check.setChecked(bool(visibility.get(lid, True)))
            check_layout.addWidget(check)
            table.setCellWidget(row, 0, check_host)
            checks[lid] = check

            rgb = tuple(colors.get(lid, self._flight_line_color(lid)))
            item = QTableWidgetItem(f"Line {lid}")
            item.setForeground(QColor(235, 235, 235))
            item.setBackground(QColor(*rgb))
            # Ensure text remains legible on bright swatches.
            if sum(rgb) > 440:
                item.setForeground(QColor(20, 20, 20))
            table.setItem(row, 1, item)
        root.addWidget(table)

        all_on = QPushButton("All on", popup)
        invert = QPushButton("Invert", popup)
        all_off = QPushButton("All off", popup)
        all_on.clicked.connect(lambda: [c.setChecked(True) for c in checks.values()])
        invert.clicked.connect(lambda: [c.setChecked(not c.isChecked()) for c in checks.values()])
        all_off.clicked.connect(lambda: [c.setChecked(False) for c in checks.values()])
        ok_btn = QPushButton("OK", popup)
        close_btn = QPushButton("Close", popup)
        ok_btn.setObjectName("applyFlightLines")
        ok_btn.setDefault(True)
        close_btn.clicked.connect(popup.reject)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)
        buttons = [all_on, invert, all_off, ok_btn, close_btn]
        for button in buttons:
            # The dialog resize handler recalculates one shared width so all
            # five controls remain equal as the popup grows or shrinks.
            button.setMinimumWidth(56)
            button.setMinimumHeight(36)
            button.setMaximumHeight(44)
            button_layout.addWidget(button, 1)
        popup._responsive_buttons = tuple(buttons)
        popup._responsive_button_spacing = button_layout.spacing()
        root.addLayout(button_layout)

        # Choose a compact initial size from the current screen's available
        # geometry. There is deliberately no fixed width or height: users can
        # resize the popup and the table/button layouts consume the space.
        available = self.screen().availableGeometry()
        preferred_width = max(380, min(620, int(available.width() * 0.34)))
        preferred_height = 20 + 40 + min(len(line_ids), 6) * 40 + 2 + 16 + 40 + 16
        popup.resize(
            min(preferred_width, max(344, available.width() - 32)),
            min(preferred_height, max(250, available.height() - 32)),
        )

        def apply_line_visibility():
            slot_visibility = {
                int(lid): check.isChecked() for lid, check in checks.items()
            }
            by_slot = getattr(app, "flight_line_visibility_by_slot", None)
            if not isinstance(by_slot, dict):
                by_slot = {}
            by_slot[slot] = slot_visibility
            app.flight_line_visibility_by_slot = by_slot
            if slot == 0:
                app.flight_line_visibility = dict(slot_visibility)
            self._line_visibility = dict(slot_visibility)
            # Refresh only the selected view. Other slots retain their own
            # independent flight-line selection.
            try:
                if slot == 0:
                    current_mode = str(getattr(app, "display_mode", "class") or "class").lower()
                    from gui.unified_actor_manager import (
                        fast_main_flight_line_visibility_update,
                        invalidate_unified_actor,
                    )
                    fast_done = (
                        current_mode == "class"
                        and fast_main_flight_line_visibility_update(app)
                    )
                    if not fast_done:
                        invalidate_unified_actor(app)
                        if current_mode == "shaded_class":
                            from gui.shading_display import clear_shading_cache
                            clear_shading_cache("Main View flight-line visibility changed")
                        elif current_mode == "surface":
                            app._surface_visible_class_signature = None
                        if hasattr(app, "set_display_mode"):
                            app.set_display_mode(current_mode)
                elif 1 <= slot <= 4:
                    from gui.unified_actor_manager import build_section_unified_actor
                    view_idx = slot - 1
                    if view_idx in (getattr(app, "section_vtks", {}) or {}):
                        build_section_unified_actor(
                            app,
                            view_idx,
                            border_percent=float(
                                getattr(app, "view_borders", {}).get(slot, 0.0)
                            ),
                        )
            except Exception as exc:
                print(f"⚠️ Flight-line filter refresh failed: {exc}")

        # Applying is intentionally non-destructive to the popup lifecycle:
        # users can inspect the result, adjust lines, and apply again. Only
        # Close (or Escape) dismisses this selector.
        ok_btn.clicked.connect(apply_line_visibility)
        self._lines_popup = popup

        def clear_popup_reference(*_args):
            if getattr(self, "_lines_popup", None) is popup:
                self._lines_popup = None

        popup.finished.connect(clear_popup_reference)
        popup.destroyed.connect(clear_popup_reference)
        popup.show()
        popup.raise_()
        popup.activateWindow()

    @staticmethod
    def _recover_flight_line_ids(app):
        """Recover Point Source IDs for legacy load paths that omitted them.

        This compatibility path is intentionally used only when app.data lacks
        the array.  It reads just the source-ID column in chunks and installs it
        only when its length exactly matches the displayed XYZ array.
        """
        data = getattr(app, "data", None)
        if not isinstance(data, dict) or data.get("xyz") is None:
            return None
        path = getattr(app, "loaded_file", None) or getattr(app, "last_save_path", None)
        if not path or not str(path).lower().endswith((".las", ".laz")):
            return None
        try:
            import laspy
            import numpy as np
            chunks = []
            with laspy.open(str(path)) as reader:
                dims = {str(d).lower() for d in reader.header.point_format.dimension_names}
                if "point_source_id" not in dims:
                    return None
                for points in reader.chunk_iterator(2_000_000):
                    chunks.append(np.asarray(points.point_source_id, dtype=np.uint16).copy())
            recovered = np.concatenate(chunks) if chunks else np.empty(0, dtype=np.uint16)
            if len(recovered) != len(data["xyz"]):
                print(
                    "⚠️ Flight-line recovery skipped: source/data lengths differ "
                    f"({len(recovered):,} != {len(data['xyz']):,})"
                )
                return None
            data["point_source_id"] = recovered
            print(
                f"✅ Recovered flight-line IDs from {os.path.basename(str(path))}: "
                f"{len(np.unique(recovered))} lines"
            )
            return recovered
        except Exception as exc:
            print(f"⚠️ Could not recover flight-line IDs: {exc}")
            return None

    def on_select_all(self):
        for row in range(self.table.rowCount()):
            chk = self.table.cellWidget(row, 0)
            if chk:
                chk.setChecked(True)

    def on_clear_all(self):
        for row in range(self.table.rowCount()):
            chk = self.table.cellWidget(row, 0)
            if chk:
                chk.setChecked(False)

    def sync_with_app_state(self):
        """
        Synchronize the dialog's widgets with the application's actual current state
        without triggering a re-apply or saving settings.
        """
        app = self._get_app_window()
        if not app:
            return

        # Keep explicit Display Mode opens constant-time. Flight-line ID
        # discovery remains deferred until Lines or Line mode is requested.
        self._rebuild_lines_menu(allow_recovery=False)

        current_mode = getattr(app, "display_mode", "class")
        if not current_mode:
            current_mode = "class"
        current_mode = current_mode.lower()

        _MODE_TO_IDX = {
            "class":        0,
            "shaded_class": 1,
            "depth":        2,
            "intensity":    3,
            "rgb":          4,
            "elevation":    5,
            "surface":      6,
            "line":         7,
        }
        target_idx = _MODE_TO_IDX.get(current_mode, 0)

        # Sync color_mode combo box index. app.display_mode is the Main View's
        # own mode flag — only apply it while Main View (slot 0) is selected,
        # or this stomps a cross-section view's independently-remembered mode.
        if hasattr(self, "color_mode") and self.color_mode is not None:
            if self.current_slot == 0 and self.color_mode.currentIndex() != target_idx:
                self.color_mode.blockSignals(True)
                self.color_mode.setCurrentIndex(target_idx)
                self.color_mode.blockSignals(False)
                print(f"✅ DisplayModeDialog: color_mode synchronized to: {current_mode} (index {target_idx})")

        # Also call self._sync_color_mode_state() to update visibility/enabling
        self._sync_color_mode_state()

    def reset_dialog(self):
        """
        Completely reset the dialog state, clearing the table and all palettes.
        Call this when a project is cleared to prevent stale classes from persisting.
        """
        self.table.setRowCount(0)
        self.view_palettes = {i: {} for i in range(6)}
        self.slot_shows = {i: {} for i in range(6)}
        self.current_ptc_path = None
        print("   âœ… DisplayModeDialog state reset")

    def _on_checkbox_toggled_fast(self, row: int, state: int) -> None:
        """
        Bug-9 fix: O(1) checkbox handler â€” row index captured at connect time.
        Replaces the O(n) sender-search loop that caused O(nÂ²) cost during
        bulk _load_slot_checkboxes (n rows Ã— n iterations = nÂ² widget compares).
        GPU sync deliberately withheld until Apply â€” state only written to dicts.
        """
        if getattr(self, "_bulk_ptc_loading", False):
            return
        try:
            code_item = self.table.item(row, 1)
            if code_item is None:
                return
            code       = int(code_item.text())
            is_checked = bool(state)

            if hasattr(self, 'view_palettes'):
                slot_pal = self.view_palettes.get(self.current_slot)
                if slot_pal is not None and code in slot_pal:
                    slot_pal[code]['show'] = is_checked

            if hasattr(self, 'slot_shows'):
                self.slot_shows.setdefault(self.current_slot, {})[code] = is_checked

            app = self._get_app_window()
            if app and hasattr(app, 'view_palettes'):
                app.view_palettes.setdefault(
                    self.current_slot, {}
                ).setdefault(code, {})['show'] = is_checked

            if self.current_slot == 0 and app and hasattr(app, 'class_palette'):
                if code in app.class_palette:
                    app.class_palette[code]['show'] = is_checked

        except Exception:
            pass    # silent â€” checkbox flicker must not interrupt user workflow

    # Keep the old name as a shim so any external connections still resolve.
    def on_checkbox_toggled(self, state):
        if getattr(self, "_bulk_ptc_loading", False):
            return
        sender = self.sender()
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 0) is sender:
                self._on_checkbox_toggled_fast(r, state)
                return

    def connect_existing_checkboxes(self):
        connected = 0
        for row in range(self.table.rowCount()):
            chk = self.table.cellWidget(row, 0)
            if chk:
                try:
                    chk.stateChanged.disconnect()
                except Exception:
                    pass
                # Keep O(1) row-mapped handler wiring (faster + safer for bulk updates).
                chk.stateChanged.connect(
                    lambda state, r=row: self._on_checkbox_toggled_fast(r, state)
                )
                connected += 1
        print(f"   âœ… Connected {connected} checkboxes")

    ###########

    def _ensure_view_borders(self):
        """Guarantee a normalized 0..5 border dictionary at runtime."""
        vb = getattr(self, "view_borders", None)
        if not isinstance(vb, dict):
            vb = {}
        norm = {}
        for i in range(6):
            raw = vb.get(i, vb.get(str(i), 0))
            try:
                norm[i] = float(raw or 0)
            except Exception:
                norm[i] = 0.0
        self.view_borders = norm
        return self.view_borders

    def on_apply(self):   
        from PySide6.QtWidgets import QMessageBox, QApplication, QAbstractItemView, QAbstractItemDelegate
        self._ensure_view_borders()
        
        # Ensure any active editor in the table commits its data
        if self.table.state() == QAbstractItemView.State.EditingState:
            editor = self.table.focusWidget()
            if editor is not None:
                self.table.commitData(editor)
                self.table.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

        visible_count = sum(
            1 for row in range(self.table.rowCount())
            if (chk := self.table.cellWidget(row, 0)) and chk.isChecked()
        )
        if visible_count == 0:
            self._show_front_message(
                QMessageBox.Warning,
                "No Classes Selected",
                "Please select at least one class.",
            )
            return

        idx          = self.color_mode.currentIndex()
        is_class_mode = (idx == 0)  # Only By Classification is a true class mode

        # Remember this slot's chosen mode immediately on Apply — not just on
        # slot-switch (on_slot_changed). Without this, drawing a brand-new
        # cross-section right after Apply (without ever switching the
        # dialog's target-view dropdown away and back) reads a stale
        # view_color_modes entry and silently loses the mode just applied.
        if not hasattr(self, 'view_color_modes'):
            self.view_color_modes = {}
        self.view_color_modes[self.current_slot] = idx

        class_map = {}
        for row in range(self.table.rowCount()):
            try:
                code_item = self.table.item(row, 1)
                if not code_item:
                    continue
                code        = int(code_item.text())
                chk         = self.table.cellWidget(row, 0)
                show        = chk.isChecked() if chk else True
                weight_item = self.table.item(row, 6)
                weight      = float(weight_item.text()) if weight_item else 1.0
                desc        = self.table.item(row, 2).text()
                draw        = self.table.item(row, 3).text()
                lvl         = self.table.item(row, 4).text() if self.table.item(row, 4) else ""
                color       = self.table.item(row, 5).background().color().getRgb()[:3]
                class_map[code] = {
                    "show": show, "description": desc, "draw": draw,
                    "lvl":  lvl,  "color": color,      "weight": weight,
                }
            except Exception:
                continue

        app = self._get_app_window()
        if not app:
            return

        quality_mode = str(self.shading_quality.currentData() or "normal").lower()
        if self.current_slot == 0 and idx in (1, 6) and quality_mode == "slow":
            total_points = len(app.data.get("xyz", [])) if isinstance(getattr(app, "data", None), dict) else 0
            if total_points > 25_000_000:
                mode_name = "Shading" if idx == 1 else "Surface"
                answer = QMessageBox.question(
                    self,
                    f"Slow {mode_name} – All Points",
                    f"Slow {mode_name.lower()} can triangulate up to {total_points:,} loaded points.\n\n"
                    "This may require very large RAM, take several minutes, or fail if "
                    "the GPU/system memory is insufficient. Continue?",
                    QMessageBox.Yes | QMessageBox.Cancel,
                    QMessageBox.Cancel,
                )
                if answer != QMessageBox.Yes:
                    return
        if idx == 1:
            self._shading_quality_value = quality_mode
            app.shading_quality = quality_mode
        elif idx == 6:
            self._surface_quality_value = quality_mode
            app.surface_quality = quality_mode
        if not hasattr(self, 'view_palettes'):
            self.view_palettes = {i: {} for i in range(6)}
        self.view_palettes[self.current_slot] = clone_palette(class_map)
        self._save_slot_checkboxes(self.current_slot)

        if not hasattr(app, 'view_palettes'):
            app.view_palettes = {}
        app.view_palettes[self.current_slot] = clone_palette(class_map)
        app.view_borders = dict(self._ensure_view_borders())

        if self.current_slot == 0:
            app.class_palette = clone_palette(class_map)

            # Keep every other already-seeded view slot's colors/weights/etc.
            # in sync with this Main View edit. Only 'show' (visibility) is
            # meant to differ per view — everything else is meant to be
            # shared. Without this, a cross-section view whose slot palette
            # was already seeded before this edit keeps showing the old
            # color even after re-taking its section, because nothing ever
            # pushed the new value into its (already-persisted) snapshot.
            for other_slot in range(1, 6):
                for target_palettes in (self.view_palettes, app.view_palettes):
                    existing = target_palettes.get(other_slot)
                    if not existing:
                        continue  # not seeded yet — will pick up fresh values when it is
                    for code, entry in class_map.items():
                        target_entry = existing.get(code)
                        if target_entry is None:
                            continue
                        keep_show = target_entry.get('show', entry.get('show', True))
                        target_entry.update(_copy.deepcopy(entry))
                        target_entry['show'] = keep_show

            # ============================================================
            # ACTIVE PTC FOR AI
            # ============================================================
            # Opening/loading a PTC does NOT activate it for AI.
            # Only clicking Display Mode -> Apply activates the PTC.
            if self.current_ptc_path:
                app.active_ptc_schema = clone_palette(class_map)
                app.active_ptc_path = str(self.current_ptc_path)

                print(
                    f"[PTC] AI schema activated by Apply: "
                    f"{app.active_ptc_path} "
                    f"({len(app.active_ptc_schema)} classes)",
                    flush=True,
                )

            if is_class_mode:
                app._main_view_borders_active = (
                    self.view_borders.get(0, 0) > 0
                )
                app.point_border_percent = float(
                    self.view_borders.get(0, 0)
                )
            else:
                app._main_view_borders_active = False
                app.point_border_percent = 0

        # â”€â”€ Track that this slot was explicitly Applied by the user â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not hasattr(app, '_slot_weights_applied'):
            app._slot_weights_applied = set()
        app._slot_weights_applied.add(self.current_slot)

        fast_path_handled = False

        if self.current_slot == 0 and idx == 1:
            # Shaded Classification — trigger the shading backend.
            # Scene ownership (Surface parking, stale edge cleanup and point
            # actor suppression) is centralized in update_shaded_class().
            # Keeping it there makes View-menu Apply, shortcuts, cached restore
            # and programmatic mode switches follow one identical entry path.
            app.display_mode = "shaded_class"
            print("🔳 Borders DISABLED for shaded_class mode (forced to 0%)")
            print("🎨 Display mode → shaded_class")
            try:
                from gui.shading_display import (
                    update_shaded_class, has_cached_geometry
                )
                azimuth = getattr(app, 'last_shade_azimuth', 45.0)
                # IMPORTANT: this argument is the USER-facing facet Sharpness
                # (0..999) in multi-class shading. last_shade_angle stores the
                # internal physical light elevation and must not be fed back as
                # Sharpness when Display Mode re-enters Shading.
                sharpness = getattr(
                    app,
                    'shading_sharpness_angle',
                    getattr(app, 'last_shade_angle', 45.0),
                )
                ambient = getattr(app, 'shade_ambient', 0.25)
                new_vis = set(
                    int(c) for c, e in class_map.items() if e.get("show", True)
                )
                app._shading_visibility_override = new_vis

                # Guard: reuse cached geometry even if the shaded actor was removed.
                _xyz = (app.data.get("xyz")
                        if hasattr(app, 'data') and app.data else None)

                if _xyz is not None and has_cached_geometry(_xyz, new_vis, quality_mode=quality_mode):
                    print("   âš¡ Geometry cached â€” skipping rebuild")
                    update_shaded_class(app, azimuth, sharpness, ambient,
                                        force_rebuild=False)
                else:
                    update_shaded_class(app, azimuth, sharpness, ambient,
                                        force_rebuild=True)
            except Exception as _se:
                print(f"âš ï¸ Shading backend failed: {_se}")
            fast_path_handled = True

        elif self.current_slot == 0 and idx == 6:
            target_mode = "surface"
            app._main_view_borders_active = False
            app.point_border_percent = 0
            if hasattr(app, '_shading_visibility_override'):
                del app._shading_visibility_override
            app.surface_quality = quality_mode
            self._surface_quality_value = quality_mode
            print(f"🎨 Display mode → surface ({quality_mode})")
            if hasattr(app, 'set_display_mode'):
                app.set_display_mode(target_mode)
                QApplication.processEvents()
            else:
                app.display_mode = target_mode
                try:
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(app, target_mode)
                except Exception as _surface_err:
                    print(f"⚠️ Surface mode switch failed: {_surface_err}")
            fast_path_handled = True

        elif self.current_slot == 0 and idx == 7:
            self._rebuild_lines_menu()
            line_visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
            selected = {int(line_id) for line_id, shown in line_visibility.items() if shown}
            if not selected:
                self._show_front_message(
                    QMessageBox.Warning, "No Flight Lines Selected",
                    "Open the Lines menu and select at least one flight line."
                )
                return
            app._main_view_borders_active = False
            app.point_border_percent = 0
            if hasattr(app, 'set_display_mode'):
                app.set_display_mode("line")
                QApplication.processEvents()
            else:
                app.display_mode = "line"
                from gui.pointcloud_display import update_pointcloud
                update_pointcloud(app, "line")
            fast_path_handled = True

        elif self.current_slot == 0 and idx in (2, 3, 4, 5):
            # â”€â”€ Depth / Intensity / RGB / Elevation modes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
            # Map combo index â†’ internal display_mode string
            _IDX_TO_MODE = {2: "depth", 3: "intensity", 4: "rgb", 5: "elevation"}
            target_mode = _IDX_TO_MODE[idx]

            # Borders are not used in these modes
            app._main_view_borders_active = False
            app.point_border_percent = float(self.view_borders.get(0, 0))

            # Clear any shading override
            if hasattr(app, '_shading_visibility_override'):
                del app._shading_visibility_override

            _border_val = app.point_border_percent
            print(f"ðŸŸ³ Border {_border_val}% preserved for {target_mode} mode (uniform only)")
            print(f"ðŸŽ¨ Display mode â†’ {target_mode}")

            # Sync shader visibility LUT â€” respect the user's checked classes.
            # Only the checked (show=True) classes will be rendered in this mode.
            # One GPU uniform upload only, no geometry/actor change.
            try:
                from gui.unified_actor_manager import (
                    _get_unified_actor, _push_uniforms_direct
                )
                _actor = _get_unified_actor(app)
                if _actor is not None:
                    _ctx = getattr(_actor, '_naksha_shader_ctx', None)
                    if _ctx is not None:
                        _base_sz = float(getattr(_actor, '_naksha_base_point_size', 2.5))
                        _ctx.load_from_palette(class_map, _border_val, _base_sz)
                        _push_uniforms_direct(_actor, _ctx)
                        vis_count = sum(1 for v in class_map.values() if v.get('show', True))
                        print(f"âš¡ Shader LUT: {vis_count}/{len(class_map)} classes visible for {target_mode}")
            except Exception as _lut_err:
                print(f"âš ï¸ Shader LUT reset failed: {_lut_err}")

            if hasattr(app, 'set_display_mode'):
                app.set_display_mode(target_mode)
                QApplication.processEvents()
            else:
                # Fallback: set flag and call update_pointcloud directly
                app.display_mode = target_mode
                try:
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(app, target_mode)
                except Exception as _me:
                    print(f"âš ï¸ {target_mode} mode switch failed: {_me}")

            fast_path_handled = True

        elif self.current_slot == 0 and idx == 0:
            target_mode  = "class"
            current_mode = getattr(app, 'display_mode', None)
            # Clear any shading override when switching back to class mode
            if hasattr(app, '_shading_visibility_override'):
                del app._shading_visibility_override

            border = float(self.view_borders.get(0, 0))

            if current_mode != target_mode:
                if hasattr(app, 'set_display_mode'):
                    print(f"âš¡ Mode switch {current_mode} â†’ {target_mode}")
                    app.set_display_mode(target_mode)
                    QApplication.processEvents()
                # âœ… FIX: Apply border AFTER mode switch â€” set_display_mode
                # rebuilds the actor with border=0 (default parameter).
                # Must re-push the user's border value to the GPU.
                if border > 0:
                    _uam_sync(app, 0, class_map, border, render=True)
                    print(f"   âœ… Border {border}% re-applied after mode switch")
                fast_path_handled = True
            else:
                refreshed = _uam_fast_refresh(app, class_map, border)
                if refreshed:
                    print(f"âš¡ Main View: fast_palette_refresh (same mode, no rebuild)")
                else:
                    has_live_data = isinstance(getattr(app, "data", None), dict) and app.data.get("xyz") is not None
                    if not has_live_data:
                        print("â„¹ï¸ No active point-cloud data; saved class settings without GPU rebuild")
                    else:
                        print("âš ï¸ Main View fast refresh unavailable - forcing rebuild")
                        update_class_mode(app, force_refresh=True)
                        # âœ… FIX: Apply border AFTER rebuild â€” update_class_mode
                        # calls build_unified_actor with border=0.
                        if border > 0:
                            _uam_sync(app, 0, class_map, border, render=True)
                            print(f"   âœ… Border {border}% applied after rebuild")
                fast_path_handled = True

        # [CS-MESH-DISPLAY] display-mode apply
        elif self.current_slot in (1, 2, 3, 4) and idx in (1, 6):
            view_idx = self.current_slot - 1
            target_mode = "shaded_class" if idx == 1 else "surface"
            ok = False
            try:
                from gui.cross_section.section_mesh_display import apply_section_display_mode
                ok = apply_section_display_mode(
                    app,
                    view_idx,
                    target_mode,
                    quality_mode=quality_mode,
                    palette=class_map,
                    force=False,
                    reason="display_mode_apply",
                )
                if not ok:
                    print(f"SECTION_MESH view={view_idx + 1} mode={target_mode} status=apply_failed")
            except Exception as _cs_mesh_err:
                print(f"SECTION_MESH view={view_idx + 1} mode={target_mode} status=apply_exception reason={_cs_mesh_err}")
                ok = False
            fast_path_handled = bool(ok)
            if not ok:
                # Fallback: gui.cross_section.section_mesh_display isn't
                # available/working yet -- use our own mesh-slab-clip
                # implementation (gui.cross_section.section_shaded_surface)
                # instead, so Shaded/Surface still works for sections today.
                from gui.cross_section.section_shaded_surface import build_section_shaded_surface_actor
                mesh_mode = "shaded" if idx == 1 else "surface"
                ok2 = build_section_shaded_surface_actor(app, view_idx, mesh_mode)
                if ok2:
                    fast_path_handled = True
                    print(f"Section {view_idx + 1} display mode -> {mesh_mode} (mesh cut, fallback)")
                else:
                    print(f"WARNING: Section {view_idx + 1} {mesh_mode} mesh-cut unavailable (fallback also failed)")

        elif self.current_slot >= 1:
            if self.current_slot <= 4:
                view_idx = self.current_slot - 1
                # [CS-MESH-DISPLAY] leave mesh before existing section renderer
                try:
                    from gui.cross_section.section_mesh_display import leave_section_mesh_mode
                    _section_mode_map = {
                        0: "class", 2: "depth", 3: "intensity", 4: "rgb",
                        5: "elevation", 7: "line",
                    }
                    _next_section_mode = _section_mode_map.get(idx, "class")
                    leave_section_mesh_mode(
                        app, view_idx, next_mode=_next_section_mode, render=False
                    )
                except Exception as _cs_leave_err:
                    print(f"SECTION_MESH view={view_idx + 1} status=leave_failed reason={_cs_leave_err}")
                border   = float(self.view_borders.get(self.current_slot, 0))
                # idx in (1, 6) (Shaded/Surface) for slots 1-4 is handled by
                # the "[CS-MESH-DISPLAY] display-mode apply" elif above --
                # this branch only ever sees the remaining section modes.
                from gui.cross_section.section_shaded_surface import (
                    remove_section_shaded_surface_actor,
                )
                remove_section_shaded_surface_actor(app, view_idx)
                _SECTION_IDX_TO_MODE = {2: "depth", 3: "intensity", 4: "rgb", 5: "elevation", 7: "line"}
                section_mode = _SECTION_IDX_TO_MODE.get(idx, "class")
                ok = _uam_refresh_section(app, view_idx, class_map, border, section_mode)
                if ok:
                    fast_path_handled = True
                    print(f"Section {view_idx + 1} display mode -> {section_mode}")
                else:
                    print(f"WARNING: Section {view_idx + 1} fast-refresh failed -- may need rebuild")
            elif self.current_slot == 5:
                if hasattr(app, 'cut_section_controller'):
                    ctrl = app.cut_section_controller
                    if hasattr(ctrl, 'apply_palette'):
                        ctrl.apply_palette(class_map)
                fast_path_handled = True

        # Bug-7 fix: emit palette_changed ONLY when the fast path did NOT already
        # push to GPU.  Unconditional emit caused a second full sync_palette_to_gpu
        if not fast_path_handled:
            self.palette_changed.emit(self.current_slot)
            payload = {
                "classes":        class_map,
                "slot":           self.current_slot,
                "target_view":    self.current_slot,
                "force_refresh":  True,
                "color_mode":     idx,
                "border_percent": (self.view_borders.get(self.current_slot, 0)
                                   if is_class_mode else 0),
            }
            self.applied.emit(payload)

        # Main View owns the canonical classification schema. Notify any
        # open picker after Add/Edit/Delete + Apply so its choices refresh.
        if self.current_slot == 0:
            self.classes_loaded.emit()

        if hasattr(app, 'statusBar'):
            view_names = ["Main View", "View 1", "View 2", "View 3", "View 4"]
            v_name = (view_names[self.current_slot]
                      if self.current_slot < len(view_names)
                      else f"View {self.current_slot}")
            app.statusBar().showMessage(f"Applied to {v_name}", 2000)

        # Bug-8 fix: debounce registry flush â€” no longer blocks Apply hot-path.
        # _save_timer fires 2 s after the last Apply click, not on every click.
        if hasattr(self, '_save_timer'):
            self._save_timer.start()

    def _on_header_double_clicked(self, logical_index):
        if logical_index != 6:
            return
        if not getattr(self, 'current_ptc_path', None):
            return
        from PySide6.QtCore import QPoint
        header = self.table.horizontalHeader()
        x = header.sectionViewportPosition(logical_index)
        y = header.height()
        global_pos = header.mapToGlobal(QPoint(x, y))
        self._weight_popup.show_at(global_pos)

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, '_weight_popup') and self._weight_popup.isVisible():
            delta = event.pos() - event.oldPos()
            self._weight_popup.reposition(delta)

    def closeEvent(self, event):
        # On real close flush immediately; on hide just stop the timer.
        if hasattr(self, '_save_timer'):
            self._save_timer.stop()
        parent = self._get_app_window()
        if getattr(self, "_allow_native_close", False) or getattr(parent, "_shutdown_in_progress", False):
            self.save_global_settings()      # blocking flush only on true app exit
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()

    def hideEvent(self, event):
        self.save_global_settings()
        super().hideEvent(event)

    def reject(self):
        if hasattr(self, '_save_timer'):
            self._save_timer.stop()
        self.save_global_settings()
        self.hide()

    def _sync_with_owner_window_state(self):
        """Mirror NakshaAI minimize/restore without changing dialog lifetime.

        Important ownership rules:
          * clicking another window/app never hides or closes Display Mode;
          * minimizing NakshaAI minimizes an open Display Mode window;
          * restoring NakshaAI restores only a dialog that NakshaAI minimized;
          * a dialog manually minimized by the user stays minimized;
          * owner restore never raises/activates Display Mode.
        """
        owner = self._get_app_window()
        if owner is None:
            return

        try:
            owner_minimized = bool(owner.windowState() & Qt.WindowMinimized)
        except Exception:
            return

        if owner_minimized:
            try:
                if not self.isVisible():
                    return
                # If the user had already minimized Display Mode manually, do
                # not claim ownership of that state and do not auto-restore it.
                if self.windowState() & Qt.WindowMinimized:
                    return

                self._was_visible_before_owner_minimize = True
                self._owner_minimized_me = True
                self._owner_state_sync = True
                try:
                    self.showMinimized()
                    # Same fix as the restore branch below: showMinimized()
                    # only changes this dialog's own window state, it does
                    # not guarantee Qt's keyboard-focus target moves off of
                    # whatever widget inside this dialog last held it -- if
                    # nothing else claims focus, shortcuts like Ctrl+Z can
                    # go nowhere (not even reach the global event filter)
                    # until the user manually clicks the viewport. Hand
                    # focus back to Main View's own render widget here too.
                    try:
                        vtk_widget = getattr(owner, "vtk_widget", None)
                        if vtk_widget is not None:
                            vtk_widget.setFocus()
                    except Exception:
                        pass
                finally:
                    self._owner_state_sync = False
                print("DISPLAY_MODE_WINDOW owner=minimized action=minimize_dialog")
            except Exception:
                self._owner_state_sync = False
            return

        # Owner returned to normal. Restore only an owner-forced minimize.
        if self._owner_minimized_me and self._was_visible_before_owner_minimize:
            try:
                self._owner_state_sync = True
                try:
                    self.showNormal()
                    # Do not steal focus from the just-restored NakshaAI window.
                    # A later explicit Display Mode command may raise it again.
                    self.lower()
                    # showNormal()/lower() only change stacking order, not Qt's
                    # internal keyboard-focus target. If some widget inside
                    # this dialog still held focus, Ctrl+Z/Ctrl+Y (and other
                    # main-window shortcuts) silently went nowhere until the
                    # user clicked the viewport manually -- explicitly hand
                    # focus back to Main View's own render widget so shortcuts
                    # keep working right after the owner window is restored.
                    try:
                        vtk_widget = getattr(owner, "vtk_widget", None)
                        if vtk_widget is not None:
                            vtk_widget.setFocus()
                    except Exception:
                        pass
                finally:
                    self._owner_state_sync = False
                print("DISPLAY_MODE_WINDOW owner=restored action=restore_dialog_background")
            except Exception:
                self._owner_state_sync = False
            finally:
                self._owner_minimized_me = False
                self._was_visible_before_owner_minimize = False

    def _raise_visible_window_family(self):
        """Bring Display Mode and its currently open child popups forward."""
        try:
            if not self.isVisible() or self.windowState() & Qt.WindowMinimized:
                return
        except RuntimeError:
            return

        windows = [self]
        try:
            for child in self.findChildren(QWidget):
                if not child.isWindow() or not child.isVisible():
                    continue
                if child.windowState() & Qt.WindowMinimized:
                    continue
                windows.append(child)
        except RuntimeError:
            pass

        active_window = None
        for window in windows:
            try:
                window.raise_()
                active_window = window
            except RuntimeError:
                continue

        if active_window is not None:
            try:
                active_window.activateWindow()
            except RuntimeError:
                pass

    def _schedule_window_family_restore(self):
        """Defer foreground restoration until Naksha owns the native focus."""
        if self._user_minimized:
            return
        try:
            if not self.isVisible():
                return
        except RuntimeError:
            return
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._raise_visible_window_family)

    def eventFilter(self, obj, event):
        owner = self._get_app_window()
        if obj is owner and event is not None:
            try:
                event_type = event.type()
                if event_type == QEvent.WindowStateChange:
                    self._sync_with_owner_window_state()
                elif event_type == QEvent.WindowActivate:
                    self._schedule_window_family_restore()
                elif event_type == QEvent.Close:
                    # App shutdown owns the true native close. Normal user X on
                    # Display Mode itself is still handled by closeEvent below.
                    self._allow_native_close = True
            except Exception:
                pass
        return super().eventFilter(obj, event)

    def show_safely(self):
        """Show only on an explicit Display Mode request.

        This is the ONLY place we intentionally raise/activate the dialog.
        Apply, shortcuts and focus changes never call hide/close here.
        """
        try:
            if self.windowFlags() & Qt.WindowStaysOnTopHint:
                self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        except Exception:
            pass
        self.setWindowFlag(Qt.Tool, False)

        if self.windowState() & Qt.WindowMinimized:
            self.showNormal()
        else:
            self.show()

        self._user_minimized = False
        self._owner_minimized_me = False
        self._was_visible_before_owner_minimize = False
        self._sync_color_mode_state()

        # Explicit open should come to the front; after this, native OS z-order
        # is respected and any clicked window is free to move above it.
        self.raise_()
        self.activateWindow()
        try:
            self.setFocus(Qt.ActiveWindowFocusReason)
        except Exception:
            pass
        print("DISPLAY_MODE_WINDOW action=explicit_show topmost=0")

    def changeEvent(self, event):
        """Track manual minimize separately from owner-forced minimize."""
        super().changeEvent(event)
        try:
            if event.type() == QEvent.WindowStateChange:
                minimized = bool(self.windowState() & Qt.WindowMinimized)
                if not self._owner_state_sync:
                    self._user_minimized = minimized
                    if minimized:
                        # Manual minimize must never be auto-restored by NakshaAI.
                        self._owner_minimized_me = False
                        self._was_visible_before_owner_minimize = False
                        print("DISPLAY_MODE_WINDOW action=user_minimized")
        except Exception:
            pass

    

    def _handle_close(self):
        self.save_global_settings()
        self.hide()

    def save_global_settings(self):
        """Persist palettes, weights, show-states, borders, PTC path to QSettings."""
        try:
            from PySide6.QtCore import QSettings
            import os

            settings = QSettings("NakshaAI", "LidarApp")

            # Always save geometry and column widths of the dialog, even if a temporary tool blocks saving full settings
            try:
                # Safely retrieve geometry and column widths only if the widgets are not deleted
                if hasattr(self, 'table') and self.table:
                    column_widths = [self.table.columnWidth(i) for i in range(self.table.columnCount())]
                    settings.setValue("display_mode_table_column_widths", column_widths)
                settings.setValue("display_mode_dialog_geometry", self.saveGeometry())
                settings.sync()
            except RuntimeError:
                # Catch RuntimeError when PySide6 objects are already destroyed/deleted during app shutdown
                pass
            except Exception as e:
                print(f"âš ï¸ Failed to save geometry/column widths: {e}")

            # â”€â”€ Guard: do NOT save while a temporary tool is active â”€â”€
            # The palette may be in a transient state (e.g. classification
            # preview, cut-from-cross selection) that should not be persisted.
            _TEMP_TOOLS = {
                "above_line", "below_line", "brush", "freehand",
                "cut_from_cross", "cut_from_cut", "cross_section",
            }
            app = self._get_app_window()
            if app:
                active = getattr(app, 'active_tool', None)
                if active in _TEMP_TOOLS:
                    print(f"[GLOBAL-SAVE-BLOCKED] active temporary tool '{active}'; "
                          f"not saving display settings")
                    return
            # â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

            # 1. Save current table edits into view_palettes for the active slot
            if self.current_slot not in self.view_palettes:
                self.view_palettes[self.current_slot] = {}
            for row in range(self.table.rowCount()):
                try:
                    code_item = self.table.item(row, 1)
                    if not code_item:
                        continue
                    code        = int(code_item.text())
                    chk         = self.table.cellWidget(row, 0)
                    show        = chk.isChecked() if chk else True
                    weight_item = self.table.item(row, 6)
                    weight      = float(weight_item.text()) if weight_item else 1.0
                    desc        = self.table.item(row, 2).text() if self.table.item(row, 2) else ''
                    lvl_item    = self.table.item(row, 4)
                    lvl         = lvl_item.text() if lvl_item else ''
                    color       = self.table.item(row, 5).background().color().getRgb()[:3] if self.table.item(row, 5) else (128, 128, 128)
                    self.view_palettes[self.current_slot][code] = {
                        'show': show, 'description': desc,
                        'lvl':  lvl,
                        'color': tuple(color), 'weight': weight,
                    }
                except Exception:
                    continue

            # 2. Serialize all slot palettes (includes weights)
            palettes_to_save = {}
            for view_idx, palette in self.view_palettes.items():
                if not palette:
                    continue
                slot_dict = {}
                for code, info in palette.items():
                    slot_dict[str(code)] = {
                        'show':        bool(info.get('show', True)),
                        'description': str(info.get('description', '')),
                        'lvl':         str(info.get('lvl', '')),
                        'color':       list(info.get('color', (128, 128, 128))),
                        'weight':      float(info.get('weight', 1.0)),
                    }
                palettes_to_save[str(view_idx)] = slot_dict
            if palettes_to_save:
                settings.setValue("global_view_palettes", palettes_to_save)

            # 3. Save checkbox states
            self._save_slot_checkboxes(self.current_slot)
            shows_to_save = {
                str(slot): {str(code): bool(v) for code, v in show_dict.items()}
                for slot, show_dict in self.slot_shows.items()
            }
            if shows_to_save:
                settings.setValue("global_slot_shows", shows_to_save)

            # 4. Save border values
            settings.setValue("global_view_borders",
                              {str(k): v for k, v in self.view_borders.items()})

            # 5. Save PTC path
            if self.current_ptc_path and os.path.exists(self.current_ptc_path):
                settings.setValue("global_last_ptc_path", self.current_ptc_path)

            # 6. Save color mode & structured border mode
            settings.setValue("global_color_mode", self.color_mode.currentIndex())
            # Persist Shading and Surface Speed independently even though they share one UI combo.
            mode_idx = int(self.color_mode.currentIndex())
            current_quality = str(self.shading_quality.currentData() or "normal")
            if mode_idx == 1:
                self._shading_quality_value = current_quality
            elif mode_idx == 6:
                self._surface_quality_value = current_quality
            settings.setValue("global_shading_quality", getattr(self, "_shading_quality_value", "normal"))
            settings.setValue("global_surface_quality", getattr(self, "_surface_quality_value", "normal"))
            
            if self.border_logic_hybrid.isChecked():
                mode_val = 2
            elif self.border_logic_object.isChecked():
                mode_val = 1
            else:
                mode_val = 0
            settings.setValue("global_border_logic_mode", mode_val)
            settings.setValue("global_structured_border", str(self.border_logic_object.isChecked()))



            settings.sync()
            print(f"âœ… Global display settings saved ({len(palettes_to_save)} palette slots)")
        except Exception as e:
            print(f"âš ï¸ save_global_settings failed: {e}")

    def get_visible_classes_for_view(self, view_idx):
        try:
            # ALWAYS prefer view_palettes â€” this is the GPU source of truth
            if hasattr(self, 'view_palettes') and view_idx in self.view_palettes:
                palette = self.view_palettes[view_idx]
                return [code for code, info in palette.items() if info.get('show', True)]

            # Fallback: read from live table ONLY if view_palettes not populated yet
            # (e.g. before first Apply click)
            if view_idx == self.current_slot:
                visible = []
                for row in range(self.table.rowCount()):
                    chk = self.table.cellWidget(row, 0)
                    if chk and chk.isChecked():
                        item = self.table.item(row, 1)
                        if item:
                            try:
                                code = int(item.text().strip())  # .strip() prevents whitespace bugs
                                visible.append(code)
                            except ValueError:
                                print(f"      âš ï¸ Bad class code in table row {row}: '{item.text()}'")
                return visible

            if hasattr(self, 'slot_shows') and view_idx in self.slot_shows:
                return [code for code, checked in self.slot_shows[view_idx].items() if checked]

            return None

        except Exception as e:
            print(f"      âŒ get_visible_classes_for_view error: {e}")
            return None ##

    def _check_pending_restores(self):
        app = self._get_app_window()
        if not app:
            return

        print(f"\n{'=' * 40}")
        print("ðŸ“¥ CHECKING PENDING RESTORES")

        pending_ptc = getattr(app, '_pending_ptc_restore', None) or \
                      getattr(app, 'pending_ptc_restore', None)
        if pending_ptc:
            if getattr(app, '_block_ptc_autoload', False):
                print(" Pending PTC restore blocked (shortcut in progress)")
                if hasattr(app, '_pending_ptc_restore'):
                    del app._pending_ptc_restore
                if hasattr(app, 'pending_ptc_restore'):
                    del app.pending_ptc_restore
            else:
                ptc_path = pending_ptc
                print(f" ðŸ“„ Found pending PTC: {os.path.basename(ptc_path)}")
                if os.path.exists(ptc_path):
                    self.load_classes_from_path(ptc_path)
                if hasattr(app, '_pending_ptc_restore'):
                    del app._pending_ptc_restore
                if hasattr(app, 'pending_ptc_restore'):
                    del app.pending_ptc_restore
        pending_shows = None
        if hasattr(app, '_pending_checkbox_states'):
            pending_shows = app._pending_checkbox_states
        elif hasattr(app, 'pending_checkbox_states'):
            pending_shows = app.pending_checkbox_states

        if pending_shows:
            saved_shows = pending_shows
            self.slot_shows = {}
            for slot_idx_str, show_dict in saved_shows.items():
                slot_idx = int(slot_idx_str)
                self.slot_shows[slot_idx] = {
                    int(code): checked for code, checked in show_dict.items()
                }
            self._load_slot_checkboxes(self.current_slot)
            if hasattr(self, 'view_palettes'):
                for slot_idx, shows in self.slot_shows.items():
                    if slot_idx in self.view_palettes:
                        for code, is_visible in shows.items():
                            if code in self.view_palettes[slot_idx]:
                                self.view_palettes[slot_idx][code]['show'] = is_visible
            if hasattr(app, '_pending_checkbox_states'):
                del app._pending_checkbox_states
            if hasattr(app, 'pending_checkbox_states'):
                del app.pending_checkbox_states
            print(" âœ… Checkbox states applied")

        if hasattr(app, '_pending_color_mode'):
            mode_idx = app._pending_color_mode
            self.color_mode.setCurrentIndex(mode_idx)
            del app._pending_color_mode
        self._sync_color_mode_state()

        if hasattr(app, 'pending_border_values'):
            border_values  = app.pending_border_values
            self.view_borders = {int(k): v for k, v in border_values.items()}
            self.load_view_border(self.current_slot)
            del app.pending_border_values

        print(f"{'=' * 40}\n")

    def _save_slot_weights(self, slot_idx: int) -> None:
        """Shim â€” delegates to unified _save_slot_state (Bug-10 fix)."""
        self._save_slot_state(slot_idx)

    def _load_slot_weights(self, slot_idx: int) -> None:
        """Shim â€” delegates to unified _load_slot_state (Bug-10 fix)."""
        self._load_slot_state(slot_idx)

    def sync_weights_to_all_views(self):
        if not hasattr(self, 'view_palettes'):
            return
        weight_map = {}
        for row in range(self.table.rowCount()):
            try:
                code        = int(self.table.item(row, 1).text())
                weight_item = self.table.item(row, 6)
                if weight_item:
                    weight_map[code] = float(weight_item.text())
            except Exception:
                continue

        for view_idx in range(1, 5):
            if view_idx not in self.view_palettes:
                continue
            for code, weight in weight_map.items():
                if code in self.view_palettes[view_idx]:
                    self.view_palettes[view_idx][code]['weight'] = weight

        app = self._get_app_window()
        if app and hasattr(app, 'view_palettes'):
            for view_idx in range(1, 5):
                if view_idx in self.view_palettes:
                    if view_idx not in app.view_palettes:
                        app.view_palettes[view_idx] = {}
                    for code, info in self.view_palettes[view_idx].items():
                        if code not in app.view_palettes[view_idx]:
                            app.view_palettes[view_idx][code] = {}
                        app.view_palettes[view_idx][code]['weight'] = info.get('weight', 1.0)

    def increase_border(self):
        current_border = self.view_borders.get(self.current_slot, 0)
        new_border     = min(100, current_border + 5)
        self.view_borders[self.current_slot] = new_border
        self.update_border_display()
        app = self._get_app_window()
        if app:
            if not hasattr(app, 'view_borders'):
                app.view_borders = {i: 0 for i in range(6)}
            app.view_borders[self.current_slot] = new_border
            self._push_border_to_gpu(app, self.current_slot, new_border)
        self.border_changed.emit(self.current_slot, float(new_border), int(self.get_border_mode()))

    def decrease_border(self):
        current_border = self.view_borders.get(self.current_slot, 0)
        new_border     = max(0, current_border - 5)
        self.view_borders[self.current_slot] = new_border
        self.update_border_display()
        app = self._get_app_window()
        if app:
            if not hasattr(app, 'view_borders'):
                app.view_borders = {i: 0 for i in range(6)}
            app.view_borders[self.current_slot] = new_border
            self._push_border_to_gpu(app, self.current_slot, new_border)
        self.border_changed.emit(self.current_slot, float(new_border), int(self.get_border_mode()))

    def _push_border_to_gpu(self, app, slot_idx, border_value):
        """Immediately push border change to GPU without needing Apply click."""
        try:
            float_border = float(border_value)

            if slot_idx == 0:
                app.point_border_percent = float_border
                _uam_sync(app, 0, border=float_border, render=True)
            else:
                _uam_sync(app, slot_idx, border=float_border, render=True)

        except Exception as e:
            print(f"⚠️ _push_border_to_gpu failed (slot={slot_idx}): {e}")

    def get_border_mode(self) -> float:
        """Single authoritative read of border logic mode for GPU use.
        Returns 2.0=Hybrid, 1.0=Structured, 0.0=Per-Point.
        Never read isChecked() directly outside this method."""
        if getattr(self, 'border_logic_hybrid', None) and self.border_logic_hybrid.isChecked():
            return 2.0
        if getattr(self, 'border_logic_object', None) and self.border_logic_object.isChecked():
            return 1.0
        return 0.0

    def _on_border_mode_changed(self):
        app = self._get_app_window()
        if app:
            current_border = self.view_borders.get(self.current_slot, 0)
            self._push_border_to_gpu(app, self.current_slot, current_border)
        self.border_changed.emit(
            self.current_slot,
            float(self.view_borders.get(self.current_slot, 0)),
            int(self.get_border_mode()),
        )

    def on_border_value_changed(self):
        pass
    def update_border_display(self):
        if not hasattr(self, 'view_borders') or self.view_borders is None:
            self.view_borders = {i: 0 for i in range(6)}
        value = self.view_borders.get(self.current_slot, 0)
        self.border_label.setText("Border")
        self.border_value_display.setText(f"{int(value)}%")

    def load_view_border(self, view_idx):
        if not hasattr(self, 'view_borders'):
            self.view_borders = {i: 0 for i in range(6)}
        border_value = self.view_borders.get(view_idx, 0)
        self.border_label.setText("Border")
        self.border_value_display.setText(f"{int(border_value)}%")

    def on_view_switched_to_cut_section(self):
        if self.current_slot != 5:
            return
        app = self._get_app_window()
        if not app or not hasattr(app, 'cut_section_controller'):
            return
        ctrl = app.cut_section_controller
        if hasattr(ctrl, 'cut_palette') and ctrl.cut_palette:
            if not hasattr(self, 'view_palettes'):
                self.view_palettes = {}
            existing_palette = self.view_palettes.get(5, {})
            # Deep-copy the source so slot 5 never shares objects with
            # the cut controller's palette or with any cross-section slot.
            source = clone_palette(ctrl.cut_palette)
            self.view_palettes[5] = {}
            for code, info in source.items():
                code = int(code)
                preserved_weight = (
                    existing_palette[code]['weight']
                    if code in existing_palette and 'weight' in existing_palette[code]
                    else info.get('weight', 1.0)
                )
                self.view_palettes[5][code] = {
                    'show':        info.get('show', True),
                    'description': info.get('description', ''),
                    'lvl':         str(info.get('lvl', '')),
                    'color':       tuple(info.get('color', (128, 128, 128))),
                    'weight':      preserved_weight
                }
            self._load_slot_checkboxes(5)
            self._load_slot_weights(5)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# EditClassDialog
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class EditClassDialog(InputPopupMixin, QDialog):
    weight_applied = Signal(float)

    def __init__(self, code=0, desc="", color=QColor("white"), parent=None,
                 draw="Not set", lvl="", weight=2.0):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.setWindowTitle("Edit Class")
        self.setStyleSheet(get_dialog_stylesheet())
        self.color           = color
        self.default_weight  = float(weight)
        self.current_weight  = float(weight)
        self.parent_dialog   = parent

        layout = QVBoxLayout(self)

        self.code_edit = QLineEdit(str(code))
        self.desc_edit = QLineEdit(desc)
        self.draw_edit = QLineEdit(draw)
        self.lvl_edit  = QLineEdit(str(lvl))

        self.color_btn = QPushButton("Pick Color")
        self.color_btn.clicked.connect(self.pick_color)

        weight_row = QHBoxLayout()
        self.weight_label      = QLabel("Weight:")
        self.weight_edit       = QLineEdit(f"{self.current_weight:.2f}")
        self.weight_edit.setFixedWidth(60)
        self.weight_edit.setAlignment(Qt.AlignCenter)
        self.apply_weight_btn  = QPushButton("Apply")
        self.reset_weight_btn  = QPushButton("Reset")
        self.apply_weight_btn.clicked.connect(self.apply_weight)
        self.reset_weight_btn.clicked.connect(self.reset_weight)
        weight_row.addWidget(self.weight_label)
        weight_row.addWidget(self.weight_edit)
        weight_row.addWidget(self.apply_weight_btn)
        weight_row.addWidget(self.reset_weight_btn)

        from PySide6.QtWidgets import QFormLayout
        form_layout = QFormLayout()
        form_layout.addRow("Code:", self.code_edit)
        form_layout.addRow("Description:", self.desc_edit)
        form_layout.addRow("Draw:", self.draw_edit)
        form_layout.addRow("Lvl:", self.lvl_edit)
        form_layout.addRow("Color:", self.color_btn)
        form_layout.addRow(weight_row)
        
        layout.addLayout(form_layout)

        row    = QHBoxLayout()
        ok     = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        row.addWidget(ok)
        row.addWidget(cancel)
        layout.addLayout(row)

    def pick_color(self):
        col = QColorDialog.getColor(self.color, self, "Pick Class Color")
        if col.isValid():
            self.color = col

   

    def apply_weight(self):
        """ðŸš€ PRODUCTION FIX: Pokes GPU uniforms when weight changes"""
        try:
            # 1. Validate input
            try:
                new_weight = float(self.weight_edit.text())
            except ValueError:
                self.weight_edit.setText(f"{self.current_weight:.2f}")
                return

            # 2. Safety Clamp (0.1x to 12.0x)
            new_weight = max(0.1, min(new_weight, 12.0))
            self.current_weight = new_weight
            self.weight_edit.setText(f"{new_weight:.2f}")

            if not isinstance(self.parent_dialog, DisplayModeDialog):
                return

            # 3. Setup Context
            parent_table = self.parent_dialog.table
            code = int(self.code_edit.text())
            current_slot = self.parent_dialog.current_slot
            app = self.parent_dialog.parent()

            # 4. Update UI Table
            for row in range(parent_table.rowCount()):
                try:
                    if int(parent_table.item(row, 1).text()) == code:
                        weight_item = parent_table.item(row, 6)
                        if weight_item:
                            weight_item.setText(f"{new_weight:.2f}")
                        break
                except Exception:
                    continue

            # 5. Synchronize Palettes in Memory
            if not hasattr(self.parent_dialog, 'view_palettes'):
                self.parent_dialog.view_palettes = {i: {} for i in range(6)}
            
            if current_slot in self.parent_dialog.view_palettes:
                if code in self.parent_dialog.view_palettes[current_slot]:
                    self.parent_dialog.view_palettes[current_slot][code]['weight'] = new_weight

            if app and hasattr(app, 'view_palettes'):
                if current_slot not in app.view_palettes:
                    app.view_palettes[current_slot] = {}
                if code not in app.view_palettes[current_slot]:
                    app.view_palettes[current_slot][code] = {}
                app.view_palettes[current_slot][code]['weight'] = new_weight

            # Keep the active main-view palette in sync before emitting
            # palette_changed. Slot 0 GPU refreshes read from app.class_palette,
            # so if only view_palettes[0] changes the next sync can restore the
            # old weight and make the update appear flaky.
            if current_slot == 0 and app:
                if not hasattr(app, 'view_palettes') or app.view_palettes is None:
                    app.view_palettes = {}
                if not hasattr(app, 'class_palette') or app.class_palette is None:
                    app.class_palette = {}

                slot_palette = self.parent_dialog.view_palettes.get(current_slot, {})
                slot_entry = dict(slot_palette.get(code, {}))
                if not slot_entry:
                    slot_entry = dict(app.class_palette.get(code, {}))

                slot_entry['weight'] = new_weight
                slot_entry.setdefault('show', True)
                slot_entry.setdefault('color', (128, 128, 128))
                slot_entry.setdefault('description', '')

                app.class_palette[code] = slot_entry
                app.view_palettes[current_slot][code] = dict(slot_entry)

            # ðŸš€ THE CRITICAL GPU POKE:
            # We use the local helper functions defined at the top of display_mode.py
            # to bypass circular imports and talk directly to the shader.
            palette = self.parent_dialog.view_palettes.get(current_slot, {})
            
            try:
                refreshed = False
                if current_slot == 0:
                    # Main View Path
                    border = float(getattr(app, 'point_border_percent', 0.0) or 0.0)
                    refreshed = _uam_fast_refresh(app, palette, border)
                    if not refreshed and app is not None:
                        print("âš ï¸ Main View GPU poke unavailable - forcing rebuild")
                        update_class_mode(app, force_refresh=True)
                        refreshed = True
                else:
                    # Cross-Section Path
                    view_idx = current_slot - 1
                    border = float(self.parent_dialog.view_borders.get(current_slot, 0.0))
                    refreshed = _uam_refresh_section(app, view_idx, palette, border)

                # Mark slot as explicitly weight-applied
                if not hasattr(app, '_slot_weights_applied'):
                    app._slot_weights_applied = set()
                app._slot_weights_applied.add(current_slot)

                # Final hardware poke to force the shader to re-read the LUT
                self.parent_dialog.palette_changed.emit(current_slot)
                print(f"âš¡ GPU Uniform Poke: Slot {current_slot} weights synchronized")

            except Exception as gpu_err:
                print(f"âš ï¸ GPU Weight Poke failed: {gpu_err}")

            # 6. UI FeedbackS
            if app and hasattr(app, 'statusBar'):
                app.statusBar().showMessage(f"âœ¨ Weight {new_weight:.2f}x applied to Slot {current_slot}", 1500)

        except Exception as e:
            print(f"âŒ critical error in apply_weight: {e}")####
        

    def _highlight_row(self, table, row):
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QColor

        original_colors = []
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item:
                original_colors.append((col, item.background()))

        highlight_color = QColor(255, 255, 0, 180)
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item and col != 5:
                item.setBackground(highlight_color)

        def restore_colors():
            for col, original_color in original_colors:
                item = table.item(row, col)
                if item:
                    item.setBackground(original_color)

        QTimer.singleShot(2000, restore_colors)

    def reset_weight(self):
        self.current_weight = self.default_weight
        self.weight_edit.setText(f"{self.default_weight:.2f}")

    def code(self):    return int(self.code_edit.text())
    def desc(self):    return self.desc_edit.text()
    def draw(self):    return self.draw_edit.text()
    def lvl(self):     return self.lvl_edit.text()
    def weight(self):  return self.current_weight
