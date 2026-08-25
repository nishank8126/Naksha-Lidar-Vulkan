"""
Shared minimize-to-chip behavior for non-modal dialogs.

Intercepts the native OS minimize button and replaces it with a small
floating chip anchored to the bottom of the screen.  A global
_ChipManager keeps all active chips in a horizontal row so they never
overlap each other.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QWidget,
)


# ─────────────────────────────────────────────────────────────────────────────
# CHIP STYLE
# ─────────────────────────────────────────────────────────────────────────────

def _get_chip_style() -> str:
    from gui.theme_manager import ThemeColors as _TC
    return f"""
QWidget#minimizedChip {{
    background-color: {_TC.get('bg_secondary')};
    border: 1px solid {_TC.get('accent')};
    border-radius: 6px;
}}
QLabel#chipLabel {{
    color: {_TC.get('accent')};
    font-size: 10px;
    font-weight: bold;
    padding: 2px 6px;
}}
QPushButton#chipRestoreBtn {{
    background-color: {_TC.get('bg_button')};
    border: none;
    border-radius: 4px;
    color: {_TC.get('accent')};
    font-size: 10px;
    font-weight: bold;
    padding: 2px 8px;
    min-width: 0;
}}
QPushButton#chipRestoreBtn:hover {{
    background-color: {_TC.get('bg_button_hover')};
    color: {_TC.get('accent_hover')};
}}
"""


# ─────────────────────────────────────────────────────────────────────────────
# CHIP MANAGER  — keeps all chips in a horizontal row at the bottom
# ─────────────────────────────────────────────────────────────────────────────

class _ChipManager:
    """
    Global registry.  Every _MinimizedChip registers itself on show and
    unregisters on hide/close.  After each change all visible chips are
    repositioned left-to-right along the bottom of the primary screen.
    """
    _chips: list[_MinimizedChip] = []

    @classmethod
    def register(cls, chip: _MinimizedChip) -> None:
        if chip not in cls._chips:
            cls._chips.append(chip)
        cls._reposition_all()

    @classmethod
    def unregister(cls, chip: _MinimizedChip) -> None:
        cls._chips = [c for c in cls._chips if c is not chip]
        cls._reposition_all()

    @classmethod
    def _reposition_all(cls) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        margin = 12
        x = geom.left() + margin
        bottom = geom.bottom() - margin
        for chip in cls._chips:
            if chip.isVisible():
                chip.adjustSize()
                chip.move(x, bottom - chip.height())
                x += chip.width() + margin

    @classmethod
    def close_all(cls) -> None:
        """
        Close and destroy every chip still registered.
        Call this during application shutdown so no Qt.Tool windows
        linger in the OS taskbar or system tray after the main window closes.
        """
        for chip in list(cls._chips):
            try:
                chip.restore_requested.disconnect()
            except Exception:
                pass
            try:
                chip.hide()
                chip.close()
                chip.deleteLater()
            except Exception:
                pass
        cls._chips.clear()
        
# ── ADD at module level, after the _ChipManager class ─────────────────────

def close_all_chips() -> None:
    """Convenience wrapper — call once during app shutdown."""
    _ChipManager.close_all()
# ─────────────────────────────────────────────────────────────────────────────
# CHIP WIDGET
# ─────────────────────────────────────────────────────────────────────────────

class _MinimizedChip(QWidget):
    """
    Small frameless widget shown at the bottom of the screen when its
    parent dialog is minimized.  Clicking anywhere on it restores the dialog.
    """
    restore_requested = Signal()

    def __init__(self, title: str) -> None:
        super().__init__(
            None,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("minimizedChip")
        self.setStyleSheet(_get_chip_style())
        self.setCursor(Qt.PointingHandCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(6)

        icon_lbl = QLabel("◼")
        icon_lbl.setObjectName("chipLabel")
        icon_lbl.setFixedWidth(14)
        lay.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("chipLabel")
        lay.addWidget(title_lbl)

        restore_btn = QPushButton("▲ Restore")
        restore_btn.setObjectName("chipRestoreBtn")
        restore_btn.clicked.connect(self.restore_requested.emit)
        lay.addWidget(restore_btn)

        self.adjustSize()

    def mousePressEvent(self, event) -> None:
        self.restore_requested.emit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        _ChipManager.register(self)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        _ChipManager.unregister(self)

    def closeEvent(self, event) -> None:
        _ChipManager.unregister(self)
        super().closeEvent(event)


# ─────────────────────────────────────────────────────────────────────────────
# MIXIN
# ─────────────────────────────────────────────────────────────────────────────

class MinimizableDialogMixin:
    """
    Mixin for QDialog subclasses that need compatibility helpers for
    minimize/restore handling.

    Current behavior uses native OS minimize/taskbar integration.
    Legacy chip APIs are kept for backward compatibility with existing
    call sites that still invoke `minimize_to_chip()` / `restore_from_chip()`.

    Usage
    -----
    class MyDialog(MinimizableDialogMixin, QDialog):
        def __init__(self, ...):
            super().__init__(...)
            self._init_minimize_state()
            # rest of setup ...
    """

    def _init_minimize_state(self) -> None:
        self._chip: _MinimizedChip | None = None
        self._saved_geometry = None

    # ── native minimize (no interception) ────────────────────────────────

    def changeEvent(self, event) -> None:
        super().changeEvent(event)

    # ── minimize / restore ────────────────────────────────────────────────

    def _cleanup_chip(self) -> None:
        chip, self._chip = self._chip, None
        if chip is None:
            return
        try:
            chip.restore_requested.disconnect()
        except Exception:
            pass
        try:
            chip.hide()
            chip.deleteLater()
        except Exception:
            pass

    def _do_minimize_to_chip(self) -> None:
        # Backward-compatible method name: route to native minimize.
        self._cleanup_chip()
        self.showMinimized()

    def _do_restore_from_chip(self) -> None:
        # Backward-compatible restore helper: native restore path.
        self._cleanup_chip()
        if self.windowState() & Qt.WindowMinimized:
            self.showNormal()
        else:
            try:
                self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            except Exception:
                pass
        self.show()
        self.raise_()
        self.activateWindow()

    # ── public helpers ────────────────────────────────────────────────────

    def minimize_to_chip(self) -> None:
        self._do_minimize_to_chip()

    def restore_from_chip(self) -> None:
        self._do_restore_from_chip()

    @property
    def _is_minimized_to_chip(self) -> bool:
        return self._chip is not None and not self.isVisible()
