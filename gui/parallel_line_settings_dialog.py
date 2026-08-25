from __future__ import annotations

import math

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


DEFAULT_PARALLEL_TOTAL_WIDTH = 0.40
DEFAULT_PARALLEL_TOLERANCE = 0.20


def _safe_non_negative_float(value, default):
    try:
        val = float(value)
    except Exception:
        return float(default)
    if not math.isfinite(val):
        return float(default)
    return max(0.0, float(val))


def ensure_parallel_line_settings(app):
    """
    Ensure app has parallel-line settings with safe defaults.
    Returns (mode, total_width, tol_above, tol_below).
    """
    settings = QSettings("NakshaAI", "LidarApp")

    mode = str(
        settings.value(
            "parallel_line/mode",
            getattr(app, "parallel_line_mode", "symmetric"),
            type=str,
        )
    ).strip().lower()
    if mode not in {"symmetric", "asymmetric"}:
        mode = "symmetric"

    total_width = _safe_non_negative_float(
        settings.value(
            "parallel_line/total_width",
            getattr(app, "parallel_line_total_width", DEFAULT_PARALLEL_TOTAL_WIDTH),
        ),
        DEFAULT_PARALLEL_TOTAL_WIDTH,
    )
    tol_above = _safe_non_negative_float(
        settings.value(
            "parallel_line/tol_above",
            getattr(app, "parallel_line_tol_above", DEFAULT_PARALLEL_TOLERANCE),
        ),
        DEFAULT_PARALLEL_TOLERANCE,
    )
    tol_below = _safe_non_negative_float(
        settings.value(
            "parallel_line/tol_below",
            getattr(app, "parallel_line_tol_below", DEFAULT_PARALLEL_TOLERANCE),
        ),
        DEFAULT_PARALLEL_TOLERANCE,
    )

    if mode == "symmetric":
        half = max(total_width * 0.5, 0.0)
        tol_above = half
        tol_below = half
    else:
        total_width = max(tol_above + tol_below, 0.0)

    app.parallel_line_mode = mode
    app.parallel_line_total_width = float(total_width)
    app.parallel_line_tol_above = float(tol_above)
    app.parallel_line_tol_below = float(tol_below)
    return mode, float(total_width), float(tol_above), float(tol_below)


