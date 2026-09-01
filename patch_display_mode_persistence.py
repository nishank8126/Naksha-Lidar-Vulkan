from __future__ import annotations

import argparse
import datetime as _dt
import re
import shutil
from pathlib import Path

DEFAULT_PATH = Path(r"E:\NAKSHA-LIDAR\gui\display_mode.py")


def _read_source(path: Path):
    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8-sig")
    text = text.replace("\r\n", "\n")
    return text, had_bom, newline


def _write_source(path: Path, text: str, had_bom: bool, newline: str):
    out = text.replace("\n", newline)
    encoding = "utf-8-sig" if had_bom else "utf-8"
    path.write_text(out, encoding=encoding, newline="")


def _method_span(text: str, method_name: str):
    start_re = re.compile(rf"(?m)^    def {re.escape(method_name)}\b")
    m = start_re.search(text)
    if not m:
        raise RuntimeError(f"Could not find DisplayModeDialog.{method_name}()")

    next_re = re.compile(r"(?m)^    def [A-Za-z_][A-Za-z0-9_]*\b")
    n = next_re.search(text, m.end())
    end = n.start() if n else len(text)
    return m.start(), end


def _replace_method(text: str, method_name: str, replacement: str) -> str:
    start, end = _method_span(text, method_name)
    return text[:start] + replacement.rstrip() + "\n\n" + text[end:]


def _patch_window_type(text: str) -> tuple[str, bool]:
    """Change only Display Mode's base window type: Tool -> Dialog.

    Qt.Tool is explicitly kept above its parent on Windows.  Qt.Dialog remains
    a normal modeless app-owned dialog, so it can yield behind the main NAKSHA
    window while preserving minimize/maximize/close controls and shared app
    ownership.
    """
    func_re = re.compile(
        r"(?ms)^def _display_mode_window_flags\(\):\n.*?(?=^def [A-Za-z_]|^class [A-Za-z_]|\Z)"
    )
    m = func_re.search(text)
    if not m:
        raise RuntimeError("Could not find _display_mode_window_flags()")

    block = m.group(0)
    if "Qt.Dialog" in block:
        return text, False
    if "Qt.Tool" not in block:
        raise RuntimeError(
            "_display_mode_window_flags() no longer contains Qt.Tool; refusing to guess."
        )

    patched = block.replace("Qt.Tool", "Qt.Dialog", 1)
    patched = patched.replace(
        '"""Return flags for an app-owned, non-topmost modeless tool window."""',
        '"""Return flags for an app-owned modeless dialog that may yield behind the main window."""',
        1,
    )
    return text[:m.start()] + patched + text[m.end():], True


def _ensure_qtimer_import(text: str) -> tuple[str, bool]:
    # Current 2026-08-31 file already imports QTimer.  Keep this fallback so the
    # patch remains safe if that import line is reordered locally.
    if re.search(r"from PySide6\.QtCore import[^\n]*\bQTimer\b", text):
        return text, False

    m = re.search(r"(?m)^from PySide6\.QtCore import ([^\n]+)$", text)
    if not m:
        raise RuntimeError("Could not find PySide6.QtCore import line to add QTimer")

    names = m.group(1).rstrip()
    replacement = f"from PySide6.QtCore import {names}, QTimer"
    return text[:m.start()] + replacement + text[m.end():], True


def _patch_event_filter(text: str) -> tuple[str, bool]:
    current_start, current_end = _method_span(text, "eventFilter")
    current = text[current_start:current_end]
    if "_yield_to_app_window" in current:
        return text, False

    replacement = '''    def eventFilter(self, obj, event):
        """
        Keep Display Mode alive while allowing normal modeless focus behavior.

        Rules:
        - A physical click inside Display Mode behaves normally.
        - A physical click in the owning NAKSHA window does NOT hide/close the
          dialog; it only yields stacking/focus so the clicked view comes front.
        - Render callbacks, GPU refreshes and keyboard shortcuts never dismiss
          the dialog.
        - Explicit Close/X and native Minimize remain handled by the existing
          closeEvent/reject/window-state paths.
        """
        try:
            if (
                event.type() == QEvent.MouseButtonPress
                and self.isVisible()
                and _should_hide_display_mode_for_pointer_target(
                    self, self._get_app_window(), obj
                )
            ):
                # Do not hide.  Let the physical click finish first, then move
                # this modeless dialog behind the main NAKSHA window.
                QTimer.singleShot(0, self._yield_to_app_window)
        except (RuntimeError, ReferenceError):
            pass
        except Exception:
            pass

        return super().eventFilter(obj, event)
'''
    text = _replace_method(text, "eventFilter", replacement)
    return text, True


