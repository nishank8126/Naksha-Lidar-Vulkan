"""Tests for relocating the main view to one SNT file row.

The canvas has a single shared camera, so a file that sits in a different
location looks missing until the camera moves to it.  Double-clicking a row in
the Attach SNT dialog relocates the view to that file.
"""
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from gui.snt_attachment import MultiSNTAttachmentDialog, SNTFileItem


class _Checkbox:
    def __init__(self, checked):
        self._checked = bool(checked)
        self.set_calls = []

    def isChecked(self):
        return self._checked

    def setChecked(self, value):
        self.set_calls.append(bool(value))
        self._checked = bool(value)


class _Item:
    def __init__(self, name, attachment, checked=True):
        self.snt_path = SimpleNamespace(name=name)
        self.attachment = attachment
        self.checkbox = _Checkbox(checked)

    def is_checked(self):
        return self.checkbox.isChecked()


def _stub_dialog(fit_result=True):
    fitted = []

    def fit(attachments):
        fitted.append(list(attachments))
        return fit_result

    return SimpleNamespace(_fit_view_to_snt_attachments=fit, fitted=fitted)


def _relocate(stub, item):
    return MultiSNTAttachmentDialog._relocate_to_snt_item(stub, item)


def _double_click_event():
    point = QPointF(5.0, 5.0)
    return QMouseEvent(
        QEvent.MouseButtonDblClick,
        point,
        point,
        Qt.LeftButton,
        Qt.LeftButton,
        Qt.NoModifier,
    )


def test_relocate_fits_the_double_clicked_file():
    attachment = {"filename": "A.snt", "fit_bounds": (0.0, 1.0, 0.0, 1.0)}
    stub = _stub_dialog()
    assert _relocate(stub, _Item("A.snt", attachment)) is True
    assert stub.fitted == [[attachment]]


def test_relocate_reveals_a_hidden_file_before_fitting():
    attachment = {"filename": "A.snt"}
    item = _Item("A.snt", attachment, checked=False)
    stub = _stub_dialog()
    assert _relocate(stub, item) is True
    assert item.checkbox.set_calls == [True]
    assert stub.fitted == [[attachment]]


def test_relocate_leaves_a_visible_file_visibility_alone():
    item = _Item("A.snt", {"filename": "A.snt"}, checked=True)
    stub = _stub_dialog()
    _relocate(stub, item)
    assert item.checkbox.set_calls == []


def test_relocate_skips_a_file_that_is_not_attached():
    stub = _stub_dialog()
    assert _relocate(stub, _Item("A.snt", None)) is False
    assert stub.fitted == []


def test_relocate_reports_failure_when_bounds_are_unavailable():
    stub = _stub_dialog(fit_result=False)
    assert _relocate(stub, _Item("A.snt", {"filename": "A.snt"})) is False


def test_relocate_ignores_a_missing_row():
    stub = _stub_dialog()
    assert _relocate(stub, None) is False
    assert stub.fitted == []


def test_file_name_label_forwards_mouse_events_to_the_row():
    QApplication.instance() or QApplication([])
    item = SNTFileItem(Path("A.snt"))
    assert item.name_label.testAttribute(Qt.WA_TransparentForMouseEvents)


def test_double_click_on_a_row_relocates_and_accepts(monkeypatch):
    QApplication.instance() or QApplication([])
    item = SNTFileItem(Path("A.snt"))
    calls = []
    monkeypatch.setattr(
        item,
        "_find_parent_dialog",
        lambda: SimpleNamespace(
            _relocate_to_snt_item=lambda it: calls.append(it) or True
        ),
    )
    event = _double_click_event()
    item.mouseDoubleClickEvent(event)
    assert calls == [item]
    assert event.isAccepted()


def test_double_click_without_a_usable_parent_does_not_raise(monkeypatch):
    QApplication.instance() or QApplication([])
    item = SNTFileItem(Path("A.snt"))
    monkeypatch.setattr(item, "_find_parent_dialog", lambda: None)
    event = _double_click_event()
    item.mouseDoubleClickEvent(event)
    assert not event.isAccepted()
