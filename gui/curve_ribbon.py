"""
Curve Ribbon for NakshaAI
Provides curve drawing tools with Nakshatech-style point-by-point workflow
"""

from PySide6.QtWidgets import QWidget, QHBoxLayout, QPushButton
from PySide6.QtCore import Signal


class CurveRibbon(QWidget):
    """Ribbon for Curve drawing tools"""

    curve_tool_selected = Signal(str)
    clear_curves = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        from gui.menu_sidebar_system import RibbonSection

        tools = RibbonSection("Curve Tools", self)

        tools.add_button(
            "Curve",
            "CP",
            lambda: self.curve_tool_selected.emit("curve_point"),
            toggleable=True
        )

        layout.addWidget(tools)

        actions = RibbonSection("Actions", self)
        actions.add_button(
             "Clear",
             "X",
             self.clear_curves.emit,
             toggleable=False
         )
        layout.addWidget(actions)
 
        layout.addStretch()
