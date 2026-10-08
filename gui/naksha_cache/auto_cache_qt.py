"""Qt adapter for AUTOMATIC cache build (PART 6/7/8/41).

The heavy work runs on a QThread so QApplication is never blocked; only the
progress dialog is touched, and only from the GUI thread via queued signals.

    MISS / STALE / CORRUPT  ->  OPTIMIZING POINT CLOUD...  ->  HIT  ->  stream
    HIT                     ->  straight to streaming, no dialog

The user never sees a command to run and never moves a cache file.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from PySide6.QtCore import QObject, QThread, Signal, Slot

from .auto_cache import (
    STATE_HIT, CacheBuildJob, classify_cache, place_cache,
)


class CacheBuildWorker(QObject):
    """Runs the build off the GUI thread. Touches NO Qt widgets.

    `progress` is emitted with plain data; `finished` carries the report dict.
    Because this object lives on a QThread, emissions are automatically queued
    to the receiver's thread - the GUI thread - which is what keeps PART 7 safe.
    """

    progress = Signal(object)
    finished = Signal(object)

    def __init__(self, source_path: str, placement=None):
        super().__init__()
        self.job = CacheBuildJob(source_path, placement)

    @Slot()
    def run(self):
        report = self.job.run(on_progress=self.progress.emit)
        self.finished.emit(report)

    @Slot()
    def cancel(self):
        self.job.cancel()


class CacheBuildController(QObject):
    """Owns the worker thread and exposes the UI-facing signals."""

    progress = Signal(object)        # BuildProgress
    finished = Signal(object)        # report dict
    state_changed = Signal(str, str)  # (STATE, reason)

    def __init__(self, source_path: str, parent=None):
        super().__init__(parent)
        self.source_path = os.path.abspath(source_path)
        self.placement = place_cache(self.source_path)
        self._thread = None
        self._worker = None

    # -- lifecycle ----------------------------------------------------- #
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def start(self) -> bool:
        if self.is_running():
            return False
        self.state_changed.emit("BUILDING", self.source_path)
        self._thread = QThread()
        self._worker = CacheBuildWorker(self.source_path, self.placement)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self.progress.emit)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()
        return True

    def cancel(self):
        """Safe from the GUI thread: sets a flag the worker polls."""
        if self._worker is not None:
            self._worker.cancel()

    @Slot(object)
    def _on_finished(self, report):
        state = classify_cache(self.source_path, self.placement)
        self.state_changed.emit(state.state, state.reason)
        self.finished.emit(report)
        self._teardown()

    def _teardown(self):
        if self._worker is not None:
            try:
                self._worker.deleteLater()
            except Exception:
                pass
        if self._thread is not None:
            try:
                self._thread.quit()
                self._thread.wait(5000)
                self._thread.deleteLater()
            except Exception:
                pass
        self._worker = None
        self._thread = None


# --------------------------------------------------------------------------- #
# PART 6/41 - the "OPTIMIZING POINT CLOUD..." UI                               #
# --------------------------------------------------------------------------- #
def build_progress_dialog(parent, source_path: str):
    """Modal progress dialog with a Cancel button (PART 8).

    Returned as (dialog, set_progress) where set_progress(percent, title) is
    invoked from the GUI thread via a queued connection.
    """
    from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel,
                                   QProgressBar, QPushButton, QVBoxLayout)
    dlg = QDialog(parent)
    dlg.setWindowTitle("Optimizing LiDAR cache")
    dlg.setModal(True)
    dlg.setMinimumWidth(460)
    lay = QVBoxLayout(dlg)

    title = QLabel(f"Optimizing point cloud for first-time access...")
    title.setWordWrap(True)
    lay.addWidget(title)

    name = QLabel(os.path.basename(source_path))
    name.setStyleSheet("color: palette(mid);")
    lay.addWidget(name)

    bar = QProgressBar()
    bar.setRange(0, 100)
    bar.setValue(0)
    lay.addWidget(bar)

    detail = QLabel("Starting...")
    detail.setWordWrap(True)
    lay.addWidget(detail)

    row = QHBoxLayout()
    row.addStretch(1)
    cancel = QPushButton("Cancel Optimization")
    row.addWidget(cancel)
    lay.addLayout(row)

    cancel.clicked.connect(dlg.reject)

    def set_progress(percent: float, text: str = ""):
        bar.setValue(int(max(0, min(100, round(percent)))))
        if text:
            detail.setText(text)
        # Pump events so the dialog actually repaints during the build.
        from PySide6.QtWidgets import QApplication
        if QApplication.instance() is not None:
            QApplication.processEvents()

    return dlg, set_progress