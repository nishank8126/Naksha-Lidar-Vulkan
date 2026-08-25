from PySide6.QtCore import QEvent, Qt
from PySide6.QtWidgets import QApplication

from gui.global_shortcuts import GlobalShortcutFilter


class _KeyEvent:
    def __init__(self, event_type, key, modifiers=Qt.NoModifier):
        self._event_type = event_type
        self._key = key
        self._modifiers = modifiers

    def type(self):
        return self._event_type

    def key(self):
        return self._key

    def modifiers(self):
        return self._modifiers

    def text(self):
        return "P"

    def isAutoRepeat(self):
        return False


class _App:
    def __init__(self):
        self.shortcuts = {("none", "P"): {"tool": "Pan", "from": None, "to": None}}
        self._left_pan_shortcut_active = False
        self._qt_main_pan_active = False
        self.cross_section_active = False
        self.cut_section_controller = None
        self.digitizer = None


class _ActiveToolsApp(_App):
    def __init__(self):
        super().__init__()
        self.active_classify_tool = "above_line"
        self.cross_section_active = True
        self.calls = []
        self.measurement_tool = type(
            "Measurement",
            (),
            {
                "active": True,
                "is_measuring": True,
                "deactivate": lambda tool: self.calls.append("measurement"),
            },
        )()

    def _cancel_cross_section_tool_only(self):
        self.calls.append("cross")
        self.cross_section_active = False

    def _deactivate_pending_cut_section_tool(self, reason):
        self.calls.append("cut")

    def _deactivate_active_identification_tools_for_escape(self):
        self.calls.append("identification")
        return True

    def deactivate_classification_tool(self):
        self.calls.append("classification")
        self.active_classify_tool = None

    def _deactivate_digitize_tool(self):
        self.calls.append("digitize")


def test_pan_shortcut_stays_active_after_primary_key_is_released():
    qt_app = QApplication.instance() or QApplication([])
    app = _App()
    shortcut_filter = GlobalShortcutFilter(app)
    shortcut_filter._test_qt_app = qt_app

    assert shortcut_filter.eventFilter(object(), _KeyEvent(QEvent.KeyPress, Qt.Key_P))
    assert app._left_pan_shortcut_active

    assert not shortcut_filter.eventFilter(object(), _KeyEvent(QEvent.KeyRelease, Qt.Key_P))
    assert app._left_pan_shortcut_active


def test_escape_does_not_arm_left_pan():
    qt_app = QApplication.instance() or QApplication([])
    app = _App()
    shortcut_filter = GlobalShortcutFilter(app)
    shortcut_filter._test_qt_app = qt_app

    assert not shortcut_filter.eventFilter(
        object(), _KeyEvent(QEvent.KeyPress, Qt.Key_Escape)
    )
    assert not app._left_pan_shortcut_active


def test_escape_deactivates_an_armed_pan_without_rearming_it():
    qt_app = QApplication.instance() or QApplication([])
    app = _App()
    shortcut_filter = GlobalShortcutFilter(app)
    shortcut_filter._test_qt_app = qt_app

    assert shortcut_filter.eventFilter(object(), _KeyEvent(QEvent.KeyPress, Qt.Key_P))
    assert app._left_pan_shortcut_active
    assert not shortcut_filter.eventFilter(
        object(), _KeyEvent(QEvent.KeyPress, Qt.Key_Escape)
    )
    assert not app._left_pan_shortcut_active


def test_pan_shortcut_deactivates_other_left_click_tools_before_arming():
    qt_app = QApplication.instance() or QApplication([])
    app = _ActiveToolsApp()
    shortcut_filter = GlobalShortcutFilter(app)
    shortcut_filter._test_qt_app = qt_app

    assert shortcut_filter.eventFilter(object(), _KeyEvent(QEvent.KeyPress, Qt.Key_P))
    assert app._left_pan_shortcut_active
    assert not app.cross_section_active
    assert app.active_classify_tool is None
    assert set(app.calls) >= {
        "cross",
        "cut",
        "identification",
        "classification",
        "measurement",
        "digitize",
    }
