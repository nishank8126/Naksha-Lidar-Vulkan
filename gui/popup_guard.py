"""Shared guard so transient input popups suppress runtime tool shortcuts.

Problem: when a feature popup (block creation, custom backup, PRJ/Display
sub-dialogs, …) is open and the user types, alphabet keys (E, W, L, …) are
mapped to drawing tools and fire on the main view — even though focus is in the
popup. The existing focus-based suppression is unreliable because other code
keeps forcing focus back to the VTK canvas.

Solution: any dialog that takes text input opts into this guard by either
subclassing InputPopupMixin or calling set_input_popup_open(app, True/False)
in its show/close handlers. GlobalShortcutFilter then suppresses tool shortcuts
for as long as the flag is set, independent of keyboard focus.
"""

from PySide6.QtWidgets import QApplication, QDialog

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


def set_input_popup_open(app, on: bool) -> None:
    """Set/clear the global input-popup guard on the app window."""
    if app is None:
        return
    try:
        app._input_popup_open = bool(on)
    except Exception:
        pass


def set_focus_input_popup(app, dialog, on: bool) -> None:
    """Register a dialog that suppresses shortcuts only while it owns focus."""
    if app is None or dialog is None:
        return
    try:
        current = list(getattr(app, "_input_popup_focus_guards", []) or [])
        live = []
        for candidate in current:
            try:
                if candidate is not None and _qt_object_is_valid(candidate):
                    candidate.objectName()
                    if candidate is not dialog:
                        live.append(candidate)
            except (RuntimeError, ReferenceError, AttributeError):
                continue
        if on and _qt_object_is_valid(dialog):
            live.append(dialog)
        app._input_popup_focus_guards = live
    except Exception:
        pass


def is_input_popup_open(app) -> bool:
    try:
        if bool(getattr(app, "_input_popup_open", False)):
            return True

        focus_widget = QApplication.focusWidget()
        active_window = QApplication.activeWindow()
        current = list(getattr(app, "_input_popup_focus_guards", []) or [])
        live = []
        focused_guard = False
        for dialog in current:
            try:
                if dialog is None or not _qt_object_is_valid(dialog):
                    continue
                dialog.objectName()
                if not dialog.isVisible():
                    continue
                live.append(dialog)
                if active_window is dialog or focus_widget is dialog:
                    focused_guard = True
                    continue
                if focus_widget is not None and dialog.isAncestorOf(focus_widget):
                    focused_guard = True
                    continue
                if active_window is not None and dialog.isAncestorOf(active_window):
                    focused_guard = True
            except (RuntimeError, ReferenceError, AttributeError):
                continue
        app._input_popup_focus_guards = live
        return focused_guard
    except Exception:
        return False


class InputPopupMixin(QDialog):
    """Mixin for any QDialog that should suppress runtime tool shortcuts while
    it is visible. Just multiply-inherit alongside QDialog.

    Example:
        class MyDialog(InputPopupMixin, QDialog):
            ...
    """

    shortcut_guard_focus_only = False

    def _set_popup_guard(self, enabled: bool) -> None:
        app = self._popup_app()
        if bool(getattr(self, "shortcut_guard_focus_only", False)):
            set_focus_input_popup(app, self, enabled)
        else:
            set_input_popup_open(app, enabled)

    def showEvent(self, event):
        try:
            self._set_popup_guard(True)
        except Exception:
            pass
        super().showEvent(event)

    def hideEvent(self, event):
        try:
            self._set_popup_guard(False)
        except Exception:
            pass
        super().hideEvent(event)

    def closeEvent(self, event):
        try:
            self._set_popup_guard(False)
        except Exception:
            pass
        super().closeEvent(event)

    def _popup_app(self):
        # Prefer an explicitly stored app reference, else walk parents.
        app = getattr(self, "app", None)
        if app is None:
            app = getattr(self, "_app", None)
        if app is None:
            app = getattr(self, "_app_window", None)
        if app is None:
            p = self.parent()
            while p is not None:
                if hasattr(p, "_input_popup_open"):
                    app = p
                    break
                if hasattr(p, "_app_window"):
                    app = p._app_window
                    break
                # Some owners (tree items, controllers) stash app as .app
                cand = getattr(p, "app", None)
                if cand is not None and hasattr(cand, "_input_popup_open"):
                    app = cand
                    break
                p = p.parent()
        return app
