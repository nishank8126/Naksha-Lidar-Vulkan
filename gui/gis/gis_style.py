"""Compact, widget-scoped visual language for Naksha's GIS subsystem.

Nothing here changes QApplication or ThemeManager. A GIS surface must opt in
explicitly, keeping the ribbon and every non-GIS tool outside these selectors.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHeaderView, QLayout, QTextBrowser, QWidget,
)

GIS_FONT_FAMILY = "Segoe UI"
GIS_FONT_SIZE = 11  # pixels, deliberately independent of application point-size/DPI styling
GIS_CONTROL_HEIGHT = 21
GIS_TOOL_HEIGHT = 20
GIS_ROW_HEIGHT = 19
GIS_ICON_SIZE = 14
GIS_MARGIN = 4
GIS_SPACING = 3

_GIS_QSS = """
QWidget[gisUi="true"] {
    background: #e9edf2; color: #1d2730; font-family: "Segoe UI"; font-size: 11px;
}
QWidget[gisUi="true"] * {
    color: #1d2730; font-family: "Segoe UI"; font-size: 11px;
}
QWidget[gisUi="true"] QToolTip {
    background: #ffffe1; color: #202020; border: 1px solid #707070; padding: 2px 4px;
}
QWidget[gisUi="true"] QLabel { background: transparent; padding: 0; }
QWidget[gisUi="true"] QLabel[secondary="true"] { color: #53616c; }
QWidget[gisUi="true"] QLabel[sectionHeader="true"] { color: #152b3b; font-weight: 600; }
QWidget[gisUi="true"] QLineEdit, QWidget[gisUi="true"] QComboBox,
QWidget[gisUi="true"] QSpinBox, QWidget[gisUi="true"] QDoubleSpinBox {
    min-height: 19px; max-height: 21px; padding: 0 3px; background: #ffffff;
    selection-background-color: #b8daf4; selection-color: #111111;
    border: 1px solid #9ba9b5; border-radius: 0;
}
QWidget[gisUi="true"] QPushButton {
    min-height: 20px; max-height: 22px; padding: 0 6px;
    border: 1px solid #929da6; border-radius: 0; background: #edf1f4;
}
QWidget[gisUi="true"] QPushButton:hover { background: #dceaf5; border-color: #648eae; }
QWidget[gisUi="true"] QPushButton:pressed { background: #c4dbea; }
QWidget[gisUi="true"] QPushButton:disabled { color: #8b9298; background: #e5e8ea; }
QWidget[gisUi="true"] QToolButton {
    min-width: 19px; min-height: 19px; max-height: 21px; padding: 0 2px;
    border: 1px solid transparent; border-radius: 0; background: transparent;
}
QWidget[gisUi="true"] QToolButton:hover, QWidget[gisUi="true"] QToolButton:checked {
    background: #d3e5f2; border-color: #779bb5;
}
QWidget[gisUi="true"] QTreeView, QWidget[gisUi="true"] QTableView,
QWidget[gisUi="true"] QListView, QWidget[gisUi="true"] QTextEdit,
QWidget[gisUi="true"] QTextBrowser {
    background: #ffffff; alternate-background-color: #f3f6f8;
    border: 1px solid #9ba9b5; gridline-color: #d8dee3;
    selection-background-color: #b8daf4; selection-color: #111111;
}
QWidget[gisUi="true"] QTreeView::item, QWidget[gisUi="true"] QListView::item,
QWidget[gisUi="true"] QTableView::item { min-height: 18px; padding: 0 2px; }
QWidget[gisUi="true"] QTreeView::item:hover, QWidget[gisUi="true"] QListView::item:hover,
QWidget[gisUi="true"] QTableView::item:hover { background: #e7f1f8; }
QWidget[gisUi="true"] QTreeView::item:selected, QWidget[gisUi="true"] QTableView::item:selected,
QWidget[gisUi="true"] QListView::item:selected { background: #b8daf4; color: #111111; }
QWidget[gisUi="true"] QHeaderView::section {
    min-height: 18px; max-height: 20px; padding: 0 3px;
    color: #17354a; background: #d9e5ee;
    border: 0; border-right: 1px solid #aab7c1; border-bottom: 1px solid #8798a5;
    font-size: 11px; font-weight: 600;
}
QWidget[gisUi="true"] QGroupBox {
    margin-top: 6px; padding-top: 2px; border: 1px solid #a6b2bc; background: #f4f6f8;
}
QWidget[gisUi="true"] QGroupBox::title { subcontrol-origin: margin; left: 4px; padding: 0 2px; }
QWidget[gisUi="true"] QTabWidget::pane { border: 1px solid #9ba9b5; background: #ffffff; }
QWidget[gisUi="true"] QTabBar::tab {
    min-height: 16px; padding: 1px 6px; background: #e3e9ee; border: 1px solid #a8b3bc;
}
QWidget[gisUi="true"] QTabBar::tab:selected { background: #ffffff; border-bottom-color: #ffffff; }
QWidget[gisUi="true"] QCheckBox, QWidget[gisUi="true"] QRadioButton { spacing: 3px; }
QWidget[gisUi="true"] QScrollBar:vertical { width: 11px; background: #edf0f2; }
QWidget[gisUi="true"] QScrollBar:horizontal { height: 11px; background: #edf0f2; }
QWidget[gisUi="true"] QSplitter::handle { background: #9ba9b5; width: 1px; height: 1px; }
"""


def compact_layout(layout: QLayout | None, margin: int = GIS_MARGIN, spacing: int = GIS_SPACING) -> None:
    if layout is not None:
        layout.setContentsMargins(margin, margin, margin, margin)
        layout.setSpacing(spacing)


def compact_view(view: QAbstractItemView) -> None:
    view.setAlternatingRowColors(True)
    view.setIconSize(QSize(GIS_ICON_SIZE, GIS_ICON_SIZE))
    if hasattr(view, "verticalHeader"):
        view.verticalHeader().setDefaultSectionSize(GIS_ROW_HEIGHT)


def compact_text_browser(browser: QTextBrowser) -> None:
    """Use dense, table-oriented typography for GIS metadata inspectors."""
    document = browser.document()
    document.setDocumentMargin(5)
    document.setDefaultStyleSheet("""
        body { font-family: 'Segoe UI'; font-size: 11px; color: #1d2730; margin: 0; background: #fff; }
        h3 { font-size: 12px; font-weight: 600; color: #17354a; margin: 2px 0 3px 0; }
        h4 { font-size: 11px; font-weight: 600; color: #17354a; margin: 5px 0 1px 0; }
        p { margin: 2px 0; }
        table { border-collapse: collapse; margin: 0; }
        th { font-size: 11px; font-weight: 600; text-align: left; color: #17354a; background: #d9e5ee; }
        th, td { font-size: 11px; padding: 0 4px 0 2px; border-bottom: 1px solid #d8dee3; }
        ul { margin: 2px 0 2px 14px; padding: 0; }
        li { margin: 0; padding: 0; }
        pre { font-family: Consolas; font-size: 11px; margin: 2px 0; }
    """)

def configure_header(header: QHeaderView, *, stretch_column: int = 0) -> None:
    header.setMinimumSectionSize(32)
    header.setStretchLastSection(False)
    for column in range(header.count()):
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    if 0 <= stretch_column < header.count():
        header.setSectionResizeMode(stretch_column, QHeaderView.Stretch)


def apply_gis_style(widget: QWidget, *, dialog: bool = False) -> QWidget:
    widget.setProperty("gisUi", True)
    font = QFont(GIS_FONT_FAMILY)
    font.setPixelSize(GIS_FONT_SIZE)
    widget.setFont(font)
    widget.setStyleSheet(_GIS_QSS)
    if dialog and isinstance(widget, QDialog):
        widget.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
    return widget


def apply_gis_dialog_style(widget: QWidget) -> QWidget:
    return apply_gis_style(widget, dialog=True)


def apply_gis_dock_style(widget: QWidget) -> QWidget:
    return apply_gis_style(widget)