def _insert_lifecycle_helpers(text: str) -> tuple[str, bool]:
    if re.search(r"(?m)^    def _yield_to_app_window\b", text):
        return text, False

    start, _ = _method_span(text, "eventFilter")
    helpers = '''    def _yield_to_app_window(self):
        """Yield focus/stacking to the owning NAKSHA window without hiding."""
        try:
            app_window = self._get_app_window()
            if app_window is None or not _qt_object_is_valid(app_window):
                return
            if not self.isVisible():
                return
            if self.windowState() & Qt.WindowMinimized:
                return

            # Qt.Dialog can participate in normal stacking.  Keep the dialog
            # visible but let the user work in Main/Cross/Cut views.
            self.lower()
            app_window.raise_()
            app_window.activateWindow()
        except (RuntimeError, ReferenceError):
            pass
        except Exception:
            pass

    def _restore_after_apply(self):
        """Keep Display Mode visible/front after its own Apply button finishes."""
        try:
            if not _qt_object_is_valid(self):
                return

            # Native minimize is an explicit user action and must be respected.
            if self.windowState() & Qt.WindowMinimized:
                return

            # Apply changes state only; it must never dismiss this window.
            if not self.isVisible():
                self.show()
            self.raise_()
            self.activateWindow()
        except (RuntimeError, ReferenceError):
            pass
        except Exception:
            pass

'''
    return text[:start] + helpers + text[start:], True


def _patch_on_apply(text: str) -> tuple[str, bool]:
    start, end = _method_span(text, "on_apply")
    block = text[start:end]
    marker = "QTimer.singleShot(0, self._restore_after_apply)"
    if marker in block:
        return text, False

    # Add only to the successful fall-through path.  Existing validation
    # returns remain untouched and all rendering/palette logic stays byte-for-
    # byte identical.
    block = block.rstrip() + '''

        # Display Mode is persistent: Apply changes the selected view but does
        # not hide/close this modeless dialog.  Queue this after synchronous
        # signal/GPU work so any temporary focus transfer has completed.
        QTimer.singleShot(0, self._restore_after_apply)
'''
    return text[:start] + block + "\n\n" + text[end:], True


def apply_patch(path: Path, make_backup: bool = True):
    if not path.exists():
        raise FileNotFoundError(path)

    original, had_bom, newline = _read_source(path)
    text = original
    changes = []

    text, changed = _ensure_qtimer_import(text)
    if changed:
        changes.append("ensured QTimer import")

    text, changed = _patch_window_type(text)
    if changed:
        changes.append("changed Display Mode window type Qt.Tool -> Qt.Dialog")

    text, changed = _insert_lifecycle_helpers(text)
    if changed:
        changes.append("added focus/stacking lifecycle helpers")

    text, changed = _patch_event_filter(text)
    if changed:
        changes.append("outside click now yields behind instead of hiding")

    text, changed = _patch_on_apply(text)
    if changed:
        changes.append("Apply now restores Display Mode visibility/front")

    if text == original:
        print("No changes required: the persistence patch is already present.")
        return None

    # Syntax validation BEFORE touching the working file.
    compile(text, str(path), "exec")

    backup = None
    if make_backup:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.name}.before_display_persistence_{stamp}.bak")
        shutil.copy2(path, backup)

    _write_source(path, text, had_bom, newline)

    # Read-back + syntax validation after write.
    verify, _, _ = _read_source(path)
    compile(verify, str(path), "exec")

    print("\nDisplay Mode persistence patch applied successfully.")
    print(f"Updated: {path}")
    if backup is not None:
        print(f"Backup : {backup}")
    print("\nChanges:")
    for item in changes:
        print(f"  - {item}")
    print("\nPreserved intentionally:")
    print("  - on_apply rendering / GPU / palette / shading / surface logic")
    print("  - closeEvent / reject / Close button behavior")
    print("  - native minimize behavior")
    print("  - PTC, border, weight, class visibility and shortcut logic")
    return backup


def main():
    parser = argparse.ArgumentParser(
        description="Safely patch NakshaAI-LiDAR Display Mode persistence behavior."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_PATH),
        help=r"Path to gui\display_mode.py",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Do not create a timestamped backup (not recommended).",
    )
    args = parser.parse_args()

    apply_patch(Path(args.path), make_backup=not args.no_backup)


if __name__ == "__main__":
    main()
