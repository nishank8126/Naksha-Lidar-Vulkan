from PySide6.QtCore import Qt

from gui.display_mode import (
    _display_mode_window_flags,
    _should_hide_display_mode_for_pointer_target,
    _widget_belongs_to_display_mode,
)


class _WindowNode:
    def __init__(self, parent=None, state=Qt.WindowNoState):
        self._parent = parent
        self._state = state

    def parentWidget(self):
        return self._parent

    def windowState(self):
        return self._state


def test_display_mode_is_owned_tool_window_not_system_topmost():
    flags = _display_mode_window_flags()

    assert flags & Qt.Tool
    assert flags & Qt.WindowMinimizeButtonHint
    assert flags & Qt.WindowCloseButtonHint
    assert not flags & Qt.WindowStaysOnTopHint


def test_pointer_inside_owned_popup_family_does_not_hide_display_mode():
    app_window = _WindowNode()
    dialog = _WindowNode(parent=app_window)
    child_dialog = _WindowNode(parent=dialog)
    popup = _WindowNode(parent=child_dialog)

    assert _widget_belongs_to_display_mode(dialog, popup)
    assert not _should_hide_display_mode_for_pointer_target(
        dialog, app_window, popup
    )


def test_pointer_press_on_naksha_background_hides_display_mode():
    app_window = _WindowNode()
    dialog = _WindowNode(parent=app_window)
    canvas = _WindowNode(parent=app_window)

    assert _should_hide_display_mode_for_pointer_target(
        dialog, app_window, canvas
    )


def test_apply_or_shortcut_focus_change_without_background_click_stays_open():
    app_window = _WindowNode()
    dialog = _WindowNode(parent=app_window)
    apply_button = _WindowNode(parent=dialog)

    assert not _should_hide_display_mode_for_pointer_target(
        dialog, app_window, apply_button
    )
    assert not _should_hide_display_mode_for_pointer_target(
        dialog, app_window, None
    )


def test_external_application_is_allowed_to_cover_display_mode():
    app_window = _WindowNode()
    dialog = _WindowNode(parent=app_window)
    external_window = _WindowNode()

    assert not _should_hide_display_mode_for_pointer_target(
        dialog, app_window, external_window
    )