class ParallelLineSettingsDialog(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.setWindowTitle("Parallel Line Settings")
        self.setModal(True)
        self.resize(420, 220)
        self._build_ui()
        self._load_from_app()
        self._refresh_mode_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(form.labelAlignment())
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Symmetric (total width)", "symmetric")
        self.mode_combo.addItem("Asymmetric (above / below)", "asymmetric")
        self.mode_combo.currentIndexChanged.connect(self._refresh_mode_ui)
        form.addRow("Mode:", self.mode_combo)

        self.total_width_spin = QDoubleSpinBox()
        self.total_width_spin.setDecimals(4)
        self.total_width_spin.setRange(0.0, 999999.0)
        self.total_width_spin.setSingleStep(0.05)
        self.total_width_spin.setSuffix(" m")
        self.total_width_spin.valueChanged.connect(self._refresh_summary)
        form.addRow("Total width:", self.total_width_spin)

        self.tol_above_spin = QDoubleSpinBox()
        self.tol_above_spin.setDecimals(4)
        self.tol_above_spin.setRange(0.0, 999999.0)
        self.tol_above_spin.setSingleStep(0.05)
        self.tol_above_spin.setSuffix(" m")
        self.tol_above_spin.valueChanged.connect(self._refresh_summary)
        form.addRow("Tolerance above:", self.tol_above_spin)

        self.tol_below_spin = QDoubleSpinBox()
        self.tol_below_spin.setDecimals(4)
        self.tol_below_spin.setRange(0.0, 999999.0)
        self.tol_below_spin.setSingleStep(0.05)
        self.tol_below_spin.setSuffix(" m")
        self.tol_below_spin.valueChanged.connect(self._refresh_summary)
        form.addRow("Tolerance below:", self.tol_below_spin)

        root.addLayout(form)

        self.summary_label = QLabel("")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        root.addLayout(btn_row)

    def _load_from_app(self):
        mode, total_width, tol_above, tol_below = ensure_parallel_line_settings(self.app)
        idx = self.mode_combo.findData(mode)
        if idx < 0:
            idx = 0
        self.mode_combo.setCurrentIndex(idx)
        self.total_width_spin.setValue(float(total_width))
        self.tol_above_spin.setValue(float(tol_above))
        self.tol_below_spin.setValue(float(tol_below))
        self._refresh_summary()

    def _refresh_mode_ui(self):
        mode = self.mode_combo.currentData()
        is_symmetric = mode == "symmetric"
        self.total_width_spin.setEnabled(is_symmetric)
        self.tol_above_spin.setEnabled(not is_symmetric)
        self.tol_below_spin.setEnabled(not is_symmetric)
        self._refresh_summary()

    def _refresh_summary(self):
        mode = self.mode_combo.currentData()
        if mode == "symmetric":
            width = float(self.total_width_spin.value())
            half = width * 0.5
            self.summary_label.setText(
                f"Parallel band: +/- {half:.4f} m from center line "
                f"(total {width:.4f} m)."
            )
        else:
            above = float(self.tol_above_spin.value())
            below = float(self.tol_below_spin.value())
            self.summary_label.setText(
                f"Parallel band: +{above:.4f} m above and -{below:.4f} m below "
                f"(total {above + below:.4f} m)."
            )

    def accept(self):
        mode = self.mode_combo.currentData()
        total_width = _safe_non_negative_float(
            self.total_width_spin.value(), DEFAULT_PARALLEL_TOTAL_WIDTH
        )
        tol_above = _safe_non_negative_float(
            self.tol_above_spin.value(), DEFAULT_PARALLEL_TOLERANCE
        )
        tol_below = _safe_non_negative_float(
            self.tol_below_spin.value(), DEFAULT_PARALLEL_TOLERANCE
        )

        if mode == "symmetric":
            half = total_width * 0.5
            tol_above = half
            tol_below = half
        else:
            total_width = tol_above + tol_below

        self.app.parallel_line_mode = mode
        self.app.parallel_line_total_width = float(total_width)
        self.app.parallel_line_tol_above = float(tol_above)
        self.app.parallel_line_tol_below = float(tol_below)

        settings = QSettings("NakshaAI", "LidarApp")
        settings.setValue("parallel_line/mode", mode)
        settings.setValue("parallel_line/total_width", float(total_width))
        settings.setValue("parallel_line/tol_above", float(tol_above))
        settings.setValue("parallel_line/tol_below", float(tol_below))

        super().accept()


def activate_parallel_line_tool_with_dialog(app, show_settings=False):
    """
    Activate parallel-line classification with optional settings dialog.

    Behavior:
    - First use: open settings dialog
    - Shift+click request: open settings dialog
    - Normal click: keep previous settings
    """
    first_time = not hasattr(app, "parallel_line_mode")
    ensure_parallel_line_settings(app)

    if first_time or show_settings:
        dialog = ParallelLineSettingsDialog(app, parent=app)
        if dialog.exec() != QDialog.Accepted:
            if hasattr(app, "statusBar"):
                app.statusBar().showMessage("Parallel line tool cancelled", 2000)
            return False

    mode, total_width, tol_above, tol_below = ensure_parallel_line_settings(app)
    app.active_classify_tool = "parallel_line"

    if hasattr(app, "statusBar"):
        if mode == "symmetric":
            app.statusBar().showMessage(
                f"Parallel Line: symmetric band {total_width:.4f} m "
                f"(+/- {total_width * 0.5:.4f} m)",
                3500,
            )
        else:
            app.statusBar().showMessage(
                f"Parallel Line: above {tol_above:.4f} m, below {tol_below:.4f} m "
                f"(total {total_width:.4f} m)",
                3500,
            )
    return True
