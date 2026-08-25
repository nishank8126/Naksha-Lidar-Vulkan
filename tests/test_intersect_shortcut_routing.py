from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from gui.global_shortcuts import GlobalShortcutFilter


class _FakeEvent:
    def __init__(self, key, modifiers):
        self._key = key
        self._modifiers = modifiers
        self.accepted = False

    def type(self):
        return QEvent.KeyPress

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def text(self):
        return ""

    def accept(self):
        self.accepted = True


class _FakeElementTool:
    def __init__(self):
        self._active = True
        self._intersect_mode = True
        self.undo_count = 0
        self.redo_count = 0

    def _intersect_undo_last_delete(self):
        self.undo_count += 1

    def _intersect_redo_last_delete(self):
        self.redo_count += 1


class _FakeDigitizer:
    def __init__(self, tool):
        self._element_select_tool = tool


class _FakeAppWindow:
    def __init__(self, tool):
        self.digitizer = _FakeDigitizer(tool)
        self.shortcuts = {}
        self.undo_stack = []
        self.redo_stack = []
        self.cross_section_active = False
        self.cut_section_controller = None


def _make_filter():
    qt_app = QApplication.instance() or QApplication([])
    tool = _FakeElementTool()
    shortcut_filter = GlobalShortcutFilter(_FakeAppWindow(tool))
    shortcut_filter._test_qt_app = qt_app
    return shortcut_filter, tool


def test_ctrl_y_is_consumed_at_qt_boundary_before_vtk():
    shortcut_filter, tool = _make_filter()
    event = _FakeEvent(Qt.Key_Y, Qt.ControlModifier)

    assert shortcut_filter.eventFilter(object(), event) is True
    assert event.accepted
    assert tool.redo_count == 1
    assert tool.undo_count == 0


def test_ctrl_shift_z_routes_to_intersect_redo_and_is_consumed():
    shortcut_filter, tool = _make_filter()
    event = _FakeEvent(Qt.Key_Z, Qt.ControlModifier | Qt.ShiftModifier)

    assert shortcut_filter.eventFilter(object(), event) is True
    assert event.accepted
    assert tool.redo_count == 1
    assert tool.undo_count == 0


def test_ctrl_z_routes_to_intersect_undo_and_is_consumed():
    shortcut_filter, tool = _make_filter()
    event = _FakeEvent(Qt.Key_Z, Qt.ControlModifier)

    assert shortcut_filter.eventFilter(object(), event) is True
    assert event.accepted
    assert tool.undo_count == 1
    assert tool.redo_count == 0
