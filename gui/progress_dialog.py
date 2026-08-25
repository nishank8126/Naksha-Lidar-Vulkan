from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QProgressBar,
    QHBoxLayout, QFrame, QApplication
)
from PySide6.QtCore import Qt, QTimer, Signal, QRect
from PySide6.QtGui import QPainter, QColor, QPen

from gui.theme_manager import get_dialog_stylesheet, ThemeColors


class _ClickBlocker(QWidget):
    """
    Transparent full-window overlay that absorbs all mouse/keyboard events
    so the user cannot interact with the main window during loading.
    Has no OS window handle — lives inside the parent widget tree.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setGeometry(parent.rect())

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 80))  # dim overlay

    def mousePressEvent(self, event):   event.accept()
    def mouseReleaseEvent(self, event): event.accept()
    def mouseMoveEvent(self, event):    event.accept()
    def keyPressEvent(self, event):     event.accept()
    def wheelEvent(self, event):        event.accept()


class LoadingProgressDialog(QWidget):
    """
    Non-modal, non-interactive loading overlay.

    Replaces QDialog entirely so no OS window handle is created.
    Windows cannot mark this "not responding" because it is not a
    top-level window — it is a child widget painted inside the parent.

    API is backward-compatible with the old QDialog version:
      show(), hide(), set_filename(), set_progress(), set_status(),
      set_points_count(), finish_success(), finish_error(), is_canceled()
    """

    # Kept for API compatibility with any code that connected to it
    cancel_requested = Signal()

    def __init__(self, parent=None, show_cancel=False):
        super().__init__(parent)

        self._canceled = False
        self._loading_active = True
        self._blocker = None

        # Frameless child widget — drawn on top of parent
        self.setWindowFlags(Qt.Widget)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._setup_ui(show_cancel)

        # Install resize tracking on parent so we re-center on resize
        if parent:
            parent.installEventFilter(self)

    # ── Geometry helpers ──────────────────────────────────────────────

    def _center_on_parent(self):
        parent = self.parent()
        if parent is None:
            return
        pw, ph = parent.width(), parent.height()
        w, h   = self.width(), self.height()
        self.move((pw - w) // 2, (ph - h) // 2)

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.parent() and event.type() == QEvent.Resize:
            if self._blocker:
                self._blocker.setGeometry(self.parent().rect())
            self._center_on_parent()
        return False

    # ── Public lifecycle ──────────────────────────────────────────────

    def show(self):
        parent = self.parent()
        if parent and self._blocker is None:
            self._blocker = _ClickBlocker(parent)
            self._blocker.show()
            self._blocker.raise_()

        super().show()
        self.raise_()          # always on top within parent
        self.adjustSize()
        self._center_on_parent()
        QApplication.processEvents()

    def hide(self):
        self._remove_blocker()
        super().hide()

    def mark_loading_done(self):
        self._loading_active = False

    def _remove_blocker(self):
        if self._blocker:
            self._blocker.hide()
            self._blocker.deleteLater()
            self._blocker = None

    # ── UI setup ──────────────────────────────────────────────────────

    def _setup_ui(self, show_cancel):
        self.setFixedWidth(520)
        self.setStyleSheet(
            get_dialog_stylesheet() + f"""
            LoadingProgressDialog {{
                background-color: {ThemeColors.get('bg_primary')};
                border: 1px solid {ThemeColors.get('border_light')};
                border-radius: 12px;
            }}
            QFrame#progressHero {{
                background-color: {ThemeColors.get('bg_secondary')};
                border: 1px solid {ThemeColors.get('border_light')};
                border-radius: 12px;
            }}
            QLabel#progressStatus {{
                color: {ThemeColors.get('text_secondary')};
                font-size: 9pt;
            }}
            QLabel#progressMetric {{
                color: {ThemeColors.get('accent')};
                font-size: 9.5pt;
                font-weight: 700;
            }}
            QProgressBar#loadingProgressBar {{
                border: 1px solid {ThemeColors.get('border_light')};
                border-radius: 11px;
                background-color: {ThemeColors.get('bg_input')};
                color: {ThemeColors.get('text_primary')};
                min-height: 22px;
                text-align: center;
                font-weight: 700;
            }}
            QProgressBar#loadingProgressBar::chunk {{
                border-radius: 10px;
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 {ThemeColors.get('accent')},
                    stop:1 {ThemeColors.get('accent_hover')}
                );
            }}
            """
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(18, 18, 18, 18)

        hero = QFrame()
        hero.setObjectName("progressHero")
        hero_layout = QVBoxLayout(hero)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(6)

        self.title_label = QLabel("Reading Block Data")
        self.title_label.setObjectName("dialogTitle")
        self.title_label.setAlignment(Qt.AlignCenter)
        hero_layout.addWidget(self.title_label)

        self.status_label = QLabel("Preparing source data and validating file contents.")
        self.status_label.setObjectName("progressStatus")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        hero_layout.addWidget(self.status_label)

        layout.addWidget(hero)

        self.file_label = QLabel("Loading file...")
        self.file_label.setObjectName("dialogSectionLabel")
        self.file_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.file_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("loadingProgressBar")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(24)
        layout.addWidget(self.progress_bar)

        self.points_label = QLabel("")
        self.points_label.setObjectName("progressMetric")
        self.points_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.points_label)

        if show_cancel:
            from PySide6.QtWidgets import QPushButton
            button_layout = QHBoxLayout()
            button_layout.addStretch()
            self.cancel_button = QPushButton("Cancel")
            self.cancel_button.setObjectName("dangerBtn")
            self.cancel_button.setFixedWidth(100)
            self.cancel_button.clicked.connect(self._on_cancel)
            button_layout.addWidget(self.cancel_button)
            button_layout.addStretch()
            layout.addLayout(button_layout)

    # ── Slot methods (called from main-thread signal handlers) ────────

    def set_filename(self, filename):
        import os
        self.file_label.setText(os.path.basename(filename))

    def set_progress(self, value, status_text=None):
        self.progress_bar.setValue(int(value))
        if status_text is not None:
            self.status_label.setText(status_text)

    def set_status(self, status_text):
        self.status_label.setText(status_text)

    def set_points_count(self, count):
        if count > 0:
            self.points_label.setText(f"{count:,} points processed")
        else:
            self.points_label.setText("")

    def is_canceled(self):
        return self._canceled

    # ── Finish states ─────────────────────────────────────────────────

    def finish_success(self, message="Loading complete"):
        self.mark_loading_done()
        self.set_status(message)
        self.set_progress(100)
        QTimer.singleShot(500, self._close_overlay)

    def finish_error(self, error_message):
        self.mark_loading_done()
        self.set_status(error_message)
        QTimer.singleShot(2000, self._close_overlay)

    def _close_overlay(self):
        self._remove_blocker()
        self.hide()

    def _on_cancel(self):
        self._canceled = True
        self.mark_loading_done()
        self.cancel_requested.emit()
        self._close_overlay()


# ── Standalone loader helper (unchanged, kept for compat) ─────────────

def load_lidar_file_with_progress(filename, parent=None, progress_callback=None):
    """
    Modified version of load_lidar_file that reports progress.
    """
    import laspy
    import numpy as np

    def report_progress(percent, status):
        if progress_callback:
            progress_callback(percent, status)

    try:
        report_progress(10, "Opening file...")
        las = laspy.read(filename)
        total_points = len(las.points)
        report_progress(20, f"Reading {total_points:,} points...")

        xyz = np.vstack([las.x, las.y, las.z]).T
        report_progress(40, "Processing coordinates...")

        rgb = None
        if hasattr(las, "red") and hasattr(las, "green") and hasattr(las, "blue"):
            rgb = np.vstack([las.red, las.green, las.blue]).T
            rgb = (rgb / 256).astype(np.uint8)
        report_progress(60, "Processing colors...")

        intensity = las.intensity if hasattr(las, "intensity") else None
        report_progress(70, "Processing intensity...")

        classification = las.classification if hasattr(las, "classification") else None
        report_progress(80, "Processing classification...")

        from gui.data_loader import _extract_crs
        crs_wkt, crs_epsg = _extract_crs(las)
        report_progress(90, "Reading CRS information...")

        lidar_data = {
            "xyz":            xyz,
            "rgb":            rgb,
            "intensity":      intensity,
            "classification": classification,
            "crs_epsg":       crs_epsg,
            "crs_wkt":        crs_wkt,
        }
        report_progress(100, f"Loaded {total_points:,} points")
        return lidar_data

    except Exception as e:
        report_progress(0, f"Error: {str(e)}")
        return None
