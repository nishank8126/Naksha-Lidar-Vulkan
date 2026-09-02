from types import SimpleNamespace

from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QPushButton

from gui.global_shortcuts import GlobalShortcutFilter


def _guard_with_focus(widget_type):
    qt_app = QApplication.instance() or QApplication([])
    dialog = QDialog()
    focused = widget_type(dialog)
    dialog.show()
    focused.show()
    dialog.activateWindow()
    focused.setFocus()
    qt_app.processEvents()

    owner = SimpleNamespace(display_mode_dialog=dialog)
    guard = GlobalShortcutFilter(owner)
    result = guard._is_display_mode_dialog_active()
    dialog.close()
    return result


def test_display_mode_button_focus_allows_global_shortcuts():
    assert not _guard_with_focus(QPushButton)


def test_display_mode_text_focus_keeps_typed_keys_local():
    assert _guard_with_focus(QLineEdit)
