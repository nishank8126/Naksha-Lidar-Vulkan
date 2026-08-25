"""
Menu-Based Ribbon System for NakshaAI
Displays all menu options horizontally in a ribbon layout
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, 
    QLabel, QFrame, QScrollArea, QGroupBox, QListWidget, QListWidgetItem,
    QCheckBox, QDoubleSpinBox, QSizePolicy, QDialog, QDialogButtonBox,
    QComboBox, QMessageBox, QAbstractItemView, QApplication, QRadioButton, QMenu  # ✅ ADD THIS
)
from PySide6.QtCore import Qt, Signal, QEvent, QSize, QSettings
from PySide6.QtGui import QFont, QPixmap, QIcon, QColor, QPainter, QPen
from html import escape
import json
import numpy as np
from scipy.spatial import cKDTree
from gui.prj_block_identifier import show_block_identifier_dialog
from .digitize_tools import PolylineSettingsDialog
from .curve_ribbon import CurveRibbon
from .digitize_tools import LineArrowSettingsDialog, PolylineSettingsDialog
from .draw_settings_dialog import DrawToolSettingsDialog
from .icon_provider import get_button_icon
# Add with other imports at the top of menu_sidebar_system.py (around line 10-15)
from gui.undo_context_manager import get_undo_context_manager


def _safe_populate_byclass(dlg):
    """Populate a By-Class-style dialog's class lists without ever blocking.

    Runs on a deferred event-loop tick so the dialog window paints instantly
    after classification (no UI freeze while a heavy main-view refresh is
    still queued). Any failure is swallowed — the dialog already shows.
    """
    if dlg is None:
        return
    try:
        if dlg._connect_display_dialog():
            if hasattr(dlg, "refresh_class_lists"):
                dlg.refresh_class_lists(
                    reason="Display Mode dialog attached", update_status=False
                )
            elif hasattr(dlg, "on_classes_changed"):
                dlg.on_classes_changed()
    except Exception:
        pass


def _safe_populate_byclass_closed(dlg):
    if dlg is None:
        return
    try:
        if dlg._connect_display_dialog() and hasattr(dlg, "on_classes_changed"):
            dlg.on_classes_changed()
    except Exception:
        pass


def _safe_populate_byclass_height(dlg):
    if dlg is None:
        return
    try:
        if dlg._connect_display_dialog() and hasattr(dlg, "on_classes_changed"):
            dlg.on_classes_changed()
    except Exception:
        pass


def _safe_populate_inside_fence(dlg):
    if dlg is None:
        return
    try:
        if dlg._connect_display_dialog() and hasattr(dlg, "on_classes_changed"):
            dlg.on_classes_changed()
    except Exception:
        pass

RIBBON_TOOLTIP_META = {
    ("FileRibbon", "File", "Open"): {
        "title": "Open File",
        "description": "Load point-cloud or project data into the current workspace.",
    },
    ("FileRibbon", "File", "Save"): {
        "title": "Save",
        "description": "Quick-save the current project to its active file.",
    },
    ("FileRibbon", "File", "Save As…"): {
        "title": "Save As",
        "description": "Save the current project to a new file or location.",
    },
    ("FileRibbon", "Vectors", "Export"): {
        "title": "Export Drawings",
        "description": "Export drawings to supported vector formats such as DXF, GeoJSON, or Shapefile.",
    },
    ("FileRibbon", "Vectors", "Import"): {
        "title": "Import Drawings",
        "description": "Import drawings from supported vector files into the current project.",
    },
    ("FileRibbon", "Attachments", "Attach DXF"): {
        "title": "Attach DXF",
        "description": "Attach one or more DXF overlay files to the current project.",
    },
    ("FileRibbon", "Attachments", "PRJ Loader"): {
        "title": "PRJ Block Identifier",
        "description": "Load a PRJ file and identify matching DXF block labels.",
    },
    ("FileRibbon", "Attachments", "Verify Labels"): {
        "title": "Verify Grid Labels",
        "description": "Compare DXF grid labels against the loaded LAZ or LAS coverage.",
    },
    ("FileRibbon", "Attachments", "Attach DWG"): {
        "title": "Attach DWG",
        "description": "Attach one or more DWG overlay files to the current project.",
    },
    ("FileRibbon", "Attachments", "Attach SNT"): {
        "title": "Attach SNT",
        "description": "Attach one or more SNT overlay files to the current project.",
    },
    ("FileRibbon", "Project", "Clear"): {
        "title": "Clear Project",
        "description": "Remove the current data and reset the workspace to a clean state.",
    },
    ("FileRibbon", "Project", "Clear Data"): {
        "title": "Clear Data",
        "description": "Remove point cloud data while preserving vector and attachment overlays.",
    },

    ("EditRibbon", "History", "Undo"): {
        "title": "Undo",
        "description": "Undo the most recent action.",
    },
    ("EditRibbon", "History", "Redo"): {
        "title": "Redo",
        "description": "Redo the last undone action.",
    },
    ("EditRibbon", "View History", "Back"): {
        "title": "Previous View",
        "description": "Move back to the previous pan or zoom view.",
    },
    ("EditRibbon", "View History", "Forward"): {
        "title": "Next View",
        "description": "Move forward to the next pan or zoom view.",
    },
    ("EditRibbon", "Clipboard", "Cut"): {
        "title": "Cut",
        "description": "Cut the current selection to the clipboard.",
    },
    ("EditRibbon", "Clipboard", "Copy"): {
        "title": "Copy",
        "description": "Copy the current selection to the clipboard.",
    },
    ("EditRibbon", "Clipboard", "Paste"): {
        "title": "Paste",
        "description": "Paste clipboard content into the current context.",
    },
    ("EditRibbon", "Delete", "Delete"): {
        "title": "Delete",
        "description": "Delete the current selection.",
    },
    ("EditRibbon", "Surface Models", "Create DTM"): {
        "title": "Create Digital Terrain Model",
        "description": "Build a bare-earth elevation grid from classified ground points.",
    },
    ("EditRibbon", "Surface Models", "Create DSM"): {
        "title": "Create Digital Surface Model",
        "description": "Build a top-surface elevation grid from the highest LiDAR returns.",
    },
    ("EditRibbon", "Surface Models", "Export DTM"): {
        "title": "Export DTM",
        "description": "Export the latest generated terrain model as a Float32 GeoTIFF.",
    },
    ("EditRibbon", "Surface Models", "Export DSM"): {
        "title": "Export DSM",
        "description": "Export the latest generated surface model as a Float32 GeoTIFF.",
    },
    ("ViewRibbon", "Views", "Top"): {
        "title": "Top View",
        "description": "Switch the main canvas to a top-down orthographic view.",
        "shortcut_tools": ("TopView",),
    },
    ("ViewRibbon", "Views", "Front"): {
        "title": "Front View",
        "description": "Switch the main canvas to a front orthographic view.",
    },
    ("ViewRibbon", "Views", "Side"): {
        "title": "Side View",
        "description": "Switch the main canvas to a side orthographic view.",
    },
    ("ViewRibbon", "Views", "3D"): {
        "title": "3D View",
        "description": "Switch the main canvas to the 3D perspective view.",
    },
    ("ViewRibbon", "Display", "Depth"): {
        "title": "Depth Display",
        "description": "Color the scene using depth-based shading.",
        "shortcut_tools": ("Depth",),
    },
    ("ViewRibbon", "Display", "RGB"): {
        "title": "RGB Display",
        "description": "Show point colors using the RGB values from the source data.",
        "shortcut_tools": ("RGB",),
    },
    ("ViewRibbon", "Display", "Intensity"): {
        "title": "Intensity Display",
        "description": "Show points using grayscale intensity values.",
        "shortcut_tools": ("Intensity",),
    },
    ("ViewRibbon", "Display", "Elevation"): {
        "title": "Elevation Display",
        "description": "Color points by elevation.",
        "shortcut_tools": ("Elevation",),
    },
    ("ViewRibbon", "Display", "Class"): {
        "title": "Class Display",
        "description": "Color points using the active classification palette.",
        "shortcut_tools": ("Class",),
    },
    ("ViewRibbon", "Display", "Shading"): {
        "title": "Shading Preset",
        "description": "Apply a saved shading preset to the current project.",
        "shortcut_tools": ("ShadingMode",),
    },
    ("ViewRibbon", "Display", "Surface"): {
        "title": "Surface Display",
        "description": "Render a terrain-style surface using elevation and slope shading.",
        "shortcut_tools": ("Surface",),
    },
    ("ViewRibbon", "Navigate", "Fit"): {
        "title": "Fit View",
        "description": "Fit the active view to all visible project content.",
    },
    ("ToolsRibbon", "Sections", "Cross"): {
        "title": "Cross Section",
        "description": "Create a cross-section from the main view.",
        "shortcut_tools": ("CrossSectionRect",),
    },
    ("ToolsRibbon", "Sections", "Cut Section"): {
        "title": "Cut Section",
        "description": "Create a cut section from the current view.",
        "shortcut_tools": ("CutSectionRect", "CutFromCross", "CutFromCut"),
    },
    ("ToolsRibbon", "Sync", "Views"): {
        "title": "Synchronize Views",
        "description": "Open view synchronization controls for linked navigation.",
    },
    ("ToolsRibbon", "Selection", "Config"): {
        "title": "Shortcut Configuration",
        "description": "Open the shortcut manager to create or edit custom shortcuts.",
    },
    ("ToolsRibbon", "Settings", "Backup"): {
        "title": "Backup Settings",
        "description": "Open backup settings for project safety and recovery.",
    },
    ("ToolsRibbon", "Settings", "Preference"): {
         "title": "Cross-Section Preferences",
         "description": "Adjust cross-section display and behavior settings.",
    },
    ("ClassifyRibbon", "Lines", "Above"): {
        "title": "Classify Above Line",
        "description": "Classify points that lie above a drawn line.",
        "shortcut_tools": ("AboveLine",),
    },
    ("ClassifyRibbon", "Lines", "Below"): {
        "title": "Classify Below Line",
        "description": "Classify points that lie below a drawn line.",
        "shortcut_tools": ("BelowLine",),
    },
    ("ClassifyRibbon", "Lines", "Parallel"): {
        "title": "Classify Parallel Band",
        "description": "Classify points inside a parallel band around a drawn line.",
        "shortcut_tools": ("ParallelLine",),
    },
    ("ClassifyRibbon", "Shapes", "Rect"): {
        "title": "Rectangle Selection",
        "description": "Classify points using a rectangular selection.",
        "shortcut_tools": ("Rectangle",),
    },
    ("ClassifyRibbon", "Shapes", "Circle"): {
        "title": "Circle Selection",
        "description": "Classify points using a circular selection.",
        "shortcut_tools": ("Circle",),
    },
    ("ClassifyRibbon", "Shapes", "Polygon"): {
        "title": "Polygon Selection",
        "description": "Classify points inside a closed polygon drawn by vertices.",
        "shortcut_tools": ("Polygon",),
    },
    ("ClassifyRibbon", "Shapes", "Free"): {
        "title": "Freehand Selection",
        "description": "Classify points using a freehand selection path.",
        "shortcut_tools": ("Freehand",),
    },
    ("ClassifyRibbon", "Points", "Brush"): {
        "title": "Brush Selection",
        "description": "Paint classifications onto points with a brush.",
        "shortcut_tools": ("Brush",),
    },
    ("ClassifyRibbon", "Points", "Point"): {
        "title": "Point Selection",
        "description": "Classify individual points directly.",
        "shortcut_tools": ("Point",),
    },
    ("DisplayRibbon", "Config", "Display"): {
        "title": "Display Mode",
        "description": "Open the display mode dialog and manage saved display presets.",
        "shortcut_tools": ("DisplayMode",),
    },
    ("DisplayRibbon", "Config", "Fields"): {
        "title": "Fields",
        "description": "View and manage point cloud attribute fields.",
        "shortcut_tools": ("Fields",),
    },
    ("MeasurementRibbon", "Distance", "Line"): {
        "title": "Measure Line",
        "description": "Measure straight-line distance between two points.",
        "shortcut_tools": ("MeasureLine",),
    },
    ("MeasurementRibbon", "Distance", "Path"): {
        "title": "Measure Path",
        "description": "Measure total distance along a multi-point path.",
        "shortcut_tools": ("MeasurePath",),
    },
    ("MeasurementRibbon", "Area", "Block"): {
        "title": "Block Area",
        "description": "Click inside a PRJ or SNT block to report its area.",
    },
    ("MeasurementRibbon", "Area", "Grid"): {
        "title": "Grid Area",
        "description": "Click inside an SNT grid cell to report its exact area.",
    },
    ("MeasurementRibbon", "Actions", "Clear"): {
        "title": "Clear Measurements",
        "description": "Remove all measurements from the scene.",
        "shortcut_tools": ("ClearMeasurements",),
    },
    ("MeasurementRibbon", "Settings", "Settings"): {
        "title": "Measurement Settings",
        "description": "Customise measurement line colour, width, and label font size.",
    },
    ("DrawRibbon", "Utilities", "Clear"): {
        "title": "Clear Drawings",
        "description": "Remove all drawings from the scene.",
    },
    ("DrawRibbon", "Utilities", "Settings"): {
        "title": "Draw Tool Settings",
        "description": "Customise draw tool appearance and saved styles.",
    },
    ("DrawRibbon", "Controls", "Select"): {
        "title": "Select Drawing",
        "description": "Select existing drawings and overlay entities, then move, rotate, or mirror them from one panel.",
    },
    ("DrawRibbon", "Controls", "Parallel"): {
        "title": "Parallel Drawing",
        "description": "Open the parallel drawing controls.",
    },
    ("DrawRibbon", "Controls", "Centerline"): {
        "title": "Interpolate Centerline",
        "description": "Generate an open centerline between two selected open line features.",
    },
    ("DrawRibbon", "Curve", "Curve"): {
        "title": "Curve Tool",
        "description": "Draw smooth curve features from clicked control points.",
    },
    ("DrawRibbon", "Curve", "Clear"): {
        "title": "Clear Curves",
        "description": "Remove all completed curve features and curve previews.",
    },
    ("DrawRibbon", "Curve", "Settings"): {
        "title": "Curve Settings",
        "description": "Customise curve colour, width, and preview appearance.",
    },
    ("DrawRibbon", "Controls", "Snap"): {
        "title": "Snap",
        "description": "Choose the active drawing snap mode.",
    },
    ("IdentificationRibbon", "Identify", "Identify"): {

        "title": "Identify Point",
        "description": "Inspect the class and coordinates of a clicked point.",
    },
    ("IdentificationRibbon", "Identify", "Zoom"): {
        "title": "Zoom Rectangle",
        "description": "Zoom to a rectangle drawn on the current view.",
    },
    ("IdentificationRibbon", "Identify", "Select"): {
        "title": "Rectangle Selection",
        "description": "Select points and overlay entities inside a rectangle.",
    },
    ("BlockRibbon", "Block", "Create Block"): {
        "title": "Create Block",
        "description": "Open block creation settings.",
    },
    ("ByClassRibbon", "By Class", "Convert"): {
        "title": "By Class Conversion",
        "description": "Convert one class to another using class-based rules.",
    },
    ("ByClassRibbon", "By Class", "Close"): {
        "title": "Closed Feature Conversion",
        "description": "Convert points around closed features using the closed-shape workflow.",
    },
    ("ByClassRibbon", "By Class", "Height"): {
        "title": "Height Conversion",
        "description": "Convert points based on height thresholds.",
    },
    ("ByClassRibbon", "By Class", "Fence"): {
        "title": "Fence Conversion",
        "description": "Convert points inside a selected fence or boundary.",
    },
    ("ByClassRibbon", "Algorithms", "Low Points"): {
        "title": "Low Points",
        "description": "Find and classify unusually low outlier points.",
    },
    ("ByClassRibbon", "Algorithms", "Isolated"): {
        "title": "Isolated Points",
        "description": "Detect and classify isolated outlier points.",
    },
    ("ByClassRibbon", "Algorithms", "Ground"): {
        "title": "Ground Classification",
        "description": "Run the ground classification workflow.",
    },
    ("ByClassRibbon", "Algorithms", "Surface"): {
        "title": "Below Surface",
        "description": "Classify points that fall below a derived surface.",
    },
}


def _ribbon_scope_label(ribbon_scope: str) -> str:
    if not ribbon_scope:
        return "Ribbon"
    return ribbon_scope.replace("Ribbon", "") or "Ribbon"


def _normalize_button_text(button_text: str) -> str:
    return button_text.replace("\n", " ").strip()


def _get_ribbon_tooltip_meta(ribbon_scope: str, section_title: str, button_text: str) -> dict:
    normalized = _normalize_button_text(button_text)
    meta = RIBBON_TOOLTIP_META.get((ribbon_scope, section_title, normalized))
    if meta:
        return meta
    return {
        "title": normalized,
        "description": f"{normalized} tool.",
    }


def _format_shortcut_label(modifier: str, key: str) -> str:
    key = (key or "").strip()
    modifier = (modifier or "none").strip().lower()
    labels = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "meta": "Meta"}
    parts = []
    if modifier and modifier != "none":
        for chunk in modifier.split("+"):
            chunk = chunk.strip().lower()
            if chunk:
                parts.append(labels.get(chunk, chunk.title()))
    key_label = "Space" if key == " " else key.upper()
    return "+".join(parts + [key_label]) if parts else key_label


def _collect_shortcuts_from_settings() -> list[tuple[str, str, str]]:
    settings = QSettings("NakshaAI", "LidarApp")
    shortcuts_data = settings.value("shortcuts", None)
    if shortcuts_data is None:
        return []
    try:
        entries = json.loads(shortcuts_data) if isinstance(shortcuts_data, str) else shortcuts_data
    except Exception:
        return []

    results = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        key = entry.get("key", "")
        modifier = entry.get("modifier", "none")
        if tool and key:
            results.append((str(tool), str(modifier), str(key)))
    return results


def _collect_shortcuts_from_app(app_window) -> list[tuple[str, str, str]]:
    results = []
    shortcuts = getattr(app_window, "shortcuts", {}) if app_window is not None else {}
    for combo, shortcut_info in shortcuts.items():
        try:
            modifier, key = combo
        except Exception:
            continue
        if not isinstance(shortcut_info, dict):
            continue
        tool = shortcut_info.get("tool")
        if tool and key:
            results.append((str(tool), str(modifier), str(key)))
    return results


def _get_tooltip_shortcuts(app_window, shortcut_tools) -> list[str]:
    if not shortcut_tools:
        return []
    target_tools = {tool for tool in shortcut_tools if tool}
    matches = set()

    for tool, modifier, key in _collect_shortcuts_from_app(app_window):
        if tool in target_tools:
            matches.add(_format_shortcut_label(modifier, key))

    for tool, modifier, key in _collect_shortcuts_from_settings():
        if tool in target_tools:
            matches.add(_format_shortcut_label(modifier, key))

    return sorted(matches)


class RibbonSection(QWidget):
    """A single section in the ribbon with toggle-capable buttons"""

    _POINT_SYNC_EXCLUSIVE_BUTTONS = {
        "ToolsRibbon": {"Cross", "Cut"},
        "DrawRibbon": {"Smart", "Line", "Polyline", "Rect", "Circle", "Free", "Text", "Vertex", "AccuDraw", "Select", "Parallel", "Centerline", "Curve"},
        "ClassifyRibbon": {"Above", "Below", "Parallel", "Rect", "Circle", "Polygon", "Free", "Brush", "Point"},
        "MeasurementRibbon": {"Line", "Path", "Block", "Grid"},
        "IdentificationRibbon": {"Identify", "Zoom", "Select"},
        "CurveRibbon": {"Curve"},
    }

    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setObjectName("ribbonSection")
        self.section_title = title
        self.ribbon_scope = parent.__class__.__name__ if parent is not None else None
        from PySide6.QtCore import Qt
        self.setAttribute(Qt.WA_StyledBackground, True)  # Required to render QSS box/borders
        self.active_button = None  # track which button is active
        self._button_count = 0
        self.setup_ui(title)
    

    def setup_ui(self, title):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(3)
        layout.setAlignment(Qt.AlignTop)
        
        # FIXED: Removed setMinimumHeight(108) which was causing excessive empty space below the ribbon tools
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

        title_label = QLabel(title)
        title_label.setObjectName("ribbonSectionTitle")
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        self.button_box = QWidget()
        self.button_box.setObjectName("ribbonSectionBox")
        self.button_box.setAttribute(Qt.WA_StyledBackground, True)

        self.button_layout = QHBoxLayout(self.button_box)
        self.button_layout.setSpacing(8)
        self.button_layout.setContentsMargins(8, 6, 8, 6)
        self.button_layout.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        layout.addWidget(self.button_box)

    def _find_app_window(self):
        widget = self.window()
        if widget is not None and hasattr(widget, "ribbon_manager"):
            return widget

        widget = self.parentWidget()
        while widget is not None:
            if hasattr(widget, "ribbon_manager"):
                return widget
            widget = widget.parentWidget()
        return None

    def _build_button_tooltip(self, btn: QPushButton) -> str:
        from gui.theme_manager import ThemeColors

        button_text = btn.property("ribbonText") or btn.text() or ""
        section_title = btn.property("ribbonSection") or self.section_title
        ribbon_scope = btn.property("ribbonScope") or self.ribbon_scope

        meta = _get_ribbon_tooltip_meta(ribbon_scope, section_title, button_text)
        title = meta.get("title", _normalize_button_text(button_text))
        description = meta.get("description", "")
        shortcut_items = _get_tooltip_shortcuts(
            self._find_app_window(),
            meta.get("shortcut_tools", ()),
        )

        title_color = ThemeColors.get("text_primary")
        body_color = ThemeColors.get("text_primary")
        muted_color = ThemeColors.get("text_secondary")
        divider_color = ThemeColors.get("border_light")

        shortcut_html = ""
        if shortcut_items:
            shortcut_label = "Shortcut" if len(shortcut_items) == 1 else "Shortcuts"
            shortcut_html = (
                f"<div style='margin-top:5px; padding-top:4px; "
                f"border-top:1px solid {divider_color};'>"
                f"<span style='font-size:7.6pt; color:{muted_color};'>{shortcut_label}</span>"
                f"<div style='font-size:8.6pt; font-weight:600; color:{title_color}; margin-top:1px;'>"
                f"{escape(', '.join(shortcut_items))}</div></div>"
            )

        return (
            f"<div style='min-width:188px;'>"
            f"<div style='font-size:9.8pt; font-weight:700; color:{title_color};'>{escape(title)}</div>"
            f"<div style='font-size:8.6pt; color:{body_color}; margin-top:3px; line-height:1.24;'>"
            f"{escape(description)}</div>"
            f"{shortcut_html}</div>"
        )

    def _refresh_button_tooltip(self, btn: QPushButton):
        btn.setToolTip(self._build_button_tooltip(btn))

    def eventFilter(self, obj, event):
        if isinstance(obj, QPushButton) and event.type() in (QEvent.Enter, QEvent.ToolTip):
            self._refresh_button_tooltip(obj)
        return super().eventFilter(obj, event)

    def add_button(self, text, icon_text, callback=None, toggleable=True):
        """
        Add a button to this ribbon section.
        - toggleable=True: button can stay pressed green
        - toggleable=False: acts like a simple push
        Uses SVG icons from icon_provider when available, falls back to text.
        """
        icon_size = 24

        btn = QPushButton()
        btn.setObjectName("ribbonButton")
        btn.setProperty("ribbonText", text)
        btn.setProperty("ribbonSection", self.section_title)
        btn.setProperty("ribbonScope", self.ribbon_scope)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFocusPolicy(Qt.NoFocus)

        icon = get_button_icon(
            text,
            section_title=self.section_title,
            ribbon_scope=self.ribbon_scope,
            size=icon_size,
        )
        if not icon.isNull():
            btn.setIcon(icon)
            btn.setIconSize(QSize(icon_size, icon_size))
        else:
            # Fallback to text (for any unmapped buttons)
            btn.setText(icon_text)
            font = btn.font()
            font.setPointSize(15)
            btn.setFont(font)

        btn.setFixedSize(44, 44)
        btn.setCheckable(toggleable)
        self._refresh_button_tooltip(btn)
        btn.installEventFilter(self)

        if callback:
            btn.clicked.connect(lambda checked, b=btn: self._on_button_click(b, callback))

        # Wrap button + label in a vertical container
        container = QWidget()
        container.setObjectName("ribbonButtonContainer")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(3)
        container_layout.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        container_layout.addWidget(btn, 0, Qt.AlignHCenter)

        label = QLabel(text)
        label.setObjectName("ribbonButtonLabel")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        label.setFixedWidth(64)
        container_layout.addWidget(label, 0, Qt.AlignHCenter)

        self.button_layout.addWidget(container)

        self._button_count += 1
        return btn

    def _on_button_click(self, btn, callback):
        """Handle button toggle states"""
        if btn.isChecked():
            # Global untoggle across all ribbons if this is a tool button being checked
            main_window = self._find_app_window()
            if main_window and hasattr(main_window, "ribbon_manager"):
                main_window.ribbon_manager.clear_all_tool_buttons(exclude_btn=btn)
            
            # Deactivate previously active button in this section if it's not this one
            if self.active_button and self.active_button != btn:
                self.active_button.setChecked(False)
            self.active_button = btn
        else:
            if self.active_button == btn:
                self.active_button = None

        self._deactivate_point_sync_for_tool_button(btn)

        # Emit or call connected function
        callback()

    def _deactivate_point_sync_for_tool_button(self, btn):
        ribbon_scope = btn.property("ribbonScope") or self.ribbon_scope
        ribbon_text = btn.property("ribbonText")

        if ribbon_text not in self._POINT_SYNC_EXCLUSIVE_BUTTONS.get(ribbon_scope, set()):
            return

        main_window = self.window()
        point_sync_tool = getattr(main_window, "point_sync_tool", None)
        if point_sync_tool and getattr(point_sync_tool, "active", False):
            try:
                point_sync_tool.deactivate()
            except Exception:
                pass


class FileRibbon(QWidget):
    open_file = Signal()
    save_file = Signal()          # keep: we will use this for "Save As..."
    save_quick = Signal()         # NEW: will be "Save" (no dialog)
    export_drawings = Signal()
    import_drawings = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.build_ribbon()
        
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        
        # File Operations
        file_ops = RibbonSection("File", self)

        file_ops.add_button("Open", "📂", self.open_file.emit, toggleable=False)

        # NEW: Save (no dialog)
        file_ops.add_button("Save", "💾", self.save_quick.emit, toggleable=False)

        # Existing Save renamed to Save As…
        file_ops.add_button("Save As…", "💾", self.save_file.emit, toggleable=False)

        layout.addWidget(file_ops)
        
        # Vector Export/Import Section
        vector_ops = RibbonSection("Vectors", self)
        vector_ops.add_button("Export", "📤", self._export_drawings)
        vector_ops.add_button("Import", "📥", self._import_drawings)
        layout.addWidget(vector_ops)

        # Combined attachment tools
        attachments = RibbonSection("Attachments", self)
        attachments.add_button(" DXF", "📎", self._attach_dxf)
        attachments.add_button("DWG", "📐", self._attach_dwg, toggleable=False)
        attachments.add_button("SNT", "🗂️", self._attach_snt, toggleable=False)
        attachments.add_button("PRJ", "📋", self._identify_blocks)
        attachments.add_button("Verify", "🔍", self._verify_grid_labels)

        layout.addWidget(attachments)

        # Project
        project = RibbonSection("Project", self)
        project.add_button("Clear", "🧹", self._clear_project)
        project.add_button("Clear Data", "🗑️", self._clear_point_cloud)
        layout.addWidget(project)
        
        layout.addStretch()
    
    def _export_drawings(self):
        """Show export dialog for drawings"""
        try:
            app = self.parent().parent().parent()
            from gui.vector_export import show_export_dialog
            show_export_dialog(app)
        except Exception as e:
            print(f"⚠️ Export failed: {e}")

    def _import_drawings(self):
        """Import GIS overlays (.shp/.tif/.geojson) into the Overlay Control Center."""
        try:
            app = self.parent().parent().parent()
            # Unified, auto-detecting import that registers each file as a managed
            # layer and opens the Overlay Control Center (Global-Mapper style).
            from gui.gis.gis_layers import unified_import_overlay
            unified_import_overlay(app)
        except Exception as e:
            print(f"⚠️ Import failed: {e}")
            # Fall back to the legacy format-picker dialog if anything goes wrong.
            try:
                from gui.vector_export import show_import_dialog
                show_import_dialog(app)
            except Exception:
                pass

    def _attach_dxf(self):
            """Attach DXF file with automatic PRJ detection"""
            try:
                app = self.parent().parent().parent()
            
                # Check if dialog exists AND is still valid (not deleted)
                if hasattr(app, 'dxf_dialog') and app.dxf_dialog is not None:
                    # Check if the dialog widget is still valid
                    try:
                        if app.dxf_dialog.isVisible():
                            # Dialog exists and is visible - restore and bring to front
                            app.dxf_dialog.setWindowState(
                                app.dxf_dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive
                            )
                            app.dxf_dialog.raise_()
                            app.dxf_dialog.activateWindow()
                        else:
                            # Dialog exists but is hidden - show it
                            app.dxf_dialog.show()
                            app.dxf_dialog.raise_()
                            app.dxf_dialog.activateWindow()
                    except (RuntimeError, ReferenceError):
                        # Dialog was deleted - create new one
                        app.dxf_dialog = None
                        from gui.dxf_attachment import show_multi_dxf_attachment_dialog
                        app.dxf_dialog = show_multi_dxf_attachment_dialog(app)
                else:
                    # Create new dialog
                    from gui.dxf_attachment import show_multi_dxf_attachment_dialog
                    app.dxf_dialog = show_multi_dxf_attachment_dialog(app)
            except Exception as e:
                print(f"⚠️ DXF attachment failed: {e}")
                import traceback
                traceback.print_exc()

    def _attach_dwg(self):
       
        try:
            app = self.parent().parent().parent()

            # Reuse existing dialog if still alive
            if hasattr(app, 'dwg_dialog') and app.dwg_dialog is not None:
                try:
                    if app.dwg_dialog.isVisible():
                        app.dwg_dialog.setWindowState(
                            app.dwg_dialog.windowState()
                            & ~Qt.WindowMinimized | Qt.WindowActive
                        )
                        app.dwg_dialog.raise_()
                        app.dwg_dialog.activateWindow()
                    else:
                        app.dwg_dialog.show()
                        app.dwg_dialog.raise_()
                        app.dwg_dialog.activateWindow()
                    return
                except (RuntimeError, ReferenceError):
                    app.dwg_dialog = None  # was deleted, fall through

            from gui.dwg_attachment import show_dwg_attachment_dialog
            app.dwg_dialog = show_dwg_attachment_dialog(app)

        except Exception as e:
            print(f"⚠️ DWG attachment failed: {e}")
            import traceback
            traceback.print_exc()  

    def _identify_blocks(self):
        """Load PRJ file and identify DXF blocks"""
        try:
            app = self.parent().parent().parent()
            
            # Check if dialog exists AND is still valid (not deleted)
            if hasattr(app, 'block_identifier_dialog') and app.block_identifier_dialog is not None:
                # Check if the dialog widget is still valid
                try:
                    if app.block_identifier_dialog.isVisible():
                        # Dialog exists and is visible - restore and bring to front
                        app.block_identifier_dialog.setWindowState(
                            app.block_identifier_dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive
                        )
                        app.block_identifier_dialog.raise_()
                        app.block_identifier_dialog.activateWindow()
                    else:
                        # Dialog exists but is hidden - show it
                        app.block_identifier_dialog.show()
                        app.block_identifier_dialog.raise_()
                        app.block_identifier_dialog.activateWindow()
                except (RuntimeError, ReferenceError):
                    # Dialog was deleted - create new one
                    app.block_identifier_dialog = None
                    from gui.prj_block_identifier import show_block_identifier_dialog
                    app.block_identifier_dialog = show_block_identifier_dialog(app)
            else:
                # Create new dialog
                from gui.prj_block_identifier import show_block_identifier_dialog
                app.block_identifier_dialog = show_block_identifier_dialog(app)
        except Exception as e:
            print(f"⚠️ Block identifier failed: {e}")
            import traceback
            traceback.print_exc()
            
    def _clear_project(self):
        """Clear project with confirmation"""
        try:
            app = self.parent().parent().parent()
            from gui.app_window import clear_project
            clear_project(app)
        except Exception as e:
            print(f"⚠️ Clear failed: {e}")

    def _clear_point_cloud(self):
        """Clear only point cloud data with confirmation"""
        try:
            app = self.parent().parent().parent()
            from gui.clear_project import clear_point_cloud
            clear_point_cloud(app)
        except Exception as e:
            print(f"⚠️ Clear point cloud failed: {e}")
            
    
    def _verify_grid_labels(self):
        """Verify DXF grid label positions vs actual LAZ data"""
        try:
            app = self.parent().parent().parent()
            if hasattr(app, 'grid_label_manager') and app.grid_label_manager:
                app.grid_label_manager.verify_dxf_labels()
            else:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "Not Available",
                    "Grid label system not initialized.\n\n"
                    "Load a DXF file first."
                )
        except Exception as e:
            print(f"⚠️ Verify labels failed: {e}")
            import traceback
            traceback.print_exc()

    def _list_dxf_text(self):
        """List all text entities in loaded DXF"""
        try:
            app = self.parent().parent().parent()
            if hasattr(app, 'grid_label_manager') and app.grid_label_manager:
                app.grid_label_manager.list_all_text_in_dxf()
            else:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self,
                    "Not Available",
                    "Grid label system not initialized.\n\n"
                    "Load a DXF file first."
                )
        except Exception as e:
            print(f"⚠️ List text failed: {e}")
            import traceback
            traceback.print_exc()

    def _attach_snt(self) -> None:
        """
        Open or restore the SNT attachment dialog.
        Mirrors _attach_dwg exactly:
        - app resolved via self.parent().parent().parent()
        - return guard after restore path prevents double-dialog creation
        - RuntimeError catch handles garbage-collected C++ Qt objects
        - No deferred Qt import (Qt already at module level)
        """
        try:
            app = self.parent().parent().parent()  # ✅ identical to _attach_dwg / _attach_dxf

            if hasattr(app, 'snt_dialog') and app.snt_dialog is not None:
                try:
                    if app.snt_dialog.isVisible():
                        # Restore from minimised state and bring to foreground
                        app.snt_dialog.setWindowState(
                            app.snt_dialog.windowState() & ~Qt.WindowMinimized | Qt.WindowActive
                        )
                        app.snt_dialog.raise_()
                        app.snt_dialog.activateWindow()
                    else:
                        # Dialog exists but was hidden — show it
                        app.snt_dialog.show()
                        app.snt_dialog.raise_()
                        app.snt_dialog.activateWindow()
                    return  # ✅ guard: prevents fall-through to dialog re-creation below

                except (RuntimeError, ReferenceError):
                    # Qt C++ object was garbage-collected — reset and fall through to recreate
                    app.snt_dialog = None

            # First launch OR after garbage-collection: create fresh dialog
            from gui.snt_attachment import show_snt_attachment_dialog
            app.snt_dialog = show_snt_attachment_dialog(app)

        except Exception as e:
            print(f"SNT attachment failed: {e}")
            import traceback
            traceback.print_exc()


# ============================================================================
# ============================================================================



class EditRibbon(QWidget):
    """Ribbon for Edit menu"""

    edit_action = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # Undo/Redo
        history = RibbonSection("History", self)
        history.add_button("Undo", "↶", lambda: self.edit_action.emit("undo"), toggleable=False)
        history.add_button("Redo", "↷", lambda: self.edit_action.emit("redo"), toggleable=False)
        layout.addWidget(history)

        # Manual main-view navigation history
        view_history = RibbonSection("View History", self)

        self.view_back_btn = view_history.add_button(
            "Back",
            "‹‹",
            self._go_previous_view,
            toggleable=False,
        )
        self.view_back_btn.setIcon(self._make_view_history_icon("back"))
        self.view_back_btn.setIconSize(QSize(24, 24))
        self.view_back_btn.setText("")
        self.view_back_btn.setText("")

        self.view_forward_btn = view_history.add_button(
            "Forward",
            "››",
            self._go_next_view,
            toggleable=False,
        )
        self.view_forward_btn.setIcon(self._make_view_history_icon("forward"))
        self.view_forward_btn.setIconSize(QSize(24, 24))
        self.view_forward_btn.setText("")
        self.view_forward_btn.setText("")

        layout.addWidget(view_history)

        # Clipboard
        clipboard = RibbonSection("Clipboard", self)
        clipboard.add_button("Cut", "✂️", lambda: self.edit_action.emit("cut"), toggleable=False)
        clipboard.add_button("Copy", "📋", lambda: self.edit_action.emit("copy"), toggleable=False)
        clipboard.add_button("Paste", "📎", lambda: self.edit_action.emit("paste"), toggleable=False)
        layout.addWidget(clipboard)

        # Delete
        delete_sec = RibbonSection("Delete", self)
        delete_sec.add_button("Delete", "🗑️", lambda: self.edit_action.emit("delete"), toggleable=False)
        layout.addWidget(delete_sec)

        # LiDAR elevation products
        surface_models = RibbonSection("Surface Models", self)
        surface_models.add_button(
            "Create DTM", "⛰", lambda: self._create_elevation_model("DTM"),
            toggleable=False,
        )
        surface_models.add_button(
            "Create DSM", "🏙", lambda: self._create_elevation_model("DSM"),
            toggleable=False,
        )
        surface_models.add_button(
            "Export DTM", "⇧", lambda: self._export_elevation_model("DTM"),
            toggleable=False,
        )
        surface_models.add_button(
            "Export DSM", "⇧", lambda: self._export_elevation_model("DSM"),
            toggleable=False,
        )
        layout.addWidget(surface_models)

        layout.addStretch()

    def _create_elevation_model(self, surface_type: str):
        try:
            from gui.elevation_models import create_elevation_model

            create_elevation_model(self.window(), surface_type)
        except Exception as exc:
            QMessageBox.critical(
                self.window(), f"Create {surface_type} Failed", str(exc)
            )

    def _export_elevation_model(self, surface_type: str):
        try:
            from gui.elevation_models import export_elevation_model

            export_elevation_model(self.window(), surface_type)
        except Exception as exc:
            QMessageBox.critical(
                self.window(), f"Export {surface_type} Failed", str(exc)
            )

    def _make_view_history_icon(self, direction: str) -> QIcon:
        """
        CAD-style previous/next view icon.
        Back    = double-left chevrons with stop marker
        Forward = double-right chevrons with stop marker
        """

        size = 24
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)

        try:
            from gui.theme_manager import ThemeColors
            color = ThemeColors.get("accent_alt")
        except Exception:
            color = "#38bdf8"

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)

        pen = QPen(QColor(color), 3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)

        if direction == "back":
            # First left chevron
            painter.drawLine(14, 6, 8, 12)
            painter.drawLine(8, 12, 14, 18)

            # Second left chevron
            painter.drawLine(20, 6, 14, 12)
            painter.drawLine(14, 12, 20, 18)

            # Stop/history marker
            marker_pen = QPen(QColor(color), 2)
            marker_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(marker_pen)
            painter.drawLine(5, 7, 5, 17)

        else:
            # First right chevron
            painter.drawLine(10, 6, 16, 12)
            painter.drawLine(16, 12, 10, 18)

            # Second right chevron
            painter.drawLine(4, 6, 10, 12)
            painter.drawLine(10, 12, 4, 18)

            # Stop/history marker
            marker_pen = QPen(QColor(color), 2)
            marker_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(marker_pen)
            painter.drawLine(19, 7, 19, 17)

        painter.end()

        return QIcon(pixmap)

    def _get_app_window(self):
        widget = self.window()

        if widget is not None and hasattr(widget, "ribbon_manager"):
            return widget

        widget = self.parentWidget()

        while widget is not None:
            if hasattr(widget, "ribbon_manager"):
                return widget

            widget = widget.parentWidget()

        return None

    def _go_previous_view(self):
        app = self._get_app_window()

        if app is None:
            print("View Back failed: app window not found")
            return

        if not hasattr(app, "go_to_previous_main_view"):
            print("View Back failed: go_to_previous_main_view() missing in app_window.py")
            try:
                app.statusBar().showMessage("View history is not available", 2000)
            except Exception:
                pass
            return

        app.go_to_previous_main_view()

    def _go_next_view(self):
        app = self._get_app_window()

        if app is None:
            print("View Forward failed: app window not found")
            return

        if not hasattr(app, "go_to_next_main_view"):
            print("View Forward failed: go_to_next_main_view() missing in app_window.py")
            try:
                app.statusBar().showMessage("View history is not available", 2000)
            except Exception:
                pass
            return

        app.go_to_next_main_view()

class ViewRibbon(QWidget):
    """Ribbon for View menu with compact amplifier controls."""

    view_changed = Signal(str)
    display_changed = Signal(str)
    shadow_toggled = Signal(bool)
    depth_toggled = Signal(bool)
    saturation_changed = Signal(int)
    sharpness_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._shadow_on = False
        self._depth_on = False
        self.current_saturation = 100  # Default 100%
        self.current_sharpness = 100   # Default 100%
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 📸 View Modes
        views = RibbonSection("Views", self)
        views.add_button("Top", "⬇️", lambda: self.view_changed.emit("top"))
        views.add_button("Front", "➡️", lambda: self.view_changed.emit("front"))
        views.add_button("Side", "⬅️", lambda: self.view_changed.emit("side"))
        views.add_button("3D", "🔄", lambda: self.view_changed.emit("3d"))
        layout.addWidget(views)

        # 🎨 Display Modes
        display = RibbonSection("Display", self)
        self.depth_btn = display.add_button("Depth", "🧱", self.toggle_depth)
        display.add_button("RGB", "🌈", lambda: self._switch_display_mode("rgb"))
        display.add_button("Intensity", "💡", lambda: self._switch_display_mode("intensity"))
        display.add_button("Elevation", "📊", lambda: self._switch_display_mode("elevation"))
        display.add_button("Class", "🏷️", lambda: self._switch_display_mode("class"))
        display.add_button("Shading", "🌓", lambda: self._switch_display_mode("shaded_class"))
        display.add_button("Surface", "⛰️", lambda: self._switch_display_mode("surface"))
        layout.addWidget(display)

        navigate = RibbonSection("Navigate", self)
        navigate.add_button(
            "Fit",
            "🧲",
            self._fit_view,
            toggleable=False
        )
        layout.addWidget(navigate)

        # layout.addStretch()

    def _switch_to_non_class_mode(self, mode):
        """
        Switch to non-classification mode and disable borders.
        ✅ Borders only work in Classification mode.
        """
        # Find parent app
        widget = self
        while widget:
            if hasattr(widget, 'point_border_percent'):
                # Disable borders for non-class modes
                widget._main_view_borders_active = False
                widget.point_border_percent = 0
                print(f"🔳 Borders DISABLED for {mode} mode")
                break
            widget = widget.parent()
        
        # Emit the display change
        self.display_changed.emit(mode)

    def _fit_view(self):
        widget = self
        while widget:
            if hasattr(widget, "fit_view_with_2d_lock"):
                widget.fit_view_with_2d_lock(source="ribbon_fit")
                return
            if hasattr(widget, "fit_view"):
                widget.fit_view()
                if hasattr(widget, "ensure_main_view_2d_interaction"):
                    try:
                        widget.ensure_main_view_2d_interaction(
                            preserve_camera=True,
                            reason="ribbon_fit_fallback",
                        )
                    except Exception:
                        pass
                return
            widget = widget.parent()

    def _on_sharpness_changed(self, value):
        """Amplifier removed — no-op. Signal is never connected."""
        pass  # Intentionally empty


    def _adjust_sharpness(self, delta):
        """Amplifier removed — no-op."""
        pass  # Intentionally empty

    def _reset_amplifiers(self):
        """Amplifier removed — no-op."""
        pass  # Intentionally empty
    # -----------------------------------------------------
    # 🧱 Depth Toggle
    # -----------------------------------------------------
    def toggle_depth(self):
        """
        Apply depth display mode (single click, like elevation/intensity).
        Shift+Click opens customization dialog.
        """
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        
        modifiers = QApplication.keyboardModifiers()
        
        if modifiers & Qt.ShiftModifier:
            # Shift+Click → Open depth settings dialog
            widget = self
            while widget:
                if hasattr(widget, '_open_depth_settings'):
                    widget._open_depth_settings()
                    return
                widget = widget.parent()
            print("⚠️ Depth settings dialog not available")
        else:
            # Normal click → Apply depth mode immediately
            # ✅ Disable borders when switching to Depth
            widget = self
            while widget:
                if hasattr(widget, 'point_border_percent'):
                    widget._main_view_borders_active = False
                    widget.point_border_percent = 0
                    print(f"🔳 Borders DISABLED for depth mode")
                    break
                widget = widget.parent()
            
            # Emit signal to apply depth display
            self.display_changed.emit("depth")
            print(f"📏 Depth mode applied")
        
        
    def _switch_display_mode(self, mode):
        """
        Switch display mode and manage border activation.
        ✅ Borders ONLY enabled for Classification mode.
        ✅ Borders disabled for all other modes.
        """
        mode = str(mode or "").lower().strip()
        from PySide6.QtWidgets import QApplication

        # Find parent app
        widget = self
        app = None
        while widget:
            if hasattr(widget, 'point_border_percent') and hasattr(widget, 'display_mode'):
                app = widget
                break
            widget = widget.parent()

        # Surface is expensive. If user clicks Surface again while already in Surface,
        # skip duplicate manual rebuild.
        if mode == "surface" and app is not None:
            if QApplication.keyboardModifiers() & Qt.ShiftModifier:
                try:
                    from gui.surface_mode import show_surface_gradient_controls
                    if show_surface_gradient_controls(app):
                        return
                except Exception as _surface_popup_err:
                    print(f"⚠️ Surface gradient settings popup failed: {_surface_popup_err}")
                return

            if str(getattr(app, "display_mode", "") or "").lower() == "surface" and \
                    getattr(app, "_surface_mesh_actor", None) is not None:
                print("⏭️ Surface already active — manual switch ignored")
                return
        
        if mode == "class":
            # ✅ ENABLE borders for Classification mode
            if app:
                # Restore border value from dialog if it exists
                if hasattr(app, 'display_mode_dialog') and app.display_mode_dialog:
                    dialog = app.display_mode_dialog
                    saved_border = dialog.view_borders.get(0, 0)  # Main View border
                    app.point_border_percent = float(saved_border)
                    app._main_view_borders_active = (saved_border > 0)
                    print(f"🔳 Borders ENABLED for Class mode: {saved_border}%")
                else:
                    # No dialog, enable if border value exists
                    app._main_view_borders_active = (app.point_border_percent > 0)
                    print(f"🔳 Borders ENABLED for Class mode: {app.point_border_percent}%")
        else:
            # ✅ DISABLE borders for all non-Class modes
            if app:
                app._main_view_borders_active = False
                app.point_border_percent = 0  # ✅ CRITICAL: Set to 0 to prevent rendering
                print(f"🔳 Borders DISABLED for {mode} mode (forced to 0%)")

        # Emit the display change (this triggers set_display_mode which will re-render)
        self.display_changed.emit(mode)

    def _update_button_style(self, button, active: bool):
        """Highlight button when active."""
        from gui.theme_manager import get_active_button_style, get_inactive_button_style
        if active:
            button.setStyleSheet(get_active_button_style())
        else:
            button.setStyleSheet(get_inactive_button_style())



class ToolsRibbon(QWidget):
    """Ribbon for Tools menu"""
    
    tool_activated = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tool_buttons = {}  
        self.build_ribbon()
        
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # ------------------------
        # ✂️ Sections: Cross / Cut / Width
        # ------------------------
        sections = RibbonSection("Sections", self)
        sections.add_button("Cross", "📐",
                            lambda: self.tool_activated.emit("cross_section"))
        sections.add_button("Cut", "🔪",
                            lambda: self.tool_activated.emit("cut_section"))
        layout.addWidget(sections)
        
        # ------------------------
        # 🔄 Sync: (for Sync Views dialog – single button)
        # ------------------------
        sync_sec = RibbonSection("Sync", self)
        sync_sec.add_button("Views", "🔄",
                            lambda: self.tool_activated.emit("sync_views"),
                            toggleable=False)
        layout.addWidget(sync_sec)
        
        # ------------------------
        # 🎯 Selection
        # ------------------------
        selection = RibbonSection("Selection", self)
        # selection.add_button(
        #     "Fence",
        #     "🔺",
        #     lambda: self.tool_activated.emit("load_by_fence"),
        #     toggleable=False,
        # )
        # selection.add_button(
        #     "FenceClr",
        #     "🧹",
        #     lambda: self.tool_activated.emit("clear_fence_load"),
        #     toggleable=False,
        # )
        # selection.add_button("Select", "🖱️",
        #                      lambda: self.tool_activated.emit("element_selection"))
        selection.add_button("Config", "⌨️", self._configure_shortcuts)
        layout.addWidget(selection)
        
        # ------------------------
        # ⚙️ Settings
        # ------------------------
        settings = RibbonSection("Settings", self)
        settings.add_button("Backup", "💾",
                            lambda: self.tool_activated.emit("backup_settings"))
        settings.add_button("Preference", "🎛️",
                            lambda: self.tool_activated.emit("cross_settings"))
        layout.addWidget(settings)
        
        layout.addStretch() 
        
    def _configure_shortcuts(self):
        """Show shortcut configuration dialog"""
        try:
            app = self.parent().parent().parent()
            
            # Check if dialog exists AND is still valid (not deleted)
            if hasattr(app, 'shortcut_manager_dialog') and app.shortcut_manager_dialog is not None:
                # Check if the dialog widget is still valid
                try:
                    dlg = app.shortcut_manager_dialog
                    # Restore from taskbar/minimized state
                    if dlg.isMinimized():
                        dlg.setWindowState(dlg.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
                    else:
                        dlg.show()
                    dlg.raise_()
                    dlg.activateWindow()
                except (RuntimeError, ReferenceError):
                    # Dialog was deleted - create new one
                    app.shortcut_manager_dialog = None
                    from gui.shortcut_manager import ShortcutManager
                    app.shortcut_manager_dialog = ShortcutManager.open_manager(app)
            else:
                # Create new dialog
                from gui.shortcut_manager import ShortcutManager
                app.shortcut_manager_dialog = ShortcutManager.open_manager(app)
        except Exception as e:
            print(f"⚠️ Shortcut config failed: {e}")
            import traceback
            traceback.print_exc()

class ClassifyRibbon(QWidget):
    """Ribbon for Classify menu"""
    
    classify_tool_selected = Signal(str)

    # Display modes that block classification tools
    _RESTRICTED_MODES = {"depth", "rgb", "intensity", "elevation"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.build_ribbon()

    # ------------------------------------------------------------------
    # Guard: check current display mode before activating any tool
    # ------------------------------------------------------------------
    def _try_activate_tool(self, tool_name: str):
        """
        Emit classify_tool_selected only when the active display mode is
        'class' or 'shaded_class'.  For Depth / RGB / Intensity / Elevation
        views show a friendly info popup instead.
        """
        # Walk up the widget tree to find the main app that owns display_mode
        app = None
        widget = self
        while widget:
            if hasattr(widget, "display_mode"):
                app = widget
                break
            widget = widget.parent()

        current_mode = getattr(app, "display_mode", "class") if app else "class"

        if current_mode in self._RESTRICTED_MODES:
            mode_label = {
                "depth": "Depth",
                "rgb": "RGB",
                "intensity": "Intensity",
                "elevation": "Elevation",
            }.get(current_mode, current_mode.capitalize())
            print(
                f"🔁 Classification tool '{tool_name}' requested in {mode_label} mode "
                f"— switching Main View to Class mode first"
            )
            try:
                if app is not None:
                    # Classification edits are valid in all modes, but class-color
                    # feedback is only visible in Class/Shading. Force Class mode
                    # so user immediately sees updated classification in Main View.
                    if hasattr(app, "_shading_visibility_override"):
                        del app._shading_visibility_override
                    if hasattr(app, "set_display_mode"):
                        app.set_display_mode("class")
                    else:
                        app.display_mode = "class"
                        from gui.pointcloud_display import update_pointcloud
                        update_pointcloud(app, "class")
                    QApplication.processEvents()
                    try:
                        if hasattr(app, "vtk_widget") and hasattr(app.vtk_widget, "render"):
                            app.vtk_widget.render()
                        elif hasattr(app, "vtk_widget") and hasattr(app.vtk_widget, "GetRenderWindow"):
                            app.vtk_widget.GetRenderWindow().Render()
                    except Exception as _render_err:
                        print(f"⚠️ Explicit render after class-mode switch failed: {_render_err}")
                    if hasattr(app, "statusBar"):
                        app.statusBar().showMessage(
                            f"Switched Main View from {mode_label} to Class mode for classification",
                            3000
                        )
            except Exception as _mode_switch_err:
                print(f"⚠️ Auto-switch to Class mode failed: {_mode_switch_err}")


        if tool_name == "brush" and app is not None:
            modifiers = QApplication.keyboardModifiers()
            app._brush_settings_requested_from_ui = bool(modifiers & Qt.ShiftModifier)
        if tool_name == "parallel_line" and app is not None:
            modifiers = QApplication.keyboardModifiers()
            app._parallel_line_settings_requested_from_ui = bool(modifiers & Qt.ShiftModifier)

        # Safe to proceed
        self.classify_tool_selected.emit(tool_name)

    def _show_restriction_popup(self, current_mode: str):
        """Show a styled, informative warning popup."""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
        from PySide6.QtCore import Qt

        mode_label = {
            "depth":     "Depth",
            "rgb":       "RGB",
            "intensity": "Intensity",
            "elevation": "Elevation",
        }.get(current_mode, current_mode.capitalize())

        try:
            from gui.theme_manager import get_dialog_stylesheet, ThemeColors
            sheet = get_dialog_stylesheet()
            accent   = ThemeColors.get("accent", "#f59e0b")
            txt_pri  = ThemeColors.get("text_primary", "#f1f5f9")
            txt_sec  = ThemeColors.get("text_secondary", "#94a3b8")
            btn_bg   = ThemeColors.get("bg_button", "#334155")
        except Exception:
            sheet = ""
            accent, txt_pri, txt_sec, btn_bg = "#f59e0b", "#f1f5f9", "#94a3b8", "#334155"

        # ── Dialog ──────────────────────────────────────────────────────
        dlg = QDialog(self.window(), Qt.Dialog | Qt.FramelessWindowHint)
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setStyleSheet(sheet + f"""
            QDialog {{
                border: 2px solid {accent};
                border-radius: 12px;
            }}
        """)
        dlg.setFixedWidth(380)

        outer = QVBoxLayout(dlg)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header bar ──────────────────────────────────────────────────
        header = QLabel(f"  ⚠️  Classification Unavailable")
        header.setFixedHeight(42)
        header.setStyleSheet(f"""
            QLabel {{
                background-color: {accent};
                color: #0f172a;
                font-weight: bold;
                font-size: 13px;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
                padding-left: 8px;
            }}
        """)
        outer.addWidget(header)

        # ── Body ────────────────────────────────────────────────────────
        body = QVBoxLayout()
        body.setContentsMargins(20, 16, 20, 20)
        body.setSpacing(10)

        icon_lbl = QLabel("🚫")
        icon_lbl.setAlignment(Qt.AlignCenter)
        icon_lbl.setStyleSheet("font-size: 36px; background: transparent;")
        body.addWidget(icon_lbl)

        msg = QLabel(
            f"<b>Classification tools are disabled</b><br>"
            f"while <b>{mode_label}</b> view is active.<br><br>"
            f"Please switch to <b>Class</b> or <b>Shading</b> view<br>"
            f"from the <i>View → Display</i> ribbon to use<br>"
            f"classification tools."
        )
        msg.setAlignment(Qt.AlignCenter)
        msg.setWordWrap(True)
        msg.setStyleSheet(f"""
            QLabel {{
                color: {txt_pri};
                font-size: 12px;
                line-height: 1.6;
                background: transparent;
            }}
        """)
        body.addWidget(msg)

        hint = QLabel("💡 Tip: Use  View › Class  or  View › Shading")
        hint.setAlignment(Qt.AlignCenter)
        hint.setStyleSheet(f"""
            QLabel {{
                color: {txt_sec};
                font-size: 10px;
                background: transparent;
                padding: 4px 8px;
                border: 1px solid {accent};
                border-radius: 6px;
            }}
        """)
        body.addWidget(hint)

        # OK button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("  Got it  ")
        ok_btn.setFixedHeight(34)
        ok_btn.setCursor(Qt.PointingHandCursor)
        ok_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {accent};
                color: #0f172a;
                font-weight: bold;
                font-size: 12px;
                border-radius: 7px;
                padding: 4px 20px;
                border: none;
            }}
            QPushButton:hover {{
                background-color: #fbbf24;
            }}
            QPushButton:pressed {{
                background-color: #d97706;
            }}
        """)
        ok_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(ok_btn)
        btn_row.addStretch()
        body.addLayout(btn_row)

        outer.addLayout(body)
        dlg.exec()

    # ------------------------------------------------------------------
    # Build the ribbon – all buttons route through _try_activate_tool
    # ------------------------------------------------------------------
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # Line Tools
        lines = RibbonSection("Lines", self)
        lines.add_button("Above", "⬆️", lambda: self._try_activate_tool("above_line"))
        lines.add_button("Below", "⬇️", lambda: self._try_activate_tool("below_line"))
        lines.add_button("Parallel", "∥", lambda: self._try_activate_tool("parallel_line"))
        layout.addWidget(lines)
        
        # Shape Selection
        shapes = RibbonSection("Shapes", self)
        shapes.add_button("Rect",   "⬜", lambda: self._try_activate_tool("rectangle"))
        shapes.add_button("Circle", "⭕", lambda: self._try_activate_tool("circle"))
        shapes.add_button("Polygon", "⬟", lambda: self._try_activate_tool("polygon"))
        shapes.add_button("Free",   "✏️", lambda: self._try_activate_tool("freehand"))
        layout.addWidget(shapes)
        
        # Point Tools
        points = RibbonSection("Points", self)
        points.add_button("Brush", "🖌️", lambda: self._try_activate_tool("brush"))
        points.add_button("Point", "📍", lambda: self._try_activate_tool("point"))
        layout.addWidget(points)

        layout.addStretch()

class DisplayRibbon(QWidget):
    """Ribbon for Display menu"""
    
    display_mode_clicked = Signal()
    fields_clicked = Signal()
    border_width_changed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Display Mode (Ctrl+D to Apply)")

        self.resize(850, 600)
        self.current_ptc_path = None
        self.current_border_value = 0
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Config
        config = RibbonSection("Config", self)
        config.add_button("Display", "🎛️", self.display_mode_clicked.emit)
        config.add_button("Fields", "📋", self.fields_clicked.emit)
        layout.addWidget(config)
        layout.addStretch()
    
    def increase_border(self):
        """Increase border value by 5%"""
        self.current_border_value = min(50, self.current_border_value + 5)
        self.update_border_display()
    
    def decrease_border(self):
        """Decrease border value by 5%"""
        self.current_border_value = max(0, self.current_border_value - 5)
        self.update_border_display()
    def update_border_display(self):
        """Update the display and emit signal with immediate re-render"""
        value = self.current_border_value
        self.border_label.setText(f"Border: {value}%")
        self.value_display.setText(f"{value}%")
        
        print(f"🔵 Border changed to: {value}%")
        
        # ✅ CRITICAL: Find main window and apply border immediately
        try:
            widget = self
            while widget is not None:
                widget = widget.parent()
                if hasattr(widget, 'on_border_changed'):
                    print(f"🔵 Found main window, applying border {value}%")
                    
                    # Update the border value in app
                    widget.point_border_percent = value
                    
                    # ✅ FIXED: Trigger immediate re-render based on display mode
                    if widget.display_mode == "class":
                        from gui.class_display import update_class_mode
                        update_class_mode(widget)
                        print(f"   ✅ Re-rendered in class mode")
                        
                    elif widget.display_mode == "shaded_class":
                        from gui.shading_display import update_shaded_class
                        update_shaded_class(
                            widget,
                            getattr(widget, "last_shade_azimuth", 45.0),
                            getattr(widget, "last_shade_angle", 45.0),
                            getattr(widget, "shade_ambient", 0.2)
                        )
                        print(f"   ✅ Re-rendered in shaded_class mode")
                        
                    else:
                        from gui.pointcloud_display import update_pointcloud
                        update_pointcloud(widget, widget.display_mode)
                        print(f"   ✅ Re-rendered in {widget.display_mode} mode")
                    
                    # Also emit signal for any connected slots
                    self.border_width_changed.emit(value)
                    
                    # Update status bar
                    if hasattr(widget, 'statusBar'):
                        widget.statusBar().showMessage(f"🔳 Border: {value}% applied", 2000)
                    
                    break
            else:
                print(f"⚠️ Could not find main window with on_border_changed method")
                
        except Exception as e:
            print(f"⚠️ Failed to update border: {e}")
            import traceback
            traceback.print_exc()

class DrawRibbon(QWidget):
    """Ribbon for Draw menu"""
   
    draw_tool_selected = Signal(str)
    curve_tool_selected = Signal(str)
    clear_requested = Signal()
   
    def __init__(self, parent=None):

        super().__init__(parent)
        self.build_ribbon()
       
    #     layout.setContentsMargins(0, 0, 0, 0)
    #     layout.setSpacing(10)
       
    #     # Draw Tools
       
    #     tools.add_button("Smart\nLine", "🔮", lambda: self._handle_smartline_click())
    #     tools.add_button("Line", "📏", lambda: self._handle_line_click())
    #     tools.add_button("Polyline", "⬡", lambda: self._handle_polyline_click())
    #     tools.add_button("Rect", "⬜", lambda: self.draw_tool_selected.emit("Rectangle"))
    #     tools.add_button("Circle", "⭕", lambda: self.draw_tool_selected.emit("Circle"))
    #     tools.add_button("Free", "✏️", lambda: self.draw_tool_selected.emit("Freehand"))
    #     tools.add_button("Text", "📝", lambda: self.draw_tool_selected.emit("Text"))
       
    #     # ✅ NEW: Move Vertex button
    #     tools.add_button("Move\nVertex", "🔄", lambda: self._handle_move_vertex_click())
       
    #     tools.add_button("Vertex", "🔵", lambda: self._handle_vertex_click())
    #     layout.addWidget(tools)
 
    #     # Actions
    #     actions.add_button("Clear", "🗑️", self.clear_requested.emit)
    #     layout.addWidget(actions)
       
    #     layout.addStretch()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # Draw Tools
        tools = RibbonSection("Tools", self)
        
        tools.add_button("Smart", "🔮", lambda: self._handle_smartline_click())
        tools.add_button("Line", "📏", lambda: self._handle_line_click())
        tools.add_button("Ortho", "📐", lambda: self._handle_ortho_click())
        tools.add_button("Polyline", "⬡", lambda: self._handle_polyline_click())
        tools.add_button("Rect",  "⬜", lambda: self._handle_rect_click())
        tools.add_button("Circle","⭕", lambda: self._handle_circle_click())
        tools.add_button("Free",  "✏️", lambda: self._handle_freehand_click())
        tools.add_button("Text", "📝", self._handle_text_click)

        self.vertex_btn = tools.add_button("Vertex", "🔵", lambda: self._handle_vertex_click())
        tools.add_button("Curve", "〰️", lambda: self._handle_curve_click())
        tools.add_button("Hatch", "▦", lambda: self._handle_hatch_click())
        tools.add_button("AccuDraw", "XYZ", lambda: self._handle_accudraw_click())
        layout.addWidget(tools)

        controls_section = RibbonSection("Controls", self)

        controls_section.add_button(
            "Select", "🎯",
            lambda: self._handle_select_drawing_click()
        )
        
        # controls_section.add_button(
        #     "Deselect\nAll", "⛔",
        #     lambda: self._handle_deselect_all_click()
        # )

        controls_section.add_button(
            "Parallel", ".Parallel",
            lambda: self._handle_parallel_click()
        )

        controls_section.add_button(
            "Centerline", "≋",
            lambda: self._handle_centerline_click()
        )

        self.snap_btn = controls_section.add_button(
            "Snap", "⌖",
            lambda: self._show_snap_menu(self.snap_btn),
            toggleable=False
        )
        layout.addWidget(controls_section)

        utilities_section = RibbonSection("Utilities", self)
        utilities_section.add_button("Clear", "🗑️", self._handle_clear_click)
        utilities_section.add_button(
            "Settings", "⚙️",
            lambda: self._show_draw_settings(),
            toggleable=False
        )
        layout.addWidget(utilities_section)
        
        layout.addStretch()

    def _deactivate_accudraw_before_other_tool(self, next_tool=""):
        """Cancel unfinished AccuDraw draft and deactivate AccuDraw before switching tools."""
        try:
            main_window = self.window()
            digitizer = getattr(main_window, "digitizer", None)
            accudraw_tool = getattr(digitizer, "accudraw_tool", None) if digitizer else None

            if accudraw_tool and getattr(accudraw_tool, "active", False):
                if hasattr(accudraw_tool, "finish_for_tool_switch"):
                    accudraw_tool.finish_for_tool_switch(next_tool)
                else:
                    accudraw_tool.deactivate(cancel=True)

                print(f"📐 AccuDraw cleaned before {next_tool}")
        except Exception as e:
            print(f"⚠️ AccuDraw cleanup failed before {next_tool}: {e}")

    def _handle_curve_click(self):
        """Activate Curve tool after cancelling unfinished AccuDraw, if needed."""
        self._deactivate_accudraw_before_other_tool("Curve")
        self.curve_tool_selected.emit("curve_point")

    def _handle_accudraw_click(self):
        """Activate separate AccuDraw XYZ/Angle tool."""
        print("📐 Normal AccuDraw activation")
        self.draw_tool_selected.emit("accudraw")

    def _handle_text_click(self):
        """
        Normal click  : activate Draw Text tool.
        Shift + click : open Draw Text visibility popup.
        """

        try:
            modifiers = QApplication.keyboardModifiers()
            shift_pressed = bool(modifiers & Qt.ShiftModifier)
        except Exception:
            shift_pressed = False

        if shift_pressed:
            app = self.window()
            digitizer = getattr(app, "digitizer", None)

            if digitizer is not None and hasattr(digitizer, "show_draw_text_visibility_dialog"):
                digitizer.show_draw_text_visibility_dialog(parent=app)
                return

            try:
                app.statusBar().showMessage("Draw text visibility option is not available", 2500)
            except Exception:
                pass
            return

        self._deactivate_accudraw_before_other_tool("Text")
        self.draw_tool_selected.emit("Text")

    def _show_snap_menu(self, button):
        if button is None:
            button = getattr(self, "snap_btn", None)
        if button is None:
            return

        main_window   = self.window()
        digitizer     = getattr(main_window, "digitizer", None)
        current_mode  = (
            getattr(digitizer, "snap_mode", None)
            if digitizer and getattr(digitizer, "snap_enabled", False)
            else None
        )

        menu = QMenu(self)
        nearby_action    = menu.addAction("Nearby Snap")
        center_action    = menu.addAction("Center Snap")
        keypoint_action  = menu.addAction("Key Point Snap")
        midpoint_action  = menu.addAction("Mid Point Snap")
        intercept_action = menu.addAction("Intercept Snap")

        for action, mode in (
            (nearby_action,    "nearby"),
            (center_action,    "center"),
            (keypoint_action,  "keypoint"),
            (midpoint_action,  "midpoint"),
            (intercept_action, "intercept"),
        ):
            action.setCheckable(True)
            action.setChecked(current_mode == mode)

        # Use lambdas: checkable actions emit triggered(bool) which would
        # break the no-argument handlers without the wrapper.
        nearby_action.triggered.connect(lambda _=False: self._enable_nearby_snap())
        center_action.triggered.connect(lambda _=False: self._enable_center_snap())
        keypoint_action.triggered.connect(lambda _=False: self._enable_keypoint_snap())
        midpoint_action.triggered.connect(lambda _=False: self._enable_midpoint_snap())
        intercept_action.triggered.connect(lambda _=False: self._enable_intercept_snap())
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def _enable_nearby_snap(self):
        self._set_snap_mode("nearby", "Nearby Snap")

    def _enable_center_snap(self):
        self._set_snap_mode("center", "Center Snap")

    def _enable_keypoint_snap(self):
        self._set_snap_mode("keypoint", "Key Point Snap")

    def _enable_midpoint_snap(self):
        self._set_snap_mode("midpoint", "Mid Point Snap")

    def _enable_intercept_snap(self):
        self._set_snap_mode("intercept", "Intercept Snap")

    def _set_snap_mode(self, mode, label):
        main_window = self.window()
        digitizer = getattr(main_window, "digitizer", None)
        if not digitizer:
            return

        already_enabled = (
            getattr(digitizer, "snap_enabled", False)
            and getattr(digitizer, "snap_mode", None) == mode
        )

        if already_enabled:
            digitizer.snap_enabled = False
            digitizer.snap_mode = None
            if hasattr(digitizer, "_hide_snap_marker_now"):
                digitizer._hide_snap_marker_now()
            message = f"{label} disabled"
        else:
            digitizer.snap_enabled = True
            digitizer.snap_mode = mode
            message = f"{label} enabled"

        if hasattr(main_window, "statusBar"):
            main_window.statusBar().showMessage(message, 2000)

    def _handle_rotate_click(self):
        """Rotate selected drawing using DigitizeManager from digitize_tools.py."""
        main_window = self.window()
        try:
            digitizer = getattr(main_window, "digitizer", None)
            if digitizer is None:
                raise RuntimeError("Digitizer is not initialized")

            digitizer.show_rotate_dialog()
        except Exception as e:
            print(f"⚠️ Rotate failed: {e}")
            try:
                main_window.statusBar().showMessage("Rotate failed", 3000)
            except Exception:
                pass

    def _handle_mirror_click(self, button=None):
        """Show Mirror X/Y/custom axis menu using DigitizeManager from digitize_tools.py."""
        main_window = self.window()
        try:
            digitizer = getattr(main_window, "digitizer", None)
            if digitizer is None:
                raise RuntimeError("Digitizer is not initialized")

            digitizer.show_mirror_menu(button)
        except Exception as e:
            print(f"⚠️ Mirror failed: {e}")
            try:
                main_window.statusBar().showMessage("Mirror failed", 3000)
            except Exception:
                pass

    def _get_clear_fence_dialog(self):
        dialog = getattr(self, "_clear_fence_dialog_ref", None)
        if dialog is None:
            return None
        try:
            dialog.windowTitle()
        except (RuntimeError, ReferenceError):
            self._clear_fence_dialog_ref = None
            return None
        return dialog

    def _clear_layout_widgets(self, layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            child_layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif child_layout is not None:
                self._clear_layout_widgets(child_layout)

    def _create_clear_fence_dialog(self):
        from gui.theme_manager import get_dialog_stylesheet, ThemeColors

        dialog = QDialog(None, Qt.Window)
        dialog.setAttribute(Qt.WA_QuitOnClose, False)
        dialog.setAttribute(Qt.WA_DeleteOnClose, False)
        dialog.setWindowTitle("Clear Classified Fences")
        dialog.setWindowModality(Qt.NonModal)
        dialog.resize(420, 400)
        dialog.setStyleSheet(get_dialog_stylesheet())

        layout = QVBoxLayout(dialog)
        layout.setSpacing(8)

        title = QLabel("Select classified fences to DELETE")
        title.setStyleSheet(
            f"color: {ThemeColors.get('danger')}; font-weight: bold; font-size: 13px; padding: 8px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Unchecked fences will be kept. Checked fences will be removed.")
        subtitle.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary')}; font-size: 10px; padding: 0 8px 8px 8px;"
        )
        subtitle.setAlignment(Qt.AlignCenter)
        layout.addWidget(subtitle)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QWidget()
        scroll_layout = QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(4)
        scroll.setWidget(scroll_widget)
        layout.addWidget(scroll, 1)

        btn_row = QHBoxLayout()

        select_all_btn = QPushButton("Select All")
        select_all_btn.setObjectName("dangerBtn")
        select_all_btn.clicked.connect(
            lambda: [cb.setChecked(True) for cb, _ in getattr(dialog, "_classified_fence_checkboxes", [])]
        )
        btn_row.addWidget(select_all_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.clicked.connect(
            lambda: [cb.setChecked(False) for cb, _ in getattr(dialog, "_classified_fence_checkboxes", [])]
        )
        btn_row.addWidget(clear_all_btn)

        btn_row.addStretch()

        keep_btn = QPushButton("Keep Selected")
        keep_btn.setToolTip("Close without deleting any fences")
        keep_btn.setObjectName("primaryBtn")
        keep_btn.clicked.connect(lambda: (self._clear_clear_fence_hover(dialog), dialog.hide()))
        btn_row.addWidget(keep_btn)

        delete_btn = QPushButton("Delete Checked")
        delete_btn.setObjectName("dangerBtn")
        delete_btn.clicked.connect(lambda: self._delete_checked_classified_fences(dialog))
        btn_row.addWidget(delete_btn)

        layout.addLayout(btn_row)

        dialog._classified_fence_scroll_layout = scroll_layout
        dialog._classified_fence_checkboxes = []
        dialog._classified_fence_digitizer = None
        dialog._classified_fence_hover_actor = None
        dialog.destroyed.connect(lambda: setattr(self, "_clear_fence_dialog_ref", None))
        dialog.finished.connect(lambda *_: self._clear_clear_fence_hover(dialog))
        return dialog

    def _coords_from_clear_fence(self, fence):
        if not isinstance(fence, dict):
            return []

        dtype = str(fence.get("type", "") or "").lower()
        if dtype == "circle":
            center = fence.get("center")
            radius = fence.get("radius")
            if center is not None and radius is not None:
                try:
                    center = np.asarray(center, dtype=np.float64)
                    radius = float(radius)
                    coords = []
                    for angle in np.linspace(0.0, 2.0 * np.pi, 96, endpoint=False):
                        coords.append((
                            float(center[0] + radius * np.cos(angle)),
                            float(center[1] + radius * np.sin(angle)),
                            float(center[2]) if center.shape[0] > 2 else 0.0,
                        ))
                    coords.append(coords[0])
                    return coords
                except Exception:
                    return []

        coords = []
        for point in fence.get("coords", []) or []:
            try:
                if len(point) >= 3:
                    coords.append((float(point[0]), float(point[1]), float(point[2])))
                elif len(point) >= 2:
                    coords.append((float(point[0]), float(point[1]), 0.0))
            except Exception:
                continue

        if len(coords) >= 3 and coords[0][:2] != coords[-1][:2]:
            coords.append(coords[0])
        return coords

    def _make_clear_fence_hover_actor(self, digitizer, coords):
        if not coords or len(coords) < 2:
            return None

        try:
            import vtk

            if digitizer is not None and hasattr(digitizer, "_build_styled_polydata_world"):
                poly = digitizer._build_styled_polydata_world(coords, line_style="solid")
                origin = getattr(poly, "_world_origin", np.zeros(3, dtype=np.float64))
            else:
                origin = np.asarray(coords[0], dtype=np.float64)
                poly = vtk.vtkPolyData()
                pts = vtk.vtkPoints()
                pts.SetDataTypeToDouble()
                lines = vtk.vtkCellArray()

                for point in coords:
                    pts.InsertNextPoint(
                        float(point[0] - origin[0]),
                        float(point[1] - origin[1]),
                        float((point[2] if len(point) > 2 else 0.0) - origin[2]),
                    )

                line = vtk.vtkPolyLine()
                line.GetPointIds().SetNumberOfIds(len(coords))
                for i in range(len(coords)):
                    line.GetPointIds().SetId(i, i)
                lines.InsertNextCell(line)
                poly.SetPoints(pts)
                poly.SetLines(lines)
                poly.Modified()

            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(poly)
            try:
                mapper.SetResolveCoincidentTopologyToPolygonOffset()
                mapper.SetResolveCoincidentTopologyPolygonOffsetParameters(-100000, -100000)
            except Exception:
                pass

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.SetPosition(float(origin[0]), float(origin[1]), float(origin[2]))
            actor.GetProperty().SetColor(1.0, 0.85, 0.05)
            actor.GetProperty().SetLineWidth(7.0)
            actor.GetProperty().SetOpacity(1.0)
            actor.PickableOff()
            try:
                actor.GetProperty().SetDepthTestingEnabled(False)
            except Exception:
                pass
            return actor
        except Exception as e:
            print(f"⚠️ Failed to create clear fence hover highlight: {e}")
            return None

    def _clear_fence_hover_renderer(self, digitizer):
        if digitizer is not None:
            if getattr(digitizer, "overlay_renderer", None) is not None:
                return digitizer.overlay_renderer
            if hasattr(digitizer, "_ensure_overlay_renderer"):
                try:
                    return digitizer._ensure_overlay_renderer()
                except Exception:
                    pass
            return getattr(digitizer, "renderer", None)
        return None

    def _render_clear_fence_hover(self, digitizer):
        try:
            if digitizer is not None and getattr(digitizer, "interactor", None) is not None:
                digitizer.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            app = getattr(digitizer, "app", None)
            if app is not None and hasattr(app, "vtk_widget"):
                app.vtk_widget.render()
        except Exception:
            pass

    def _clear_clear_fence_hover(self, dialog):
        actor = getattr(dialog, "_classified_fence_hover_actor", None)
        if actor is None:
            return

        digitizer = getattr(dialog, "_classified_fence_digitizer", None)
        renderer = self._clear_fence_hover_renderer(digitizer)
        if renderer is not None:
            try:
                renderer.RemoveActor(actor)
            except Exception:
                pass
            try:
                renderer.RemoveViewProp(actor)
            except Exception:
                pass
        dialog._classified_fence_hover_actor = None
        self._render_clear_fence_hover(digitizer)

    def _show_clear_fence_hover(self, dialog, fence):
        self._clear_clear_fence_hover(dialog)

        digitizer = getattr(dialog, "_classified_fence_digitizer", None)
        coords = self._coords_from_clear_fence(fence)
        actor = self._make_clear_fence_hover_actor(digitizer, coords)
        if actor is None:
            return

        renderer = self._clear_fence_hover_renderer(digitizer)
        if renderer is None:
            return

        try:
            renderer.AddActor(actor)
            dialog._classified_fence_hover_actor = actor
            self._render_clear_fence_hover(digitizer)
        except Exception as e:
            print(f"⚠️ Failed to show clear fence hover highlight: {e}")

    def _populate_clear_fence_dialog(self, dialog, digitizer, classified_fences):
        from gui.theme_manager import ThemeColors
        from PySide6.QtCore import QTimer

        shape_icons = {
            "rectangle": "▭",
            "circle": "○",
            "polygon": "⬟",
            "polyline": "⬡",
            "line": "─",
            "smartline": "⚡",
            "smart_line": "⚡",
            "centerline": "≋",
            "freehand": "✏️",
        }

        scroll_layout = getattr(dialog, "_classified_fence_scroll_layout", None)
        if scroll_layout is None:
            return

        refresh_timer = getattr(dialog, "_classified_fence_refresh_timer", None)
        if refresh_timer is not None:
            try:
                refresh_timer.stop()
            except Exception:
                pass

        self._clear_layout_widgets(scroll_layout)
        dialog._classified_fence_digitizer = digitizer
        dialog._classified_fence_checkboxes = []
        dialog._classified_fence_source_ids = {id(f) for f in (classified_fences or [])}

        if not classified_fences:
            empty_label = QLabel("No classified fences available.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(
                f"color: {ThemeColors.get('text_secondary')}; font-size: 11px; padding: 20px;"
            )
            scroll_layout.addWidget(empty_label)
            scroll_layout.addStretch()
            return

        for idx, fence in enumerate(classified_fences):
            shape_type = fence.get("type", "unknown")
            coords = np.array(fence.get("coords", []))
            icon = shape_icons.get(shape_type, "◆")

            coord_count = len(coords)
            if coord_count > 0:
                min_pt = coords.min(axis=0)
                max_pt = coords.max(axis=0)
                width = max_pt[0] - min_pt[0]
                height = max_pt[1] - min_pt[1]
                size_text = f"{width:.1f}x{height:.1f}m"
            else:
                size_text = "?"

            row = QWidget()
            row.setStyleSheet(
                f"""
                QWidget {{
                    background-color: {ThemeColors.get('bg_button')};
                    border: 1px solid {ThemeColors.get('border')};
                    border-radius: 6px;
                    padding: 6px;
                }}
                QWidget:hover {{
                    background-color: {ThemeColors.get('bg_button_hover')};
                    border: 1px solid {ThemeColors.get('border_light')};
                }}
            """
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)

            checkbox = QCheckBox()
            checkbox.setStyleSheet(
                f"""
                QCheckBox::indicator {{
                    width: 18px; height: 18px;
                    border-radius: 3px; border: 1px solid {ThemeColors.get('border_light')};
                    background-color: {ThemeColors.get('bg_input')};
                }}
                QCheckBox::indicator:checked {{
                    background-color: {ThemeColors.get('danger')}; border: 1px solid {ThemeColors.get('danger')};
                }}
            """
            )
            row_layout.addWidget(checkbox)

            indicator = QFrame()
            indicator.setFixedSize(8, 30)
            indicator.setStyleSheet(
                f"background-color: {ThemeColors.get('accent')}; border-radius: 2px;"
            )
            row_layout.addWidget(indicator)

            label = QLabel(f"{icon} #{idx + 1}: {shape_type.capitalize()}\n{coord_count} pts | {size_text}")
            label.setStyleSheet(
                f"color: {ThemeColors.get('text_primary')}; font-size: 10px; background: transparent; padding-left: 4px;"
            )
            row_layout.addWidget(label, 1)

            def make_click(cb):
                def on_click(event):
                    cb.setChecked(not cb.isChecked())
                return on_click

            row.mousePressEvent = make_click(checkbox)
            row.enterEvent = lambda event, f=fence: self._show_clear_fence_hover(dialog, f)
            row.leaveEvent = lambda event: self._clear_clear_fence_hover(dialog)
            row.setCursor(Qt.PointingHandCursor)

            dialog._classified_fence_checkboxes.append((checkbox, fence))
            scroll_layout.addWidget(row)

        scroll_layout.addStretch()

        def _live_classified_fences():
            if digitizer is None:
                return []
            drawings = list(getattr(digitizer, "drawings", []) or [])
            return [d for d in drawings if d.get("classified_fence", False)]

        def _refresh_if_needed():
            if dialog is None or not dialog.isVisible():
                return
            live_fences = _live_classified_fences()
            live_ids = {id(f) for f in live_fences}
            if live_ids == getattr(dialog, "_classified_fence_source_ids", set()):
                return
            self._populate_clear_fence_dialog(dialog, digitizer, live_fences)

        refresh_timer = QTimer(dialog)
        refresh_timer.setInterval(400)
        refresh_timer.timeout.connect(_refresh_if_needed)
        refresh_timer.start()
        dialog._classified_fence_refresh_timer = refresh_timer

    def _delete_checked_classified_fences(self, dialog):
        digitizer = getattr(dialog, "_classified_fence_digitizer", None)
        checkboxes = list(getattr(dialog, "_classified_fence_checkboxes", []) or [])
        self._clear_clear_fence_hover(dialog)
        if digitizer is None:
            dialog.hide()
            return

        fences_to_delete = [fence for cb, fence in checkboxes if cb.isChecked()]
        if not fences_to_delete:
            dialog.hide()
            return

        for fence in fences_to_delete:
            try:
                digitizer._remove_drawing(fence)
                print(f"🗑️ Deleted classified fence: {fence.get('type', 'unknown')}")
            except Exception as e:
                print(f"⚠️ Failed to delete fence: {e}")

        try:
            removed_ids = {id(f) for f in fences_to_delete}
            removed_actor_ids = {
                id(f.get("actor")) for f in fences_to_delete
                if isinstance(f, dict) and f.get("actor") is not None
            }

            def _coords_key(shape):
                try:
                    return tuple(tuple(c[:2]) for c in (shape.get("coords", []) or []))
                except Exception:
                    return ()

            removed_keys = {_coords_key(f) for f in fences_to_delete}

            dialog_attrs = (
                "inside_fence_dialog",
                "by_class_dialog",
                "closed_by_class_dialog",
                "height_convert_dialog",
            )
            # Dialogs live on ByClassRibbon, not on app directly
            _by_class_ribbon = getattr(
                getattr(getattr(digitizer, 'app', None), 'ribbon_manager', None),
                'ribbons', {}
            ).get('by_class')

            for attr in dialog_attrs:
                other_dialog = getattr(_by_class_ribbon, attr, None) if _by_class_ribbon else None
                if not other_dialog:
                    other_dialog = getattr(digitizer.app, attr, None)
                if not other_dialog:
                    continue

                try:
                    if hasattr(other_dialog, "selected_fences"):
                        selected = list(getattr(other_dialog, "selected_fences", []) or [])
                        kept = []
                        for fence in selected:
                            drop = False
                            if id(fence) in removed_ids:
                                drop = True
                            elif _coords_key(fence) in removed_keys:
                                drop = True
                            elif (
                                isinstance(fence, dict)
                                and fence.get("actor") is not None
                                and id(fence.get("actor")) in removed_actor_ids
                            ):
                                drop = True

                            if not drop:
                                kept.append(fence)
                        other_dialog.selected_fences = kept

                    if hasattr(other_dialog, "_prune_stale_fences"):
                        try:
                            other_dialog._prune_stale_fences()
                        except Exception:
                            pass

                    if hasattr(other_dialog, "_clear_fence_highlights"):
                        other_dialog._clear_fence_highlights()

                    if getattr(other_dialog, "selected_fences", None) and hasattr(other_dialog, "_restore_highlights_from_data"):
                        try:
                            other_dialog._restore_highlights_from_data()
                        except Exception:
                            pass

                    if hasattr(other_dialog, "update_fence_display"):
                        try:
                            other_dialog.update_fence_display()
                        except Exception:
                            pass

                    if not getattr(other_dialog, 'selected_fences', None) and hasattr(other_dialog, 'fence_status') and other_dialog.fence_status:
                        try:
                            _set_dialog_status(other_dialog.fence_status, "No fence selected", "neutral")
                        except Exception:
                            pass
                    elif getattr(other_dialog, 'selected_fences', None) and hasattr(other_dialog, 'fence_status') and other_dialog.fence_status:
                        try:
                            fences = other_dialog.selected_fences
                            shape_fc = sum(1 for f in fences if f.get('source') != 'curve_tool')
                            curve_fc = sum(1 for f in fences if f.get('source') == 'curve_tool')
                            total_pts = sum(len(f.get('coords', [])) for f in fences)
                            mode_text = "Permanent" if getattr(other_dialog, 'permanent_fence_mode', False) else "Temporary"
                            parts = []
                            if shape_fc: parts.append(f"{shape_fc} shape(s)")
                            if curve_fc: parts.append(f"{curve_fc} curve(s)")
                            _set_dialog_status(
                                other_dialog.fence_status,
                                f"{' + '.join(parts)} selected ({total_pts} pts) - {mode_text}",
                                "success",
                            )
                        except Exception:
                            pass
                except Exception as e:
                    print(f"⚠️ {attr} cleanup warning: {e}")
        except Exception as e:
            print(f"⚠️ Fence dialog cleanup: {e}")

        try:
            if hasattr(digitizer, "overlay_renderer") and digitizer.overlay_renderer:
                digitizer.overlay_renderer.Modified()
            digitizer.renderer.Modified()
            digitizer.interactor.GetRenderWindow().Render()
            digitizer.app.vtk_widget.render()
        except Exception:
            pass

        print(f"✅ Deleted {len(fences_to_delete)} classified fence(s)")
        dialog.hide()

    def _handle_clear_click(self):
        main_window = self.window()
        if not hasattr(main_window, "digitizer") or not main_window.digitizer:
            return

        digitizer = main_window.digitizer

        try:
            accudraw_tool = getattr(digitizer, "accudraw_tool", None)
            if accudraw_tool is not None:
                if getattr(accudraw_tool, "active", False):
                    accudraw_tool.deactivate(cancel=True)
                if hasattr(accudraw_tool, "reset_after_external_clear"):
                    accudraw_tool.reset_after_external_clear()
        except Exception as e:
            print(f"⚠️ AccuDraw clear cleanup failed: {e}")

        curve_tool = getattr(main_window, "curve_tool", None)
        classified_fences = [d for d in digitizer.drawings if d.get("classified_fence", False)]
        has_curves = bool(curve_tool and getattr(curve_tool, "finalized_actors", None))
        has_non_classified_drawings = any(
            not drawing.get("classified_fence", False) for drawing in digitizer.drawings
        )

        dialog = self._get_clear_fence_dialog()
        if (
            classified_fences
            and dialog is not None
            and (dialog.isVisible() or dialog.windowState() & Qt.WindowMinimized)
            and not has_curves
            and not has_non_classified_drawings
        ):
            print("🗂️ Clear: reusing existing classified fence picker...")
            self._show_clear_fence_dialog(digitizer, classified_fences)
            return

        if has_curves or digitizer.drawings:
            digitizer._save_state()

        clear_text = True
        if digitizer._has_hidden_texts():
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.question(
                self,
                "Hidden Texts Warning",
                "Hidden texts are getting cleared.\nDO YOU WANT TO CONTINUE?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                clear_text = False
                print("🗑️ Clear: user chose to preserve hidden texts")

        if not classified_fences:
            if curve_tool:
                curve_tool.clear_all_curves()
            digitizer.clear_drawings(clear_classified=True, record_undo=False, preserve_committed=True, clear_text=clear_text)
            print("🗑️ Clear: all drawings cleared (no classified fences)")
        else:
            # Has classified fences — clear non-classified drawings and curves only,
            # then show the dialog. curve_tool.clear_all_curves() skips classified
            # fence curves so they remain visible for the picker.
            if curve_tool:
                curve_tool.clear_all_curves()
            digitizer.clear_drawings(clear_classified=False, record_undo=False, preserve_committed=True, clear_text=clear_text)
            print("🗑️ Clear: non-classified drawings cleared, showing fence picker...")
            self._show_clear_fence_dialog(digitizer, classified_fences)

    def _show_clear_fence_dialog(self, digitizer, classified_fences):
        """Show a popup letting user choose which classified fences to keep or delete."""
        dialog = self._get_clear_fence_dialog()
        if dialog is None:
            dialog = self._create_clear_fence_dialog()
            self._clear_fence_dialog_ref = dialog

        self._populate_clear_fence_dialog(dialog, digitizer, classified_fences)
        self._restore_tool_dialog(dialog)

    def _show_draw_settings(self):
        """Open the Draw Tool Settings dialog."""
        try:
            # Walk up to find the actual NakshaApp main window with digitizer
            main_window = self.window()
            widget = self
            while widget:
                if hasattr(widget, 'digitizer'):
                    main_window = widget
                    break
                widget = widget.parent()
            
            print(f"🔧 Draw Settings: app={type(main_window).__name__}, has digitizer={hasattr(main_window, 'digitizer')}")
            dialog = getattr(main_window, "draw_settings_dialog", None)
            already_open = dialog is not None and dialog.isVisible()

            if dialog is None:
                dialog = DrawToolSettingsDialog(main_window, parent=main_window)
                main_window.draw_settings_dialog = dialog
                dialog.destroyed.connect(
                    lambda: setattr(main_window, "draw_settings_dialog", None)
                )

            dialog.show()
            if dialog.isMinimized():
                dialog.showNormal()
            dialog.raise_()
            dialog.activateWindow()

            if already_open:
                dialog.highlight_already_open()
            
        except Exception as e:
            print(f"⚠️ Failed to open Draw Settings: {e}")
            import traceback
            traceback.print_exc()


    def _handle_select_drawing_click(self):
        """Open the MicroStation-style Element Selection panel.

        Stands down any conflicting tools (draw, curve, measurement) first,
        then shows the SelectionModeDialog which activates ElementSelectTool.
        """
        main_window = self.window()

        self._deactivate_accudraw_before_other_tool("Select Drawing")

        # Stand down draw tool — ElementSelectTool will also do this defensively
        if hasattr(main_window, 'digitizer') and main_window.digitizer:
            try:
                main_window.digitizer.set_tool(None)
            except Exception:
                pass

        if hasattr(main_window, 'curve_tool') and main_window.curve_tool:
            ct = main_window.curve_tool
            try:
                if ct.active:
                    ct.deactivate()
                if getattr(ct, '_select_mode', False):
                    ct.deactivate_select_mode()
                setattr(main_window, "_draw_curve_context_active", False)
            except Exception:
                pass

        if hasattr(main_window, 'measurement_tool'):
            try:
                main_window.measurement_tool.deactivate()
            except Exception:
                pass

        # Open the panel (reuse a single instance so re-clicks raise it).
        try:
            existing = getattr(main_window, "_element_selection_dialog", None)
            if existing is not None:
                try:
                    self._restore_tool_dialog(existing)
                    return
                except Exception:
                    pass

            from gui.selection_popup import SelectionModeDialog
            dlg = SelectionModeDialog(main_window)
            main_window._element_selection_dialog = dlg
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()

            if hasattr(main_window, 'statusBar'):
                main_window.statusBar().showMessage(
                    "🎯 Element Selection — click an element, drag a block, "
                    "or use Shape/Line. Move, Rotate, and Mirror are available in the same panel. Ctrl=Add, Shift=Subtract.", 5000
                )
        except Exception as e:
            print(f"⚠️ Could not open SelectionModeDialog: {e}")


    def _handle_deselect_all_click(self):
        """Exit ALL tool modes — drawing, selection, everything."""
        main_window = self.window()
        
        if hasattr(main_window, 'digitizer') and main_window.digitizer:
            try:
                main_window.digitizer.deactivate_all()
            except Exception:
                try:
                    main_window.digitizer.set_tool(None)
                except Exception:
                    pass
        
        # ✅ FIXED: was calling deactivate, now calls activate_select_mode
        if hasattr(main_window, 'curve_tool') and main_window.curve_tool:
            main_window.curve_tool.activate_select_mode()
            print("🎯 Select Drawing mode activated from ribbon")
        else:
            print("⚠️ Curve tool not available")

    # ========================================================================
    # MOVE VERTEX HANDLER
    # ========================================================================
    def _handle_move_vertex_click(self):
        """Handle Move Vertex button click with Shift detection for settings"""
        modifiers = QApplication.keyboardModifiers()
       
        if modifiers & Qt.ShiftModifier:
            print("🔧 Shift detected - opening Move Vertex settings")
            self._show_move_vertex_settings()
        else:
            self._deactivate_accudraw_before_other_tool("Move Vertex")
            print("🔄 Normal Move Vertex activation")
            self.draw_tool_selected.emit("Move Vertex")
   
    def _show_move_vertex_settings(self):
        """Show Move Vertex settings dialog"""
        digitizer = self.window().digitizer
        current_mode = getattr(digitizer, 'vertex_move_mode_type', 'click')
       
        dialog = VertexMoveSettingsDialog(current_mode, self)
        if dialog.exec_() == QDialog.Accepted:
            digitizer.vertex_move_mode_type = dialog.get_move_mode()
            mode_str = "Click and Drag" if digitizer.vertex_move_mode_type == 'drag' else "Click to Select/Place"
            print(f"🔧 Vertex move mode: {mode_str}")
           
            # Activate Move Vertex tool
            self._deactivate_accudraw_before_other_tool("Move Vertex")
            self.draw_tool_selected.emit("Move Vertex")
    # ========================================================================
    # SMARTLINE SETTINGS
    # ========================================================================
    def _handle_smartline_click(self):
        """Handle SmartLine button click with Shift detection for arrow settings"""
        modifiers = QApplication.keyboardModifiers()
       
        if modifiers & Qt.ShiftModifier:
            print("🔧 Shift detected - opening SmartLine arrow settings")
            self._show_smartline_settings()
        else:
            self._deactivate_accudraw_before_other_tool("SmartLine")
            print("🖊️ Normal SmartLine activation")
            self.draw_tool_selected.emit("Smartline")
   
    def _show_smartline_settings(self):
        """Show SmartLine arrow settings dialog"""
        digitizer = self.window().digitizer
        current_arrow = getattr(digitizer, 'smartline_arrow_mode', False)
       
        dialog = LineArrowSettingsDialog("SmartLine", current_arrow, self)
        if dialog.exec_() == QDialog.Accepted:
            digitizer.smartline_arrow_mode = dialog.get_arrow_mode()
            print(f"🔧 SmartLine arrow mode: {'ENABLED' if digitizer.smartline_arrow_mode else 'DISABLED'}")
            # Activate SmartLine
            self._deactivate_accudraw_before_other_tool("SmartLine")
            self.draw_tool_selected.emit("Smartline")
   
    # ========================================================================
    # LINE SETTINGS
    # ========================================================================
    def _handle_line_click(self):
        """Handle Line button click with Shift detection for arrow settings"""
        modifiers = QApplication.keyboardModifiers()
       
        if modifiers & Qt.ShiftModifier:
            print("🔧 Shift detected - opening Line arrow settings")
            self._show_line_settings()
        else:
            self._deactivate_accudraw_before_other_tool("Line")
            print("🖊️ Normal Line activation")
            self.draw_tool_selected.emit("Line")
   
    def _show_line_settings(self):
        """Show Line arrow settings dialog"""
        digitizer = self.window().digitizer
        current_arrow = getattr(digitizer, 'line_arrow_mode', False)
       
        dialog = LineArrowSettingsDialog("Line", current_arrow, self)
        if dialog.exec_() == QDialog.Accepted:
            digitizer.line_arrow_mode = dialog.get_arrow_mode()
            print(f"🔧 Line arrow mode: {'ENABLED' if digitizer.line_arrow_mode else 'DISABLED'}")
           
            # Activate Line
            self._deactivate_accudraw_before_other_tool("Line")
            self.draw_tool_selected.emit("Line")
   
    # ========================================================================
    # ORTHO SETTINGS
    # ========================================================================
    def _handle_ortho_click(self):
        """Handle Ortho button click with Shift detection."""
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            self._show_tool_permanent_settings('orthopolygon', 'orthopolygon_permanent_mode', 'Orthopolygon')
        else:
            self._deactivate_accudraw_before_other_tool("Ortho")
            self.draw_tool_selected.emit("orthopolygon")

    # ========================================================================
    # POLYLINE SETTINGS (existing)
    # ========================================================================
    def _handle_polyline_click(self):
        """Handle Polyline button click with Shift detection"""
        modifiers = QApplication.keyboardModifiers()
       
        if modifiers & Qt.ShiftModifier:
            print("🔧 Shift detected - opening settings dialog")
            self._show_polyline_settings()
        else:
            self._deactivate_accudraw_before_other_tool("Polyline")
            print("🖊️ Normal polyline activation")
            self.draw_tool_selected.emit("Polyline")
 
    def _show_polyline_settings(self):
        """Show polyline settings dialog"""
        digitizer = self.window().digitizer
        current_permanent = getattr(digitizer, 'polyline_permanent_mode', False)
       
        dialog = PolylineSettingsDialog(current_permanent, self)
        if dialog.exec_() == QDialog.Accepted:
            digitizer.polyline_permanent_mode = dialog.get_permanent_mode()
            print(f"🔧 Polyline mode set to: {'Permanent' if digitizer.polyline_permanent_mode else 'Temporary'}")
           
            # Activate polyline with new mode
            mode = "Polyline_Permanent" if digitizer.polyline_permanent_mode else "Polyline"
            self._deactivate_accudraw_before_other_tool(mode)
            self.draw_tool_selected.emit(mode)
           
    def _handle_rect_click(self):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            self._show_tool_permanent_settings('rectangle', 'rectangle_permanent_mode', 'Rectangle')
        else:
            self._deactivate_accudraw_before_other_tool("Rectangle")
            self.draw_tool_selected.emit("Rectangle")

    def _handle_circle_click(self):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            self._show_tool_permanent_settings('circle', 'circle_permanent_mode', 'Circle')
        else:
            self._deactivate_accudraw_before_other_tool("Circle")
            self.draw_tool_selected.emit("Circle")

    def _handle_freehand_click(self):
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            self._show_tool_permanent_settings('freehand', 'freehand_permanent_mode', 'Freehand')
        else:
            self._deactivate_accudraw_before_other_tool("Freehand")
            self.draw_tool_selected.emit("Freehand")

    def _handle_hatch_click(self):
        self._deactivate_accudraw_before_other_tool("Hatch")
        self.draw_tool_selected.emit("hatcharea")

    def _show_tool_permanent_settings(self, flag_name, attr_name, emit_name):

        """Generic permanent mode settings dialog for any draw tool"""
        digitizer = self.window().digitizer
        current_permanent = getattr(digitizer, attr_name, False)
        dialog = PolylineSettingsDialog(current_permanent, self)  # reuse existing dialog
        if dialog.exec_() == QDialog.Accepted:
            setattr(digitizer, attr_name, dialog.get_permanent_mode())
            mode = f"{emit_name}_Permanent" if getattr(digitizer, attr_name) else emit_name
            print(f"🔧 {emit_name} mode: {'Permanent' if getattr(digitizer, attr_name) else 'Temporary'}")
            self._deactivate_accudraw_before_other_tool(emit_name)
            self.draw_tool_selected.emit(emit_name)  # always activate the tool after setting                  
    # ========================================================================
    # PARALLEL TOOL
    # ========================================================================
    def _handle_parallel_click(self):
        """Handle Parallel button click — open the Parallel Figure dialog."""
        main_window = self.window()
        digitizer = getattr(main_window, "digitizer", None)
        if not digitizer:
            print("⚠️ Parallel tool: digitizer not available")
            return

        self._deactivate_draw_tools_for_parallel(main_window, digitizer)

        centerline_dialog = getattr(main_window, "_centerline_tool_dialog", None)
        if centerline_dialog is not None:
            try:
                centerline_dialog.close()
            except Exception:
                pass

        existing = getattr(main_window, "_parallel_tool_dialog", None)
        if existing is not None:
            try:
                self._restore_tool_dialog(existing)
                return
            except (RuntimeError, ReferenceError):
                main_window._parallel_tool_dialog = None

        from gui.parallel_tool_dialog import ParallelToolDialog
        dialog = ParallelToolDialog(digitizer, parent=main_window)
        main_window._parallel_tool_dialog = dialog
        dialog.finished.connect(
            lambda _=0, mw=main_window, dlg=dialog: self._clear_parallel_dialog_ref(mw, dlg)
        )
        dialog.show()

    def _deactivate_draw_tools_for_parallel(self, main_window, digitizer):
        """Deactivate any active draw tool before opening the Parallel/Centerline dialog."""
        self._deactivate_accudraw_before_other_tool("Parallel/Centerline")

        try:
            digitizer.deactivate_all()
        except Exception:
            try:
                digitizer.set_tool(None)
            except Exception:
                pass

        try:
            if hasattr(main_window, "curve_tool") and main_window.curve_tool:
                curve_tool = main_window.curve_tool
                if getattr(curve_tool, "active", False):
                    curve_tool.deactivate()
                if getattr(curve_tool, "_select_mode", False):
                    curve_tool.deactivate_select_mode()
                elif hasattr(curve_tool, "_deselect_curve"):
                    try:
                        curve_tool._deselect_curve()
                    except Exception:
                        pass
                setattr(main_window, "_draw_curve_context_active", False)
        except Exception:
            pass

        for section in self.findChildren(RibbonSection):
            if section.section_title == "Parallel":
                continue
            if section.active_button:
                section.active_button.setChecked(False)
                section.active_button = None

    @staticmethod
    def _clear_parallel_dialog_ref(main_window, dialog):
        if getattr(main_window, "_parallel_tool_dialog", None) is dialog:
            main_window._parallel_tool_dialog = None
        dialog.deleteLater()

    # ========================================================================
    # CENTERLINE TOOL
    # ========================================================================
    def _handle_centerline_click(self):
        """Handle Centerline button click — open the Centerline dialog."""
        main_window = self.window()
        digitizer = getattr(main_window, "digitizer", None)
        if not digitizer:
            print("⚠️ Centerline tool: digitizer not available")
            return

        digitizer.enabled = True
        self._deactivate_draw_tools_for_parallel(main_window, digitizer)

        parallel_dialog = getattr(main_window, "_parallel_tool_dialog", None)
        if parallel_dialog is not None:
            try:
                parallel_dialog.close()
            except Exception:
                pass

        existing = getattr(main_window, "_centerline_tool_dialog", None)
        if existing is not None:
            try:
                self._restore_tool_dialog(existing)
                return
            except (RuntimeError, ReferenceError):
                main_window._centerline_tool_dialog = None

        from gui.centerline_tool_dialog import CenterlineToolDialog
        dialog = CenterlineToolDialog(digitizer, parent=main_window)
        main_window._centerline_tool_dialog = dialog
        dialog.finished.connect(
            lambda _=0, mw=main_window, dlg=dialog: self._clear_centerline_dialog_ref(mw, dlg)
        )
        dialog.show()

    @staticmethod
    def _clear_centerline_dialog_ref(main_window, dialog):
        if getattr(main_window, "_centerline_tool_dialog", None) is dialog:
            main_window._centerline_tool_dialog = None
        dialog.deleteLater()

    @staticmethod
    def _restore_tool_dialog(dialog):
        """Restore a modeless tool dialog from native minimize/taskbar state."""
        if hasattr(dialog, "restore_from_chip"):
            dialog.restore_from_chip()
        else:
            if dialog.windowState() & Qt.WindowMinimized:
                dialog.showNormal()
            else:
                dialog.setWindowState(dialog.windowState() & ~Qt.WindowMinimized)
                dialog.show()
            dialog.raise_()
            dialog.activateWindow()

    # ========================================================================
    # VERTEX SETTINGS
    # ========================================================================
    def _handle_vertex_click(self):
        """Show vertex edit actions; Shift still opens insert settings."""
        modifiers = QApplication.keyboardModifiers()
       
        if modifiers & Qt.ShiftModifier:
            print("🔧 Shift detected - opening Vertex settings")
            self._show_vertex_settings()
        else:
            self._show_vertex_action_menu()

    def _show_vertex_action_menu(self):
        """Show Create/Move/Delete Vertex actions from the Vertex ribbon button."""
        menu = QMenu(self)
        create_action = menu.addAction("Add Vertex")
        move_action   = menu.addAction("Move Vertex")
        delete_action = menu.addAction("Delete Vertex")

        button = getattr(self, "vertex_btn", None)
        if button is not None:
            action = menu.exec(button.mapToGlobal(button.rect().bottomLeft()))
        else:
            action = menu.exec(self.mapToGlobal(self.rect().bottomLeft()))

        if action is create_action:
            self._deactivate_accudraw_before_other_tool("Add Vertex")
            print("🔵 Add Vertex tool activation")
            self.draw_tool_selected.emit("Vertex")
        elif action is move_action:
            self._deactivate_accudraw_before_other_tool("Move Vertex")
            print("🔄 Move Vertex tool activation")
            self.draw_tool_selected.emit("Move Vertex")
        elif action is delete_action:
            self._deactivate_accudraw_before_other_tool("Delete Vertex")
            print("🗑️ Delete Vertex tool activation")
            self.draw_tool_selected.emit("Delete Vertex")
 
    def _show_vertex_settings(self):
        """Show Vertex tool settings dialog"""
        digitizer = self.window().digitizer
        current_auto_drag = getattr(digitizer, 'vertex_auto_drag', False)
       
        dialog = VertexInsertSettingsDialog(current_auto_drag, self)
        if dialog.exec_() == QDialog.Accepted:
            digitizer.vertex_auto_drag = dialog.get_auto_drag_mode()
            mode_str = "Insert and Drag" if digitizer.vertex_auto_drag else "Insert Only"
            print(f"🔧 Vertex mode: {mode_str}")
           
            # Activate Vertex tool
        self._deactivate_accudraw_before_other_tool("Vertex")
        self.draw_tool_selected.emit("Vertex")

class RibbonManager:
    """Manages all ribbon panels"""
    
    def __init__(self, parent_window):
        self.parent = parent_window
        self.ribbons = {}
        self.current_ribbon = None
        self.create_ribbons()
        
    def create_ribbons(self):
        """Create all ribbon instances"""
        self.ribbons['file'] = FileRibbon(self.parent)
        self.ribbons['edit'] = EditRibbon(self.parent)
        self.ribbons['view'] = ViewRibbon(self.parent)
        self.ribbons['tools'] = ToolsRibbon(self.parent)
        self.ribbons['classify'] = ClassifyRibbon(self.parent)
        self.ribbons['display'] = DisplayRibbon(self.parent)
       
        self.ribbons['measure'] = MeasurementRibbon(self.parent)
        self.ribbons['identify'] = IdentificationRibbon(self.parent, app=self.parent)
        self.ribbons['by_class'] = ByClassRibbon(self.parent, app=self.parent)
        self.ribbons['draw'] = DrawRibbon(self.parent)
        self.ribbons['curve'] = CurveRibbon(self.parent)
        self.ribbons['ai'] = AIRibbon(self.parent, app=self.parent)
        self.ribbons['block'] = BlockRibbon(self.parent, app=self.parent)
        self.ribbons['plugins'] = PluginsRibbon(self.parent, app=self.parent)


        
        for ribbon in self.ribbons.values():
            ribbon.hide()
            
    def clear_all_tool_buttons(self, exclude_btn=None):
        """Untoggle all toggleable ribbon buttons across all sections in all ribbons, except the excluded one."""
        for ribbon in self.ribbons.values():
            for section in ribbon.findChildren(RibbonSection):
                if section.active_button and section.active_button != exclude_btn:
                    try:
                        section.active_button.setChecked(False)
                        section.active_button = None
                    except Exception:
                        pass
            
    def show_ribbon(self, ribbon_name):
        """Show a specific ribbon"""
        if ribbon_name not in self.ribbons:
            return

        if self.current_ribbon != ribbon_name:
            point_sync_tool = getattr(self.parent, 'point_sync_tool', None)
            if point_sync_tool and getattr(point_sync_tool, 'active', False):
                try:
                    point_sync_tool.deactivate()
                except Exception:
                    pass

        # Hide current ribbon if different
        if self.current_ribbon and self.current_ribbon != ribbon_name:
            # ◀◀◀ NEW: Deactivate Curve tools when leaving 'curve' tab
            if self.current_ribbon == 'curve' and ribbon_name != 'curve':
                if hasattr(self.parent, 'curve_tool') and self.parent.curve_tool:
                    try:
                        self.parent.curve_tool.deactivate()
                        self.parent.curve_tool.deactivate_select_mode()
                    except Exception: pass
            
            # ◀◀◀ NEW: Deactivate Draw tools when leaving 'draw' tab
            elif self.current_ribbon == 'draw' and ribbon_name != 'draw':
                if hasattr(self.parent, 'digitizer') and self.parent.digitizer:
                    try:
                        accudraw_tool = getattr(self.parent.digitizer, "accudraw_tool", None)
                        if accudraw_tool and getattr(accudraw_tool, "active", False):
                            if hasattr(accudraw_tool, "finish_for_tool_switch"):
                                accudraw_tool.finish_for_tool_switch(f"ribbon:{ribbon_name}")
                            else:
                                accudraw_tool.deactivate(cancel=True)
                    except Exception as e:
                        print(f"⚠️ AccuDraw ribbon-switch cleanup failed: {e}")

                    try: self.parent.digitizer.set_tool(None)
                    except Exception: pass
                if hasattr(self.parent, 'curve_tool') and self.parent.curve_tool:
                    try:
                        self.parent.curve_tool.deactivate()
                        self.parent.curve_tool.deactivate_select_mode()
                    except Exception:
                        pass
            
            # ◀◀◀ NEW: Deactivate Measure tools when leaving 'measure' tab
            elif self.current_ribbon == 'measure' and ribbon_name != 'measure':
                if hasattr(self.parent, 'measurement_tool') and self.parent.measurement_tool:
                    try: self.parent.measurement_tool.deactivate()
                    except Exception: pass
            
            # ◀◀◀ NEW: Deactivate Identify tools when leaving 'identify' tab
            elif self.current_ribbon == 'identify' and ribbon_name != 'identify':
                ribbon = self.ribbons.get('identify')
                if ribbon and hasattr(ribbon, 'deactivate_all_tools'):
                    try: ribbon.deactivate_all_tools()
                    except Exception: pass
                    
            # Deactivate Classify tools when leaving 'classify' tab
            elif self.current_ribbon == 'classify' and ribbon_name != 'classify':
                if getattr(self.parent, 'active_classify_tool', None):
                    try: self.parent.deactivate_classification_tool(preserve_cross_section=True)
                    except: pass

            # Deactivate Tools tools when leaving 'tools' tab
            elif self.current_ribbon == 'tools' and ribbon_name != 'tools':
                try: self.parent.deactivate_cross_section_tool()
                except: pass
                try:
                    if hasattr(self.parent, 'cut_section_controller') and self.parent.cut_section_controller:
                        self.parent.cut_section_controller.deactivate_tool_only()
                        self.parent.cut_section_mode_on = False
                        self.parent.set_cross_cursor_active(False)
                except: pass
                try:
                    glm = getattr(self.parent, "grid_label_manager", None)
                    if glm and hasattr(glm, "deactivate_load_by_fence_tool"):
                        glm.deactivate_load_by_fence_tool()
                except Exception:
                    pass

            self.ribbons[self.current_ribbon].hide()
        
        # Show requested ribbon
        ribbon = self.ribbons[ribbon_name]
        ribbon.show()
        self.current_ribbon = ribbon_name
        
    def hide_current(self):
        """Hide the currently visible ribbon"""
        if self.current_ribbon:
            self.ribbons[self.current_ribbon].hide()
            self.current_ribbon = None
            
    def toggle_ribbon(self, name):
        """Toggle ribbon visibility"""
        if self.current_ribbon == name:
            self.hide_current()
        else:
            self.show_ribbon(name)

        # Deactivate all buttons in previously open ribbon
        if self.current_ribbon and self.current_ribbon != name:
            for section in self.ribbons[self.current_ribbon].findChildren(RibbonSection):
                if section.active_button:
                    section.active_button.setChecked(False)
                    section.active_button = None

class BlockRibbon(QWidget):
    """Ribbon for block creation tools"""

    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        block_section = RibbonSection("Block", self)
        block_section.add_button(
            "Create Block",
            "▦",
            self._show_block_creation_settings,
            toggleable=False,
        )
        layout.addWidget(block_section)
        layout.addStretch()

    def _show_block_creation_settings(self):
        try:
            app = getattr(self, "app", None) or self.window()
            existing = getattr(app, "block_creation_settings_dialog", None)
            
            # Check if the dialog exists and is valid
            dialog_valid = False
            if existing is not None:
                try:
                    existing.windowTitle()
                    dialog_valid = True
                except (RuntimeError, ReferenceError):
                    app.block_creation_settings_dialog = None
                    existing = None

            if dialog_valid and existing is not None:
                if getattr(existing, "_is_minimized_to_chip", False):
                    existing._do_restore_from_chip()
                elif existing.isMinimized():
                    existing.showNormal()
                    existing.raise_()
                    existing.activateWindow()
                elif existing.isVisible():
                    existing.blink()
                else:
                    existing.show()
                    existing.raise_()
                    existing.activateWindow()
            else:
                from gui.block_creation_settings_dialog import show_block_creation_settings_dialog
                app.block_creation_settings_dialog = show_block_creation_settings_dialog(app)
        except Exception as e:
            print(f"⚠️ Failed to open block creation settings: {e}")
            import traceback

            traceback.print_exc()


class MeasurementRibbon(QWidget):
    """Ribbon for Measurement tools with live distance display"""
    
    measure_tool_selected = Signal(str)
    clear_measurements = Signal()
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.build_ribbon()
        
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # 📏 Distance Tools
        distance = RibbonSection("Distance", self)
        distance.add_button("Line", "📐", lambda: self.measure_tool_selected.emit("measure_line"))
        distance.add_button("Path", "🛤️", lambda: self.measure_tool_selected.emit("measure_path"))
        layout.addWidget(distance)

        area = RibbonSection("Area", self)
        area.add_button("Block", "▦", lambda: self.measure_tool_selected.emit("measure_block_area"))
        area.add_button("Grid", "▦", lambda: self.measure_tool_selected.emit("measure_grid_area"))
        layout.addWidget(area)
    
        # 🗑️ Actions
        actions = RibbonSection("Actions", self)
        actions.add_button("Clear", "🗑️", self.clear_measurements.emit)
        layout.addWidget(actions)

        # ⚙️ Settings
        settings_section = RibbonSection("Settings", self)
        settings_section.add_button(
            "Settings", "⚙️",
            lambda: self._show_measure_settings(),
            toggleable=False,
        )
        layout.addWidget(settings_section)

        layout.addStretch()

    def _show_measure_settings(self):
        """Open the Measurement Tool Settings dialog."""
        try:
            main_window = self.window()
            from gui.measure_settings_dialog import MeasureSettingsDialog
            dialog = getattr(main_window, "measure_settings_dialog", None)
            already_open = dialog is not None and dialog.isVisible()

            if dialog is None:
                dialog = MeasureSettingsDialog(main_window, parent=main_window)
                main_window.measure_settings_dialog = dialog
                dialog.destroyed.connect(
                    lambda: setattr(main_window, "measure_settings_dialog", None)
                )

            dialog.show()
            if dialog.isMinimized():
                dialog.showNormal()
            dialog.raise_()
            dialog.activateWindow()

            if already_open:
                dialog.highlight_already_open()
        except Exception as e:
            print(f"⚠️ Failed to open Measurement Settings: {e}")
            import traceback
            traceback.print_exc()

class IdentificationRibbon(QWidget):
    """Ribbon for Identification tool to identify point classes on click"""
   
    identify_tool_selected = Signal(str)
    identify_toggled = Signal(bool)
   
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.identify_active = False
        self.select_rectangle_active = False
        self.zoom_rectangle_active = False
        self._pick_method_popup_ref = None
        self.build_ribbon()
       
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
       
        # 🔍 Identification Tools
        # ✅ RibbonSection should be defined in the same file (menu_sidebar_system.py)
        # No import needed if it's in the same module
        identify = RibbonSection("Identify", self)
        self.identify_btn = identify.add_button("Identify", "🔍", self.toggle_identify, toggleable=True)
        self.zoom_rect_btn = identify.add_button("Zoom", "⬚", self.toggle_zoom_rectangle, toggleable=True) 
        self.select_rect_btn = identify.add_button("Select", "☑️", self.toggle_select_rectangle, toggleable=True)  # ✅ NEW
        layout.addWidget(identify)
            
        # ℹ️ Info Display
        info = QWidget()
        info.setObjectName("ribbonSection")
        info_outer_layout = QVBoxLayout(info)
        info_outer_layout.setContentsMargins(4, 3, 4, 3)
        info_outer_layout.setSpacing(3)
        info_outer_layout.setAlignment(Qt.AlignTop)
       
        # Title
        info_title = QLabel("Point Info")
        info_title.setObjectName("ribbonSectionTitle")
        info_title.setAlignment(Qt.AlignCenter)
        info_outer_layout.addWidget(info_title)

        info_box = QWidget()
        info_box.setObjectName("ribbonSectionBox")
        info_box.setAttribute(Qt.WA_StyledBackground, True)
        info_layout = QVBoxLayout(info_box)
        info_layout.setContentsMargins(8, 8, 8, 8)
        info_layout.setSpacing(6)
       
        # Status label - THIS IS THE ONE WE'LL UPDATE
        self.status_label = QLabel("Click on point cloud")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)  # ✅ Allow multi-line text
        self.status_label.setObjectName("paramLabel")
        info_layout.addWidget(self.status_label)
        
        
        
        self.delete_btn = QPushButton("Delete Selected")
        self.delete_btn.setFixedHeight(28)
        self.delete_btn.setVisible(False)  # Hidden until points are selected
        self.delete_btn.setObjectName("sidebarButtonDanger")
        self.delete_btn.clicked.connect(self.delete_selected_points)
        info_layout.addWidget(self.delete_btn)

        info_outer_layout.addWidget(info_box)
        layout.addWidget(info)
        layout.addStretch()
        self._reset_status_label()
        
        
    def toggle_select_rectangle(self):
        """Toggle select rectangle mode on/off"""
        self.select_rectangle_active = not self.select_rectangle_active
        
        if self.select_rectangle_active:
            self._deactivate_identify()
            self._deactivate_zoom_rectangle()

            # Cancel cross-section drawing tool if active (it interferes on first activation)
            if self.app and getattr(self.app, 'cross_section_active', False):
                if hasattr(self.app, '_cancel_cross_section_tool_only'):
                    self.app._cancel_cross_section_tool_only()

            # ✅ RESET: Clear the pick method so the user MUST pick every time
            if hasattr(self.app, 'select_rectangle_tool'):
                self.app.select_rectangle_tool.pick_method = None

            # ✅ TRIGGER: Show the Pick Method selector popup
            try:
                popup = self._ensure_pick_method_popup()
                popup.prepare_for_show()
                self._restore_pick_method_popup(popup)
            except Exception as e:
                print(f"❌ Failed to show Pick Method popup: {e}")
                # Fallback to default block if popup fails
                self.status_label.setText("Draw rectangle to select points")
                self.status_label.setStyleSheet("color: #ff9800; font-weight: bold;")
                if hasattr(self.app, 'select_rectangle_tool'):
                    self.app.select_rectangle_tool.activate()
                    self.app.select_rectangle_tool.set_pick_method("Block")
                
        else:
            self._deactivate_select_rectangle()
            self._reset_status_label()

    def _get_pick_method_popup(self):
        popup = getattr(self, "_pick_method_popup_ref", None)
        if popup is None:
            return None
        try:
            popup.windowTitle()
        except (RuntimeError, ReferenceError):
            self._pick_method_popup_ref = None
            return None
        return popup

    def _ensure_pick_method_popup(self):
        popup = self._get_pick_method_popup()
        if popup is not None:
            return popup

        from gui.pick_method_popup import PickMethodPopup

        popup = PickMethodPopup(parent=self)
        popup.method_changed.connect(self._on_pick_method_chosen)
        popup.rejected.connect(self._on_pick_method_popup_rejected)
        popup.destroyed.connect(lambda: setattr(self, "_pick_method_popup_ref", None))
        self._pick_method_popup_ref = popup
        return popup

    def _restore_pick_method_popup(self, popup):
        if popup.windowState() & Qt.WindowMinimized:
            popup.showNormal()
        elif not popup.isVisible():
            popup.show()
        else:
            popup.show()
        popup.raise_()
        popup.activateWindow()

    def _on_pick_method_chosen(self, method):
        if not method:
            self._on_pick_method_popup_rejected()
            return

        self.status_label.setText(f"Selection Mode: {method}")
        self.status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
        if hasattr(self.app, 'select_rectangle_tool'):
            self.app.select_rectangle_tool.activate()
            self.app.select_rectangle_tool.set_pick_method(method)

    def _on_pick_method_popup_rejected(self):
        if not self.select_rectangle_active:
            return
        self._deactivate_select_rectangle()
        self._reset_status_label()

    def deactivate_all_tools(self):
        """Deactivate all identify/select tools in this ribbon."""
        self._deactivate_identify()
        self._deactivate_zoom_rectangle()
        self._deactivate_select_rectangle()
        self._reset_status_label()

    def _reset_button_style(self, button):
        """Reset button to default style"""
        button.setStyleSheet("")

    def _reset_status_label(self):
        """Reset ribbon status to the default idle state."""
        from gui.theme_manager import get_status_label_style

        self.status_label.setText("Click on point cloud")
        self.status_label.setStyleSheet(get_status_label_style())

    def _deactivate_identify(self):
        """Ensure the Identify tool is fully turned off."""
        self.identify_active = False
        self.identify_btn.setChecked(False)
        self._reset_button_style(self.identify_btn)

        if self.app and hasattr(self.app, 'identification_tool'):
            self.app.identification_tool.deactivate()

    def _deactivate_zoom_rectangle(self):
        """Ensure the Zoom tool is fully turned off."""
        self.zoom_rectangle_active = False
        self.zoom_rect_btn.setChecked(False)
        self._reset_button_style(self.zoom_rect_btn)

        if self.app and hasattr(self.app, 'zoom_rectangle_tool'):
            self.app.zoom_rectangle_tool.deactivate()

    def _deactivate_select_rectangle(self):
        """Ensure the Select tool is fully turned off."""
        self.select_rectangle_active = False
        self.select_rect_btn.setChecked(False)
        self._reset_button_style(self.select_rect_btn)
        self.delete_btn.setVisible(False)

        popup = self._get_pick_method_popup()
        if popup is not None:
            popup.hide()

        if self.app and hasattr(self.app, 'select_rectangle_tool'):
            self.app.select_rectangle_tool.deactivate()

    def update_selected_count(self, count):
        """Update status label with number of selected points"""
        if count > 0:
            self.status_label.setText(f"{count:,} points selected")
            self.status_label.setStyleSheet("color: #ff9800; font-weight: bold;")
            self.delete_btn.setVisible(True)
        else:
            self.status_label.setText("Draw rectangle to select points")
            self.delete_btn.setVisible(False)

    def delete_selected_points(self):
        """Delete the selected points"""
        if self.app and hasattr(self.app, 'select_rectangle_tool'):
            self.app.select_rectangle_tool.delete_selected_points()
            
    def toggle_identify(self):
        """Toggle identification mode on/off"""
        self.identify_active = not self.identify_active
        
        if self.identify_active:
            self._deactivate_zoom_rectangle()
            self._deactivate_select_rectangle()

            # Cancel cross-section drawing tool if active (it interferes on first activation)
            if self.app and getattr(self.app, 'cross_section_active', False):
                if hasattr(self.app, '_cancel_cross_section_tool_only'):
                    self.app._cancel_cross_section_tool_only()

        if self.identify_active:
            self.status_label.setText("Active")

            self.status_label.setStyleSheet("color: #4caf50; font-weight: bold;")
           
            # Activate the identification tool
            if self.app and hasattr(self.app, 'identification_tool'):
                self.app.identification_tool.activate()
            else:
                print("❌ ERROR: Cannot find identification_tool!")
               
        else:
            self._deactivate_identify()
            self._reset_status_label()
       
        self.identify_toggled.emit(self.identify_active)
        
        
    def toggle_zoom_rectangle(self):
        """Toggle zoom rectangle mode on/off"""
        self.zoom_rectangle_active = not self.zoom_rectangle_active
        
        if self.zoom_rectangle_active:
            self._deactivate_identify()
            self._deactivate_select_rectangle()

            # Cancel cross-section drawing tool if active (it interferes on first activation)
            if self.app and getattr(self.app, 'cross_section_active', False):
                if hasattr(self.app, '_cancel_cross_section_tool_only'):
                    self.app._cancel_cross_section_tool_only()

            self.status_label.setText("Draw rectangle & right-click")
            self.status_label.setStyleSheet("color: #2196f3; font-size: 9px; font-weight: bold; padding: 4px;")

            # Activate zoom rectangle tool
            if self.app and hasattr(self.app, 'zoom_rectangle_tool'):
                self.app.zoom_rectangle_tool.activate()

        else:
            self._deactivate_zoom_rectangle()
            self._reset_status_label()
   
    def update_info(self, class_code, class_name, xyz, color=None, lvl=None, fields=None):
        """
        Update the displayed information with class color background.

        Args:
            class_code: Classification code (e.g., 2 for Ground)
            class_name: Human-readable name (e.g., "Ground")
            xyz: Tuple of (x, y, z) coordinates
            color: Optional RGB tuple (r, g, b) for background color
            lvl: Optional level/priority string (e.g., "5")
            fields: Optional ordered dict of {field_label: display_value},
                already filtered to what the user checked in the View Fields
                dialog (Display ▸ Fields). When provided, this drives the
                displayed text instead of the fixed Class/Lvl/XYZ layout.
        """
        if fields:
            pairs = [f"{label}: {value}" for label, value in fields.items()]
            lines = ["  |  ".join(pairs[i:i + 2]) for i in range(0, len(pairs), 2)]
            info_text = "\n".join(lines)
        else:
            # ✅ NEW: Build info text with Level
            x, y, z = xyz
            info_text = f"Class {class_code}: {class_name}"

            # ✅ ADD: Show Level if available
            if lvl:
                info_text += f" | Lvl: {lvl}"

            # Add coordinates on second line
            info_text += f"\nXYZ: ({x:.2f}, {y:.2f}, {z:.2f})"

        self.status_label.setText(info_text)
        
        # ✅ Apply class color as background if provided
        if color:
            r, g, b = color
            
            # Calculate luminance to determine if we need dark or light text
            luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            text_color = "#000000" if luminance > 0.5 else "#FFFFFF"
            
            # Apply colored background to status_label
            self.status_label.setStyleSheet(f"""
                QLabel {{
                    color: {text_color};
                    font-size: 9px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: rgba({r}, {g}, {b}, 0.9);
                    border: 2px solid rgb({r}, {g}, {b});
                    border-radius: 3px;
                }}
            """)
        else:
            # Fallback to default blue
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #ffffff;
                    font-size: 9px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #0d47a1;
                    border: 2px solid #1565c0;
                    border-radius: 3px;
                }
            """)
    def clear(self):
        """Clear the information display."""
        self._reset_status_label()

def make_color_icon(rgb):
    """Helper to create color icons for class picker"""
    pix = QPixmap(20, 12)
    pix.fill(QColor(*rgb))
    return QIcon(pix)


CONVERSION_STANDARD_LEVELS = {
    0: "Created",
    1: "Ground",
    2: "Low vegetation",
    3: "Medium vegetation",
    4: "High vegetation",
    5: "Buildings",
    6: "Water",
    7: "Railways",
    8: "Railways (structure)",
    9: "Type 1 Street",
    10: "Type 2 Street",
    11: "Type 3 Street",
    12: "Type 4 Street",
    13: "Bridge",
    14: "Bare Conductors",
    15: "Elicord Overhead Cables",
    16: "Pylons or Poles",
    17: "HV Overhead Lines",
    18: "MV Overhead Lines",
    19: "LV Overhead Lines",
    20: "Other Lines",
    21: "Class 21",
    22: "Class 22",
    31: "Practicable Positions",
    32: "Class 32",
    51: "Low point",
    52: "Isolated point",
}


def _coerce_rgb_tuple(value, default=(128, 128, 128)):
    """Best-effort conversion of QColor/tuple/list into an RGB tuple."""
    try:
        if isinstance(value, QColor):
            return (int(value.red()), int(value.green()), int(value.blue()))
    except Exception:
        pass

    if isinstance(value, (tuple, list)) and len(value) >= 3:
        try:
            return (int(value[0]), int(value[1]), int(value[2]))
        except Exception:
            pass

    return default


def _collect_conversion_classes(app, standard_levels=None):
    """
    Build class rows used by conversion dialogs from the best available sources.
    Priority: Display Mode table -> slot 0 palettes -> app.class_palette.
    """
    resolved_levels = dict(CONVERSION_STANDARD_LEVELS)
    if isinstance(standard_levels, dict):
        resolved_levels.update(standard_levels)

    class_map = {}

    def _norm_text(value):
        if value is None:
            return ""
        return " ".join(str(value).strip().split())

    def _is_placeholder_label(value, code):
        """
        True when text is effectively a placeholder for the numeric class code
        (e.g. "", "20", "Class 20", "cls20"), not a real human label.
        """
        text = _norm_text(value)
        if not text:
            return True

        code_text = str(code)
        low = text.lower()
        placeholder_tokens = {
            code_text.lower(),
            f"class {code_text}",
            f"class{code_text}",
            f"cls {code_text}",
            f"cls{code_text}",
            f"code {code_text}",
            f"code{code_text}",
            "unknown",
            "n/a",
            "na",
            "-",
        }
        if low in placeholder_tokens:
            return True

        try:
            if text.replace(".", "", 1).isdigit():
                return int(float(text)) == int(code)
        except Exception:
            pass
        return False

    def _is_informative_label(value, code):
        return not _is_placeholder_label(value, code)

    def _upsert(code, desc="", lvl="", color=None):
        try:
            code = int(code)
        except Exception:
            return

        entry = class_map.get(code)
        if entry is None:
            entry = {"code": code, "desc": "", "lvl": "", "color": (128, 128, 128)}
            class_map[code] = entry

        desc_text = _norm_text(desc)
        lvl_text = _norm_text(lvl)

        # Prefer informative labels over placeholders, regardless of source order.
        if desc_text:
            if _is_informative_label(desc_text, code):
                if not _is_informative_label(entry["desc"], code):
                    entry["desc"] = desc_text
            elif not entry["desc"]:
                entry["desc"] = desc_text

        if lvl_text:
            if _is_informative_label(lvl_text, code):
                if not _is_informative_label(entry["lvl"], code):
                    entry["lvl"] = lvl_text
            elif not entry["lvl"]:
                entry["lvl"] = lvl_text

        if color is not None:
            entry["color"] = _coerce_rgb_tuple(color, entry["color"])

    # Source 1: Active Display Mode table (authoritative when available).
    display_dialog = getattr(app, "display_mode_dialog", getattr(app, "display_dialog", None))
    table = getattr(display_dialog, "table", None) if display_dialog is not None else None
    if table is not None:
        for row in range(table.rowCount()):
            code = None
            for code_col in (1, 0, 2):
                item = table.item(row, code_col)
                if item is None:
                    continue
                try:
                    code = int(str(item.text()).strip())
                    break
                except Exception:
                    continue
            if code is None:
                continue

            desc_item = table.item(row, 2)
            desc = desc_item.text() if desc_item is not None else ""

            lvl_item = table.item(row, 4)
            lvl = lvl_item.text() if lvl_item is not None else ""

            color_item = table.item(row, 5)
            qcolor = color_item.background().color() if color_item is not None else None

            _upsert(code, desc=desc, lvl=lvl, color=qcolor)

    # Source 2+: Palette dicts, including slot 0 mirrors.
    palette_candidates = []
    if callable(getattr(app, "_get_main_view_palette", None)):
        try:
            palette_candidates.append(app._get_main_view_palette())
        except Exception:
            pass
    if display_dialog is not None and hasattr(display_dialog, "view_palettes"):
        palette_candidates.append((display_dialog.view_palettes or {}).get(0))
    if hasattr(app, "view_palettes"):
        palette_candidates.append((app.view_palettes or {}).get(0))
    palette_candidates.append(getattr(app, "class_palette", None))

    for palette in palette_candidates:
        if not isinstance(palette, dict):
            continue
        for code, info in palette.items():
            if not isinstance(info, dict):
                continue
            desc = info.get("description", "")
            lvl = info.get("lvl", "")
            if not lvl and desc and str(desc).strip() and str(desc).strip() != str(code):
                lvl = desc
            _upsert(code, desc=desc, lvl=lvl, color=info.get("color", (128, 128, 128)))

    # Last resort: infer codes from loaded classification array.
    if not class_map:
        data = getattr(app, "data", None)
        cls_arr = data.get("classification") if isinstance(data, dict) else None
        if cls_arr is not None:
            try:
                for code in np.unique(cls_arr):
                    _upsert(code)
            except Exception:
                pass

    class_list = []
    for code in sorted(class_map.keys()):
        entry = class_map[code]
        lvl = _norm_text(entry.get("lvl", ""))
        desc = _norm_text(entry.get("desc", ""))

        if _is_placeholder_label(lvl, code):
            # If description has the real class text, promote it to level.
            if _is_informative_label(desc, code):
                lvl = desc
                desc = ""
            else:
                lvl = resolved_levels.get(code, str(code))

        # Hide placeholder descriptions.
        if _is_placeholder_label(desc, code):
            desc = ""

        # Avoid labels like "Ground (Ground)" or "20 (20)".
        if desc:
            same_as_lvl = desc.lower() == lvl.lower()
            same_as_code = desc == str(code)
            if same_as_lvl or same_as_code:
                desc = ""

        class_list.append(
            {
                "code": code,
                "desc": desc,
                "lvl": lvl,
                "color": _coerce_rgb_tuple(entry.get("color", (128, 128, 128))),
            }
        )

    return class_list


def _apply_dialog_theme(dialog):
    """Apply the shared themed dialog stylesheet when available."""
    try:
        from gui.theme_manager import get_dialog_stylesheet
        dialog.setStyleSheet(get_dialog_stylesheet())
    except Exception:
        pass


def _create_dialog_info_strip(text):
    """Create a compact top info strip like the newer management dialogs."""
    frame = QFrame()
    frame.setObjectName("dialogInfoStrip")

    layout = QHBoxLayout(frame)
    layout.setContentsMargins(10, 6, 10, 6)
    layout.setSpacing(6)

    label = QLabel(text)
    label.setObjectName("dialogInlineNote")
    label.setWordWrap(True)
    layout.addWidget(label)

    return frame, label


def _create_dialog_card(title=None, badge_text=None):
    """Create a compact bordered card for dialog sections."""
    frame = QFrame()
    frame.setObjectName("dialogCard")

    layout = QVBoxLayout(frame)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(6)

    badge = None
    if title:
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        label = QLabel(title)
        label.setObjectName("dialogSectionLabel")
        header.addWidget(label)
        header.addStretch()

        if badge_text is not None:
            badge = QLabel(str(badge_text))
            badge.setObjectName("valuePill")
            badge.setAlignment(Qt.AlignCenter)
            badge.setMinimumWidth(28)
            header.addWidget(badge)

        layout.addLayout(header)

    return frame, layout, badge


def _set_compact_button_role(button, role="secondary"):
    """Use compact themed button variants instead of ad-hoc inline styles."""
    if role == "primary":
        button.setObjectName("displayApplyBtn")
        button.setStyleSheet("QPushButton#displayApplyBtn { min-height: 26px; }")
    elif role == "danger":
        button.setObjectName("dangerBtn")
        button.setStyleSheet("QPushButton#dangerBtn { min-height: 26px; }")
    else:
        button.setObjectName("displayActionButton")
        button.setStyleSheet("QPushButton#displayActionButton { min-height: 26px; }")
    return button


def _set_dialog_status(label, text, tone="neutral"):
    """Apply a compact bordered status style to a label."""
    from gui.theme_manager import ThemeColors

    styles = {
        "neutral": (
            ThemeColors.get("bg_input"),
            ThemeColors.get("border_light"),
            ThemeColors.get("text_secondary"),
        ),
        "success": (
            ThemeColors.get("bg_input"),
            ThemeColors.get("success"),
            ThemeColors.get("text_primary"),
        ),
        "danger": (
            ThemeColors.get("bg_input"),
            ThemeColors.get("danger"),
            ThemeColors.get("danger"),
        ),
    }
    background, border, color = styles.get(tone, styles["neutral"])

    label.setText(text)
    label.setWordWrap(True)
    label.setStyleSheet(
        "QLabel {{"
        f"background-color:{background};"
        f"border:1px solid {border};"
        "border-radius:6px;"
        "padding:5px 8px;"
        f"color:{color};"
        "font-size:8.5pt;"
        "}}"
    )


def _create_dialog_scroll_container(parent_layout):
    """Create a scrollable body while keeping footer actions always visible."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(4, 4, 4, 4)
    layout.setSpacing(4)

    scroll.setWidget(container)
    parent_layout.addWidget(scroll, 1)
    return layout


def _set_fence_picker_button_state(owner, is_open):
    """Reflect whether the child fence picker is open on the launcher button."""
    button = getattr(owner, "select_fence_btn", None)
    if button is None:
        return
    if is_open:
        button.setText("Close Fence Picker")
    else:
        count = len(getattr(owner, "selected_fences", []))
        if count > 0:
            button.setText(f"Select Fence ({count})")
        else:
            button.setText("Select Fence")
    _set_compact_button_role(button, "secondary" if is_open else "primary")


def _get_fence_identity(shape):
    """Return a stable identity for digitizer and curve-tool fences."""
    if not isinstance(shape, dict):
        return id(shape)
    if shape.get("source") == "curve_tool":
        return id(shape.get("curve_data", shape))
    return id(shape)


def _prepare_fence_shape(shape):
    """Close open line-like shapes on a copy so fence masking can use polygons."""
    if not isinstance(shape, dict):
        return shape
    if shape.get("type") not in ["line", "smart_line", "polyline", "smartline", "curve"]:
        return shape

    coords = shape.get("coords", [])
    if isinstance(coords, list):
        coords = np.array(coords)

    try:
        if len(coords) > 0 and not np.array_equal(coords[0], coords[-1]):
            prepared = dict(shape)
            prepared["coords"] = (
                np.vstack([coords, coords[0]])
                if isinstance(coords, np.ndarray)
                else list(coords) + [list(coords[0])]
            )
            return prepared
    except Exception:
        pass

    return shape


def _show_fence_selection_dialog(owner, window_title, info_text):
    """Open a compact non-modal fence picker shared by the By Class dialogs."""
    from PySide6.QtCore import QTimer

    existing_dialog = getattr(owner, "_fence_selection_dialog", None)
    if existing_dialog is not None:
        try:
            if existing_dialog.isVisible():
                existing_dialog.close()
                return
        except (RuntimeError, ReferenceError):
            pass
        owner._fence_selection_dialog = None

    digitize = getattr(owner.app, "digitizer", None)
    curve_tool = getattr(owner.app, "curve_tool", None)

    valid_shapes = []
    if digitize:
        drawings = getattr(digitize, "drawings", [])
        if drawings:
            valid_shapes = [
                drawing
                for drawing in drawings
                if drawing.get("type") in [
                    "rectangle",
                    "circle",
                    "polygon",
                    "freehand",
                    "line",
                    "smart_line",
                    "polyline",
                    "smartline",
                ]
            ]

    curve_fences = []
    if curve_tool and hasattr(curve_tool, "get_curves_as_fences"):
        curve_fences = curve_tool.get_curves_as_fences()

    all_fences = valid_shapes + curve_fences
    if not all_fences:
        QMessageBox.warning(
            owner,
            "No Shapes Found",
            "No shapes or curves found.\n\n"
            "• Draw shapes using Digitize tools, OR\n"
            "• Draw curves using the Curve tool",
        )
        return

    if not digitize and not curve_tool:
        QMessageBox.warning(
            owner,
            "No Tools Available",
            "Digitize manager and Curve tool not found.",
        )
        return

    from gui.theme_manager import ThemeColors
    from PySide6.QtWidgets import QLabel as QLabel2

    renderer = getattr(getattr(owner.app, "vtk_widget", None), "renderer", None)

    def _add_actor_to_renderer(actor):
        if actor is None:
            return
        r = getattr(getattr(owner.app, "vtk_widget", None), "renderer", None)
        if r is None:
            return
        try:
            if hasattr(actor, "IsA") and actor.IsA("vtkActor2D"):
                r.AddViewProp(actor)
            else:
                r.AddActor(actor)
        except Exception:
            pass

    def _remove_actor_from_renderer(actor):
        if actor is None:
            return
        r = getattr(getattr(owner.app, "vtk_widget", None), "renderer", None)
        if r is None:
            return
        try:
            r.RemoveActor(actor)
        except Exception:
            pass
        try:
            r.RemoveActor2D(actor)
        except Exception:
            pass
        try:
            r.RemoveViewProp(actor)
        except Exception:
            pass

    def _render_view():
        try:
            owner.app.vtk_widget.render()
        except Exception:
            pass

    def _make_highlight_actor(coords, color, width):
        try:
            import vtk

            points = vtk.vtkPoints()
            points.SetDataTypeToDouble()
            for coord in coords:
                z_value = float(coord[2]) if len(coord) > 2 else 0.0
                points.InsertNextPoint(float(coord[0]), float(coord[1]), z_value)

            polyline = vtk.vtkPolyLine()
            polyline.GetPointIds().SetNumberOfIds(len(coords))
            for index in range(len(coords)):
                polyline.GetPointIds().SetId(index, index)

            cell_array = vtk.vtkCellArray()
            cell_array.InsertNextCell(polyline)

            poly_data = vtk.vtkPolyData()
            poly_data.SetPoints(points)
            poly_data.SetLines(cell_array)

            mapper = vtk.vtkPolyDataMapper2D()
            mapper.SetInputData(poly_data)

            transform = vtk.vtkCoordinate()
            transform.SetCoordinateSystemToWorld()
            mapper.SetTransformCoordinate(transform)

            actor = vtk.vtkActor2D()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(*color)
            actor.GetProperty().SetLineWidth(width)
            actor.GetProperty().SetDisplayLocationToForeground()
            return actor
        except Exception:
            try:
                if hasattr(owner.app, "digitizer"):
                    return owner.app.digitizer._make_polyline_actor(
                        coords,
                        color=color,
                        width=width,
                    )
            except Exception:
                pass
            return None

    if hasattr(owner, "_selection_highlight_actors"):
        for actor in list(getattr(owner, "_selection_highlight_actors", []) or []):
            _remove_actor_from_renderer(actor)
        owner._selection_highlight_actors = []

    current_hover_actor = [None]
    selection_highlight_actors = {}

    def highlight_fence_in_3d(shape):
        if current_hover_actor[0] is not None:
            _remove_actor_from_renderer(current_hover_actor[0])
            current_hover_actor[0] = None
        if not shape:
            _render_view()
            return
        coords = shape.get("coords", [])
        if not coords:
            return
        highlight_actor = _make_highlight_actor(coords, (1, 1, 0), 6)
        if highlight_actor is not None:
            _add_actor_to_renderer(highlight_actor)
            current_hover_actor[0] = highlight_actor
            _render_view()

    def add_selection_highlight(shape):
        coords = shape.get("coords", [])
        if not coords:
            return
        actor = _make_highlight_actor(coords, (0, 0.5, 1), 5)
        if actor is not None:
            selection_highlight_actors[_get_fence_identity(shape)] = actor
            _add_actor_to_renderer(actor)
            _render_view()

    def remove_selection_highlight(shape):
        actor = selection_highlight_actors.pop(_get_fence_identity(shape), None)
        if actor is not None:
            _remove_actor_from_renderer(actor)
            _render_view()

    def row_stylesheet(is_curve=False, selected=False):
        background = ThemeColors.get("dialog_selection", ThemeColors.get("bg_tertiary"))
        if not selected:
            background = ThemeColors.get("bg_secondary") if is_curve else ThemeColors.get("bg_input")
        border = ThemeColors.get("border_active") if selected else ThemeColors.get("border_light")
        hover_background = ThemeColors.get("bg_button_hover")
        return (
            "QWidget#fencePickerRow {"
            f"background-color:{background};"
            f"border:1px solid {border};"
            "border-radius:8px;"
            "}"
            "QWidget#fencePickerRow:hover {"
            f"background-color:{hover_background};"
            f"border:1px solid {ThemeColors.get('border_active')};"
            "}"
        )

    dialog = QDialog(owner, Qt.Dialog)
    owner._fence_selection_dialog = dialog
    _apply_dialog_theme(dialog)
    dialog.setAttribute(Qt.WA_DeleteOnClose, True)
    dialog.setWindowTitle(window_title)
    dialog.setWindowModality(Qt.NonModal)
    dialog.resize(430, 460)

    _set_fence_picker_button_state(owner, True)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(6)

    info_strip, _ = _create_dialog_info_strip(info_text)
    layout.addWidget(info_strip)

    summary_card, summary_layout, summary_badge = _create_dialog_card("Available Fences", len(all_fences))
    count_label = QLabel(
        f"Digitizer: {len(valid_shapes)} | Curves: {len(curve_fences)}"
    )
    count_label.setObjectName("dialogCaption")
    summary_layout.addWidget(count_label)

    permanent_check = QCheckBox("Keep selected fences for next conversion")
    permanent_check.setChecked(bool(getattr(owner, "permanent_fence_mode", True) or not owner.selected_fences))
    summary_layout.addWidget(permanent_check)
    layout.addWidget(summary_card)

    list_card, list_layout, _ = _create_dialog_card("Fence List")
    fence_list = QListWidget()
    fence_list.setSelectionMode(QAbstractItemView.NoSelection)
    fence_list.setStyleSheet(
        "QListWidget {"
        f"background-color:{ThemeColors.get('bg_input')};"
        f"border:1px solid {ThemeColors.get('border_light')};"
        "border-radius:8px;"
        "padding:4px;"
        "}"
        "QListWidget::item {"
        "border:none;"
        "padding:0px;"
        "}"
    )
    list_layout.addWidget(fence_list, 1)

    stats_label = QLabel()
    list_layout.addWidget(stats_label)
    layout.addWidget(list_card, 1)

    custom_widgets = []
    current_ids = {_get_fence_identity(fence) for fence in owner.selected_fences}
    live_source_ids = {_get_fence_identity(fence) for fence in all_fences}

    for index, fence in enumerate(all_fences):
        shape_type = fence.get("type", "unknown")
        coords = fence.get("coords", [])
        is_curve = fence.get("source") == "curve_tool"
        title_text = (
            f"Curve #{fence.get('curve_index', index) + 1}"
            if is_curve
            else f"#{index + 1}: {shape_type.replace('_', ' ').title()}"
        )
        source_tag = "Curve Tool" if is_curve else "Digitizer"

        size_text = ""
        try:
            coord_array = np.array(coords)
            width = coord_array[:, 0].max() - coord_array[:, 0].min()
            height = coord_array[:, 1].max() - coord_array[:, 1].min()
            size_text = f"{width:.1f} x {height:.1f} m"
        except Exception:
            pass

        is_current = _get_fence_identity(fence) in current_ids

        item_widget = QWidget()
        item_widget.setObjectName("fencePickerRow")
        item_widget.setStyleSheet(row_stylesheet(is_curve, is_current))
        item_widget.setCursor(Qt.PointingHandCursor)

        item_layout = QHBoxLayout(item_widget)
        item_layout.setContentsMargins(8, 4, 8, 4)
        item_layout.setSpacing(6)

        checkbox = QCheckBox()
        checkbox.setCursor(Qt.PointingHandCursor)
        item_layout.addWidget(checkbox, 0, Qt.AlignVCenter)

        title_label = QLabel2(title_text)
        title_label.setStyleSheet(
            f"color:{ThemeColors.get('text_primary')};"
            "font-weight:600;"
            "background:transparent;"
            "border:none;"
        )
        item_layout.addWidget(title_label, 1, Qt.AlignVCenter)

        delete_button = None
        if not is_curve:
            delete_button = QPushButton("X")
            delete_button.setAutoDefault(False)
            _set_compact_button_role(delete_button, "danger")
            delete_button.setStyleSheet(
                "QPushButton#dangerBtn {"
                "  padding: 0px;"
                "  min-height: 24px;"
                "  min-width: 24px;"
                "  max-width: 24px;"
                "  max-height: 24px;"
                "  border-radius: 4px;"
                "}"
            )
            item_layout.addWidget(delete_button, 0, Qt.AlignVCenter)
        else:
            spacer = QWidget()
            spacer.setFixedWidth(24)
            item_layout.addWidget(spacer)

        list_item = QListWidgetItem()
        list_item.setSizeHint(item_widget.sizeHint())
        fence_list.addItem(list_item)
        fence_list.setItemWidget(list_item, item_widget)

        def make_toggle(row_widget, shape_data, curve_shape):
            def toggle(checked):
                row_widget.setStyleSheet(row_stylesheet(curve_shape, checked))
                if checked:
                    add_selection_highlight(shape_data)
                else:
                    remove_selection_highlight(shape_data)
            return toggle

        checkbox.toggled.connect(make_toggle(item_widget, fence, is_curve))

        if is_current:
            checkbox.setChecked(True)

        if delete_button is not None:
            def make_row_click(row_checkbox, danger_button, row_item):
                def on_mouse_press(event):
                    fence_list.setCurrentItem(row_item)
                    if not danger_button.underMouse():
                        row_checkbox.setChecked(not row_checkbox.isChecked())
                return on_mouse_press

            item_widget.mousePressEvent = make_row_click(checkbox, delete_button, list_item)

            def make_delete(row_checkbox):
                def delete_action():
                    row_checkbox.setChecked(False)
                return delete_action

            delete_button.clicked.connect(make_delete(checkbox))
        else:
            def make_curve_click(row_checkbox, row_item):
                def on_mouse_press(event):
                    fence_list.setCurrentItem(row_item)
                    row_checkbox.setChecked(not row_checkbox.isChecked())
                return on_mouse_press

            item_widget.mousePressEvent = make_curve_click(checkbox, list_item)

        class HoverEventFilter(QWidget):
            def __init__(self, parent, shape_data):
                super().__init__(parent)
                self.shape_data = shape_data

            def eventFilter(self, obj, event):
                if event.type() == QEvent.Enter:
                    highlight_fence_in_3d(self.shape_data)
                elif event.type() == QEvent.Leave:
                    highlight_fence_in_3d(None)
                return super().eventFilter(obj, event)

        item_widget.installEventFilter(HoverEventFilter(item_widget, fence))
        custom_widgets.append((checkbox, fence))

    def _reopen_with_live_data():
        dialog = getattr(owner, "_fence_selection_dialog", None)
        if dialog is None or not dialog.isVisible():
            return
        digitize_live = getattr(owner.app, "digitizer", None)
        curve_tool_live = getattr(owner.app, "curve_tool", None)
        live_valid = []
        if digitize_live:
            drawings = getattr(digitize_live, "drawings", []) or []
            if drawings:
                live_valid = [
                    drawing
                    for drawing in drawings
                    if drawing.get("type") in [
                        "rectangle",
                        "circle",
                        "polygon",
                        "freehand",
                        "line",
                        "smart_line",
                        "polyline",
                        "smartline",
                    ]
                ]
        live_curve_fences = []
        if curve_tool_live and hasattr(curve_tool_live, "get_curves_as_fences"):
            live_curve_fences = curve_tool_live.get_curves_as_fences()
        live_ids = {_get_fence_identity(fence) for fence in (live_valid + live_curve_fences)}
        if live_ids == live_source_ids:
            return
        try:
            dialog.close()
        except Exception:
            pass
        QTimer.singleShot(
            0,
            lambda: _show_fence_selection_dialog(owner, window_title, info_text),
        )

    refresh_timer = QTimer(dialog)
    refresh_timer.setInterval(350)
    refresh_timer.timeout.connect(_reopen_with_live_data)
    refresh_timer.start()
    dialog._fence_selection_refresh_timer = refresh_timer

    def update_stats():
        checked_count = sum(1 for checkbox, _ in custom_widgets if checkbox.isChecked())
        if checked_count == 0:
            _set_dialog_status(stats_label, "No fences selected", "neutral")
        elif checked_count == 1:
            _set_dialog_status(stats_label, "1 fence selected", "success")
        else:
            _set_dialog_status(stats_label, f"{checked_count} fences selected", "success")
        if summary_badge is not None:
            summary_badge.setText(str(len(all_fences)))

    for checkbox, _ in custom_widgets:
        checkbox.toggled.connect(update_stats)

    update_stats()

    footer = QHBoxLayout()
    footer.setContentsMargins(0, 0, 0, 0)
    footer.setSpacing(6)

    select_all_button = QPushButton("Select All")
    select_all_button.setAutoDefault(False)
    _set_compact_button_role(select_all_button)
    select_all_button.clicked.connect(
        lambda: [checkbox.setChecked(True) for checkbox, _ in custom_widgets]
    )
    footer.addWidget(select_all_button)

    clear_all_button = QPushButton("Clear All")
    clear_all_button.setAutoDefault(False)
    _set_compact_button_role(clear_all_button)

    def clear_all():
        for checkbox, _ in custom_widgets:
            checkbox.setChecked(False)

    clear_all_button.clicked.connect(clear_all)
    footer.addWidget(clear_all_button)
    footer.addStretch()

    apply_button = QPushButton("Apply Selection")
    apply_button.setAutoDefault(False)
    apply_button.setMinimumWidth(140)
    _set_compact_button_role(apply_button, "primary")

    applied = [False]

    def apply_selection():
        applied[0] = True
        highlight_fence_in_3d(None)
        selected_shapes = [shape for checkbox, shape in custom_widgets if checkbox.isChecked()]

        owner.permanent_fence_mode = permanent_check.isChecked()
        prepared_shapes = []
        seen_ids = set()
        for shape in selected_shapes:
            fence_id = _get_fence_identity(shape)
            if fence_id in seen_ids:
                continue
            prepared_shapes.append(_prepare_fence_shape(shape))
            seen_ids.add(fence_id)

        owner.selected_fences = prepared_shapes

        owner._selection_highlight_actors = list(selection_highlight_actors.values())
        owner.update_fence_display()

        shape_count = sum(1 for fence in owner.selected_fences if fence.get("source") != "curve_tool")
        curve_count = sum(1 for fence in owner.selected_fences if fence.get("source") == "curve_tool")
        total_points = sum(len(fence.get("coords", [])) for fence in owner.selected_fences)

        parts = []
        if shape_count:
            parts.append(f"{shape_count} shape(s)")
        if curve_count:
            parts.append(f"{curve_count} curve(s)")

        if hasattr(owner, "fence_status"):
            if not parts:
                _set_dialog_status(
                    owner.fence_status,
                    "No fence selected",
                    "neutral",
                )
            else:
                _set_dialog_status(
                    owner.fence_status,
                    f"{' + '.join(parts)} selected ({total_points} pts)",
                    "success",
                )

        _render_view()
        dialog.close()

    apply_button.clicked.connect(apply_selection)
    footer.addWidget(apply_button)

    close_button = QPushButton("Close")
    close_button.setAutoDefault(False)
    _set_compact_button_role(close_button)
    close_button.clicked.connect(dialog.close)
    footer.addWidget(close_button)
    layout.addLayout(footer)

    def on_dialog_finished(result):
        highlight_fence_in_3d(None)
        if not applied[0]:
            for actor in list(selection_highlight_actors.values()):
                _remove_actor_from_renderer(actor)
            selection_highlight_actors.clear()
            if owner.selected_fences and hasattr(owner, "_restore_highlights_from_data"):
                try:
                    owner._restore_highlights_from_data()
                except Exception:
                    pass
        owner._fence_selection_dialog = None
        _set_fence_picker_button_state(owner, False)

    dialog.finished.connect(on_dialog_finished)

    def select_focused_or_first_fence():
        row = fence_list.currentRow()
        if row < 0:
            row = 0
        if 0 <= row < len(custom_widgets):
            custom_widgets[row][0].setChecked(True)

    def dialog_key_press(event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            select_focused_or_first_fence()
            event.accept()
            return
        QDialog.keyPressEvent(dialog, event)

    dialog.keyPressEvent = dialog_key_press
    dialog.show()
    try:
        dialog.raise_()
    except Exception:
        try:
            getattr(dialog, "raise")()
        except Exception:
            pass
    try:
        dialog.activateWindow()
    except Exception:
        pass

    parent_rect = owner.frameGeometry()
    dialog.move(
        parent_rect.x() + max(0, (parent_rect.width() - dialog.width()) // 2),
        parent_rect.y() + max(0, (parent_rect.height() - dialog.height()) // 2),
    )


class ByClassRibbon(QWidget):
    """
    Ribbon for By Class tool - automatic class conversion
    Opens a dialog similar to ClassPicker but with Convert instead of Invert
    
    ✅ NEW: Dialogs auto-restore when hidden/minimized
    """
    conversion_applied = Signal()
    
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._dialog_guard_host = None
        self._dialog_guard_app = None
        self._dialog_raise_scheduled = False
        
        # ✅ Store dialog references
        self.by_class_dialog = None
        self.closed_by_class_dialog = None
        self.height_convert_dialog = None 
        self.inside_fence_dialog = None
        self.low_points_dialog    = None
        self.isolated_dialog      = None
        self.ground_dialog        = None
        self.below_surface_dialog = None
        self.build_ribbon()

        # These work even when dialogs are closed
        from PySide6.QtGui import QShortcut, QKeySequence
        from PySide6.QtCore import Qt
        
        self.undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.undo_shortcut.activated.connect(self.perform_classification_undo)
        self.undo_shortcut.setContext(Qt.ApplicationShortcut)  # ✅ Works app-wide
        
        self.redo_shortcut = QShortcut(QKeySequence("Ctrl+Y"), self)
        self.redo_shortcut.activated.connect(self.perform_classification_redo)
        self.redo_shortcut.setContext(Qt.ApplicationShortcut)  # ✅ Works app-wide
        self._install_dialog_zorder_guard()
        
        print("✅ ByClassRibbon: Application-level undo/redo shortcuts installed")

    def _install_dialog_zorder_guard(self):
        """Keep conversion dialogs above the main Naksha window while not minimized."""
        host = self.app if isinstance(self.app, QWidget) else self.window()
        if host is None or host is self._dialog_guard_host:
            return
        try:
            host.installEventFilter(self)
            from PySide6.QtWidgets import QApplication
            app_instance = QApplication.instance()
            if app_instance is not None and app_instance is not self._dialog_guard_app:
                app_instance.installEventFilter(self)
                self._dialog_guard_app = app_instance
            self._dialog_guard_host = host
            print("✅ ByClassRibbon: dialog z-order guard installed")
        except Exception as e:
            print(f"⚠️ ByClassRibbon: failed to install z-order guard: {e}")

    def _iter_byclass_dialogs(self):
        """Yield only By Class workflow dialogs that must stay above main window."""
        for dlg in (
            self.by_class_dialog,
            self.closed_by_class_dialog,
            self.height_convert_dialog,
            self.inside_fence_dialog,
            self.low_points_dialog,
            self.isolated_dialog,
            self.ground_dialog,
            self.below_surface_dialog,
        ):
            if dlg is not None:
                yield dlg

    def _schedule_dialog_zorder_refresh(self):
        if self._dialog_raise_scheduled:
            return
        self._dialog_raise_scheduled = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, self._refresh_dialog_zorder)

    def _refresh_dialog_zorder(self):
        """Re-raise visible non-minimized By Class dialogs without changing minimize behavior."""
        self._dialog_raise_scheduled = False
        host = self._dialog_guard_host
        if host is None:
            return
        if hasattr(host, "isMinimized") and host.isMinimized():
            return

        for dialog in self._iter_byclass_dialogs():
            try:
                if dialog.isHidden() or not dialog.isVisible():
                    continue
                if dialog.isMinimized():
                    # Respect existing minimize feature exactly.
                    continue
                dialog.raise_()
            except Exception:
                continue

    def eventFilter(self, obj, event):
        try:
            if event is None:
                return super().eventFilter(obj, event)

            host = self._dialog_guard_host
            if host is not None:
                et = event.type()
                is_host_event = (obj is host)
                is_host_child_event = (
                    isinstance(obj, QWidget) and
                    (obj is host or host.isAncestorOf(obj))
                )
                if et == QEvent.WindowActivate and is_host_event:
                    self._schedule_dialog_zorder_refresh()
                elif et in (QEvent.FocusIn, QEvent.MouseButtonPress) and is_host_child_event:
                    self._schedule_dialog_zorder_refresh()
        except Exception:
            pass
        return super().eventFilter(obj, event)
    
    def build_ribbon(self):
        """Build the ribbon UI"""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # 🔄 By Class Section
        by_class_section = RibbonSection("By Class", self)
        
        self.convert_btn = by_class_section.add_button(
            "Convert", 
            "🔄", 
            self.open_convert_dialog,
            toggleable=False
        )
        self.closed_convert_btn = by_class_section.add_button(
            "Close", 
            "📍", 
            self.open_closed_convert_dialog,
            toggleable=False
        )
        self.height_convert_btn = by_class_section.add_button(
            "Height",
            "📏",
            self.open_height_convert_dialog,
            toggleable=False
        )
        self.inside_fence_btn = by_class_section.add_button(
            "Fence", 
            "🔷", 
            self.open_inside_fence_dialog,
            toggleable=False
        )
                    
        layout.addWidget(by_class_section)
        
        algo_section = RibbonSection("Algorithms", self)
        algo_section.add_button("Low Points", "⬇️", self.open_low_points_dialog,    toggleable=False)
        algo_section.add_button("Isolated",   "🔴", self.open_isolated_dialog,      toggleable=False)
        algo_section.add_button("Ground",     "🏔️", self.open_ground_dialog,        toggleable=False)
        algo_section.add_button("Surface",    "📐", self.open_below_surface_dialog,  toggleable=False)
        layout.addWidget(algo_section)

        # ℹ️ Info Display
        info = QWidget()
        info.setObjectName("infoWidget")
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(8, 4, 8, 4)
        info_layout.setSpacing(2)
        
        # Title
        info_title = QLabel("Conversion Info")
        info_title.setObjectName("ribbonSectionTitle")
        info_title.setFont(QFont("Segoe UI", 8))
        info_title.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(info_title)
        
        # Status label
        self.status_label = QLabel("Click Convert to begin")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("""
            QLabel {
                color: #aaaaaa;
                font-size: 9px;
                padding: 4px;
                background-color: #2c2c2c;
                border-radius: 3px;
            }
        """)
        info_layout.addWidget(self.status_label)
        
        layout.addWidget(info)
        layout.addStretch()
        
        
    def perform_classification_undo(self):
        # ✅ If digitizer tool is active, let it handle undo
        # BUT: if InsideFenceDialog is open, classification undo takes priority
        fence_dialog_open = False
        if self.inside_fence_dialog is not None:
            try:
                fence_dialog_open = self.inside_fence_dialog.isVisible()
            except Exception:
                pass

        if not fence_dialog_open:
            digitizer = getattr(self.app, 'digitizer', None)
            if digitizer and getattr(digitizer, 'active_tool', None):
                print("🔄 ByClassRibbon: Digitizer active — routing Ctrl+Z to digitizer")
                digitizer.undo()
                return

        print("🔄 ByClassRibbon: Classification undo triggered")
        if not hasattr(self.app, 'undo_classification'):
            print("⚠️ ByClassRibbon: undo_classification not available")
            return
        
        try:
            # ✅ FIXED: undo_classification() already handles ALL refreshes
            # No need to call update_class_mode again!
            self.app.undo_classification()
            
            # Update point count widget (if undo didn't already do it)
            if hasattr(self.app, 'point_count_widget'):
                if not hasattr(self.app, 'update_point_count_display'):
                    self.app.point_count_widget.schedule_update()
            
            # Update status
            self.update_status("↶ Undo performed", "neutral")
            
            print("✅ Classification undo complete")
            
        except Exception as e:
            print(f"❌ Undo failed: {e}")
            import traceback
            traceback.print_exc()
            self.update_status("❌ Undo failed", "error")
            
    def perform_classification_redo(self):
        """
        ✅ Application-level CLASSIFICATION redo
        Works even when dialogs are closed
        """
        print("🔄 ByClassRibbon: Classification redo triggered")
        
        if not hasattr(self.app, 'redo_classification'):
            print("⚠️ ByClassRibbon: redo_classification not available")
            return
        
        try:
            # ✅ FIXED: redo_classification() already handles ALL refreshes
            # No need to call update_class_mode again!
            self.app.redo_classification()
            
            # Update point count widget (if redo didn't already do it)
            if hasattr(self.app, 'point_count_widget'):
                if not hasattr(self.app, 'update_point_count_display'):
                    self.app.point_count_widget.schedule_update()
            
            # Update status
            self.update_status("↷ Redo performed", "neutral")
            
            print("✅ Classification redo complete")
            
        except Exception as e:
            print(f"❌ Redo failed: {e}")
            import traceback
            traceback.print_exc()
            self.update_status("❌ Redo failed", "error")
            
    def _show_or_raise_dialog(self, dialog, dialog_name):
        """
        ✅ Helper: Show/raise dialog if hidden or minimized
        
        Args:
            dialog: Dialog instance
            dialog_name: Name for logging
        """
        if dialog is None:
            return
        
        # Show if hidden
        if dialog.isHidden():
            print(f"👁️ {dialog_name} was hidden - showing...")
            dialog.show()
        
        # Restore if minimized
        if dialog.isMinimized():
            print(f"⬆️ {dialog_name} was minimized - restoring...")
            dialog.showNormal()
        else:
            try:
                dialog.setWindowState(dialog.windowState() & ~Qt.WindowMinimized)
            except Exception:
                pass

        # Raise to front and activate
        dialog.raise_()
        dialog.activateWindow()
        self._schedule_dialog_zorder_refresh()
        print(f"✅ {dialog_name} raised to front")

    def open_convert_dialog(self):
        """
        ✅ FIXED: Open/show By Class conversion dialog
        Creates new dialog or shows existing one
        """
        from gui.menu_sidebar_system import ByClassDialog
        
        # Create if doesn't exist
        if self.by_class_dialog is None:
            print("🆕 Creating new ByClassDialog...")
            self.by_class_dialog = ByClassDialog(self.app, self)
            
            # Clear reference when destroyed
            self.by_class_dialog.destroyed.connect(
                lambda: setattr(self, 'by_class_dialog', None)
            )

        # Keep class labels fresh even if Display Mode dialog was created later.
        # Defer the population to the next event-loop tick so the dialog window
        # paints IMMEDIATELY after classification (no UI freeze while a heavy
        # main-view refresh is still queued). Behavior is unchanged; only the
        # ordering vs. the paint is relaxed.
        try:
            _dlg = self.by_class_dialog
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: _safe_populate_byclass(_dlg))
        except Exception:
            pass

        # Show and raise
        self._show_or_raise_dialog(self.by_class_dialog, "ByClassDialog")

    def open_closed_convert_dialog(self):
        """
        ✅ FIXED: Open/show Closed By Class dialog
        """
        # Create if doesn't exist
        if self.closed_by_class_dialog is None:
            print("🆕 Creating new ClosedByClassDialog...")
            self.closed_by_class_dialog = ClosedByClassDialog(self.app, self)
            
            # Clear reference when destroyed
            self.closed_by_class_dialog.destroyed.connect(
                lambda: setattr(self, 'closed_by_class_dialog', None)
            )

        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: _safe_populate_byclass_closed(self.closed_by_class_dialog))
        except Exception:
            pass

        # Show and raise
        self._show_or_raise_dialog(self.closed_by_class_dialog, "ClosedByClassDialog")
    
    def open_height_convert_dialog(self):
        """
        ✅ FIXED: Open/show By Height dialog
        """
        # Create if doesn't exist
        if self.height_convert_dialog is None:
            print("🆕 Creating new ByClassHeightDialog...")
            self.height_convert_dialog = ByClassHeightDialog(self.app, self)
            
            # Clear reference when destroyed
            self.height_convert_dialog.destroyed.connect(
                lambda: setattr(self, 'height_convert_dialog', None)
            )

        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: _safe_populate_byclass_height(self.height_convert_dialog))
        except Exception:
            pass

        # Show and raise
        self._show_or_raise_dialog(self.height_convert_dialog, "ByClassHeightDialog")
    
    def open_inside_fence_dialog(self):
        """
        ✅ FIXED: Open/show Inside Fence dialog
        """
        # Create if doesn't exist
        if self.inside_fence_dialog is None:
            print("🆕 Creating new InsideFenceDialog...")
            self.inside_fence_dialog = InsideFenceDialog(self.app, self)
            
            # Clear reference when destroyed
            self.inside_fence_dialog.destroyed.connect(
                lambda: setattr(self, 'inside_fence_dialog', None)
            )
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: _safe_populate_inside_fence(self.inside_fence_dialog))
        except Exception:
            pass
        self._show_or_raise_dialog(self.inside_fence_dialog, "InsideFenceDialog")
    

    def _remember_classifier_dialog(self, attr, dialog):
        """Track the app-owned canonical classifier without creating a copy."""
        previous = getattr(self, attr, None)
        setattr(self, attr, dialog)
        if dialog is None or previous is dialog:
            return
        dialog.destroyed.connect(
            lambda _obj=None, ref=dialog, name=attr: setattr(
                self, name, None
            )
            if getattr(self, name, None) is ref
            else None
        )

    def open_low_points_dialog(self):
        try:
            from gui.lidar_classification_tools import open_classify_low_points
        except ImportError:
            from lidar_classification_tools import open_classify_low_points
        self._remember_classifier_dialog(
            "low_points_dialog",
            open_classify_low_points(self.app),
        )

    def open_isolated_dialog(self):
        try:
            from gui.lidar_classification_tools import open_classify_isolated_points
        except ImportError:
            from lidar_classification_tools import open_classify_isolated_points
        self._remember_classifier_dialog(
            "isolated_dialog",
            open_classify_isolated_points(self.app),
        )

    def open_ground_dialog(self):
        try:
            from gui.lidar_classification_tools import open_classify_ground
        except ImportError:
            from lidar_classification_tools import open_classify_ground
        self._remember_classifier_dialog(
            "ground_dialog",
            open_classify_ground(self.app),
        )

    def open_surface_points_dialog(self):
        try:
            from gui.lidar_classification_tools import open_classify_surface_points
        except ImportError:
            from lidar_classification_tools import open_classify_surface_points
        self._remember_classifier_dialog(
            "surface_points_dialog",
            open_classify_surface_points(self.app),
        )

    def open_below_surface_dialog(self):
        try:
            from gui.lidar_classification_tools import open_classify_below_surface
        except ImportError:
            from lidar_classification_tools import open_classify_below_surface
        self._remember_classifier_dialog(
            "below_surface_dialog",
            open_classify_below_surface(self.app),
        )

    def update_status(self, message, color_type="neutral"):
        """Update the status label with conversion info"""
        self.status_label.setText(message)
        
        if color_type == "success":
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 9px;
                    font-weight: bold;
                    padding: 4px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
        elif color_type == "error":
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #f44336;
                    font-size: 9px;
                    font-weight: bold;
                    padding: 4px;
                    background-color: #c62828;
                    border-radius: 3px;
                }
            """)
        else:
            self.status_label.setStyleSheet("""
                QLabel {
                    color: #aaaaaa;
                    font-size: 9px;
                    padding: 4px;
                    background-color: #2c2c2c;
                    border-radius: 3px;
                }
            """)



from PySide6.QtGui import QShortcut, QKeySequence

class ByClassDialog(QDialog):
    """
    Class Conversion Dialog - Similar to ClassPicker but with Convert button
    ✅ FIXED: Shows level and description like ClassPicker
    ✅ FIXED: Auto-updates when PTC changes in Display Mode
    ✅ FIXED: Preserves selections when classes reload
    ✅ FIXED: Proper undo/redo shortcuts that don't conflict with Digitizer
    """
   
    def __init__(self, app, ribbon_parent):
        # ✅ FIX 1: Robust Parent Finding
        from PySide6.QtWidgets import QWidget
        target_parent = None
        if isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, 'window') and isinstance(app.window, QWidget):
            target_parent = app.window

        # Keep dialog owned by the main app window so it stays above Naksha
        # while remaining non-modal and not globally always-on-top.
        # Parentless top-level window:
        # keeps normal taskbar minimize behavior (no floating mini-bar on desktop).
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_NativeWindow, True)  # Fix: GetDC invalid window handle  
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowModality(Qt.NonModal)
        self.app = app
        self.ribbon_parent = ribbon_parent
        
        self.setWindowTitle("Convert Classes")
        self.setGeometry(200, 200, 380, 320)
        _apply_dialog_theme(self)

        # Shortcuts
       
        
        self.init_ui()
        self.populate_classes()
        self._last_conversion_info = None
        self._connected_display_dialog = None

        self._connect_display_dialog()

    def _get_display_dialog(self):
        return getattr(self.app, 'display_mode_dialog', getattr(self.app, 'display_dialog', None))

    def _connect_display_dialog(self):
        """Keep the dialog wired to the current Display Mode instance."""
        display_dialog = self._get_display_dialog()
        if display_dialog is None:
            return False
        if display_dialog is self._connected_display_dialog:
            return False

        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass

        try:
            display_dialog.classes_loaded.connect(self.on_classes_changed)
        except Exception as e:
            print(f"⚠️ ByClassDialog could not connect to Display Mode updates: {e}")
            return False

        self._connected_display_dialog = display_dialog
        print("✅ ByClassDialog connected to display_mode_dialog.classes_loaded")
        return True

    def closeEvent(self, event):
        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(
                    self.on_classes_changed)
            except Exception:
                pass
            self._connected_display_dialog = None
        super().closeEvent(event)

    def showEvent(self, event):
        """Ensure dialog gets focus when shown."""
        super().showEvent(event)
        self.setFocus()
        self.activateWindow()
        if self._connect_display_dialog():
            self.refresh_class_lists(reason="Display Mode dialog attached", update_status=False)
        print("🔵 ByClassDialog activated and focused")

    def _get_selected_from_classes(self):
        return [item.data(Qt.UserRole) for item in self.from_list.selectedItems()]

    def refresh_class_lists(self, reason="classes changed", update_status=True):
        """Rebuild the class pickers while preserving current selections."""
        old_from_classes = self._get_selected_from_classes()
        old_to = self.to_combo.currentData()

        print("\n" + "=" * 60)
        print(f"🔄 BY CLASS DIALOG: Refresh requested ({reason})")
        print("=" * 60)
        print(f"   📋 Saving selections: From={old_from_classes}, To={old_to}")

        self.populate_classes()

        restored_count = 0
        if old_to is not None:
            idx = self.to_combo.findData(old_to)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)
                restored_count += 1

        if old_from_classes:
            for code in old_from_classes:
                for i in range(self.from_list.count()):
                    item = self.from_list.item(i)
                    if item.data(Qt.UserRole) == code:
                        item.setSelected(True)
                        restored_count += 1
                        break

        print(f"✅ ByClassDialog updated ({restored_count} selections restored)")
        print("=" * 60 + "\n")

        if update_status:
            self.info_label.setText("Class definitions refreshed")
            if hasattr(self.app, 'statusBar'):
                self.app.statusBar().showMessage(
                    "✅ Convert Classes updated with latest PTC definitions",
                    2000
                )

    
    def perform_undo(self):
        """
        Perform CLASSIFICATION undo operation
        ✅ FIXED: Display-mode-aware — doesn't destroy shading with update_class_mode
        ✅ FIXED: undo_classification already handles full refresh for ALL modes
        """
        print("🔄 ByClassDialog: Performing CLASSIFICATION undo...")
        
        if hasattr(self.app, 'undo_classification'):
            try:
                # ✅ undo_classification already handles:
                #    - Reverting classification data
                #    - Display-mode-aware main view refresh (class/shaded/other)
                #    - Cross-section refresh
                #    - Cut section refresh
                #    - Point count update
                self.app.undo_classification()
                
                # ✅ FIX: Only do supplementary class refresh for CLASS mode
                # Calling update_class_mode would DESTROY the shading mesh!
                display_mode = getattr(self.app, 'display_mode', 'class')
                
                if display_mode == 'shaded_class':
                    print(f"   🌓 Shading mode — undo refresh already handled")
                    # ✅ Shading was already rebuilt by _force_main_view_refresh_after_undo
                    # Do NOT call update_class_mode here!
                    
                elif display_mode == 'class':
                    # ✅ Supplementary rebuild for class mode (safety net)
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True) 
                    print(f"   ✅ Main view refreshed (class mode)")
                    
                else:
                    # Do NOT call update_pointcloud — it would repaint with raw
                    # scalar arrays and corrupt classification colors or data.
                    print(f"   📊 {display_mode} mode — undo stored; "
                          f"skipping main view repaint")

                # Refresh cross-sections (safety net — undo_classification also does this)
                if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                    for view_idx in list(self.app.section_vtks.keys()):
                        try:
                            if hasattr(self.app, '_refresh_single_section_view'):
                                self.app._refresh_single_section_view(view_idx)
                        except Exception as e:
                            print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

                # Update point count widget
                if hasattr(self.app, 'point_count_widget'):
                    self.app.point_count_widget.schedule_update()

                
                self.info_label.setText("↶ Undo performed")
                self.info_label.setStyleSheet("""
                    QLabel {
                        color: #ff9800;
                        font-size: 10px;
                        font-weight: bold;
                        padding: 6px;
                        background-color: #3e2723;
                        border-radius: 3px;
                    }
                """)
                print("✅ Classification undo performed from ByClassDialog")
            except Exception as e:
                print(f"❌ Classification undo failed: {e}")
                import traceback
                traceback.print_exc()
                QMessageBox.warning(self, "Undo Failed", f"Could not undo: {str(e)}")
        else:
            print("❌ No undo_classification method found on app")
            QMessageBox.warning(self, "Undo Not Available", 
                            "Classification undo functionality not found in application")

    def perform_redo(self):
        """
        Perform CLASSIFICATION redo operation
        ✅ FIXED: Display-mode-aware — doesn't destroy shading with update_class_mode
        """
        print("🔄 ByClassDialog: Performing CLASSIFICATION redo...")
        
        if hasattr(self.app, 'redo_classification'):
            try:
                self.app.redo_classification()
                
                # ✅ FIX: Display-mode-aware supplementary refresh
                display_mode = getattr(self.app, 'display_mode', 'class')
                
                if display_mode == 'shaded_class':
                    print(f"   🌓 Shading mode — redo refresh needs shading rebuild")
                    try:
                        # ✅ FIX: Save shading visibility BEFORE any palette restore
                        from gui.shading_display import get_cache, update_shaded_class, clear_shading_cache
                        cache = get_cache()
                        saved_vis = getattr(cache, 'visible_classes_set', None)
                        if saved_vis is not None:
                            saved_vis = saved_vis.copy()
                        else:
                            saved_vis = {
                                int(c) for c, e in self.app.class_palette.items()
                                if e.get("show", True)
                            }
                        
                        print(f"   📍 Preserved shading visibility: {sorted(saved_vis)}")
                        
                        # ✅ DON'T call _restore_main_view_palette_for_refresh() — 
                        #    it overrides single-class visibility with slot 0!
                        
                        # ✅ Force class_palette to match saved shading visibility
                        for c in self.app.class_palette:
                            self.app.class_palette[c]["show"] = (int(c) in saved_vis)
                        
                        clear_shading_cache("redo classification in shading mode")
                        update_shaded_class(
                            self.app,
                            getattr(self.app, "last_shade_azimuth", 45.0),
                            getattr(self.app, "last_shade_angle", 45.0),
                            getattr(self.app, "shade_ambient", 0.2),
                            force_rebuild=True
                        )
                        print(f"   ✅ Shading mesh rebuilt after redo "
                              f"({'single' if len(saved_vis) == 1 else 'multi'}-class preserved)")
                    except Exception as e:
                        print(f"   ⚠️ Shading rebuild failed, falling back to class mode: {e}")
                        from gui.class_display import update_class_mode
                        update_class_mode(self.app, force_refresh=True)
                        
                elif display_mode == 'class':
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True)
                    print(f"   ✅ Main view refreshed (class mode)")
                    
                else:
                    print(f"   📊 {display_mode} mode — redo refresh already handled")

                # Refresh cross-sections if needed
                if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                    for view_idx in list(self.app.section_vtks.keys()):
                        try:
                            if hasattr(self.app, '_refresh_single_section_view'):
                                self.app._refresh_single_section_view(view_idx)
                        except Exception as e:
                            print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

                # Update point count widget
                if hasattr(self.app, 'point_count_widget'):
                    self.app.point_count_widget.schedule_update()

                
                self.info_label.setText("↷ Redo performed")
                self.info_label.setStyleSheet("""
                    QLabel {
                        color: #ff9800;
                        font-size: 10px;
                        font-weight: bold;
                        padding: 6px;
                        background-color: #3e2723;
                        border-radius: 3px;
                    }
                """)
                print("✅ Classification redo performed from ByClassDialog")
            except Exception as e:
                print(f"❌ Classification redo failed: {e}")
                import traceback
                traceback.print_exc()
                QMessageBox.warning(self, "Redo Failed", f"Could not redo: {str(e)}")
        else:
            print("❌ No redo_classification method found on app")
            QMessageBox.warning(self, "Redo Not Available", 
                            "Classification redo functionality not found in application")

    def init_ui(self):
        """Initialize a compact class-to-class conversion dialog."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        # Info note
        info_label = QLabel("Convert one or more source classes into a target class.")
        info_label.setStyleSheet("font-size: 9pt; color: gray;")
        layout.addWidget(info_label)

        # From classes
        from_label = QLabel("From Classes:")
        from_label.setStyleSheet("font-weight: bold; font-size: 9.5pt;")
        layout.addWidget(from_label)

        from_note = QLabel("Ctrl+Click to select multiple classes.")
        from_note.setStyleSheet("font-size: 8.5pt; color: gray;")
        layout.addWidget(from_note)

        self.from_list = QListWidget()
        self.from_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.from_list.setMinimumHeight(90)
        self.from_list.setStyleSheet("QListWidget::item { border-bottom: none; padding: 4px; }")
        layout.addWidget(self.from_list, 1)

        # Target class
        to_label = QLabel("Target Class:")
        to_label.setStyleSheet("font-weight: bold; font-size: 9.5pt;")
        layout.addWidget(to_label)

        self.to_combo = QComboBox()
        layout.addWidget(self.to_combo)

        self.info_label = QLabel()
        self.info_label.setAlignment(Qt.AlignCenter)
        _set_dialog_status(self.info_label, "Select source classes and choose the target class.", "neutral")
        layout.addWidget(self.info_label)

        self.convert_btn = QPushButton("Convert")
        _set_compact_button_role(self.convert_btn, "primary")
        self.convert_btn.clicked.connect(self.perform_conversion)
        layout.addWidget(self.convert_btn)


    def populate_classes(self):
        """Populate with LEVEL and DESCRIPTION like ClassPicker"""
        print(f"\n🔄 Populating ByClassDialog...")
        
        self.from_list.clear()
        self.to_combo.clear()
        class_list = _collect_conversion_classes(
            self.app,
            standard_levels={17: "Other Poles"},
        )
        
        if not class_list:
            print("⚠️ No classes found")
            return
        
        # Add "Any class" option
        any_item = QListWidgetItem("Any class")
        any_item.setData(Qt.UserRole, None)
        try:
            from gui.theme_manager import ThemeColors
            from PySide6.QtGui import QColor as _QColor
            any_item.setBackground(_QColor(ThemeColors.get('bg_secondary')))
            any_item.setForeground(_QColor(ThemeColors.get('text_primary')))
        except Exception:
            any_item.setBackground(QColor(240, 240, 240))
        self.from_list.addItem(any_item)
        
        # Populate lists
        for cls in class_list:
            code = cls['code']
            lvl = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            
            icon = make_color_icon(color)
            
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, code)
            self.from_list.addItem(item)
            
            self.to_combo.addItem(icon, text, code)
        
        print(f"✅ Populated ByClassDialog with {len(class_list)} classes")
    
    def on_classes_changed(self):
        """Refresh class lists when Display Mode loads a different PTC."""
        self.refresh_class_lists(reason="PTC changed in Display Mode", update_status=True)
    
    def perform_conversion(self):
        """Perform the class conversion with visibility-aware refresh"""
        # Get selected From classes
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one 'From' class")
            return
        
        # Check if "Any class" is selected
        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
        
        if any_selected:
            from_classes = None  # Will convert ALL classes
        else:
            from_classes = [item.data(Qt.UserRole) for item in selected_items]
        
        # Get To class
        to_class = self.to_combo.currentData()
        if to_class is None:
            QMessageBox.warning(self, "No Target", "Please select a 'To' class")
            return
        
        # Confirm conversion
        if from_classes is None:
            msg = f"Convert ALL classes to class {to_class}?"
        elif len(from_classes) == 1:
            msg = f"Convert class {from_classes[0]} to class {to_class}?"
        else:
            msg = f"Convert classes {from_classes} to class {to_class}?"
        
        reply = QMessageBox.question(
            self, "Confirm Conversion", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Perform conversion
        try:
            classification = self.app.data.get("classification")
            if classification is None:
                QMessageBox.critical(self, "Error", "No classification data available")
                return
            
            # Create mask for points to convert
            if from_classes is None:
                mask = np.ones(len(classification), dtype=bool)
                converted_count = len(classification)
            else:
                mask = np.isin(classification, from_classes)
                converted_count = np.sum(mask)
            
            if converted_count == 0:
                QMessageBox.information(self, "No Points", "No points found to convert")
                return
            
            # Save undo
            old_classes = classification[mask].copy()
            new_classes = np.full(mask.sum(), to_class, dtype=classification.dtype)
            
            # ✅ FIX: Use BOTH key name variants for maximum compatibility
            undo_step = {
                "mask": mask.copy(),
                "oldclasses": old_classes,
                "old_classes": old_classes,
                "newclasses": new_classes,
                "new_classes": new_classes
            }
            
            # ✅ FIX: Correct stack attribute names (undostack, NOT undo_stack)
            if hasattr(self.app, 'undostack'):
                self.app.undostack.append(undo_step)
            elif hasattr(self.app, 'undo_stack'):
                self.app.undo_stack.append(undo_step)
            else:
                print("⚠️ No undo stack found on app!")
            
            if hasattr(self.app, 'redostack'):
                self.app.redostack.clear()
            elif hasattr(self.app, 'redo_stack'):
                self.app.redo_stack.clear()
            from gui.memory_manager import trim_undo_stack
            trim_undo_stack(self.app)

            # Apply conversion
            classification[mask] = to_class
            
            # ✅ Store conversion info for targeted refresh
            if from_classes:
                self._last_conversion_info = {
                    'from_classes': from_classes,
                    'to_class': to_class,
                    'count': converted_count
                }
            
            print(f"\n{'='*60}")
            print(f"✅ Converted {converted_count:,} points: {from_classes} → {to_class}")
            print(f"{'='*60}")

            display_mode = getattr(self.app, 'display_mode', 'class')
            print(f"   📍 Current display mode: {display_mode}")          
            
            if display_mode == "shaded_class":
                print(f"   🔺 Rebuilding SHADING mesh after classification...")
                
                # ✅ FIX: Save current shading visibility BEFORE any palette manipulation
                # This preserves single-class mode — don't let slot 0 override it!
                from gui.shading_display import get_cache, update_shaded_class, clear_shading_cache
                cache = get_cache()
                saved_shading_visibility = getattr(cache, 'visible_classes_set', None)
                
                if saved_shading_visibility is None:
                    # Fallback: use current class_palette (before any restore overwrites it)
                    saved_shading_visibility = {
                        int(c) for c, e in self.app.class_palette.items()
                        if e.get("show", True)
                    }
                
                saved_shading_visibility = saved_shading_visibility.copy()
                is_single = len(saved_shading_visibility) == 1
                print(f"   📍 Saved shading visibility: {sorted(saved_shading_visibility)} "
                      f"({'single-class' if is_single else 'multi-class'})")
                
                # ✅ DON'T call _restore_main_view_palette_for_refresh() — 
                #    it would override single-class visibility with slot 0 (all 26 classes)!
                
                # ✅ Force class_palette to match SAVED shading visibility (not slot 0!)
                for c in self.app.class_palette:
                    self.app.class_palette[c]["show"] = (int(c) in saved_shading_visibility)
                
                # Debug: show what will be visible after conversion
                visible_with_points = []
                for c, e in sorted(self.app.class_palette.items()):
                    if e.get("show", True):
                        pts = np.sum(classification == c)
                        if pts > 0:
                            visible_with_points.append(c)
                            print(f"      Class {c}: VISIBLE, {pts:,} points")
                
                if not visible_with_points:
                    print(f"   ⚠️ No visible classes have points after conversion")
                    print(f"      (All points moved to hidden class {to_class})")
                    print(f"      View will be empty — user can switch visibility")
                
                clear_shading_cache("class conversion changed visible point set")
                update_shaded_class(
                    self.app,
                    getattr(self.app, "last_shade_azimuth", 45.0),
                    getattr(self.app, "last_shade_angle", 45.0),
                    getattr(self.app, "shade_ambient", 0.2),
                    force_rebuild=True
                )
                print(f"   ✅ Shaded mesh rebuilt (visibility preserved: "
                      f"{'single' if is_single else 'multi'}-class)")
                
            elif display_mode == "class":
                # Standard class-colored point actors
                print(f"   🎨 Refreshing CLASS mode...")
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                print(f"   ✅ Class mode refreshed")
                
            else:
                # (depth) or corrupting the raw data array in-place (rgb/brush).
                # The cross-section views carry the classification result; the
                # main view stays in its current non-class display as-is.
                print(f"   ℹ️ {display_mode} mode — skipping main view repaint "
                      f"(classification stored; visible in section views)")

            # Refresh cross-sections if needed
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception as e:
                        print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

            # Refresh Cut Section view if active
            try:
                ctrl = getattr(self.app, 'cut_section_controller', None)
                if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                    if hasattr(ctrl, '_refresh_cut_colors_fast'):
                            ctrl._refresh_cut_colors_fast()
            except Exception as e:
                print(f"   ⚠️ Cut Section refresh failed: {e}")

            # Update point count widget
            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()

            
            # Update UI
            if self.ribbon_parent:
                self.ribbon_parent.update_status(
                    f"✅ Converted {converted_count:,} points to class {to_class}", 
                    "success"
                )
            
            self.info_label.setText(f"✅ Converted {converted_count:,} points")
            self.info_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
            
            QMessageBox.information(
                self, "Conversion Complete",
                f"Successfully converted {converted_count:,} points to class {to_class}"
            )
            
        except Exception as e:
            error_msg = f"Conversion failed: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()


    # ========================================
    # ✅ VISIBILITY-AWARE REFRESH METHODS
    # ========================================
    
    def _refresh_all_views(self):
        """
        ✅ FIXED: Handle both unified and by-class actor modes
        For by-class mode: rebuild affected actors
        For unified mode: update colors only
        COPIED FROM WORKING FENCE DIALOG - NO MORE BLANK SCREENS!
        """
        print(f"\n🔄 REFRESHING VIEWS (partial + visibility-aware)...")
        
        # ✅ CRITICAL: Save camera position BEFORE any updates
        saved_camera = None
        if hasattr(self.app, 'vtk_widget') and self.app.vtk_widget.renderer:
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                if camera:
                    saved_camera = {
                        'position': tuple(camera.GetPosition()),
                        'focal_point': tuple(camera.GetFocalPoint()),
                        'view_up': tuple(camera.GetViewUp()),
                        'parallel_scale': camera.GetParallelScale(),
                        'parallel_projection': camera.GetParallelProjection(),
                    }
                    print(f"   📷 Camera saved: pos={saved_camera['position'][:2]}, scale={saved_camera['parallel_scale']:.2f}")
            except Exception as e:
                print(f"   ⚠️ Camera save failed: {e}")
        
        # ✅ DETECT MODE: By-class actors or unified actor?
        has_by_class_actors = False
        if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'actors'):
            if isinstance(self.app.vtk_widget.actors, dict):
                for key in self.app.vtk_widget.actors.keys():
                    if 'class_' in str(key).lower():
                        has_by_class_actors = True
                        break
        
        update_success = False
        
        if has_by_class_actors:
            print(f"   🔍 Detected BY-CLASS actor mode - rebuilding affected actors...")
            
            # This requires calling the display mode's refresh method
            try:
                # Try to find and call the display mode's update method
                display_dialog = getattr(self.app, 'display_mode_dialog', 
                                        getattr(self.app, 'display_dialog', None))
                
                if display_dialog:
                    # Method 1: Try update_display_mode or similar
                    if hasattr(display_dialog, 'update_display_mode'):
                        display_dialog.update_display_mode()
                        print(f"   ✅ Called display_dialog.update_display_mode()")
                        update_success = True
                    
                    # Method 2: Try refresh_actors or rebuild_actors
                    elif hasattr(display_dialog, 'refresh_actors'):
                        display_dialog.refresh_actors()
                        print(f"   ✅ Called display_dialog.refresh_actors()")
                        update_success = True
                    
                    # Method 3: Try apply_class_filter or similar
                    elif hasattr(display_dialog, 'apply_class_filter'):
                        display_dialog.apply_class_filter()
                        print(f"   ✅ Called display_dialog.apply_class_filter()")
                        update_success = True
                    
                    # Method 4: Simulate clicking the apply/update button
                    elif hasattr(display_dialog, 'apply_btn'):
                        display_dialog.apply_btn.click()
                        print(f"   ✅ Triggered display_dialog.apply_btn.click()")
                        update_success = True
            
            except Exception as e:
                print(f"   ⚠️ Display mode refresh failed: {e}")
        
        # ✅ UNIFIED ACTOR MODE: Try smart update or direct VTK update
        if not update_success:
            print(f"   🔍 Using unified actor mode - updating colors...")
            
            # Try smart_update_colors
            try:
                from gui.pointcloud_display import smart_update_colors
                smart_update_colors(self.app, None)
                print(f"   ✅ Main view updated (smart_update_colors)")
                update_success = True
            except ImportError:
                print(f"   ⚠️ smart_update_colors not found - trying direct VTK update")
            except Exception as e:
                print(f"   ⚠️ smart_update_colors failed: {e}")
            
            # Try direct VTK color update
            if not update_success:
                update_success = self._force_vtk_color_update()
                if update_success:
                    print(f"   ✅ Main view updated (direct VTK with visibility)")
        
        # ✅ FALLBACK: Old method
        if not update_success:
            print(f"   ⚠️ All methods failed - using fallback (full refresh)")
            from gui.class_display import update_class_mode
            update_class_mode(self.app, force_refresh=True)
            print(f"   ✅ Main view refreshed (forced rebuild)")
            update_success = True
        
        # ✅ CRITICAL: Restore camera position IMMEDIATELY
        if saved_camera:
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                camera.SetPosition(saved_camera['position'])
                camera.SetFocalPoint(saved_camera['focal_point'])
                camera.SetViewUp(saved_camera['view_up'])
                camera.SetParallelScale(saved_camera['parallel_scale'])
                
                if saved_camera['parallel_projection']:
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
                
                self.app.vtk_widget.renderer.ResetCameraClippingRange()
                print(f"   📷✅ Camera restored")
            except Exception as e:
                print(f"   ⚠️ Camera restore failed: {e}")
        
        # Refresh cross-sections
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx in list(self.app.section_vtks.keys()):
                try:
                    if hasattr(self.app, '_refresh_single_section_view'):
                        self.app._refresh_single_section_view(view_idx)
                except Exception as e:
                    print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")
        
        # Update point statistics
        if hasattr(self.app, 'point_count_widget'):
            try:
                self.app.point_count_widget.schedule_update()
            except Exception:
                pass
        
        # ✅ Force final render
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        mode_str = "by-class" if has_by_class_actors else "unified"
        print(f"✅ REFRESH COMPLETE ({mode_str} mode, visibility-aware: {update_success})\n")


    def _force_vtk_color_update(self):
        """
        Direct VTK color update with visibility awareness
        Called when smart_update_colors is not available
        """
        try:
            if not hasattr(self.app, 'vtk_widget'):
                return False
            
            vtk_widget = self.app.vtk_widget
            
            # Get the actor (unified mode)
            actor = None
            if hasattr(vtk_widget, 'actor') and vtk_widget.actor:
                actor = vtk_widget.actor
            elif hasattr(vtk_widget, 'actors') and isinstance(vtk_widget.actors, dict):
                # Try to find a unified actor
                for key, act in vtk_widget.actors.items():
                    if 'unified' in str(key).lower() or key == 'main':
                        actor = act
                        break
            
            if not actor:
                print(f"      ⚠️ No unified actor found for direct VTK update")
                return False
            
            # Get mapper and update colors
            mapper = actor.GetMapper()
            if not mapper:
                return False
            
            # Get classification data
            classification = self.app.data.get("classification")
            if classification is None:
                return False
            
            # Get visibility mask if available
            visible_mask = None
            if hasattr(self.app, 'get_visible_points_mask'):
                visible_mask = self.app.get_visible_points_mask()
            
            # Update colors based on classification
            from vtk import vtkUnsignedCharArray
            colors = vtkUnsignedCharArray()
            colors.SetNumberOfComponents(3)
            colors.SetName("Colors")
            
            class_palette = getattr(self.app, 'class_palette', {})
            
            for i, cls in enumerate(classification):
                # Check visibility
                if visible_mask is not None and not visible_mask[i]:
                    # Make invisible points black or very dark
                    colors.InsertNextTuple3(20, 20, 20)
                else:
                    # Use class color
                    color_entry = class_palette.get(int(cls), {'color': (128, 128, 128)})
                    color = color_entry.get('color', (128, 128, 128))
                    colors.InsertNextTuple3(int(color[0]), int(color[1]), int(color[2]))
            
            # Update the polydata
            polydata = mapper.GetInput()
            if polydata:
                polydata.GetPointData().SetScalars(colors)
                polydata.Modified()
                mapper.Modified()
                actor.Modified()
            
            print(f"      ✅ Direct VTK color update complete")
            return True
            
        except Exception as e:
            print(f"      ❌ Direct VTK update failed: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def _save_camera_state(self):
        """Save camera state"""
        if not hasattr(self.app, 'vtk_widget'):
            return None
        if not hasattr(self.app.vtk_widget, 'renderer'):
            return None
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            if camera:
                return {
                    'position': tuple(camera.GetPosition()),
                    'focal_point': tuple(camera.GetFocalPoint()),
                    'view_up': tuple(camera.GetViewUp()),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection(),
                }
        except Exception:
            pass
        return None
    
    def _restore_camera_state(self, saved_camera):
        """Restore camera state"""
        if not saved_camera:
            return
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            camera.SetPosition(saved_camera['position'])
            camera.SetFocalPoint(saved_camera['focal_point'])
            camera.SetViewUp(saved_camera['view_up'])
            camera.SetParallelScale(saved_camera['parallel_scale'])
            
            if saved_camera['parallel_projection']:
                camera.ParallelProjectionOn()
            else:
                camera.ParallelProjectionOff()
            
            self.app.vtk_widget.renderer.ResetCameraClippingRange()
        except Exception:
            pass
    
    def _detect_by_class_mode(self):
        """Detect if using by-class actors"""
        if not hasattr(self.app, 'vtk_widget'):
            return False
        if not hasattr(self.app.vtk_widget, 'actors'):
            return False
        
        actors = self.app.vtk_widget.actors
        if isinstance(actors, dict):
            for key in actors.keys():
                if 'class_' in str(key).lower():
                    return True
        return False

    def naksha_dark_theme(self):
        return """
        QWidget {
            background-color: #0a0a0a; /* Deep black background like Pic 2 */
            color: #e0e0e0;
            font-family: "Segoe UI", sans-serif;
            font-size: 10pt;
        }

        /* Section Titles (Teal color from Pic 2) */
        QLabel#sectionLabel {
            color: #1abc9c;
            font-weight: bold;
            font-size: 11pt;
            margin-bottom: 2px;
        }

        /* Smaller italic text */
        QLabel#subText {
            color: #7f8c8d;
            font-size: 9pt;
            font-style: italic;
        }

        /* Gray status label */
        QLabel#statusLabel {
            color: #bdc3c7;
            background-color: #151515;
            padding: 10px;
            border-radius: 6px;
            font-size: 9pt;
        }

        /* Lists and Boxes */
        QListWidget, QComboBox {
            background-color: #151515;
            border: 1px solid #2a2a2a;
            border-radius: 6px;
            padding: 8px;
            color: #ffffff !important;
        }

        QListWidget::item {
            padding: 5px;
            border-radius: 4px;
        }

        QListWidget::item:selected {
            background-color: #1abc9c; /* Teal selection */
            color: #000000;
        }

        /* Main Teal Button */
        QPushButton#primaryButton {
            background-color: #1abc9c;
            color: #0a0a0a;
            font-size: 11pt;
            font-weight: bold;
            padding: 12px;
            border: none;
            border-radius: 8px;
        }

        QPushButton#primaryButton:hover {
            background-color: #16a085;
        }

        QPushButton#primaryButton:pressed {
            background-color: #12876f;
        }

        /* Scrollbar styling to match dark theme */
        QScrollBar:vertical {
            border: none;
            background: #0a0a0a;
            width: 10px;
            margin: 0px;
        }
        QScrollBar::handle:vertical {
            background: #333;
            min-height: 20px;
            border-radius: 5px;
        }
        """


def make_color_icon(rgb):
    """Create a color icon from RGB tuple"""
    pix = QPixmap(20, 12)
    pix.fill(QColor(*rgb))
    return QIcon(pix)

import numpy as np
from scipy.spatial import cKDTree
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
                               QListWidget, QComboBox, QPushButton, 
                               QDoubleSpinBox, QCheckBox, QMessageBox, QListWidgetItem)
from PySide6.QtCore import Qt, Signal, QEvent  # ✅ Added QEvent
from PySide6.QtGui import QPixmap, QColor, QIcon


class ClosedByClassDialog(QDialog):
    """
    Proximity-Based Class Conversion Dialog
    
    🔗 CLUSTERING MODE:
    Converts "From class" points that are near "To class" points
    
    Workflow:
    1. Load cross-section (points are automatically captured)
    2. Select "From class" (points to convert) - SUPPORTS MULTI-SELECT
    3. Select "To class" (reference points)
    4. Set radius (meters)
    5. Optional: Filter by specific class in radius
    6. Click Convert → From class points within radius of To class → become To class
    
    Example:
    - From: Ground (class 1) + Low vegetation (class 2)
    - To: Building (class 5)
    - Radius: 1m
    → Ground & Low veg points within 1m of Building points → become Building
    
    ✅ NEW: Supports undo/redo with Ctrl+Z/Ctrl+Y
    ✅ NEW: Visibility-aware refresh (respects Display Mode checkboxes)
    """
    
    selection_required = Signal()
   

    def __init__(self, app, ribbon_parent):
        # ✅ FIX 1: Robust Parent Finding
        from PySide6.QtWidgets import QWidget
        target_parent = None
        if isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, 'window') and isinstance(app.window, QWidget):
            target_parent = app.window

        # Keep dialog owned by the main app window so it stays above Naksha
        # while remaining non-modal and not globally always-on-top.
        # Parentless top-level window:
        # keeps normal taskbar minimize behavior (no floating mini-bar on desktop).
        super().__init__(None, Qt.Window)
        
        self.setAttribute(Qt.WA_NativeWindow, True)  # Fix: GetDC invalid window handle
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.setWindowModality(Qt.NonModal)
        self.app = app
        self.ribbon_parent = ribbon_parent
        self.manual_selection_indices = None
        self.selected_fences = []
        self.permanent_fence_mode = False
        self._conversion_completed = False
        self._fences_from_selection = False
        self._hover_highlight_actor = None
        self._selection_highlight_actors = []
        self._classified_fence_actors = []
        
        self.setWindowTitle("Closed By Class Conversion")
        # self.setStyleSheet(self.naksha_dark_theme()) # Inherits global theme
        try:
            from gui.theme_manager import get_dialog_stylesheet
            self.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass
        self.setGeometry(200, 200, 460, 640)
        
        # Shortcuts - same approach as ByClassDialog
       
        
        self.init_ui()
        self.populate_classes()
        self._connected_display_dialog = None
        self._connect_display_dialog()

    def _get_display_dialog(self):
        return getattr(self.app, 'display_mode_dialog', getattr(self.app, 'display_dialog', None))

    def _connect_display_dialog(self):
        display_dialog = self._get_display_dialog()
        if display_dialog is None:
            return False
        if display_dialog is self._connected_display_dialog:
            return False

        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass

        try:
            display_dialog.classes_loaded.connect(self.on_classes_changed)
        except Exception as e:
            print(f"⚠️ ClosedByClassDialog could not connect to Display Mode updates: {e}")
            return False

        self._connected_display_dialog = display_dialog
        print("✅ ClosedByClassDialog connected to display_mode_dialog.classes_loaded")
        return True
    
    def closeEvent(self, event):
        # Close any open sub-dialog
        if hasattr(self, '_fence_selection_dialog') and self._fence_selection_dialog is not None:
            try:
                self._fence_selection_dialog.close()
            except (RuntimeError, ReferenceError):
                pass
            self._fence_selection_dialog = None

        try:
            self._clear_fence_highlights()
        except Exception:
            pass
        self.selected_fences = []
        self.permanent_fence_mode = False
        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass
            self._connected_display_dialog = None
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._connect_display_dialog():
            self.on_classes_changed()
        if self.selected_fences and hasattr(self, '_restore_highlights_from_data'):
            try:
                self._restore_highlights_from_data()
            except Exception:
                pass

    def select_fence(self):
        return InsideFenceDialog.select_fence(self)

    def clear_fence_selection(self):
        return InsideFenceDialog.clear_fence_selection(self)

    def update_fence_display(self):
        return InsideFenceDialog.update_fence_display(self)

    def add_fences_from_selection(self, fence_shapes):
        return InsideFenceDialog.add_fences_from_selection(self, fence_shapes)

    def _highlight_selected_fences_in_3d(self, fence_list):
        return InsideFenceDialog._highlight_selected_fences_in_3d(self, fence_list)

    def _clear_fence_highlights(self):
        return InsideFenceDialog._clear_fence_highlights(self)

    def _highlight_classified_fences(self):
        return InsideFenceDialog._highlight_classified_fences(self)

    def _restore_highlights_from_data(self):
        return InsideFenceDialog._restore_highlights_from_data(self)

    def _points_inside_polygon(self, points, polygon_coords):
        return InsideFenceDialog._points_inside_polygon(self, points, polygon_coords)

    def on_height_filter_toggled(self, checked):
        return InsideFenceDialog.on_height_filter_toggled(self, checked)

    def on_height_mode_changed(self, index):
        return InsideFenceDialog.on_height_mode_changed(self, index)

    def auto_analyze_heights(self):
        return InsideFenceDialog.auto_analyze_heights(self)

    def perform_undo(self):
        """Perform CLASSIFICATION undo"""
        print("🔄 ClosedByClassDialog: Performing CLASSIFICATION undo...")
        
        if hasattr(self.app, 'undo_classification'):
            try:
                self.app.undo_classification()
                
                # ✅ CRITICAL: Respect current display mode (FIXED)
                display_mode = getattr(self.app, 'display_mode', 'class')
                print(f"   📍 Current display mode: {display_mode}")
                
                if display_mode == "shaded_class":
                    # Maintain shaded mesh visualization
                    print(f"   🔺 Maintaining SHADING mode...")
                    from gui.shading_display import update_shaded_class
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=False
                    )
                    print(f"   ✅ Shaded mesh maintained after undo")
                    
                elif display_mode == "class":
                    # Standard class-colored point actors
                    print(f"   🎨 Refreshing CLASS mode...")
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True)
                    print(f"   ✅ Class mode refreshed")
                    
                else:
                    # Other display modes
                    print(f"   🌈 Refreshing {display_mode.upper()} mode...")
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(self.app, display_mode)
                    print(f"   ✅ {display_mode} mode refreshed")

                # Refresh cross-sections if needed
                if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                    for view_idx in list(self.app.section_vtks.keys()):
                        try:
                            if hasattr(self.app, '_refresh_single_section_view'):
                                self.app._refresh_single_section_view(view_idx)
                        except Exception as e:
                            print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

                # Refresh Cut Section view if active
                try:
                    ctrl = getattr(self.app, 'cut_section_controller', None)
                    if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                        if hasattr(ctrl, '_refresh_cut_colors_fast'):
                                ctrl._refresh_cut_colors_fast()
                except Exception as e:
                    print(f"   ⚠️ Cut Section refresh failed: {e}")

                # Update point count widget
                if hasattr(self.app, 'point_count_widget'):
                    self.app.point_count_widget.schedule_update()

                
                self.preview_label.setText("↶ Undo performed")
                
                self.preview_label.setStyleSheet("""
                    QLabel {
                        color: #ff9800;
                        font-size: 10px;
                        font-weight: bold;
                        padding: 8px;
                        background-color: #3e2723;
                        border-radius: 3px;
                    }
                """)
                print("✅ Classification undo performed from ClosedByClassDialog")
            except Exception as e:
                print(f"❌ Undo failed: {e}")
                import traceback
                traceback.print_exc()
                QMessageBox.warning(self, "Undo Failed", f"Could not undo: {str(e)}")
        else:
            QMessageBox.warning(self, "Undo Not Available", 
                               "Classification undo functionality not found")
    
    def perform_redo(self):
        """Perform CLASSIFICATION redo"""
        print("🔄 ClosedByClassDialog: Performing CLASSIFICATION redo...")
        
        if hasattr(self.app, 'redo_classification'):
            try:
                self.app.redo_classification()
                
                # ✅ CRITICAL: Respect current display mode (FIXED)
                display_mode = getattr(self.app, 'display_mode', 'class')
                print(f"   📍 Current display mode: {display_mode}")
                
                if display_mode == "shaded_class":
                    # Maintain shaded mesh visualization
                    print(f"   🔺 Maintaining SHADING mode...")
                    from gui.shading_display import update_shaded_class
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=False
                    )
                    print(f"   ✅ Shaded mesh maintained after redo")
                    
                elif display_mode == "class":
                    # Standard class-colored point actors
                    print(f"   🎨 Refreshing CLASS mode...")
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True)
                    print(f"   ✅ Class mode refreshed")
                    
                else:
                    # Other display modes
                    print(f"   🌈 Refreshing {display_mode.upper()} mode...")
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(self.app, display_mode)
                    print(f"   ✅ {display_mode} mode refreshed")

                # Refresh cross-sections if needed
                if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                    for view_idx in list(self.app.section_vtks.keys()):
                        try:
                            if hasattr(self.app, '_refresh_single_section_view'):
                                self.app._refresh_single_section_view(view_idx)
                        except Exception as e:
                            print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

                # Refresh Cut Section view if active
                try:
                    ctrl = getattr(self.app, 'cut_section_controller', None)
                    if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                        if hasattr(ctrl, '_refresh_cut_colors_fast'):
                                ctrl._refresh_cut_colors_fast()
                except Exception as e:
                    print(f"   ⚠️ Cut Section refresh failed: {e}")

                # Update point count widget
                if hasattr(self.app, 'point_count_widget'):
                    self.app.point_count_widget.schedule_update()

                
                self.preview_label.setText("↷ Redo performed")
                self.preview_label.setStyleSheet("""
                    QLabel {
                        color: #ff9800;
                        font-size: 10px;
                        font-weight: bold;
                        padding: 8px;
                        background-color: #3e2723;
                        border-radius: 3px;
                    }
                """)
                print("✅ Classification redo performed from ClosedByClassDialog")
            except Exception as e:
                print(f"❌ Redo failed: {e}")
                import traceback
                traceback.print_exc()
                QMessageBox.warning(self, "Redo Failed", f"Could not redo: {str(e)}")
        else:
            QMessageBox.warning(self, "Redo Not Available", 
                               "Classification redo functionality not found")
    
    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        body = layout

        # Info Banner
        self.info_banner = QLabel("Convert source-class points when they fall near the target class.")
        self.info_banner.setStyleSheet("font-size: 9pt; color: gray;")
        body.addWidget(self.info_banner)

        # Fence Filter
        self.fence_count_badge = QLabel()  # kept for compatibility
        fence_title = QLabel("Fence Filter:")
        fence_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(fence_title)

        fence_btn_row = QHBoxLayout()
        self.select_fence_btn = QPushButton("Select Fence")
        _set_compact_button_role(self.select_fence_btn, "primary")
        self.select_fence_btn.clicked.connect(self.select_fence)
        fence_btn_row.addWidget(self.select_fence_btn, 3)

        self.clear_fence_btn = QPushButton("Clear")
        _set_compact_button_role(self.clear_fence_btn)
        self.clear_fence_btn.clicked.connect(self.clear_fence_selection)
        fence_btn_row.addWidget(self.clear_fence_btn, 1)
        body.addLayout(fence_btn_row)

        self.fence_status = QLabel()
        _set_dialog_status(self.fence_status, "No fence selected", "neutral")

        # Height Filter
        height_title = QLabel("Height Filter:")
        height_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(height_title)

        self.height_filter_enabled = QCheckBox("Enable height filter")
        self.height_filter_enabled.toggled.connect(self.on_height_filter_toggled)
        body.addWidget(self.height_filter_enabled)

        self.height_filter_container = QWidget()
        h_layout = QVBoxLayout(self.height_filter_container)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(4)

        self.height_mode_combo = QComboBox()
        self.height_mode_combo.addItem("Within Range", "within")
        self.height_mode_combo.addItem("Above Height", "above")
        self.height_mode_combo.addItem("Below Height", "below")
        self.height_mode_combo.currentIndexChanged.connect(self.on_height_mode_changed)
        h_layout.addWidget(self.height_mode_combo)

        min_row = QHBoxLayout()
        self.min_height_label = QLabel("Min:")
        self.min_height_spin = QDoubleSpinBox()
        self.min_height_spin.setRange(-1000, 10000)
        self.min_height_spin.setDecimals(2)
        self.min_height_spin.setSingleStep(1.0)
        self.min_height_spin.setValue(0.0)
        self.min_height_spin.setSuffix(" m")
        min_row.addWidget(self.min_height_label)
        min_row.addWidget(self.min_height_spin)
        h_layout.addLayout(min_row)

        max_row = QHBoxLayout()
        self.max_height_label = QLabel("Max:")
        self.max_height_spin = QDoubleSpinBox()
        self.max_height_spin.setRange(-1000, 10000)
        self.max_height_spin.setDecimals(2)
        self.max_height_spin.setSingleStep(1.0)
        self.max_height_spin.setValue(100.0)
        self.max_height_spin.setSuffix(" m")
        max_row.addWidget(self.max_height_label)
        max_row.addWidget(self.max_height_spin)
        h_layout.addLayout(max_row)

        self.height_stats_label = QLabel("Enable height filter and select classes to analyze.")
        self.height_stats_label.setObjectName("dialogCaption")
        h_layout.addWidget(self.height_stats_label)

        analyze_heights_btn = QPushButton("Auto-Analyze Heights")
        _set_compact_button_role(analyze_heights_btn)
        analyze_heights_btn.clicked.connect(self.auto_analyze_heights)
        h_layout.addWidget(analyze_heights_btn)

        self.height_filter_container.setVisible(False)
        body.addWidget(self.height_filter_container)

        # Select Classes
        classes_title = QLabel("Select Classes:")
        classes_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(classes_title)

        from_lbl = QLabel("From class (Ctrl+Click for multiple):")
        from_lbl.setObjectName("dialogCaption")
        body.addWidget(from_lbl)

        self.from_list = QListWidget()
        self.from_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.from_list.setMinimumHeight(90)
        self.from_list.setStyleSheet("QListWidget::item { border-bottom: none; padding: 4px; }")
        self.from_list.itemSelectionChanged.connect(self.on_from_selection_changed)
        body.addWidget(self.from_list)

        self.from_selection_label = QLabel("No classes selected")
        self.from_selection_label.setObjectName("dialogCaption")
        body.addWidget(self.from_selection_label)

        clear_selection_btn = QPushButton("Clear Selection")
        _set_compact_button_role(clear_selection_btn)
        clear_selection_btn.clicked.connect(self.clear_from_selection)
        body.addWidget(clear_selection_btn)

        to_lbl = QLabel("To class (reference points):")
        to_lbl.setObjectName("dialogCaption")
        body.addWidget(to_lbl)

        self.to_combo = QComboBox()
        self.to_combo.currentIndexChanged.connect(self.on_class_selection_changed)
        body.addWidget(self.to_combo)

        # Radius
        radius_title = QLabel("Radius:")
        radius_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(radius_title)

        analyze_btn = QPushButton("Analyze Distances")
        _set_compact_button_role(analyze_btn)
        analyze_btn.clicked.connect(self.analyze_distances)
        body.addWidget(analyze_btn)

        self.radius_combo = QComboBox()
        self.radius_combo.addItem("Select classes and click Analyze first", None)
        self.radius_combo.currentIndexChanged.connect(self.on_radius_combo_changed)
        body.addWidget(self.radius_combo)

        radius_row = QHBoxLayout()
        radius_row.addWidget(QLabel("Manual:"))
        self.radius_spin = QDoubleSpinBox()
        self.radius_spin.setRange(0.1, 100.0)
        self.radius_spin.setValue(1.0)
        self.radius_spin.setSuffix(" m")
        radius_row.addWidget(self.radius_spin)
        body.addLayout(radius_row)

        # Optional Filter
        filter_title = QLabel("Optional Filter:")
        filter_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(filter_title)

        self.filter_checkbox = QCheckBox("Only convert if a specific class exists in radius")
        self.filter_checkbox.toggled.connect(self.on_filter_toggled)
        body.addWidget(self.filter_checkbox)

        self.filter_combo = QComboBox()
        self.filter_combo.setEnabled(False)
        body.addWidget(self.filter_combo)

        self.distance_info_label = QLabel("")
        self.distance_info_label.setObjectName("dialogCaption")
        body.addWidget(self.distance_info_label)

        self.preview_label = QLabel("Set the classes and radius, then convert.")
        self.preview_label.setObjectName("dialogCaption")
        self.preview_label.setWordWrap(True)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addWidget(self.preview_label, 1)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setMinimumWidth(140)
        _set_compact_button_role(self.convert_btn, "primary")
        self.convert_btn.clicked.connect(self.perform_conversion)
        footer.addWidget(self.convert_btn)
        layout.addLayout(footer)


    def populate_classes(self):
        """Populate class lists from Display Mode"""
        print(f"\n🔄 Populating ClosedByClassDialog...")
        
        self.from_list.clear()
        self.to_combo.clear()
        self.filter_combo.clear()
        class_list = _collect_conversion_classes(self.app)
        
        if not class_list:
            print("⚠️ No classes found")
            return
        
        # ADD "Any class" option FIRST to From list
        any_item = QListWidgetItem("Any class (convert all points)")
        any_item.setData(Qt.UserRole, None)  # None = Any class
        try:
            from gui.theme_manager import ThemeColors
            from PySide6.QtGui import QColor as _QColor
            any_item.setBackground(_QColor(ThemeColors.get('bg_secondary')))
            any_item.setForeground(_QColor(ThemeColors.get('text_primary')))
        except Exception:
            any_item.setBackground(QColor(70, 130, 180))
            any_item.setForeground(QColor(255, 255, 255))
        self.from_list.addItem(any_item)
        
        # Populate all combos with actual classes
        for cls in class_list:
            code = cls['code']
            lvl = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            
            icon = make_color_icon(color)
            
            # Add to From list (multi-select)
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, code)
            self.from_list.addItem(item)
            
            # Add to To combo
            self.to_combo.addItem(icon, text, code)
            self.filter_combo.addItem(icon, text, code)
        
        print(f"✅ Populated ClosedByClassDialog with {len(class_list)} classes + 'Any class' option")
    
    def on_classes_changed(self):
        """Called when Display Mode loads new PTC file - preserves selections"""
        print("\n" + "="*60)
        print("🔄 CLOSED BY CLASS DIALOG: Detected PTC change from Display Mode")
        print("="*60)
        
        old_from_codes = self._get_selected_from_classes()
        old_to = self.to_combo.currentData()
        old_filter = self.filter_combo.currentData()
        old_min_height = self.min_height_spin.value() if hasattr(self, 'min_height_spin') else 0.0
        old_max_height = self.max_height_spin.value() if hasattr(self, 'max_height_spin') else 100.0
        has_fences = len(getattr(self, 'selected_fences', [])) > 0
        
        print(f"   📋 Saving selections:")
        print(f"      From: {old_from_codes}")
        print(f"      To: {old_to}")
        print(f"      Filter: {old_filter}")
        print(f"      Fence: {'Selected' if has_fences else 'None'}")
        
        # Rebuild lists with new class definitions
        self.populate_classes()
        
        # ✅ Restore previous selections
        restored_count = 0
        
        if old_from_codes is None:
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                if item.data(Qt.UserRole) is None:
                    item.setSelected(True)
                    restored_count += 1
                    print("      ✅ Restored From: Any Class")
                    break
        elif old_from_codes:
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                code = item.data(Qt.UserRole)
                if code in old_from_codes:
                    item.setSelected(True)
                    restored_count += 1
                    print(f"      ✅ Restored From: Class {code}")
        
        # Restore To combo
        if old_to is not None:
            idx = self.to_combo.findData(old_to)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)
                print(f"      ✅ Restored To: Class {old_to}")
                restored_count += 1
        
        # Restore Filter combo
        if old_filter is not None:
            idx = self.filter_combo.findData(old_filter)
            if idx >= 0:
                self.filter_combo.setCurrentIndex(idx)
                print(f"      ✅ Restored Filter: Class {old_filter}")
                restored_count += 1

        if hasattr(self, 'min_height_spin'):
            self.min_height_spin.setValue(old_min_height)
        if hasattr(self, 'max_height_spin'):
            self.max_height_spin.setValue(old_max_height)

        if has_fences:
            try:
                if hasattr(self.app, 'digitizer'):
                    self.app.digitizer.rebind_drawings()
                self._restore_highlights_from_data()
                self.update_fence_display()
                restored_count += 1
                print("      ✅ Fence highlights restored")
            except Exception as e:
                print(f"      ⚠️ Fence restore failed: {e}")
        
        print(f"✅ ClosedByClassDialog updated ({restored_count} selections restored)")
        print("="*60 + "\n")

        self.on_from_selection_changed()
        
        # Show user feedback
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "✅ Close By Class updated with new definitions",
                2000
            )
    
    def on_filter_toggled(self, checked):
        """Enable/disable filter class combo"""
        self.filter_combo.setEnabled(checked)
    
    def preview_conversion(self):
        """Preview which points would be converted"""
        from_classes = self._get_selected_from_classes()
        if from_classes == []:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return

        from_display = "All classes" if from_classes is None else ", ".join(str(c) for c in from_classes)
        
        to_class = self.to_combo.currentData()
        radius = self.radius_spin.value()
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select To class")
            return
        
        # Don't allow converting TO class to itself
        if from_classes and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", f"Cannot convert class {to_class} to itself")
            return
        
        # Calculate
        try:
            affected_count = self._calculate_conversion(
                from_classes, to_class, radius, preview=True
            )
            
            if affected_count is None:
                return

            scope_parts = [f"From: {from_display} within {radius}m of Class {to_class}"]
            if self.selected_fences:
                scope_parts.append(f"inside {len(self.selected_fences)} fence(s)")
            if self.height_filter_enabled.isChecked():
                mode = self.height_mode_combo.currentData()
                if mode == "within":
                    scope_parts.append(
                        f"Z {self.min_height_spin.value():.2f}m - {self.max_height_spin.value():.2f}m"
                    )
                elif mode == "above":
                    scope_parts.append(f"Z > {self.min_height_spin.value():.2f}m")
                elif mode == "below":
                    scope_parts.append(f"Z < {self.max_height_spin.value():.2f}m")

            self.preview_label.setText(
                f"📊 Preview: {affected_count:,} points would be converted\n"
                + " | ".join(scope_parts)
            )
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #2196f3;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1a237e;
                    border-radius: 3px;
                }
            """)
            
        except Exception as e:
            print(f"❌ Preview failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Preview Failed", str(e))
    
    def perform_conversion(self):
        """Perform the conversion"""
        
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            active_sections = [k for k, v in self.app.section_vtks.items() if v is not None]
            if active_sections:
                reply = QMessageBox.question(
                    self, 
                    "Active Cross-Section", 
                    "There are active cross-section views. Converting classes will affect "
                    "the entire dataset, not just the cross-section.\n\n"
                    "Close cross-sections first?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                    QMessageBox.Yes
                )
                
                if reply == QMessageBox.Cancel:
                    return
                elif reply == QMessageBox.Yes:
                    # Close all cross-sections
                    if hasattr(self.app, 'close_all_sections'):
                        self.app.close_all_sections()
        
        from_classes = self._get_selected_from_classes()
        if from_classes == []:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return

        from_display = "All classes" if from_classes is None else ", ".join(str(c) for c in from_classes)
        
        to_class = self.to_combo.currentData()
        radius = self.radius_spin.value()
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select To class")
            return
        
        # Don't allow converting TO class to itself
        if from_classes and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", f"Cannot convert class {to_class} to itself")
            return
        
        # Confirm
        total_points = len(self.app.data.get("classification", []))
        scope_lines = [f"Convert {from_display} → class {to_class}", f"Within {radius}m radius"]
        if self.selected_fences:
            scope_lines.append(f"Inside {len(self.selected_fences)} fence(s)")
        if self.height_filter_enabled.isChecked():
            mode = self.height_mode_combo.currentData()
            if mode == "within":
                scope_lines.append(
                    f"Height filter: {self.min_height_spin.value():.2f}m - {self.max_height_spin.value():.2f}m"
                )
            elif mode == "above":
                scope_lines.append(f"Height filter: above {self.min_height_spin.value():.2f}m")
            elif mode == "below":
                scope_lines.append(f"Height filter: below {self.max_height_spin.value():.2f}m")
        msg = (
            "\n".join(scope_lines)
            + "\n\n"
            f"Scope: {total_points:,} points in dataset"
        )
        
        reply = QMessageBox.question(
            self,
            "Confirm Conversion",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        )
        
        if reply != QMessageBox.Yes:
            return
        
        # Perform conversion
        try:
            converted_count = self._calculate_conversion(
                from_classes, to_class, radius, preview=False
            )
            
            if converted_count is None or converted_count == 0:
                QMessageBox.information(self, "No Points", "No points found to convert")
                return
            
            # Update UI
            self.preview_label.setText(f"✅ Converted {converted_count:,} points")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
            
            if self.ribbon_parent:
                self.ribbon_parent.update_status(
                    f"✅ Converted {converted_count:,} points",
                    "success"
                )
            
            QMessageBox.information(
                self,
                "Conversion Complete",
                f"✅ Successfully converted {converted_count:,} points"
            )
            try:
                self._clear_fence_highlights()
                self._highlight_classified_fences()
                if hasattr(self.app, 'digitizer') and self.app.digitizer:
                    try:
                        self.app.digitizer.rebind_drawings()
                        print("   🖊️ Drawing actors rebound to overlay after closed by class conversion")
                    except Exception as _rb_err:
                        print(f"   ⚠️ rebind_drawings failed: {_rb_err}")
                self.selected_fences = []
                self.permanent_fence_mode = False
                self.update_fence_display()
            except Exception:
                pass
            
        except Exception as e:
            error_msg = f"Conversion failed: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            

    def _calculate_conversion(self, from_classes, to_class, radius, preview=False):
        """
        Calculate/perform the conversion
        
        Args:
            from_classes: List of class codes to convert FROM, or None for "any class"
            to_class: Class code to convert TO
            radius: Distance radius in meters
            preview: If True, just count points; if False, perform conversion
        """
        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        
        if classification is None or xyz is None:
            print("❌ No data!")
            return None
        
        print(f"\n{'='*60}")
        print(f"🔍 CLOSED BY CLASS CONVERSION")
        print(f"{'='*60}")
        print(f"   From class: {from_classes if from_classes else 'ANY CLASS'}")
        print(f"   To class: {to_class}")
        print(f"   Radius: {radius}m")
        print(f"   Total points in dataset: {len(classification):,}")
        
        # Work with entire dataset
        section_mask = np.ones(len(classification), dtype=bool)

        if self.selected_fences:
            combined_inside_mask = np.zeros(len(classification), dtype=bool)
            for fence in self.selected_fences:
                fence_coords = fence.get('coords', [])
                if isinstance(fence_coords, list):
                    fence_coords = np.array(fence_coords)
                if fence_coords is None or len(fence_coords) == 0:
                    continue
                combined_inside_mask |= self._points_inside_polygon(xyz, fence_coords)

            section_mask &= combined_inside_mask
            section_count = int(np.sum(section_mask))
            print(f"   Fence filter active: {section_count:,} points inside {len(self.selected_fences)} fence(s)")

            if section_count == 0:
                print("⚠️ No points inside selected fence(s)")
                return 0
        
        # Get To class points (reference points)
        to_class_mask = (classification == to_class) & section_mask
        to_class_count = np.sum(to_class_mask)
        
        print(f"   To class points: {to_class_count:,}")
        
        if to_class_count == 0:
            print("⚠️ No To class points found")
            
            # MODE 2: Convert ALL From class points when To class doesn't exist
            if from_classes is None:
                # ANY CLASS - exclude the to_class itself
                from_class_mask = (classification != to_class) & section_mask
            else:
                # Specific classes
                from_class_mask = np.isin(classification, from_classes) & section_mask

            if self.height_filter_enabled.isChecked():
                z_values = xyz[:, 2]
                mode = self.height_mode_combo.currentData()
                if mode == "within":
                    height_mask = (z_values >= self.min_height_spin.value()) & (z_values <= self.max_height_spin.value())
                elif mode == "above":
                    height_mask = z_values >= self.min_height_spin.value()
                else:
                    height_mask = z_values <= self.max_height_spin.value()
                from_class_mask &= height_mask
            
            from_class_count = np.sum(from_class_mask)
            
            if from_class_count == 0:
                print("⚠️ No From class points to convert")
                return 0
            
            # Show warning
            from_display = "ALL classes" if from_classes is None else f"classes {from_classes}"
            msg = (
                f"⚠️ Class {to_class} doesn't exist in dataset yet.\n\n"
                f"This will convert ALL {from_class_count:,} points from {from_display} → class {to_class}\n\n"
                f"Do you want to proceed?"
            )
            
            reply = QMessageBox.question(
                self,
                "No Reference Points - Convert All?",
                msg,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            
            if reply != QMessageBox.Yes:
                return None
            
            if preview:
                return from_class_count
            
            # Perform conversion
            final_mask = from_class_mask.copy()
            old_classes = classification[final_mask].copy()
            classification[final_mask] = to_class
            self.app._just_did_conversion = True
            
            # Save undo
            undo_step = {
                "mask": final_mask.copy(),
                "old_classes": old_classes,
                "new_classes": np.full(np.sum(final_mask), to_class, dtype=classification.dtype)
            }
            # self.app.undo_stack.append(undo_step)
            # self.app.redo_stack.clear()


            if hasattr(self.app, 'undostack'):
                self.app.undostack.append(undo_step)
            elif hasattr(self.app, 'undo_stack'):
                self.app.undo_stack.append(undo_step)
            if hasattr(self.app, 'redostack'):
                self.app.redostack.clear()
            elif hasattr(self.app, 'redo_stack'):
                self.app.redo_stack.clear()
            from gui.memory_manager import trim_undo_stack
            trim_undo_stack(self.app)

            print(f"   ✅ Converted ALL {from_class_count:,} From class points to To class")

            self.app._conversion_just_happened = True
            # ✅ CRITICAL: Direct refresh - simple and always works
            from gui.class_display import update_class_mode
            update_class_mode(self.app, force_refresh=True)
            print(f"   ✅ Main view refreshed (forced rebuild)")

            # Refresh cross-sections if needed
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception as e:
                        print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

            # Update point count widget
            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()

            # Refresh Cut Section view if active
            try:
                ctrl = getattr(self.app, 'cut_section_controller', None)
                if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                    if hasattr(ctrl, '_refresh_cut_colors_fast'):
                        ctrl._refresh_cut_colors_fast()
            except Exception as e:
                print(f"   ⚠️ Cut Section refresh failed: {e}")

            return from_class_count
        
        # Get From class points (to convert)
        if from_classes is None:
            # ANY CLASS - convert all except the to_class itself
            from_class_mask = (classification != to_class) & section_mask
        else:
            # Specific classes
            from_class_mask = np.isin(classification, from_classes) & section_mask

        if self.height_filter_enabled.isChecked():
            z_values = xyz[:, 2]
            mode = self.height_mode_combo.currentData()
            if mode == "within":
                height_mask = (z_values >= self.min_height_spin.value()) & (z_values <= self.max_height_spin.value())
            elif mode == "above":
                height_mask = z_values >= self.min_height_spin.value()
            else:
                height_mask = z_values <= self.max_height_spin.value()
            from_class_mask &= height_mask
            print(
                f"   Height filter active ({mode}): "
                f"{np.sum(from_class_mask):,} candidate From points remain"
            )
        
        from_class_count = np.sum(from_class_mask)
        
        print(f"   From class points: {from_class_count:,}")
        
        if from_class_count == 0:
            print("⚠️ No From class points to convert")
            return 0
        
        # Build KDTree from To class points
        to_class_xyz = xyz[to_class_mask]
        tree = cKDTree(to_class_xyz)
        print(f"   ✅ KDTree built with {len(to_class_xyz):,} reference points")
        
        # Query From class points
        from_class_xyz = xyz[from_class_mask]
        distances, _ = tree.query(from_class_xyz, distance_upper_bound=radius)
        
        # Points within radius
        near_mask = distances < radius
        near_count = np.sum(near_mask)
        
        print(f"   From class points within {radius}m of To class: {near_count:,}")
        
        # Apply filter if enabled
        if self.filter_checkbox.isChecked():
            filter_class = self.filter_combo.currentData()
            if filter_class is not None:
                print(f"   🔍 Applying filter: class {filter_class} must exist near To class")
                
                filter_class_mask = (classification == filter_class) & section_mask
                filter_class_xyz = xyz[filter_class_mask]
                
                if len(filter_class_xyz) == 0:
                    print("   ⚠️ No filter class points found - no conversions will occur")
                    near_mask[:] = False
                    near_count = 0
                else:
                    filter_tree = cKDTree(filter_class_xyz)
                    to_distances, _ = filter_tree.query(to_class_xyz, distance_upper_bound=radius)
                    valid_to_mask = to_distances < radius
                    
                    print(f"   To class points with filter class nearby: {np.sum(valid_to_mask):,}")
                    
                    if np.sum(valid_to_mask) == 0:
                        print("   ⚠️ No To class points have filter class nearby")
                        near_mask[:] = False
                        near_count = 0
                    else:
                        valid_to_xyz = to_class_xyz[valid_to_mask]
                        filtered_tree = cKDTree(valid_to_xyz)
                        filtered_distances, _ = filtered_tree.query(from_class_xyz, distance_upper_bound=radius)
                        near_mask = filtered_distances < radius
                        near_count = np.sum(near_mask)
                    
                    print(f"   After filter: {near_count:,} From class points will be converted")
        
        if preview:
            print(f"{'='*60}\n")
            return near_count
        
        # Perform conversion
        if near_count == 0:
            print(f"{'='*60}\n")
            return 0
        
        # Build final mask for main dataset
        final_mask = np.zeros(len(classification), dtype=bool)
        from_indices = np.where(from_class_mask)[0]
        convert_indices = from_indices[near_mask]
        final_mask[convert_indices] = True
        
        # Save old classes
        old_classes = classification[final_mask].copy()
        
        # Convert
        classification[final_mask] = to_class
        self.app._just_did_conversion = True
        
        # Save undo
        undo_step = {
            "mask": final_mask.copy(),
            "old_classes": old_classes,
            "new_classes": np.full(np.sum(final_mask), to_class, dtype=classification.dtype)
        }
        if hasattr(self.app, 'undostack'):
            self.app.undostack.append(undo_step)
        elif hasattr(self.app, 'undo_stack'):
            self.app.undo_stack.append(undo_step)
        if hasattr(self.app, 'redostack'):
            self.app.redostack.clear()
        elif hasattr(self.app, 'redo_stack'):
            self.app.redo_stack.clear()
        from gui.memory_manager import trim_undo_stack
        trim_undo_stack(self.app)

        print(f"   ✅ Converted {near_count:,} points")

        print(f"{'='*60}\n")
        self.app._conversion_just_happened = True
        
        # ✅ CRITICAL: Respect current display mode (FIXED)
        display_mode = getattr(self.app, 'display_mode', 'class')
        print(f"   📍 Current display mode: {display_mode}")
        
        if display_mode == "shaded_class":
            # Maintain shaded mesh visualization
            print(f"   🔺 Maintaining SHADING mode...")
            from gui.shading_display import update_shaded_class
            update_shaded_class(
                self.app,
                getattr(self.app, "last_shade_azimuth", 45.0),
                getattr(self.app, "last_shade_angle", 45.0),
                getattr(self.app, "shade_ambient", 0.2),
                force_rebuild=False
            )
            print(f"   ✅ Shaded mesh maintained after classification")
            
        elif display_mode == "class":
            # Standard class-colored point actors
            print(f"   🎨 Refreshing CLASS mode...")
            from gui.class_display import update_class_mode
            update_class_mode(self.app, force_refresh=True)
            print(f"   ✅ Class mode refreshed")
            
        else:
            # Other display modes
            print(f"   🌈 Refreshing {display_mode.upper()} mode...")
            from gui.pointcloud_display import update_pointcloud
            update_pointcloud(self.app, display_mode)
            print(f"   ✅ {display_mode} mode refreshed")

        # Refresh cross-sections if needed
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx in list(self.app.section_vtks.keys()):
                try:
                    if hasattr(self.app, '_refresh_single_section_view'):
                        self.app._refresh_single_section_view(view_idx)
                except Exception as e:
                    print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

        # Refresh Cut Section view if active
        try:
            ctrl = getattr(self.app, 'cut_section_controller', None)
            if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                if hasattr(ctrl, '_refresh_cut_colors_fast'):
                        ctrl._refresh_cut_colors_fast()
        except Exception as e:
            print(f"   ⚠️ Cut Section refresh failed: {e}")

        # Update point count widget
        if hasattr(self.app, 'point_count_widget'):
            self.app.point_count_widget.schedule_update()

        
        return near_count
    
    def _refresh_all_views(self):
        """
        ✅ ULTIMATE FIXED: Combines fence dialog smart refresh + force rebuild fallback
        - First tries smart update (like fence dialog - prevents blank screens)
        - If that fails, forces full rebuild by clearing actors
        - Guaranteed to work in all scenarios!
        """
        print(f"\n🔄 REFRESHING VIEWS (smart + visibility-aware)...")
        
        # ✅ CRITICAL: Save camera position BEFORE any updates
        saved_camera = None
        if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'renderer'):
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                if camera:
                    saved_camera = {
                        'position': tuple(camera.GetPosition()),
                        'focal_point': tuple(camera.GetFocalPoint()),
                        'view_up': tuple(camera.GetViewUp()),
                        'parallel_scale': camera.GetParallelScale(),
                        'parallel_projection': camera.GetParallelProjection(),
                    }
                    print(f"   📷 Camera saved: pos={saved_camera['position'][:2]}, scale={saved_camera['parallel_scale']:.2f}")
            except Exception as e:
                print(f"   ⚠️ Camera save failed: {e}")
        
        # ✅ DETECT MODE: By-class actors or unified actor?
        has_by_class_actors = False
        if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'actors'):
            if isinstance(self.app.vtk_widget.actors, dict):
                for key in self.app.vtk_widget.actors.keys():
                    if 'class_' in str(key).lower():
                        has_by_class_actors = True
                        break
        
        update_success = False
        
        # ========================================
        # METHOD 1: SMART UPDATE (FROM FENCE DIALOG)
        # ========================================
        if has_by_class_actors:
            print(f"   🔍 Detected BY-CLASS actor mode - rebuilding affected actors...")
            
            try:
                display_dialog = getattr(self.app, 'display_mode_dialog', 
                                        getattr(self.app, 'display_dialog', None))
                
                if display_dialog:
                    # Try various display dialog methods
                    if hasattr(display_dialog, 'update_display_mode'):
                        display_dialog.update_display_mode()
                        print(f"   ✅ Called display_dialog.update_display_mode()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'refresh_actors'):
                        display_dialog.refresh_actors()
                        print(f"   ✅ Called display_dialog.refresh_actors()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'apply_class_filter'):
                        display_dialog.apply_class_filter()
                        print(f"   ✅ Called display_dialog.apply_class_filter()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'apply_btn'):
                        display_dialog.apply_btn.click()
                        print(f"   ✅ Triggered display_dialog.apply_btn.click()")
                        update_success = True
            
            except Exception as e:
                print(f"   ⚠️ Display mode refresh failed: {e}")
        
        # ✅ UNIFIED ACTOR MODE: Try smart update
        if not update_success:
            print(f"   🔍 Using unified actor mode - trying smart update...")
            
            # Try smart_update_colors
            try:
                from gui.pointcloud_display import smart_update_colors
                smart_update_colors(self.app, None)
                print(f"   ✅ Main view updated (smart_update_colors)")
                update_success = True
            except ImportError:
                print(f"   ⚠️ smart_update_colors not found")
            except Exception as e:
                print(f"   ⚠️ smart_update_colors failed: {e}")
            
            # Try direct VTK color update
            if not update_success:
                try:
                    if self._force_vtk_color_update():
                        print(f"   ✅ Main view updated (direct VTK with visibility)")
                        update_success = True
                except Exception:
                    pass
        
        # ========================================
        # METHOD 2: FORCE REBUILD (FALLBACK)
        # ========================================
        if not update_success:
            print(f"   ⚠️ Smart update failed - forcing full rebuild...")
            
            if hasattr(self.app, 'vtk_widget'):
                # Remove all actors to force rebuild
                if hasattr(self.app.vtk_widget, 'actor') and self.app.vtk_widget.actor:
                    old_actor = self.app.vtk_widget.actor
                    self.app.vtk_widget.actor = None
                    if hasattr(self.app.vtk_widget, 'renderer'):
                        self.app.vtk_widget.renderer.RemoveActor(old_actor)
                    print(f"      ✅ Removed unified actor")
                
                # Clear actors dict
                if hasattr(self.app.vtk_widget, 'actors'):
                    old_actors = self.app.vtk_widget.actors
                    if isinstance(old_actors, dict):
                        for actor in old_actors.values():
                            if actor and hasattr(self.app.vtk_widget, 'renderer'):
                                self.app.vtk_widget.renderer.RemoveActor(actor)
                        old_actors.clear()
                        print(f"      ✅ Cleared {len(old_actors)} actors")
            
            # Call update_class_mode - will detect missing actors and do full rebuild
            try:
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                print(f"   ✅ Main view refreshed (forced rebuild)")
                update_success = True
            except Exception as e:
                print(f"      ❌ Full rebuild failed: {e}")
        
        # ========================================
        # RESTORE & FINALIZE
        # ========================================
        
        # ✅ CRITICAL: Restore camera position IMMEDIATELY
        if saved_camera:
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                camera.SetPosition(saved_camera['position'])
                camera.SetFocalPoint(saved_camera['focal_point'])
                camera.SetViewUp(saved_camera['view_up'])
                camera.SetParallelScale(saved_camera['parallel_scale'])
                
                if saved_camera['parallel_projection']:
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
                
                self.app.vtk_widget.renderer.ResetCameraClippingRange()
                print(f"   📷✅ Camera restored")
            except Exception as e:
                print(f"   ⚠️ Camera restore failed: {e}")
        
        # Refresh cross-sections
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx in list(self.app.section_vtks.keys()):
                try:
                    if hasattr(self.app, '_refresh_single_section_view'):
                        self.app._refresh_single_section_view(view_idx)
                except Exception as e:
                    print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")
        
        # Update point statistics
        if hasattr(self.app, 'point_count_widget'):
            try:
                self.app.point_count_widget.schedule_update()
            except Exception:
                pass
        
        # ✅ Force final render
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        mode_str = "by-class" if has_by_class_actors else "unified"
        print(f"✅ REFRESH COMPLETE ({mode_str} mode, success: {update_success})\n")


    def _force_vtk_color_update(self):
        """
        Direct VTK color update with visibility awareness
        Called when smart_update_colors is not available
        """
        try:
            if not hasattr(self.app, 'vtk_widget'):
                return False
            
            vtk_widget = self.app.vtk_widget
            
            # Get the actor (unified mode)
            actor = None
            if hasattr(vtk_widget, 'actor') and vtk_widget.actor:
                actor = vtk_widget.actor
            elif hasattr(vtk_widget, 'actors') and isinstance(vtk_widget.actors, dict):
                # Try to find a unified actor
                for key, act in vtk_widget.actors.items():
                    if 'unified' in str(key).lower() or key == 'main':
                        actor = act
                        break
            
            if not actor:
                return False
            
            # Get mapper and update colors
            mapper = actor.GetMapper()
            if not mapper:
                return False
            
            # Get classification data
            classification = self.app.data.get("classification")
            if classification is None:
                return False
            
            # Get visibility mask if available
            visible_mask = None
            if hasattr(self.app, 'get_visible_points_mask'):
                visible_mask = self.app.get_visible_points_mask()
            
            # Update colors based on classification
            from vtk import vtkUnsignedCharArray
            colors = vtkUnsignedCharArray()
            colors.SetNumberOfComponents(3)
            colors.SetName("Colors")
            
            class_palette = getattr(self.app, 'class_palette', {})
            
            for i, cls in enumerate(classification):
                # Check visibility
                if visible_mask is not None and not visible_mask[i]:
                    # Make invisible points black or very dark
                    colors.InsertNextTuple3(20, 20, 20)
                else:
                    # Use class color
                    color_entry = class_palette.get(int(cls), {'color': (128, 128, 128)})
                    color = color_entry.get('color', (128, 128, 128))
                    colors.InsertNextTuple3(int(color[0]), int(color[1]), int(color[2]))
            
            # Update the polydata
            polydata = mapper.GetInput()
            if polydata:
                polydata.GetPointData().SetScalars(colors)
                polydata.Modified()
                mapper.Modified()
                actor.Modified()
            
            return True
            
        except Exception as e:
            print(f"      ❌ Direct VTK update failed: {e}")
            return False

        
    def on_from_selection_changed(self):
        """Called when From class selection changes - update display label"""
        selected_items = self.from_list.selectedItems()

        if not selected_items:
            _set_dialog_status(self.from_selection_label, "No classes selected", "danger")
            if hasattr(self, 'selected_classes_label'):
                _set_dialog_status(self.selected_classes_label, "Selected: None", "neutral")
        else:
            any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)

            if any_selected:
                from_text = "Selected: Any class (all eligible points)"
                if hasattr(self, 'selected_classes_label'):
                    _set_dialog_status(self.selected_classes_label, "Selected From: Any Class", "success")
            else:
                codes = [str(item.data(Qt.UserRole)) for item in selected_items]
                from_text = (
                    f"Selected: Classes {', '.join(codes)} "
                    f"({len(codes)} class{'es' if len(codes) > 1 else ''})"
                )
                if hasattr(self, 'selected_classes_label'):
                    _set_dialog_status(self.selected_classes_label, f"Selected From: {', '.join(codes)}", "success")

            _set_dialog_status(self.from_selection_label, from_text, "success")

        self.on_class_selection_changed()

    def clear_from_selection(self):
        self.from_list.clearSelection()
        self.on_from_selection_changed()

    def _get_selected_from_classes(self):
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            return []
        codes = [item.data(Qt.UserRole) for item in selected_items]
        if any(code is None for code in codes):
            return None
        return codes

    def on_class_selection_changed(self):
        """Called when From or To class selection changes"""
        # Reset radius suggestions
        self.radius_combo.clear()
        self.radius_combo.addItem("Click 'Analyze Distances' to see options", None)
        self.distance_info_label.setText(
            "Classes changed. Click 'Analyze Distances' to recalculate."
        )
        self.distance_info_label.setStyleSheet("")

    def on_radius_combo_changed(self, index):
        """Update spin box when dropdown selection changes"""
        radius = self.radius_combo.currentData()
        if radius is not None:
            self.radius_spin.setValue(radius)
    
    def analyze_distances(self):
        """Analyze actual distances between From and To class points"""
        # Get selected From classes (multi-select)
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return
        
        # Check if "Any class" is selected
        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
        
        if any_selected:
            from_classes = None  # Analyze ALL classes
            from_display = "All classes"
        else:
            from_classes = [item.data(Qt.UserRole) for item in selected_items]
            from_display = ", ".join(str(c) for c in from_classes)
        
        to_class = self.to_combo.currentData()
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select To class")
            return
        
        # Don't allow analyzing TO class to itself
        if from_classes and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", f"Cannot analyze class {to_class} to itself")
            return
        
        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        
        if classification is None or xyz is None:
            return
        
        print(f"\n{'='*60}")
        print(f"📊 ANALYZING POINT-TO-POINT DISTANCES")
        print(f"{'='*60}")
        
        # Work with entire dataset
        section_mask = np.ones(len(classification), dtype=bool)
        
        # Get To class points (reference points)
        to_class_mask = (classification == to_class) & section_mask
        to_class_count = np.sum(to_class_mask)
        
        print(f"   To class {to_class} points: {to_class_count:,}")
        
        if to_class_count == 0:
            QMessageBox.warning(
                self,
                "No Reference Points",
                f"No points of class {to_class} found in dataset."
            )
            return
        
        # Get From class points
        if from_classes is None:
            # ANY CLASS - analyze all except the to_class itself
            from_class_mask = (classification != to_class) & section_mask
        else:
            # Specific classes
            from_class_mask = np.isin(classification, from_classes) & section_mask
        
        from_class_count = np.sum(from_class_mask)
        
        print(f"   From class ({from_display}) points: {from_class_count:,}")
        
        if from_class_count == 0:
            QMessageBox.warning(
                self,
                "No Source Points",
                f"No points from {from_display} found in dataset."
            )
            return
        
        # Build KDTree from To class points
        to_class_xyz = xyz[to_class_mask]
        tree = cKDTree(to_class_xyz)
        
        # Query From class points - find nearest To class point for each
        from_class_xyz = xyz[from_class_mask]
        distances, _ = tree.query(from_class_xyz, k=1)
        
        # Calculate statistics
        min_dist = np.min(distances)
        max_dist = np.max(distances)
        mean_dist = np.mean(distances)
        median_dist = np.median(distances)
        
        # Count points at different percentile thresholds
        percentiles = [50, 75, 90, 95, 99, 100]
        radius_options = []
        
        for p in percentiles:
            threshold = np.percentile(distances, p)
            count = np.sum(distances <= threshold)
            percentage = (count / len(distances)) * 100
            radius_options.append({
                'percentile': p,
                'radius': threshold,
                'count': count,
                'percentage': percentage
            })
        
        print(f"\n   Distance Statistics:")
        print(f"   Min: {min_dist:.3f}m")
        print(f"   Max: {max_dist:.3f}m")
        print(f"   Mean: {mean_dist:.3f}m")
        print(f"   Median: {median_dist:.3f}m")
        print(f"\n   Point Coverage at Different Radii:")
        for opt in radius_options:
            print(f"   {opt['radius']:.3f}m → {opt['count']:,} points ({opt['percentage']:.1f}%)")
        print(f"{'='*60}\n")
        
        # Populate the dropdown with results
        self.radius_combo.clear()
        
        for opt in radius_options:
            label = f"{opt['radius']:.3f}m — {opt['percentile']}% coverage ({opt['count']:,} pts / {opt['percentage']:.1f}%)"
            self.radius_combo.addItem(label, opt['radius'])
        
        # Add separator
        self.radius_combo.insertSeparator(self.radius_combo.count())
        
        # Add some common manual values
        common_radii = [0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]
        for r in common_radii:
            if r <= max_dist:
                count = np.sum(distances <= r)
                percentage = (count / len(distances)) * 100
                label = f"{r:.1f}m (manual) — {count:,} pts ({percentage:.1f}%)"
                self.radius_combo.addItem(label, r)
        
        # Select the 90% coverage option by default (usually index 2)
        self.radius_combo.setCurrentIndex(2)
        
        # Update info label
        self.distance_info_label.setText(
            f"Analyzed {from_class_count:,} From points -> {to_class_count:,} To points | "
            f"Min: {min_dist:.3f}m | Max: {max_dist:.3f}m | Avg: {mean_dist:.3f}m"
        )
        self.distance_info_label.setStyleSheet("color: #4caf50; font-size: 9px; font-weight: bold;")
        
        # Show detailed message box
        msg = (
            f"📊 Point-to-Point Distance Analysis\n\n"
            f"From: {from_display}\n"
            f"To: Class {to_class}\n"
            f"Total points to convert: {from_class_count:,}\n"
            f"Reference points: {to_class_count:,}\n\n"
            f"Distance Statistics:\n"
            f"• Minimum: {min_dist:.3f}m (closest point)\n"
            f"• Maximum: {max_dist:.3f}m (farthest point)\n"
            f"• Average: {mean_dist:.3f}m\n"
            f"• Median: {median_dist:.3f}m\n\n"
            f"Select a radius from the dropdown to see\n"
            f"how many points will be converted!"
        )
        
        QMessageBox.information(self, "Distance Analysis Complete", msg)
    

    
    def naksha_dark_theme(self):
        """Return 'Obsidian & Teal' theme stylesheet to match Brush Settings"""
        return """
            QDialog {
                background-color: #0a0a0a;
                color: #eeeeee;
            }
            QLabel {
                color: #eeeeee;
            }
            /* Style for the Group Labels/Headers */
            QLabel#header_label {
                color: #00c8aa;
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            QListWidget {
                background-color: #121212;
                color: #ffffff;
                border: 1px solid #222222;
                border-radius: 5px;
                padding: 5px;
                font-size: 11px;
            }
            QListWidget::item {
                padding: 6px;
                border-bottom: 1px solid #1a1a1a;
            }
            QListWidget::item:selected {
                background-color: #00c8aa;
                color: #000000;
                border-radius: 3px;
            }
            QComboBox, QDoubleSpinBox {
                background-color: #1a1a1a;
                color: #ffffff;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 6px;
            }
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1a1a;
                color: #ffffff;
                selection-background-color: #00c8aa;
                selection-color: #000000;
                border: 1px solid #00c8aa;
            }
            QPushButton {
                background-color: #222222;
                color: #ffffff;
                border: 1px solid #333333;
                padding: 8px;
                border-radius: 4px;
            }
            QPushButton#primary_btn {
                background-color: #00c8aa;
                color: #000000;
                font-weight: bold;
                border: none;
            }
            QPushButton#primary_btn:hover {
                background-color: #00e6c3;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            QCheckBox {
                color: #aaaaaa;
            }
        """

    def make_color_icon(color):
        """Helper function to create color icon for list items"""
        from PyQt5.QtGui import QPixmap, QIcon
        from PyQt5.QtCore import Qt
        
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        
        from PyQt5.QtGui import QPainter, QColor
        painter = QPainter(pixmap)
        painter.setBrush(QColor(*color))
        painter.setPen(QColor(80, 80, 80))
        painter.drawRect(0, 0, 15, 15)
        painter.end()
        return QIcon(pixmap)

class ByClassHeightDialog(QDialog):
    """Height-Based Class Conversion Dialog"""
    
    def __init__(self, app, ribbon_parent):
        # ✅ FIX 1: Robust Parent Finding
        from PySide6.QtWidgets import QWidget
        target_parent = None
        if isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, 'window') and isinstance(app.window, QWidget):
            target_parent = app.window

        # Keep dialog owned by the main app window so it stays above Naksha
        # while remaining non-modal and not globally always-on-top.
        # Parentless top-level window:
        # keeps normal taskbar minimize behavior (no floating mini-bar on desktop).
        super().__init__(None, Qt.Window)
        
        self.setAttribute(Qt.WA_NativeWindow, True)  # Fix: GetDC invalid window handle
        self.setAttribute(Qt.WA_QuitOnClose, False)

        self.setWindowModality(Qt.NonModal)
        self.app = app
        self.ribbon_parent = ribbon_parent
        
        self.setWindowTitle("By Class Height - Height-Based Classification")
        # self.setStyleSheet(self.naksha_dark_theme()) # Inherits global theme
        try:
            from gui.theme_manager import get_dialog_stylesheet
            self.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass
        self.setGeometry(200, 200, 460, 580)
        
        self.setFocusPolicy(Qt.StrongFocus)
        
        # Shortcuts...
        
        self._last_conversion_info = None
        self.selected_fences = []
        self.permanent_fence_mode = False
        self._selection_highlight_actors = []
        self._hover_highlight_actor = None
        self._classified_fence_actors = []
        self._fence_selection_dialog = None
        self._conversion_completed = False
        self.init_ui()
        self.populate_classes()
        self._connected_display_dialog = None
        self._connect_display_dialog()

    def _get_display_dialog(self):
        return getattr(self.app, 'display_mode_dialog', getattr(self.app, 'display_dialog', None))

    def _connect_display_dialog(self):
        display_dialog = self._get_display_dialog()
        if display_dialog is None:
            return False
        if display_dialog is self._connected_display_dialog:
            return False

        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass

        try:
            display_dialog.classes_loaded.connect(self.on_classes_changed)
        except Exception as e:
            print(f"⚠️ ByClassHeightDialog could not connect to Display Mode updates: {e}")
            return False

        self._connected_display_dialog = display_dialog
        print("✅ ByClassHeightDialog connected to display_mode_dialog.classes_loaded")
        return True

    def closeEvent(self, event):
        # Disconnect display dialog signal
        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass
            self._connected_display_dialog = None
        # Close any open sub-dialog
        if hasattr(self, '_fence_selection_dialog') and self._fence_selection_dialog is not None:
            try:
                self._fence_selection_dialog.close()
            except (RuntimeError, ReferenceError):
                pass
            self._fence_selection_dialog = None
        # Clean up VTK actors
        try:
            self._clear_fence_highlights(remove_classified=False)
        except Exception:
            pass
        self.selected_fences = []
        self.permanent_fence_mode = False
        super().closeEvent(event)


    def showEvent(self, event):
        """Ensure dialog gets focus when shown"""
        super().showEvent(event)
        self.setFocus()
        self.activateWindow()
        if self._connect_display_dialog():
            self.on_classes_changed()
        if hasattr(self, 'selected_fences') and self.selected_fences:
            self._restore_highlights_from_data()
        print("🔵 ByClassHeightDialog activated and focused")
    
    def focusInEvent(self, event):
        """Called when dialog gains focus"""
        super().focusInEvent(event)
        print("🔵 ByClassHeightDialog gained focus - undo/redo active")
    
    def focusOutEvent(self, event):
        """Called when dialog loses focus"""
        super().focusOutEvent(event)
        print("⚪ ByClassHeightDialog lost focus")

    def perform_undo(self):
        """
        Perform CLASSIFICATION undo operation
        ✅ FIXED: Display-mode-aware — undo_classification handles its own refresh
        """
        if not hasattr(self.app, 'undo_classification'):
            QMessageBox.warning(self, "Undo Not Available", "Classification undo not found")
            return
        try:
            print("🔄 ByClassHeightDialog: Performing CLASSIFICATION undo...")
            
            # ✅ undo_classification handles ALL refresh internally
            self.app.undo_classification()
            
            # ✅ FIX: Only do supplementary refresh for CLASS mode
            # undo_classification already handles shading rebuild
            display_mode = getattr(self.app, 'display_mode', 'class')
            
            if display_mode == 'shaded_class':
                print(f"   🌓 Shading mode — undo refresh already handled")
                # Do NOT call _refresh_after_classification — would double-refresh
                
            elif display_mode == 'class':
                # Safety net for class mode
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                print(f"   ✅ Main view refreshed (class mode)")
            else:
                print(f"   📊 {display_mode} mode — undo refresh already handled")
            
            # Update point count widget
            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()
            
            self.preview_label.setText("↶ Undo performed")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #ff9800;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #3e2723;
                    border-radius: 3px;
                }
            """)
            print("✅ Classification undo performed")
        except Exception as e:
            print(f"❌ Undo failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Undo Failed", f"Could not undo: {str(e)}")
    
    def perform_redo(self):
        """
        Perform CLASSIFICATION redo operation
        ✅ FIXED: Display-mode-aware — handles shading rebuild properly
        """
        if not hasattr(self.app, 'redo_classification'):
            QMessageBox.warning(self, "Redo Not Available", 
                            "Classification redo functionality not found")
            return
        
        print("🔄 ByClassHeightDialog: Performing CLASSIFICATION redo...")
        
        try:
            self.app.redo_classification()
            
            # ✅ FIX: Display-mode-aware supplementary refresh
            display_mode = getattr(self.app, 'display_mode', 'class')
            
            if display_mode == 'shaded_class':
                print(f"   🌓 Shading mode — redo needs explicit rebuild")
                try:
                    from gui.shading_display import get_cache, update_shaded_class, clear_shading_cache
                    cache = get_cache()
                    
                    # Save visibility from cache or app store
                    saved_vis = getattr(cache, 'visible_classes_set', None)
                    if saved_vis is None or len(saved_vis) == 0:
                        saved_vis = getattr(self.app, '_shading_visible_classes', None)
                    if saved_vis is None or len(saved_vis) == 0:
                        saved_vis = {
                            int(c) for c, e in self.app.class_palette.items()
                            if e.get("show", True)
                        }
                    saved_vis = saved_vis.copy()
                    
                    print(f"   📍 Preserved visibility: {sorted(saved_vis)}")
                    
                    # Force palette to match
                    for c in self.app.class_palette:
                        self.app.class_palette[c]["show"] = (int(c) in saved_vis)
                    
                    clear_shading_cache("redo classification in shading mode")
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=True
                    )
                    print(f"   ✅ Shading rebuilt after redo "
                          f"({'single' if len(saved_vis) == 1 else 'multi'}-class)")
                except Exception as e:
                    print(f"   ⚠️ Shading rebuild failed: {e}")
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True)
                    
            elif display_mode == 'class':
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                print(f"   ✅ Main view refreshed (class mode)")
            else:
                print(f"   📊 {display_mode} mode — redo refresh already handled")

            # Refresh cross-sections if needed
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception as e:
                        print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

            # Update point count widget
            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()

            
            self.preview_label.setText("↷ Redo performed")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #ff9800;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #3e2723;
                    border-radius: 3px;
                }
            """)
            print("✅ Classification redo performed")
        except Exception as e:
            print(f"❌ Redo failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.warning(self, "Redo Failed", f"Could not redo: {str(e)}")
    
    def naksha_dark_theme(self):
        """Return 'Obsidian & Teal' theme stylesheet"""
        return """
            QDialog {
                background-color: #0a0a0a;
                color: #eeeeee;
            }
            QLabel {
                color: #eeeeee;
            }
            /* Teal Section Headers */
            QLabel#header_label {
                color: #00c8aa;
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 1px;
            }
            QListWidget {
                background-color: #121212;
                color: #ffffff;
                border: 1px solid #222222;
                border-radius: 5px;
                padding: 5px;
            }
            QListWidget::item:selected {
                background-color: #00c8aa;
                color: #000000;
                border-radius: 3px;
            }
            QComboBox, QDoubleSpinBox {
                background-color: #1a1a1a;
                color: #ffffff;
                border: 1px solid #333333;
                border-radius: 4px;
                padding: 6px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1a1a;
                color: #ffffff;
                selection-background-color: #00c8aa;
                selection-color: #000000;
            }
            QPushButton {
                background-color: #222222;
                color: #ffffff;
                border: 1px solid #333333;
                padding: 8px;
                border-radius: 4px;
            }
            /* Solid Teal Action Button */
            QPushButton#primary_btn {
                background-color: #00c8aa;
                color: #000000;
                font-weight: bold;
                border: none;
            }
            QPushButton#primary_btn:hover {
                background-color: #00e6c3;
            }
        """


    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        body = layout

        self.info_banner = QLabel("Convert points by height relative to the selected reference class.")
        self.info_banner.setStyleSheet("font-size: 9pt; color: gray;")
        body.addWidget(self.info_banner)

        # Fence Filter
        self.fence_count_badge = QLabel() # kept for compatibility
        fence_title = QLabel("Fence Filter:")
        fence_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(fence_title)

        fence_btn_row = QHBoxLayout()
        self.select_fence_btn = QPushButton("Select Fence")
        _set_compact_button_role(self.select_fence_btn, "primary")
        self.select_fence_btn.clicked.connect(self.select_fence)
        fence_btn_row.addWidget(self.select_fence_btn, 3)

        self.clear_fence_btn = QPushButton("Clear")
        _set_compact_button_role(self.clear_fence_btn)
        self.clear_fence_btn.clicked.connect(self.clear_fence_selection)
        fence_btn_row.addWidget(self.clear_fence_btn, 1)
        body.addLayout(fence_btn_row)

        self.fence_status = QLabel()
        _set_dialog_status(self.fence_status, "No fence selected", "neutral")

        # Select Classes
        classes_title = QLabel("Select Classes:")
        classes_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(classes_title)

        from_lbl = QLabel("From class (Ctrl+Click for multiple):")
        from_lbl.setObjectName("dialogCaption")
        body.addWidget(from_lbl)

        self.from_list = QListWidget()
        self.from_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.from_list.setMinimumHeight(90)
        self.from_list.setStyleSheet("QListWidget::item { border-bottom: none; padding: 4px; }")
        self.from_list.itemSelectionChanged.connect(self.on_from_selection_changed)
        body.addWidget(self.from_list)

        self.from_selection_label = QLabel("No classes selected")
        self.from_selection_label.setObjectName("dialogCaption")
        body.addWidget(self.from_selection_label)

        to_lbl = QLabel("To class (convert to):")
        to_lbl.setObjectName("dialogCaption")
        body.addWidget(to_lbl)

        self.to_combo = QComboBox()
        body.addWidget(self.to_combo)

        # Set Height Range
        height_title = QLabel("Set Height Range:")
        height_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(height_title)

        ref_lbl = QLabel("Reference class (heights measured from):")
        ref_lbl.setObjectName("dialogCaption")
        body.addWidget(ref_lbl)

        self.ref_class_combo = QComboBox()
        self.ref_class_combo.setToolTip(
            "Heights are measured from the nearest point of this class. "
            "Default: Class 1 (Ground)."
        )
        self.ref_class_combo.currentIndexChanged.connect(self.on_ref_class_changed)
        body.addWidget(self.ref_class_combo)

        ref_info = QLabel("Min = 0 m means at reference class level.")
        ref_info.setObjectName("dialogCaption")
        body.addWidget(ref_info)

        analyze_btn = QPushButton("Analyze Heights")
        _set_compact_button_role(analyze_btn)
        analyze_btn.clicked.connect(self.analyze_heights)
        body.addWidget(analyze_btn)

        self.height_combo = QComboBox()
        self.height_combo.addItem("Select class and click Analyze first", None)
        self.height_combo.currentIndexChanged.connect(self.on_height_combo_changed)
        body.addWidget(self.height_combo)

        range_row = QHBoxLayout()
        range_row.addWidget(QLabel("Min:"))
        self.min_height_spin = QDoubleSpinBox()
        self.min_height_spin.setRange(-999999.0, 999999.0)
        self.min_height_spin.setSingleStep(0.01)
        self.min_height_spin.setDecimals(2)
        self.min_height_spin.setValue(0.01)
        self.min_height_spin.setSuffix(" m")
        range_row.addWidget(self.min_height_spin)
        range_row.addWidget(QLabel("Max:"))
        self.max_height_spin = QDoubleSpinBox()
        self.max_height_spin.setRange(-999999.0, 999999.0)
        self.max_height_spin.setSingleStep(0.01)
        self.max_height_spin.setDecimals(2)
        self.max_height_spin.setValue(0.01)
        self.max_height_spin.setSuffix(" m")
        range_row.addWidget(self.max_height_spin)
        body.addLayout(range_row)

        self.height_info_label = QLabel("Select a class and click 'Analyze Heights' to begin.")
        self.height_info_label.setObjectName("dialogCaption")
        body.addWidget(self.height_info_label)

        self.preview_label = QLabel("Select classes and height range, then convert.")
        self.preview_label.setObjectName("dialogCaption")
        self.preview_label.setWordWrap(True)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addWidget(self.preview_label, 1)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setMinimumWidth(140)
        _set_compact_button_role(self.convert_btn, "primary")
        self.convert_btn.clicked.connect(self.perform_conversion)
        footer.addWidget(self.convert_btn)
        layout.addLayout(footer)



    def select_fence(self):
        """Select fences from digitize manager AND curve tool."""
        self._conversion_completed = False
        if getattr(self, '_fence_selection_dialog', None) is not None:
            try:
                self._fence_selection_dialog.close()
            except Exception:
                pass
            self._fence_selection_dialog = None
            return
        _show_fence_selection_dialog(
            self,
            "Select Fence(s) for Height Classification",
            "Select fence(s) to limit the height conversion area.",
        )

    def clear_fence_selection(self):
        """Deselect all fences (does NOT delete the drawn shapes from the digitizer)."""
        self._conversion_completed = False
        self.selected_fences = []
        self.permanent_fence_mode = False

        _set_dialog_status(self.fence_status, "No fence selected", "neutral")

        self._clear_fence_highlights()
        self.update_fence_display()

        try:
            self.app.vtk_widget.render()
        except Exception:
            pass


    def _clear_fence_highlights(self, remove_classified=True):
        # Remove hover highlight (yellow)
        if hasattr(self, '_hover_highlight_actor') and self._hover_highlight_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self._hover_highlight_actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(self._hover_highlight_actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(self._hover_highlight_actor)
            except Exception: pass
            self._hover_highlight_actor = None
    
        # Remove selection highlights (blue)
        if hasattr(self, '_selection_highlight_actors'):
            for actor in self._selection_highlight_actors:
                try:
                    self.app.vtk_widget.renderer.RemoveActor(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveActor2D(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveViewProp(actor)
                except Exception: pass
            self._selection_highlight_actors = []
    
        # We do NOT remove the classified drawing actors from the overlay renderer.
        # They belong to the digitizer and should stay visible on the screen.
        if remove_classified and hasattr(self, '_classified_fence_actors'):
            self._classified_fence_actors = []
    
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    def update_fence_display(self):
        count = len(self.selected_fences) if self.selected_fences else 0
        self.fence_count_badge.setText(str(count))
        if hasattr(self, "select_fence_btn") and self.select_fence_btn:
            if count > 0:
                self.select_fence_btn.setText(f"Select Fence ({count})")
            else:
                self.select_fence_btn.setText("Select Fence")

    def add_fences_from_selection(self, fence_shapes):
        """Pre-load fence shapes from the Element Selection tool.

        Enables the fence filter, hides the manual fence picker buttons, and
        pre-populates selected_fences — matching InsideFenceDialog behaviour.
        """
        if not fence_shapes:
            return

        existing_ids = {id(f) for f in self.selected_fences}
        added = 0
        for shape in fence_shapes:
            coords = shape.get('coords', [])
            if not coords:
                continue
            fence_id = id(shape)
            if fence_id in existing_ids:
                continue
            s = dict(shape)
            if s.get('type') in ['line', 'smart_line', 'polyline', 'smartline', 'curve']:
                c = s.get('coords', [])
                if not isinstance(c, np.ndarray):
                    c = np.array(c)
                if len(c) > 0 and not np.allclose(c[0], c[-1]):
                    s['coords'] = list(np.vstack([c, c[0]]))
            self.selected_fences.append(s)
            existing_ids.add(fence_id)
            added += 1

        if not added:
            return



        # Keep manual fence picker visible so user can select/clear fences
        pass

        if hasattr(self, 'fence_status'):
            _set_dialog_status(
                self.fence_status,
                f"{added} fence(s) added from selection ({len(self.selected_fences)} total)",
                "success",
            )

        self.update_fence_display()
        self.show()
        self.raise_()
        self.activateWindow()
        print(f"🔷 Height dialog: {added} fence(s) pre-loaded from selection")

    def _restore_highlights_from_data(self):
        try:
            if hasattr(self, '_selection_highlight_actors'):
                for actor in self._selection_highlight_actors:
                    try: self.app.vtk_widget.renderer.RemoveViewProp(actor)
                    except Exception: pass
            self._selection_highlight_actors = []
            if not self.selected_fences: return
            for shape in self.selected_fences:
                coords = shape.get('coords', [])
                if not coords: continue
                if hasattr(self.app, 'digitizer'):
                    ha = self.app.digitizer._make_polyline_actor(coords, color=(0, 0.5, 1), width=5)
                else:
                    import vtk
                    pts = vtk.vtkPoints()
                    for c in coords: pts.InsertNextPoint(c)
                    ln = vtk.vtkPolyLine(); ln.GetPointIds().SetNumberOfIds(len(coords))
                    for i in range(len(coords)): ln.GetPointIds().SetId(i, i)
                    cls = vtk.vtkCellArray(); cls.InsertNextCell(ln)
                    pd = vtk.vtkPolyData(); pd.SetPoints(pts); pd.SetLines(cls)
                    mp = vtk.vtkPolyDataMapper(); mp.SetInputData(pd)
                    ha = vtk.vtkActor(); ha.SetMapper(mp)
                    ha.GetProperty().SetColor(0, 0.5, 1); ha.GetProperty().SetLineWidth(5)
                self.app.vtk_widget.renderer.AddActor(ha)
                self._selection_highlight_actors.append(ha)
            self.app.vtk_widget.render()
        except Exception as e:
            print(f"⚠️ Restore highlights failed: {e}")

    def _points_inside_polygon(self, points, polygon_coords):
        from matplotlib.path import Path
        if isinstance(polygon_coords, list):
            poly_xy = np.array([(c[0], c[1]) for c in polygon_coords])
        else:
            poly_xy = polygon_coords[:, :2]
        points_xy = points[:, :2]
        min_x, min_y = np.min(poly_xy, axis=0)
        max_x, max_y = np.max(poly_xy, axis=0)
        bbox_mask = (points_xy[:, 0] >= min_x) & (points_xy[:, 0] <= max_x) & \
                    (points_xy[:, 1] >= min_y) & (points_xy[:, 1] <= max_y)
        inside = np.zeros(len(points), dtype=bool)
        if np.any(bbox_mask):
            poly_path = Path(poly_xy)
            inside[bbox_mask] = poly_path.contains_points(points_xy[bbox_mask])
        return inside

    def _highlight_classified_fences(self):
        if not self.selected_fences: return
        self._classified_fence_actors = []

        def _coords_key(coords):
            try:
                pts = [tuple(c[:2]) for c in (coords or [])]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                return tuple(pts)
            except Exception:
                return ()

        def _source_drawing(fence):
            digitizer = getattr(self.app, 'digitizer', None)
            drawings = list(getattr(digitizer, 'drawings', []) or [])
            if any(drawing is fence for drawing in drawings):
                return fence
            actor = fence.get('actor') if isinstance(fence, dict) else None
            if actor is not None:
                for drawing in drawings:
                    if drawing.get('actor') is actor:
                        return drawing
            fence_key = _coords_key(fence.get('coords', []) if isinstance(fence, dict) else [])
            if fence_key:
                for drawing in drawings:
                    if _coords_key(drawing.get('coords', [])) == fence_key:
                        return drawing
            return fence

        for fence in self.selected_fences:
            drawing = _source_drawing(fence)

            if isinstance(drawing, dict):
                drawing['classified_fence'] = True
                drawing['ai_auto_select'] = True
                drawing['source'] = drawing.get('source') or 'digitizer'

            if isinstance(fence, dict):
                fence['classified_fence'] = True
                fence['ai_auto_select'] = True
                fence['source'] = fence.get('source') or 'digitizer'

            actor = drawing.get('actor') if isinstance(drawing, dict) else None
            if actor:
                try:
                    self._classified_fence_actors.append(actor)
                except Exception as e:
                    print(f"⚠️ Storing classified actor failed: {e}")
    
    def populate_classes(self):
        """Populate class lists from Display Mode"""
        print(f"\n🔄 Populating ByClassHeightDialog...")
        
        self.from_list.clear()
        self.to_combo.clear()
        class_list = _collect_conversion_classes(
            self.app,
            standard_levels={17: "Other Poles"},
        )
        
        if not class_list:
            print("⚠️ No classes found")
            return
        
        # Add "Any class" option
        any_item = QListWidgetItem("Any class (convert all points)")
        any_item.setData(Qt.UserRole, None)
        try:
            from gui.theme_manager import ThemeColors
            from PySide6.QtGui import QColor as _QColor
            any_item.setBackground(_QColor(ThemeColors.get('bg_secondary')))
            any_item.setForeground(_QColor(ThemeColors.get('text_primary')))
        except Exception:
            any_item.setBackground(QColor(50, 50, 50))
            any_item.setForeground(QColor(255, 255, 255))
        self.from_list.addItem(any_item)

        # Populate with actual classes
        for cls in class_list:
            code = cls['code']
            lvl = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            
            icon = make_color_icon(color)
            
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, code)
            self.from_list.addItem(item)
            
            self.to_combo.addItem(icon, text, code)

        # ── Populate Reference Class combo ───────────────────────────────────
        # Keep current selection if possible
        old_ref = self.ref_class_combo.currentData() if self.ref_class_combo.count() > 0 else 1
        self.ref_class_combo.clear()
        for cls in class_list:
            code = cls['code']
            lvl  = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            icon = make_color_icon(color)
            self.ref_class_combo.addItem(icon, text, code)

        # Default to Class 1 (Ground) if available, else restore old selection
        restore_idx = self.ref_class_combo.findData(old_ref if old_ref is not None else 1)
        if restore_idx < 0:
            restore_idx = self.ref_class_combo.findData(1)   # fallback: Ground
        if restore_idx >= 0:
            self.ref_class_combo.setCurrentIndex(restore_idx)
        # ─────────────────────────────────────────────────────────────────────

        print(f"✅ Populated ByClassHeightDialog with {len(class_list)} classes")
    
    def on_classes_changed(self):
        """Called when Display Mode loads new PTC file"""
        print("\n" + "="*60)
        print("🔄 BY CLASS HEIGHT DIALOG: Detected PTC change")
        print("="*60)
        
        # Save current selections
        selected_items = self.from_list.selectedItems()
        old_from_codes = [item.data(Qt.UserRole) for item in selected_items]
        old_to = self.to_combo.currentData()
        old_ref = self.ref_class_combo.currentData()
        old_min_height = self.min_height_spin.value()
        old_max_height = self.max_height_spin.value()
        
        # Rebuild lists
        self.populate_classes()
        
        # Restore selections
        for i in range(self.from_list.count()):
            item = self.from_list.item(i)
            if item.data(Qt.UserRole) in old_from_codes:
                item.setSelected(True)
        
        if old_to is not None:
            idx = self.to_combo.findData(old_to)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)

        # Restore reference class selection
        if old_ref is not None:
            ref_idx = self.ref_class_combo.findData(old_ref)
            if ref_idx >= 0:
                self.ref_class_combo.setCurrentIndex(ref_idx)
        
        self.min_height_spin.setValue(old_min_height)
        self.max_height_spin.setValue(old_max_height)
        
        print("✅ ByClassHeightDialog updated")
        print("="*60 + "\n")
            
    def on_from_selection_changed(self):
        """Called when From class selection changes"""
        selected_items = self.from_list.selectedItems()
        
        if not selected_items:
            _set_dialog_status(self.from_selection_label, "No classes selected", "danger")
        else:
            any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
            
            if any_selected:
                _set_dialog_status(self.from_selection_label, "Selected: Any class (all eligible points)", "success")
            else:
                codes = [str(item.data(Qt.UserRole)) for item in selected_items]
                _set_dialog_status(self.from_selection_label, f"Selected: Classes {', '.join(codes)}", "success")
        
        self.on_class_selection_changed()

    def on_class_selection_changed(self):
        """Called when From class selection changes"""
        self.height_combo.clear()
        self.height_combo.addItem("Click 'Analyze Heights' to see options", None)
        self.height_info_label.setText("Class changed. Click 'Analyze Heights' to recalculate.")
        self.height_info_label.setStyleSheet("")

    def on_ref_class_changed(self, index):
        """Called when the Reference Class combo changes — force re-analysis."""
        self.height_combo.clear()
        self.height_combo.addItem("Click 'Analyze Heights' to see options", None)
        ref_text = self.ref_class_combo.currentText() if self.ref_class_combo.count() > 0 else "?"
        self.height_info_label.setText(
            f"Reference class changed to [{ref_text}]. "
            "Click 'Analyze Heights' to recalculate heights."
        )
        self.height_info_label.setStyleSheet("")
    
    def on_height_combo_changed(self, index):
        """Update spin boxes when dropdown selection changes"""
        data = self.height_combo.currentData()
        if data is not None and isinstance(data, dict):
            self.min_height_spin.setValue(data['min'])
            self.max_height_spin.setValue(data['max'])
    
    def analyze_heights(self):
        """Analyze actual height distribution — optionally restricted to fence"""
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return
        
        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
        if any_selected:
            QMessageBox.warning(self, "Invalid Selection", "Cannot analyze 'Any class' - select specific classes")
            return
        
        from_class = selected_items[0].data(Qt.UserRole)

        # ── Reference class (replaces the hardcoded ground_class = 1) ────────
        ground_class = self.ref_class_combo.currentData()
        if ground_class is None:
            ground_class = 1   # safety fallback
        ref_class_text = self.ref_class_combo.currentText()
        print(f"   📐 Reference class: {ground_class} ({ref_class_text})")
        # ─────────────────────────────────────────────────────────────────────

        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        if classification is None or xyz is None:
            return

        # Validate: reference class must differ from the From class
        if ground_class == from_class:
            QMessageBox.warning(
                self, "Invalid Reference Class",
                f"Reference class ({ground_class}) cannot be the same as the From class.\n"
                "Please choose a different reference class."
            )
            return

        # ✅ CHECK FENCE FILTER
        use_fence = len(self.selected_fences) > 0
        
        print(f"\n{'='*60}")
        print(f"📊 ANALYZING HEIGHT DISTRIBUTION {'(FENCE-RESTRICTED)' if use_fence else ''}")
        print(f"{'='*60}")
        
        # Get reference-class points (user-selected, no longer hardcoded to Class 1)
        ground_mask = (classification == ground_class)
        ground_count = np.sum(ground_mask)
        if ground_count == 0:
            QMessageBox.warning(
                self, "No Reference Points",
                f"No points found for reference class {ground_class} ({ref_class_text}).\n"
                "Please choose a different reference class."
            )
            return
        
        # Get From class points
        from_class_mask = (classification == from_class)
        from_class_count = np.sum(from_class_mask)
        if from_class_count == 0:
            QMessageBox.warning(self, "No Source Points", f"No points of class {from_class} found")
            return
        
        # ✅ FENCE FILTER: restrict to points inside fence(s)
        if use_fence:
            from_class_xyz_all = xyz[from_class_mask]
            
            combined_inside = np.zeros(len(from_class_xyz_all), dtype=bool)
            for fence in self.selected_fences:
                fence_coords = fence['coords']
                if isinstance(fence_coords, list):
                    fence_coords = np.array(fence_coords)
                inside = self._points_inside_polygon(from_class_xyz_all, fence_coords)
                combined_inside |= inside
            
            from_class_xyz = from_class_xyz_all[combined_inside]
            fence_label_text = f" (inside {len(self.selected_fences)} fence(s))"
            
            if len(from_class_xyz) == 0:
                QMessageBox.warning(self, "No Points", 
                    f"No class {from_class} points found inside fence")
                return
            
            # Also filter ground to fence area for better local reference
            ground_xyz_all = xyz[ground_mask]
            ground_inside = np.zeros(len(ground_xyz_all), dtype=bool)
            for fence in self.selected_fences:
                fence_coords = fence['coords']
                if isinstance(fence_coords, list):
                    fence_coords = np.array(fence_coords)
                inside = self._points_inside_polygon(ground_xyz_all, fence_coords)
                ground_inside |= inside
            
            ground_xyz = ground_xyz_all[ground_inside] if np.sum(ground_inside) > 0 else ground_xyz_all
            print(f"   Ground points in fence: {np.sum(ground_inside):,}")
        else:
            from_class_xyz = xyz[from_class_mask]
            ground_xyz = xyz[ground_mask]
            fence_label_text = ""
        
        # Build KDTree
        ground_xy = ground_xyz[:, :2]
        tree = cKDTree(ground_xy)
        
        # Query From class points
        from_class_xy = from_class_xyz[:, :2]
        distances, indices = tree.query(from_class_xy, k=1)
        
        # Calculate heights
        from_class_z = from_class_xyz[:, 2]
        nearest_ground_z = ground_xyz[indices, 2]
        heights = from_class_z - nearest_ground_z
        
        min_h = np.min(heights)
        max_h = np.max(heights)
        mean_h = np.mean(heights)
        median_h = np.median(heights)
        
        # Create height options — use actual min_h so ranges reflect real data
        percentiles = [25, 50, 75, 90, 95, 99]
        height_options = []
        for p in percentiles:
            threshold = np.percentile(heights, p)
            lower     = np.percentile(heights, 100 - p)   # symmetric lower bound
            actual_min = float(min_h)
            count = np.sum((heights >= actual_min) & (heights <= threshold))
            percentage = (count / len(heights)) * 100
            height_options.append({
                'percentile': p,
                'min': round(actual_min, 3),
                'max': round(float(threshold), 3),
                'count': count, 'percentage': percentage
            })
        
        print(f"   Points analyzed: {len(from_class_xyz):,}{fence_label_text}")
        print(f"   Min: {min_h:.3f}m, Max: {max_h:.3f}m, Mean: {mean_h:.3f}m")
        print(f"{'='*60}\n")
        
        # Populate dropdown — labels now show the real min, not a hardcoded 0
        self.height_combo.clear()
        for opt in height_options:
            label = (f"{opt['min']:.2f}m → {opt['max']:.2f}m  "
                     f"— {opt['percentile']}th pct  ({opt['count']:,} pts, {opt['percentage']:.0f}%)")
            self.height_combo.addItem(label, opt)
        
        self.height_combo.insertSeparator(self.height_combo.count())
        
        # Common relative ranges — only add if they fall within actual data range
        common_ranges = [
            (min_h, max_h),          # full range
            (min_h, np.percentile(heights, 50)),   # lower half
            (0.0, 0.5), (0.0, 1.0), (0.0, 2.0),
            (0.0, 5.0), (0.0, 10.0),
            (0.2, 2.0), (2.0, 10.0),
        ]
        seen_ranges = set()
        for min_r, max_r in common_ranges:
            min_r = round(float(min_r), 3)
            max_r = round(float(max_r), 3)
            key = (min_r, max_r)
            if key in seen_ranges:
                continue
            seen_ranges.add(key)
            if min_r >= max_r:
                continue
            if max_r > max_h + 0.01:   # skip ranges beyond actual data
                continue
            count = np.sum((heights >= min_r) & (heights <= max_r))
            percentage = (count / len(heights)) * 100
            label = f"{min_r:.2f}m → {max_r:.2f}m  — {count:,} pts ({percentage:.0f}%)"
            self.height_combo.addItem(label, {'min': min_r, 'max': max_r})
        
        # Default selection: 75th percentile (index 2)
        if len(height_options) > 2:
            self.height_combo.setCurrentIndex(2)
        
        # ✅ Auto-fill spinboxes with the actual analyzed min/max
        self.min_height_spin.setValue(round(float(min_h), 3))
        self.max_height_spin.setValue(round(float(max_h), 3))
        
        self.height_info_label.setText(
            f"Analyzed {len(from_class_xyz):,} points{fence_label_text} | "
            f"Ref: [{ref_class_text}] | Min: {min_h:.3f}m | Max: {max_h:.3f}m"
        )
        self.height_info_label.setStyleSheet("color: #4caf50; font-size: 9px; font-weight: bold;")
        
        QMessageBox.information(self, "Height Analysis Complete", 
            f"Analyzed {len(from_class_xyz):,} points{fence_label_text}\n"
            f"Reference class: {ref_class_text}\n"
            f"Min: {min_h:.3f}m, Max: {max_h:.3f}m above reference\n"
            f"Select a height range from dropdown")
        
    def preview_conversion(self):
        """Preview conversion"""
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return

        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)

        if any_selected:
            from_classes = None
            from_display = "All classes"
        else:
            from_classes = [item.data(Qt.UserRole) for item in selected_items]
            from_display = ", ".join(str(c) for c in from_classes)

        to_class = self.to_combo.currentData()
        ground_class = self.ref_class_combo.currentData() or 1
        ref_class_text = self.ref_class_combo.currentText()
        min_height = self.min_height_spin.value()
        max_height = self.max_height_spin.value()
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select a To class")
            return
        
        if from_classes and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", f"Cannot convert class {to_class} to itself")
            return
                
        try:
            affected_count = self._calculate_conversion(
                from_classes, to_class, ground_class, min_height, max_height, preview=True
            )
            
            if affected_count is None:
                return
            
            self.preview_label.setText(
                f"📊 Preview: {affected_count:,} points would be converted\n"
                f"From: {from_display} → To: Class {to_class}\n"
                f"Height: {min_height}m - {max_height}m above [{ref_class_text}]"
            )
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #2196f3;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1a237e;
                    border-radius: 3px;
                }
            """)
            
        except Exception as e:
            QMessageBox.critical(self, "Preview Failed", str(e))
    
    def perform_conversion(self):
        """Perform conversion"""
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return

        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)

        if any_selected:
            from_classes = None
            from_display = "All classes"
        else:
            from_classes = [item.data(Qt.UserRole) for item in selected_items]
            from_display = ", ".join(str(c) for c in from_classes)

        to_class = self.to_combo.currentData()
        ground_class = self.ref_class_combo.currentData() or 1
        ref_class_text = self.ref_class_combo.currentText()
        min_height = self.min_height_spin.value()
        max_height = self.max_height_spin.value()
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select a To class")
            return
        
        if from_classes and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", f"Cannot convert class {to_class} to itself")
            return

        # Guard: reference class must not be the same as a From class
        if from_classes and ground_class in from_classes:
            QMessageBox.warning(
                self, "Invalid Reference Class",
                f"Reference class ({ground_class}) cannot be one of the From classes.\n"
                "Please select a different reference class."
            )
            return
        
        # ✅ CHECK FENCE
        use_fence = len(self.selected_fences) > 0
        
        fence_text = ""
        if use_fence:
            fence_text = f"\nInside {len(self.selected_fences)} fence(s)"
        
        msg = (
            f"Convert {from_display} → class {to_class}\n"
            f"Height: {min_height}m - {max_height}m above [{ref_class_text}]{fence_text}"
        )
        
        reply = QMessageBox.question(self, "Confirm Conversion", msg,
                                     QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        
        if reply != QMessageBox.Yes:
            return
        
        try:
            converted_count = self._calculate_conversion(
                from_classes, to_class, ground_class, min_height, max_height, preview=False
            )
            
            if converted_count is None or converted_count == 0:
                QMessageBox.information(self, "No Points", "No points found to convert")
                return
            
            # ✅ Store conversion info
            if from_classes:
                self._last_conversion_info = {
                    'from_classes': from_classes,
                    'to_class': to_class,
                    'count': converted_count
                }
            
            self.preview_label.setText(f"✅ Converted {converted_count:,} points")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
            
            if self.ribbon_parent:
                self.ribbon_parent.update_status(f"✅ Converted {converted_count:,} points", "success")
            
            # ✅ FENCE CLEANUP after successful conversion
            if use_fence:
                self._clear_fence_highlights()          # remove blue actors
                self._highlight_classified_fences()     # recolour drawn rect → cyan
 
                if hasattr(self.app, 'digitizer') and self.app.digitizer:
                    try:
                        self.app.digitizer.rebind_drawings()
                        print("   🖊️ Drawing actors rebound to overlay after fence conversion")
                    except Exception as _rb_err:
                        print(f"   ⚠️ rebind_drawings failed: {_rb_err}")
 
                # ✅ FIX: ALWAYS clear selected_fences after classification.
                # Keep converted fence drawings so AI popup can find and auto-select them.
                if hasattr(self.app, 'digitizer'):
                    for fence in list(self.selected_fences):
                        try:
                            fence['classified_fence'] = True
                            fence['ai_auto_select'] = True
                            fence['source'] = fence.get('source') or 'digitizer'
                        except Exception:
                            pass

                        for drawing in list(getattr(self.app.digitizer, 'drawings', []) or []):
                            try:
                                fc = fence.get('coords', [])
                                dc = drawing.get('coords', [])

                                if len(fc) == len(dc) and all(
                                    tuple(a) == tuple(b) for a, b in zip(fc, dc)
                                ):
                                    drawing['classified_fence'] = True
                                    drawing['ai_auto_select'] = True
                                    drawing['source'] = drawing.get('source') or 'digitizer'
                                    break
                            except Exception:
                                pass
 
                self.selected_fences = []
                self.update_fence_display()
                _set_dialog_status(
                    self.fence_status,
                    "Classified. AI will auto-select this fence.",
                    "success",
                )
                self._conversion_completed = True
            
            QMessageBox.information(self, "Conversion Complete",
                                   f"✅ Successfully converted {converted_count:,} points")
            
        except Exception as e:
            error_msg = f"Conversion failed: {str(e)}"
            print(f"❌ {error_msg}")
            import traceback
            traceback.print_exc()
            
    def _calculate_conversion(self, from_classes, to_class, ground_class, 
                         min_height, max_height, preview=False):
        """Calculate/perform height-based conversion"""
        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        
        if classification is None or xyz is None:
            return None
        
        print(f"\n{'='*60}")
        print(f"📏 HEIGHT-BASED CONVERSION")
        print(f"   Reference class (baseline): {ground_class}")
        print(f"{'='*60}")
        
        # Get reference-class points (previously always Class 1 / Ground)
        ground_mask = (classification == ground_class)
        ground_count = np.sum(ground_mask)
        
        if ground_count == 0:
            QMessageBox.warning(
                self, "No Reference Points",
                f"No points found for reference class {ground_class}.\n"
                "Please select a different reference class or load more data."
            )
            return None
        
        # Get From class points
        if from_classes is None:
            from_class_mask = (classification != to_class)
        else:
            from_class_mask = np.isin(classification, from_classes)

        from_class_count = np.sum(from_class_mask)
        
        if from_class_count == 0:
            return 0
        
        # Build KDTree
        # ✅ Get indices for tracking
        from_class_indices = np.where(from_class_mask)[0]
        from_class_xyz = xyz[from_class_mask]
        
        # ✅ FENCE FILTER: restrict to points inside fence(s)
        use_fence = len(self.selected_fences) > 0
        if use_fence:
            combined_inside = np.zeros(len(from_class_xyz), dtype=bool)
            for fence in self.selected_fences:
                fence_coords = fence['coords']
                if isinstance(fence_coords, list):
                    fence_coords = np.array(fence_coords)
                inside = self._points_inside_polygon(from_class_xyz, fence_coords)
                combined_inside |= inside
            
            from_class_xyz = from_class_xyz[combined_inside]
            from_class_indices = from_class_indices[combined_inside]
            
            print(f"   Fence filter: {np.sum(combined_inside):,} of {from_class_count:,} pts inside fence")
            
            if len(from_class_xyz) == 0:
                print("   ⚠️ No points inside fence")
                if preview:
                    return 0
                return 0
            
            # Use fence-local ground for better height reference
            ground_xyz_all = xyz[ground_mask]
            ground_inside_mask = np.zeros(len(ground_xyz_all), dtype=bool)
            for fence in self.selected_fences:
                fence_coords = fence['coords']
                if isinstance(fence_coords, list):
                    fence_coords = np.array(fence_coords)
                inside = self._points_inside_polygon(ground_xyz_all, fence_coords)
                ground_inside_mask |= inside
            ground_xyz = ground_xyz_all[ground_inside_mask] if np.sum(ground_inside_mask) > 0 else ground_xyz_all
        else:
            ground_xyz = xyz[ground_mask]
        
        # Build KDTree
        ground_xy = ground_xyz[:, :2]
        tree = cKDTree(ground_xy)
        
        # Query From class points
        from_class_xy = from_class_xyz[:, :2]
        
        distances, indices = tree.query(from_class_xy, k=1)
        
        # Calculate heights
        from_class_z = from_class_xyz[:, 2]
        nearest_ground_z = ground_xyz[indices, 2]
        heights = from_class_z - nearest_ground_z
        
        # Points within height range
        # Points within height range
        height_mask = (heights >= min_height) & (heights <= max_height)
        in_range_count = np.sum(height_mask)
        
        print(f"   Points in range: {in_range_count:,}")
        
        if preview:
            print(f"{'='*60}\n")
            return in_range_count
        
        if in_range_count == 0:
            print(f"{'='*60}\n")
            return 0
        
        # Build final mask for main dataset
        # ✅ FIXED: Use from_class_indices (already fence-filtered if applicable)
        final_mask = np.zeros(len(classification), dtype=bool)
        convert_indices = from_class_indices[height_mask]
        final_mask[convert_indices] = True
        
        # Save undo
        old_classes = classification[final_mask].copy()
        
        # Convert
        classification[final_mask] = to_class
        
        
        new_classes_arr = np.full(np.sum(final_mask), to_class, dtype=classification.dtype)
        undo_step = {
            "mask": final_mask.copy(),
            "oldclasses": old_classes,
            "old_classes": old_classes,
            "newclasses": new_classes_arr,
            "new_classes": new_classes_arr
        }
        
        # ✅ FIX: Correct stack attribute names
        if hasattr(self.app, 'undostack'):
            self.app.undostack.append(undo_step)
        elif hasattr(self.app, 'undo_stack'):
            self.app.undo_stack.append(undo_step)
        else:
            print("⚠️ No undo stack found on app!")
        
        if hasattr(self.app, 'redostack'):
            self.app.redostack.clear()
        elif hasattr(self.app, 'redo_stack'):
            self.app.redo_stack.clear()
        from gui.memory_manager import trim_undo_stack
        trim_undo_stack(self.app)

        print(f"   ✅ Converted {in_range_count:,} points")
        print(f"{'='*60}\n")
        
        # ✅ Store conversion info for TARGETED refresh
        self._last_conversion_info = {
            'from_classes': from_classes if from_classes else list(np.unique(old_classes)),
            'to_class': to_class,
            'converted_indices': convert_indices,  # ✅ NEW: Store exact indices
            'count': in_range_count
        }
        
        # AFTER
        current_display_mode = getattr(self.app, 'display_mode', 'class')
        print(f"   🔄 Post-conversion refresh (display_mode='{current_display_mode}')...")

        # ⚡ For class mode, keep the existing actor alive so _fast_classification_inject
        #    can patch it in-place (O(changed) instead of O(all)).
        #    Only wipe for shaded_class/other modes where a full rebuild is needed anyway.
        if current_display_mode not in ("shaded_class", "class"):
            if hasattr(self.app, 'vtk_widget'):
                if hasattr(self.app.vtk_widget, 'actors'):
                    old_actors = self.app.vtk_widget.actors
                    if isinstance(old_actors, dict):
                        for actor in old_actors.values():
                            try:
                                self.app.vtk_widget.renderer.RemoveActor(actor)
                            except Exception:
                                pass
                        old_actors.clear()
                        print("      ✅ Cleared actor cache")
                if hasattr(self.app.vtk_widget, 'actor'):
                    self.app.vtk_widget.actor = None

        # Step 2: Now call Display Mode to rebuild from scratch
        display_dialog = getattr(self.app, 'display_mode_dialog', 
                                getattr(self.app, 'display_dialog', None))
        #     display_dialog.on_apply()
            
        self.app._conversion_just_happened = True
        # Fallback
        self._refresh_after_classification() 

        # Refresh cross-sections if needed
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx in list(self.app.section_vtks.keys()):
                try:
                    if hasattr(self.app, '_refresh_single_section_view'):
                        self.app._refresh_single_section_view(view_idx)
                except Exception as e:
                    print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")

        # Update point count widget
        if hasattr(self.app, 'point_count_widget'):
            self.app.point_count_widget.schedule_update()
     
        return in_range_count

    def _fast_classification_inject(self):
        """
        Fast classification refresh using the same pipeline as interactive classify.
        This avoids stale actor-array indexing after undo/redo.
        """
        import time
        import numpy as np

        t0 = time.perf_counter()

        info = getattr(self, '_last_conversion_info', None)
        convert_indices = info.get('converted_indices') if info else None
        to_class = info.get('to_class') if info else None

        try:
            if convert_indices is None or to_class is None:
                return False
            if not hasattr(self.app, 'data') or self.app.data is None:
                return False

            classification = self.app.data.get('classification')
            if classification is None:
                return False

            total_pts = len(classification)
            if total_pts == 0:
                return False

            convert_indices = np.asarray(convert_indices, dtype=np.int64)
            valid = (convert_indices >= 0) & (convert_indices < total_pts)
            if not np.any(valid):
                return False

            changed_mask = np.zeros(total_pts, dtype=bool)
            changed_mask[convert_indices[valid]] = True

            from gui.unified_actor_manager import fast_classify_update
            ok = fast_classify_update(self.app, changed_mask, int(to_class))
            if not ok:
                return False

            # Keep section mirrors and statistics in sync through app signal bus.
            try:
                self.app.classification_finished.emit(changed_mask)
            except Exception:
                pass

            elapsed = (time.perf_counter() - t0) * 1000
            print(f"   fast_inject unified update: {int(np.count_nonzero(changed_mask)):,} pts [{elapsed:.1f} ms]")
            return True

        except Exception as e:
            print(f"   _fast_classification_inject failed: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _refresh_after_classification(self):
        """
        Refresh the view respecting the CURRENT display_mode.
        ✅ FIXED: Forces mesh REBUILD if points are converted to a HIDDEN class.
        """
        display_mode = getattr(self.app, 'display_mode', 'class')
        print(f"   📍 Refreshing after classification: display_mode='{display_mode}'")
        try:
            if display_mode == "shaded_class":
                from gui.shading_display import (
                    get_cache, refresh_shaded_after_classification_fast,
                    clear_shading_cache, update_shaded_class
                )
                
                cache = get_cache()
                
                # 1. Get current visibility set
                saved_vis = getattr(cache, 'visible_classes_set', None)
                if saved_vis is None or len(saved_vis) == 0:
                    saved_vis = getattr(self.app, '_shading_visible_classes', None)
                if saved_vis is None or len(saved_vis) == 0:
                    saved_vis = {
                        int(c) for c, e in self.app.class_palette.items()
                        if e.get("show", True)
                    }
                saved_vis = saved_vis.copy()
                
                print(f"   📍 Shading visibility: {sorted(saved_vis)}")
                
                # Force palette to match saved visibility
                for c in self.app.class_palette:
                    self.app.class_palette[c]["show"] = (int(c) in saved_vis)
                
                # 2. ✅ CRITICAL FIX: Check if we converted points INTO a hidden class
                target_is_hidden = False
                if hasattr(self, '_last_conversion_info') and self._last_conversion_info:
                    to_class = self._last_conversion_info.get('to_class')
                    if to_class is not None:
                        if int(to_class) not in saved_vis:
                            target_is_hidden = True
                            print(f"   🙈 Target Class {to_class} is UNCHECKED/HIDDEN")
                
                # 3. Check if any visible points remain at all
                classification = self.app.data.get("classification")
                has_visible_points = False
                if classification is not None:
                    for vc in saved_vis:
                        if np.sum(classification == vc) > 0:
                            has_visible_points = True
                            break
                
                # 4. DECISION LOGIC
                if not has_visible_points:
                    # Case A: Everything is gone
                    print(f"   🖤 No visible points remain — clearing mesh")
                    clear_shading_cache("all visible points converted away")
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=True
                    )

                elif target_is_hidden:
                    # Case B: ✅ Converted to hidden class -> MUST REBUILD GEOMETRY
                    # Fast color swap is NOT enough because geometry must be removed
                    print(f"   ♻️ Converted to hidden class — FORCING MESH REBUILD to hide geometry")
                    clear_shading_cache("converted to hidden class")
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=True
                    )
                    
                else:
                    # Case C: Converted to visible class -> FAST COLOR SWAP (GPU)
                    print(f"   ⚡ Target class is visible — using fast color injection")
                    
                    # Pull the changed point indices
                    info = getattr(self, '_last_conversion_info', None)
                    changed_mask = None
                    if info and 'converted_indices' in info:
                        changed_mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
                        changed_mask[info['converted_indices']] = True
                    
                    refresh_shaded_after_classification_fast(self.app, changed_mask=changed_mask)
                
                print(f"   ✅ Refreshed in shaded_class mode")
                
            elif display_mode == "class":
                # ⚡ FAST PATH — patch only the converted points, same strategy as undo's fast_undo_update
                fast_ok = self._fast_classification_inject()
                if not fast_ok:
                    # Fallback: full rebuild (slow, only if fast path is unavailable)
                    from gui.class_display import update_class_mode
                    update_class_mode(self.app, force_refresh=True)
                print("   ✅ Refreshed in class mode")
            else:
                from gui.pointcloud_display import update_pointcloud
                update_pointcloud(self.app, display_mode)
                print(f"   ✅ Refreshed in {display_mode} mode")
                
        except Exception as e:
            print(f"   ⚠️ Refresh failed ({e}), falling back to class mode")
            import traceback
            traceback.print_exc()
            try:
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
            except Exception as e2:
                print(f"   ❌ Fallback also failed: {e2}")
        
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
    
    def _refresh_all_views(self):
        """
        ✅ ULTIMATE FIXED: Combines fence dialog smart refresh + force rebuild fallback
        - First tries smart update (like fence dialog - prevents blank screens)
        - If that fails, forces full rebuild by clearing actors
        - Guaranteed to work in all scenarios!
        """
        print(f"\n🔄 REFRESHING VIEWS (smart + visibility-aware)...")
        
        # ✅ CRITICAL: Save camera position BEFORE any updates
        saved_camera = None
        if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'renderer'):
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                if camera:
                    saved_camera = {
                        'position': tuple(camera.GetPosition()),
                        'focal_point': tuple(camera.GetFocalPoint()),
                        'view_up': tuple(camera.GetViewUp()),
                        'parallel_scale': camera.GetParallelScale(),
                        'parallel_projection': camera.GetParallelProjection(),
                    }
                    print(f"   📷 Camera saved: pos={saved_camera['position'][:2]}, scale={saved_camera['parallel_scale']:.2f}")
            except Exception as e:
                print(f"   ⚠️ Camera save failed: {e}")
        
        # ✅ DETECT MODE: By-class actors or unified actor?
        has_by_class_actors = False
        if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'actors'):
            if isinstance(self.app.vtk_widget.actors, dict):
                for key in self.app.vtk_widget.actors.keys():
                    if 'class_' in str(key).lower():
                        has_by_class_actors = True
                        break
        
        update_success = False
        
        # ========================================
        # METHOD 1: SMART UPDATE (FROM FENCE DIALOG)
        # ========================================
        if has_by_class_actors:
            print(f"   🔍 Detected BY-CLASS actor mode - rebuilding affected actors...")
            
            try:
                display_dialog = getattr(self.app, 'display_mode_dialog', 
                                        getattr(self.app, 'display_dialog', None))
                
                if display_dialog:
                    # Try various display dialog methods
                    if hasattr(display_dialog, 'update_display_mode'):
                        display_dialog.update_display_mode()
                        print(f"   ✅ Called display_dialog.update_display_mode()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'refresh_actors'):
                        display_dialog.refresh_actors()
                        print(f"   ✅ Called display_dialog.refresh_actors()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'apply_class_filter'):
                        display_dialog.apply_class_filter()
                        print(f"   ✅ Called display_dialog.apply_class_filter()")
                        update_success = True
                    
                    elif hasattr(display_dialog, 'apply_btn'):
                        display_dialog.apply_btn.click()
                        print(f"   ✅ Triggered display_dialog.apply_btn.click()")
                        update_success = True
            
            except Exception as e:
                print(f"   ⚠️ Display mode refresh failed: {e}")
        
        # ✅ UNIFIED ACTOR MODE: Try smart update
        if not update_success:
            print(f"   🔍 Using unified actor mode - trying smart update...")
            
            # Try smart_update_colors
            try:
                from gui.pointcloud_display import smart_update_colors
                smart_update_colors(self.app, None)
                print(f"   ✅ Main view updated (smart_update_colors)")
                update_success = True
            except ImportError:
                print(f"   ⚠️ smart_update_colors not found")
            except Exception as e:
                print(f"   ⚠️ smart_update_colors failed: {e}")
            
            # Try direct VTK color update
            if not update_success:
                try:
                    if self._force_vtk_color_update():
                        print(f"   ✅ Main view updated (direct VTK with visibility)")
                        update_success = True
                except Exception:
                    pass
        
        # ========================================
        # METHOD 2: FORCE REBUILD (FALLBACK)
        # ========================================
        if not update_success:
            print(f"   ⚠️ Smart update failed - forcing full rebuild...")
            
            if hasattr(self.app, 'vtk_widget'):
                # Remove all actors to force rebuild
                if hasattr(self.app.vtk_widget, 'actor') and self.app.vtk_widget.actor:
                    old_actor = self.app.vtk_widget.actor
                    self.app.vtk_widget.actor = None
                    if hasattr(self.app.vtk_widget, 'renderer'):
                        self.app.vtk_widget.renderer.RemoveActor(old_actor)
                    print(f"      ✅ Removed unified actor")
                
                # Clear actors dict
                if hasattr(self.app.vtk_widget, 'actors'):
                    old_actors = self.app.vtk_widget.actors
                    if isinstance(old_actors, dict):
                        for actor in old_actors.values():
                            if actor and hasattr(self.app.vtk_widget, 'renderer'):
                                self.app.vtk_widget.renderer.RemoveActor(actor)
                        old_actors.clear()
                        print(f"      ✅ Cleared {len(old_actors)} actors")
            
            # Call update_class_mode - will detect missing actors and do full rebuild
            try:
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                print(f"   ✅ Main view refreshed (forced rebuild)")
                update_success = True
            except Exception as e:
                print(f"      ❌ Full rebuild failed: {e}")
        
        # ========================================
        # RESTORE & FINALIZE
        # ========================================
        
        # ✅ CRITICAL: Restore camera position IMMEDIATELY
        if saved_camera:
            try:
                camera = self.app.vtk_widget.renderer.GetActiveCamera()
                camera.SetPosition(saved_camera['position'])
                camera.SetFocalPoint(saved_camera['focal_point'])
                camera.SetViewUp(saved_camera['view_up'])
                camera.SetParallelScale(saved_camera['parallel_scale'])
                
                if saved_camera['parallel_projection']:
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
                
                self.app.vtk_widget.renderer.ResetCameraClippingRange()
                print(f"   📷✅ Camera restored")
            except Exception as e:
                print(f"   ⚠️ Camera restore failed: {e}")
        
        # Refresh cross-sections
        if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
            for view_idx in list(self.app.section_vtks.keys()):
                try:
                    if hasattr(self.app, '_refresh_single_section_view'):
                        self.app._refresh_single_section_view(view_idx)
                except Exception as e:
                    print(f"   ⚠️ Section {view_idx+1} refresh failed: {e}")
        
        # Update point statistics
        if hasattr(self.app, 'point_count_widget'):
            try:
                self.app.point_count_widget.schedule_update()
            except Exception:
                pass
        
        # ✅ Force final render
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        mode_str = "by-class" if has_by_class_actors else "unified"
        print(f"✅ REFRESH COMPLETE ({mode_str} mode, success: {update_success})\n")


    def _force_vtk_color_update(self):
        """
        Direct VTK color update with visibility awareness
        Called when smart_update_colors is not available
        """
        try:
            if not hasattr(self.app, 'vtk_widget'):
                return False
            
            vtk_widget = self.app.vtk_widget
            
            # Get the actor (unified mode)
            actor = None
            if hasattr(vtk_widget, 'actor') and vtk_widget.actor:
                actor = vtk_widget.actor
            elif hasattr(vtk_widget, 'actors') and isinstance(vtk_widget.actors, dict):
                # Try to find a unified actor
                for key, act in vtk_widget.actors.items():
                    if 'unified' in str(key).lower() or key == 'main':
                        actor = act
                        break
            
            if not actor:
                return False
            
            # Get mapper and update colors
            mapper = actor.GetMapper()
            if not mapper:
                return False
            
            # Get classification data
            classification = self.app.data.get("classification")
            if classification is None:
                return False
            
            # Get visibility mask if available
            visible_mask = None
            if hasattr(self.app, 'get_visible_points_mask'):
                visible_mask = self.app.get_visible_points_mask()
            
            # Update colors based on classification
            from vtk import vtkUnsignedCharArray
            colors = vtkUnsignedCharArray()
            colors.SetNumberOfComponents(3)
            colors.SetName("Colors")
            
            class_palette = getattr(self.app, 'class_palette', {})
            
            for i, cls in enumerate(classification):
                # Check visibility
                if visible_mask is not None and not visible_mask[i]:
                    # Make invisible points black or very dark
                    colors.InsertNextTuple3(20, 20, 20)
                else:
                    # Use class color
                    color_entry = class_palette.get(int(cls), {'color': (128, 128, 128)})
                    color = color_entry.get('color', (128, 128, 128))
                    colors.InsertNextTuple3(int(color[0]), int(color[1]), int(color[2]))
            
            # Update the polydata
            polydata = mapper.GetInput()
            if polydata:
                polydata.GetPointData().SetScalars(colors)
                polydata.Modified()
                mapper.Modified()
                actor.Modified()
            
            return True
            
        except Exception as e:
            print(f"      ❌ Direct VTK update failed: {e}")
            return False


    def _save_camera_state(self):
        """Save camera state"""
        if not hasattr(self.app, 'vtk_widget'):
            return None
        if not hasattr(self.app.vtk_widget, 'renderer'):
            return None
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            if camera:
                return {
                    'position': tuple(camera.GetPosition()),
                    'focal_point': tuple(camera.GetFocalPoint()),
                    'view_up': tuple(camera.GetViewUp()),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection(),
                }
        except Exception:
            pass
        return None


    def _restore_camera_state(self, saved_camera):
        """Restore camera state"""
        if not saved_camera:
            return
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            camera.SetPosition(saved_camera['position'])
            camera.SetFocalPoint(saved_camera['focal_point'])
            camera.SetViewUp(saved_camera['view_up'])
            camera.SetParallelScale(saved_camera['parallel_scale'])
            
            if saved_camera['parallel_projection']:
                camera.ParallelProjectionOn()
            else:
                camera.ParallelProjectionOff()
            
            self.app.vtk_widget.renderer.ResetCameraClippingRange()
        except Exception:
            pass



    def _debug_vtk_structure(self):
        """Debug: Show VTK structure"""
        print("\n" + "="*60)
        print("🔍 DEBUGGING VTK STRUCTURE")
        print("="*60)
        
        if not hasattr(self.app, 'vtk_widget'):
            print("❌ No vtk_widget found")
            return
        
        vtk_widget = self.app.vtk_widget
        print(f"✅ vtk_widget exists: {type(vtk_widget)}")
        
        # Check all attributes
        print("\n📋 vtk_widget attributes containing 'actor':")
        for attr in dir(vtk_widget):
            if 'actor' in attr.lower():
                try:
                    value = getattr(vtk_widget, attr)
                    print(f"   - {attr}: {type(value)}")
                    
                    if attr == 'actors' and isinstance(value, dict):
                        print(f"     → actors dict keys: {list(value.keys())}")
                        for key, actor in value.items():
                            if actor:
                                try:
                                    mapper = actor.GetMapper()
                                    if mapper:
                                        input_data = mapper.GetInput()
                                        if input_data:
                                            pts = input_data.GetNumberOfPoints()
                                            print(f"        '{key}': {pts:,} points")
                                except Exception:
                                    pass
                except Exception:
                    pass
        
        # Check renderer
        if hasattr(vtk_widget, 'renderer'):
            renderer = vtk_widget.renderer
            print(f"\n✅ renderer exists")
            
            actor_collection = renderer.GetActors()
            actor_count = actor_collection.GetNumberOfItems()
            print(f"   → {actor_count} actors in renderer")
            
            actor_collection.InitTraversal()
            for i in range(actor_count):
                actor = actor_collection.GetNextActor()
                if actor:
                    try:
                        mapper = actor.GetMapper()
                        if mapper:
                            input_data = mapper.GetInput()
                            if input_data:
                                pts = input_data.GetNumberOfPoints()
                                print(f"      Actor #{i}: {pts:,} points")
                    except Exception:
                        pass
        
        print("="*60 + "\n")


    def _update_specific_points_colors(self, converted_indices, to_class):
        """
        ✅ NEW: Update colors for ONLY the specific converted points
        Respects visibility - only shows color if to_class is checked
        """
        print(f"   🎯 Targeted color update for {len(converted_indices):,} points...")
        
        try:
            import vtk
            from vtk.util import numpy_support
            import numpy as np
            
            classification = self.app.data.get("classification")
            if classification is None:
                return False
            
            # ✅ Check if target class is VISIBLE
            visible_classes = self._get_visible_classes()
            
            is_visible = to_class in visible_classes
            
            if is_visible:
                print(f"   ✅ Class {to_class} is VISIBLE (checked)")
            else:
                print(f"   ❌ Class {to_class} is HIDDEN (unchecked) - points will be blank")
            
            # Get target color
            if is_visible:
                if hasattr(self.app, 'class_palette') and to_class in self.app.class_palette:
                    target_color = self.app.class_palette[to_class].get('color', (128, 128, 128))
                else:
                    target_color = (128, 128, 128)
                print(f"   → Target color: RGB{target_color}")
            else:
                target_color = (0, 0, 0)  # Black/blank
                print(f"   → Target color: BLANK (class hidden)")
            
            # ✅ Find VTK actor
            actor = self._find_main_vtk_actor()
            
            if not actor:
                print("   ❌ No VTK actor found")
                return False
            
            mapper = actor.GetMapper()
            input_data = mapper.GetInput()
            point_count = input_data.GetNumberOfPoints()
            
            # ✅ Get downsample indices
            downsample_indices = self._find_downsample_indices(len(classification), point_count)
            
            if downsample_indices is None:
                return False
            
            # ✅ Find which VTK points correspond to converted points
            # This creates a reverse mapping: full_index -> vtk_index
            reverse_map = {}
            for vtk_idx, full_idx in enumerate(downsample_indices):
                reverse_map[full_idx] = vtk_idx
            
            # Get VTK indices for converted points
            vtk_indices_to_update = []
            for full_idx in converted_indices:
                if full_idx in reverse_map:
                    vtk_indices_to_update.append(reverse_map[full_idx])
            
            print(f"   → Converting {len(converted_indices):,} full indices to {len(vtk_indices_to_update):,} VTK indices")
            
            if len(vtk_indices_to_update) == 0:
                print("   ⚠️ No VTK points found for converted indices")
                return False
            
            # ✅ Get existing colors array
            existing_colors_vtk = input_data.GetPointData().GetScalars()
            
            if existing_colors_vtk:
                existing_colors = numpy_support.vtk_to_numpy(existing_colors_vtk)
            else:
                # No existing colors - create default
                existing_colors = np.zeros((point_count, 3), dtype=np.uint8)
            
            # ✅ Update ONLY the converted points' colors
            for vtk_idx in vtk_indices_to_update:
                existing_colors[vtk_idx] = target_color
            
            print(f"   ✅ Updated {len(vtk_indices_to_update):,} VTK points to RGB{target_color}")
            
            # ✅ Update VTK actor
            vtk_colors = numpy_support.numpy_to_vtk(existing_colors, deep=True, 
                                                    array_type=vtk.VTK_UNSIGNED_CHAR)
            vtk_colors.SetName("Colors")
            
            input_data.GetPointData().SetScalars(vtk_colors)
            input_data.Modified()
            mapper.Modified()
            actor.Modified()
            
            # Force renderer update
            if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'renderer'):
                self.app.vtk_widget.renderer.Modified()
            
            print(f"   ✅ Targeted color update complete")
            
            return True
            
        except Exception as e:
            print(f"   ❌ Targeted update failed: {e}")
            import traceback
            traceback.print_exc()
            return False


    def _find_main_vtk_actor(self):
        """Helper to find main VTK actor"""
        actor = None
        point_count = 0
        
        if hasattr(self.app, 'vtk_widget'):
            vtk_widget = self.app.vtk_widget
            
            # Method 1: Direct actor attribute
            if hasattr(vtk_widget, 'actor') and vtk_widget.actor:
                return vtk_widget.actor
            
            # Method 2: actors dictionary
            elif hasattr(vtk_widget, 'actors'):
                actors = vtk_widget.actors
                
                if isinstance(actors, dict):
                    # Find largest actor
                    for key, a in actors.items():
                        if a:
                            try:
                                mapper = a.GetMapper()
                                if mapper:
                                    input_data = mapper.GetInput()
                                    if input_data:
                                        pts = input_data.GetNumberOfPoints()
                                        if pts > point_count:
                                            actor = a
                                            point_count = pts
                            except Exception:
                                continue
                elif actors:
                    return actors
            
            # Method 3: Renderer's actors
            if not actor and hasattr(vtk_widget, 'renderer'):
                renderer = vtk_widget.renderer
                actor_collection = renderer.GetActors()
                actor_collection.InitTraversal()
                
                for i in range(actor_collection.GetNumberOfItems()):
                    a = actor_collection.GetNextActor()
                    if a:
                        try:
                            mapper = a.GetMapper()
                            if mapper:
                                input_data = mapper.GetInput()
                                if input_data:
                                    pts = input_data.GetNumberOfPoints()
                                    if pts > 1000:
                                        if pts > point_count:
                                            actor = a
                                            point_count = pts
                        except Exception:
                            continue
        
        return actor


    def _force_vtk_color_update_with_visibility(self):
        """
        ✅ CRITICAL: Update VTK colors with VISIBILITY FILTERING
        Only colors points whose class is CHECKED in Display Mode
        """
        print("   🎨 Direct VTK color update (visibility-aware)...")
        self._debug_vtk_structure()
        try:
            import vtk
            try:
                from vtk.util import numpy_support
            except ImportError:
                from vtkmodules.util import numpy_support
            import numpy as np
           
            classification = self.app.data.get("classification")
            if classification is None:
                print("   ❌ No classification data")
                return False
            
            # ✅ STEP 1: Get visible (checked) classes from Display Mode
            visible_classes = self._get_visible_classes()
            
            if not visible_classes:
                print("   ⚠️ No visible classes found - showing ALL")
                visible_classes = set(np.unique(classification))
            else:
                print(f"   ✅ Visible classes (checked): {sorted(visible_classes)}")
            
            # ✅ STEP 2: Find main VTK actor (IMPROVED LOGIC)
            actor = None
            point_count = 0
            
            if hasattr(self.app, 'vtk_widget'):
                vtk_widget = self.app.vtk_widget
                
                # Method 1: Direct actor attribute
                if hasattr(vtk_widget, 'actor') and vtk_widget.actor:
                    actor = vtk_widget.actor
                    print(f"   ✅ Found vtk_widget.actor")
                
                # Method 2: actors dictionary
                elif hasattr(vtk_widget, 'actors'):
                    actors = vtk_widget.actors
                    
                    if isinstance(actors, dict):
                        print(f"   🔍 Searching in actors dict ({len(actors)} entries)...")
                        
                        # First try: Find actor NOT starting with 'class_'
                        for key, a in actors.items():
                            if a:
                                key_str = str(key).lower()
                                if 'class_' not in key_str:
                                    try:
                                        mapper = a.GetMapper()
                                        if mapper:
                                            input_data = mapper.GetInput()
                                            if input_data:
                                                pts = input_data.GetNumberOfPoints()
                                                if pts > point_count:
                                                    actor = a
                                                    point_count = pts
                                                    print(f"   ✅ Found actor '{key}' with {pts:,} points")
                                    except Exception:
                                        continue
                        
                        # Second try: If no actor found, use ANY actor with points
                        if not actor:
                            print(f"   🔍 No unified actor found, trying any actor...")
                            for key, a in actors.items():
                                if a:
                                    try:
                                        mapper = a.GetMapper()
                                        if mapper:
                                            input_data = mapper.GetInput()
                                            if input_data:
                                                pts = input_data.GetNumberOfPoints()
                                                if pts > point_count:
                                                    actor = a
                                                    point_count = pts
                                                    print(f"   ✅ Found actor '{key}' with {pts:,} points")
                                    except Exception:
                                        continue
                    
                    elif actors:  # Single actor, not dict
                        actor = actors
                        print(f"   ✅ Found vtk_widget.actors (single actor)")
                
                # Method 3: Renderer's actors
                if not actor and hasattr(vtk_widget, 'renderer'):
                    renderer = vtk_widget.renderer
                    actor_collection = renderer.GetActors()
                    actor_collection.InitTraversal()
                    
                    print(f"   🔍 Searching in renderer actors...")
                    
                    for i in range(actor_collection.GetNumberOfItems()):
                        a = actor_collection.GetNextActor()
                        if a:
                            try:
                                mapper = a.GetMapper()
                                if mapper:
                                    input_data = mapper.GetInput()
                                    if input_data:
                                        pts = input_data.GetNumberOfPoints()
                                        if pts > 1000:  # Ignore small actors
                                            if pts > point_count:
                                                actor = a
                                                point_count = pts
                                                print(f"   ✅ Found actor #{i} with {pts:,} points")
                            except Exception:
                                continue
            
            if not actor:
                print("   ❌ No VTK actor found")
                print("   💡 Available attributes:")
                if hasattr(self.app, 'vtk_widget'):
                    for attr in dir(self.app.vtk_widget):
                        if 'actor' in attr.lower():
                            print(f"      - vtk_widget.{attr}")
                return False
            
            # Get point count if not already set
            if point_count == 0:
                mapper = actor.GetMapper()
                input_data = mapper.GetInput()
                point_count = input_data.GetNumberOfPoints()
            
            print(f"   ✅ Using actor with {point_count:,} points")
            
            # ✅ STEP 3: Get downsample indices
            downsample_indices = self._find_downsample_indices(len(classification), point_count)
            
            if downsample_indices is None:
                print("   ❌ Could not find downsample indices")
                return False
            
            # Validate indices
            if len(downsample_indices) != point_count:
                print(f"   ❌ Index count mismatch: {len(downsample_indices)} vs {point_count}")
                return False
            
            if np.max(downsample_indices) >= len(classification):
                print(f"   ❌ Invalid indices: max={np.max(downsample_indices)}, dataset={len(classification)}")
                return False
            
            # ✅ STEP 4: Get downsampled classification
            downsampled_classification = classification[downsample_indices]
            
            # ✅ STEP 5: Create colors with VISIBILITY FILTER
            colors = np.zeros((point_count, 3), dtype=np.uint8)
            
            visible_count = 0
            hidden_count = 0
            
            for class_code in np.unique(downsampled_classification):
                mask = (downsampled_classification == class_code)
                count = np.sum(mask)
                
                # ✅ Only color if class is VISIBLE (checked)
                if class_code in visible_classes:
                    if hasattr(self.app, 'class_palette') and class_code in self.app.class_palette:
                        color = self.app.class_palette[class_code].get('color', (128, 128, 128))
                        colors[mask] = color
                        visible_count += count
                        print(f"      ✅ Class {class_code}: {count:,} points → RGB{color} (VISIBLE)")
                    else:
                        colors[mask] = (128, 128, 128)
                        visible_count += count
                        print(f"      ✅ Class {class_code}: {count:,} points → Gray (VISIBLE)")
                else:
                    # Leave as (0,0,0) = blank/black
                    hidden_count += count
                    print(f"      ❌ Class {class_code}: {count:,} points → BLANK (HIDDEN)")
            
            print(f"   📊 Summary: {visible_count:,} visible, {hidden_count:,} hidden")
            
            # ✅ STEP 6: Update VTK actor
            mapper = actor.GetMapper()
            input_data = mapper.GetInput()
            
            vtk_colors = numpy_support.numpy_to_vtk(colors, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
            vtk_colors.SetName("Colors")
            
            input_data.GetPointData().SetScalars(vtk_colors)
            input_data.Modified()
            mapper.Modified()
            actor.Modified()
            
            # Force renderer update
            if hasattr(self.app, 'vtk_widget') and hasattr(self.app.vtk_widget, 'renderer'):
                self.app.vtk_widget.renderer.Modified()
            
            print(f"   ✅ VTK colors updated with visibility filter")
            
            return True
            
        except Exception as e:
            print(f"   ❌ VTK color update failed: {e}")
            import traceback
            traceback.print_exc()
            return False



    def _get_visible_classes(self):
        """Get set of visible (checked) classes from Display Mode dialog"""
        visible_classes = set()
        
        display_dialog = getattr(self.app, 'display_mode_dialog',
                                getattr(self.app, 'display_dialog', None))
        
        if not display_dialog or not hasattr(display_dialog, 'table'):
            return visible_classes
        
        table = display_dialog.table
        
        for row in range(table.rowCount()):
            try:
                # Get checkbox widget in column 0
                checkbox_item = table.cellWidget(row, 0)
                code_item = table.item(row, 1)
                
                if not code_item:
                    continue
                
                class_code = int(code_item.text())
                
                # Check if checkbox is checked
                if checkbox_item:
                    if hasattr(checkbox_item, 'isChecked'):
                        if checkbox_item.isChecked():
                            visible_classes.add(class_code)
                    elif hasattr(checkbox_item, 'checkState'):
                        from PySide6.QtCore import Qt
                        if checkbox_item.checkState() == Qt.CheckState.Checked:
                            visible_classes.add(class_code)
                else:
                    # No checkbox widget - check item itself
                    if hasattr(code_item, 'checkState'):
                        from PySide6.QtCore import Qt
                        if code_item.checkState() == Qt.CheckState.Checked:
                            visible_classes.add(class_code)
            except Exception as e:
                continue
        
        return visible_classes


    def _find_downsample_indices(self, full_count, target_count):
        """Find downsample indices from various sources"""
        
        # Try common attribute names
        for attr_name in ['downsample_indices', 'displayed_indices', 'visible_indices',
                        'current_indices', 'vtk_indices', 'display_mask', 'shown_indices']:
            if hasattr(self.app, attr_name):
                indices = getattr(self.app, attr_name)
                if indices is not None and isinstance(indices, np.ndarray):
                    # Direct index array
                    if len(indices) == target_count:
                        print(f"   ✅ Found: app.{attr_name}")
                        return indices
                    # Boolean mask
                    elif indices.dtype == bool and len(indices) == full_count:
                        import numpy as np
                        idx_array = np.where(indices)[0]
                        if len(idx_array) == target_count:
                            print(f"   ✅ Found: app.{attr_name} (boolean mask)")
                            return idx_array
        
        # Try data object
        if hasattr(self.app, 'data'):
            for attr_name in ['downsample_indices', 'displayed_indices', 'vtk_indices']:
                if hasattr(self.app.data, attr_name):
                    indices = getattr(self.app.data, attr_name)
                    if indices is not None and isinstance(indices, np.ndarray):
                        if len(indices) == target_count:
                            print(f"   ✅ Found: app.data.{attr_name}")
                            return indices
        
        # Last resort: uniform sampling
        print(f"   ⚠️ No stored indices - using UNIFORM sampling")
        import numpy as np
        step = max(1, full_count // target_count)
        return np.arange(0, full_count, step)[:target_count]


    def _save_camera_state(self):
        """Save camera state"""
        if not hasattr(self.app, 'vtk_widget'):
            return None
        if not hasattr(self.app.vtk_widget, 'renderer'):
            return None
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            if camera:
                return {
                    'position': tuple(camera.GetPosition()),
                    'focal_point': tuple(camera.GetFocalPoint()),
                    'view_up': tuple(camera.GetViewUp()),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection(),
                }
        except Exception:
            pass
        return None


    def _restore_camera_state(self, saved_camera):
        """Restore camera state"""
        if not saved_camera:
            return
        
        try:
            camera = self.app.vtk_widget.renderer.GetActiveCamera()
            camera.SetPosition(saved_camera['position'])
            camera.SetFocalPoint(saved_camera['focal_point'])
            camera.SetViewUp(saved_camera['view_up'])
            camera.SetParallelScale(saved_camera['parallel_scale'])
            
            if saved_camera['parallel_projection']:
                camera.ParallelProjectionOn()
            else:
                camera.ParallelProjectionOff()
            
            self.app.vtk_widget.renderer.ResetCameraClippingRange()
        except Exception:
            pass


def make_color_icon(rgb):
    """Create a color icon from RGB tuple"""
    pix = QPixmap(20, 12)
    pix.fill(QColor(*rgb))
    return QIcon(pix)

class InsideFenceDialog(QDialog):
    """
    Inside Fence Conversion Dialog with Height Filtering
    
    🔷 FENCE MODE:
    Converts points inside digitized shapes (fence) from one/multiple classes to another
    Now includes optional height-based filtering!
    
    Workflow:
    1. Draw a shape using Digitize tools (line, rectangle, circle, polygon, freehand)
    2. Select the drawn shape
    3. (Optional) Enable height filter and set height threshold/range
    4. Select multiple "From classes" (points to convert) using Ctrl+Click
    5. Select "To class" (target class)
    6. Click Convert → Points inside fence (and matching height criteria) become To class
    """

    def __init__(self, app, ribbon_parent):
        # ✅ FIX 1: Ensure we have a valid QWidget parent (the Main Window)
        from PySide6.QtWidgets import QWidget
        
        target_parent = None
        if isinstance(app, QWidget):
            target_parent = app
        elif hasattr(app, 'window') and isinstance(app.window, QWidget):
            target_parent = app.window
            
        # Keep dialog owned by the main app window so it stays above Naksha
        # while remaining non-modal and not globally always-on-top.
        # Parentless top-level window:
        # keeps normal taskbar minimize behavior (no floating mini-bar on desktop).
        super().__init__(None, Qt.Window)
        self.setAttribute(Qt.WA_NativeWindow, True)  # Fix: GetDC invalid window handle
        self.setAttribute(Qt.WA_QuitOnClose, False)
        flags = self.windowFlags()
        print(f"\n🔍 ByClassDialog Window Flags Debug:")
        print(f"   Raw flags value: {flags}")
        print(f"   Qt.Window: {Qt.Window}")
        print(f"   Qt.Tool: {Qt.Tool}")
        print(f"   Flags & Qt.Window: {flags & Qt.Window}")
        print(f"   Flags & Qt.Tool: {flags & Qt.Tool}")
        print(f"   Flags & Qt.FramelessWindowHint: {flags & Qt.FramelessWindowHint}")
        print(f"   Target parent type: {type(target_parent)}")
        if target_parent:
            print(f"   Parent window flags: {target_parent.windowFlags()}\n")
        
        # self.setWindowFlags(Qt.Window)
        
        self.setWindowModality(Qt.NonModal)
        
        # Store references
        self.app = app
        self.ribbon_parent = ribbon_parent
        self.selected_fence = None
        self.selected_fences = []
        self.permanent_fence_mode = False
        self._conversion_in_progress = False
        self._conversion_completed = False
        self._fences_from_selection = False
        
        self.setWindowTitle("Inside Fence - Convert Points Within Shape")
        self._selection_highlight_actors = []
        self._hover_highlight_actor = None
        self._classified_fence_actors = []
        try:
            from gui.theme_manager import get_dialog_stylesheet
            self.setStyleSheet(get_dialog_stylesheet())
        except Exception:
            pass

        # self.setStyleSheet(self.naksha_dark_theme()) # Inherits global theme
        self.setGeometry(200, 200, 500, 500)
        
        # ... (rest of your init code: shortcuts, init_ui, connections) ...
        
        # ✅ Keyboard shortcuts
    
        
        self.init_ui()
        self.populate_classes()
        
        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)
        
        # Set strong focus policy
        self.setFocusPolicy(Qt.StrongFocus)
        self._connected_display_dialog = None
        self._connect_display_dialog()

    def _get_display_dialog(self):
        return getattr(self.app, 'display_mode_dialog', getattr(self.app, 'display_dialog', None))

    def _connect_display_dialog(self):
        display_dialog = self._get_display_dialog()
        if display_dialog is None:
            return False
        if display_dialog is self._connected_display_dialog:
            return False

        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass

        try:
            display_dialog.classes_loaded.connect(self.on_classes_changed)
        except Exception as e:
            print(f"⚠️ Could not connect InsideFenceDialog to Display Mode updates: {e}")
            return False

        self._connected_display_dialog = display_dialog
        print("✅ InsideFenceDialog connected to display_mode_dialog.classes_loaded")
        return True
            
            
    def eventFilter(self, obj, event):
        """
        ✅ CRITICAL: Intercept keyboard events BEFORE they reach Digitizer
        Installed on QApplication for global capture when dialog is visible
        
        ✅ FIXED: Now respects undo context priority - classification gets
        priority when active, draw tool only gets undo when classification is NOT active.
        """
        from PySide6.QtCore import QEvent
        
        if event.type() == QEvent.KeyPress:
            key = event.key()
            modifiers = event.modifiers()
            
            # Check for Ctrl+Z (Undo)
            if key == Qt.Key_Z and modifiers == Qt.ControlModifier:
                # ✅ CRITICAL FIX: Check undo context BEFORE passing to digitizer
                # Classification tool should ALWAYS get priority when active
                try:
                    ctx_mgr = get_undo_context_manager(self.app)
                    
                    if ctx_mgr.is_classification_active():
                        print("🔵 InsideFenceDialog: Classification active — NOT intercepting Ctrl+Z")
                        # Let event propagate to classification undo handler
                        return False
                except Exception as e:
                    print(f"⚠️ Undo context check failed: {e}")
                
                # Only pass to digitizer if draw tool OWNS the undo context
                digitizer = getattr(self.app, 'digitizer', None)
                if digitizer and getattr(digitizer, 'enabled', False):
                    undo_stack = getattr(digitizer, 'undo_stack', None)
                    if undo_stack and len(undo_stack) > 0:
                        # Double-check that draw tool owns the context
                        try:
                            ctx_mgr = get_undo_context_manager(self.app)
                            if ctx_mgr._current_context == ctx_mgr.DRAW:
                                print("🔵 InsideFenceDialog: Draw tool owns undo — passing Ctrl+Z to digitizer")
                                digitizer.undo()
                                event.accept()
                                return True
                        except Exception:
                            pass
                
                # Don't intercept - let classification or global shortcut handle it
                return False

            # Check for Ctrl+Y (Redo)
            elif key == Qt.Key_Y and modifiers == Qt.ControlModifier:
                # ✅ CRITICAL FIX: Check undo context BEFORE passing to digitizer
                try:
                    ctx_mgr = get_undo_context_manager(self.app)
                    
                    if ctx_mgr.is_classification_active():
                        print("🔵 InsideFenceDialog: Classification active — NOT intercepting Ctrl+Y")
                        return False
                except Exception as e:
                    print(f"⚠️ Undo context check failed: {e}")
                
                # Only pass to digitizer if draw tool OWNS the undo context
                digitizer = getattr(self.app, 'digitizer', None)
                if digitizer and getattr(digitizer, 'enabled', False):
                    redo_stack = getattr(digitizer, 'redo_stack', None)
                    if redo_stack and len(redo_stack) > 0:
                        try:
                            ctx_mgr = get_undo_context_manager(self.app)
                            if ctx_mgr._current_context == ctx_mgr.DRAW:
                                print("🔵 InsideFenceDialog: Draw tool owns redo — passing Ctrl+Y to digitizer")
                                digitizer.redo()
                                event.accept()
                                return True
                        except Exception:
                            pass
                
                return False
        
        return super().eventFilter(obj, event)


    def keyPressEvent(self, event):
        """Fallback: Handle keyboard shortcuts directly on dialog
        
        ✅ FIXED: Now respects undo context priority - classification gets
        priority when active, draw tool only gets undo when classification is NOT active.
        """
        if event.modifiers() == Qt.ControlModifier:
            if event.key() == Qt.Key_Z:
                # ✅ CRITICAL FIX: Check undo context BEFORE handling
                try:
                    ctx_mgr = get_undo_context_manager(self.app)
                    
                    if ctx_mgr.is_classification_active():
                        print("🔵 InsideFenceDialog: Classification active — ignoring Ctrl+Z (fallback)")
                        event.ignore()  # Let it propagate
                        return
                except Exception:
                    pass
                
                # Only handle if draw tool owns undo context
                digitizer = getattr(self.app, 'digitizer', None)
                if digitizer and getattr(digitizer, 'enabled', False):
                    undo_stack = getattr(digitizer, 'undo_stack', None)
                    if undo_stack and len(undo_stack) > 0:
                        try:
                            ctx_mgr = get_undo_context_manager(self.app)
                            if ctx_mgr._current_context == ctx_mgr.DRAW:
                                print("🔵 InsideFenceDialog: Draw tool owns undo — Ctrl+Z (fallback)")
                                digitizer.undo()
                                event.accept()
                                return
                        except Exception:
                            pass
                
                event.ignore()
                return
                
            elif event.key() == Qt.Key_Y:
                # ✅ CRITICAL FIX: Check undo context BEFORE handling
                try:
                    ctx_mgr = get_undo_context_manager(self.app)
                    
                    if ctx_mgr.is_classification_active():
                        print("🔵 InsideFenceDialog: Classification active — ignoring Ctrl+Y (fallback)")
                        event.ignore()
                        return
                except Exception:
                    pass
                
                # Only handle if draw tool owns undo context
                digitizer = getattr(self.app, 'digitizer', None)
                if digitizer and getattr(digitizer, 'enabled', False):
                    redo_stack = getattr(digitizer, 'redo_stack', None)
                    if redo_stack and len(redo_stack) > 0:
                        try:
                            ctx_mgr = get_undo_context_manager(self.app)
                            if ctx_mgr._current_context == ctx_mgr.DRAW:
                                print("🔵 InsideFenceDialog: Draw tool owns redo — Ctrl+Y (fallback)")
                                digitizer.redo()
                                event.accept()
                                return
                        except Exception:
                            pass
                
                event.ignore()
                return
        
        super().keyPressEvent(event)
        
        
    def _restore_highlights_from_data(self):
        """Rebuilds blue highlight actors directly from the selected_fences array"""
        try:
            # Clear old garbage
            if hasattr(self, '_selection_highlight_actors'):
                for actor in self._selection_highlight_actors:
                    try: self.app.vtk_widget.renderer.RemoveViewProp(actor)
                    except Exception: pass
            self._selection_highlight_actors = []

            if not hasattr(self, 'selected_fences') or not self.selected_fences:
                return

            import vtk
            for shape in self.selected_fences:
                coords = shape.get('coords', [])
                if not coords: continue

                # Build the blue highlight actor
                if hasattr(self.app, 'digitizer'):
                    highlight_actor = self.app.digitizer._make_polyline_actor(
                        coords, color=(0, 0.5, 1), width=5
                    )
                else:
                    # Fallback
                    points = vtk.vtkPoints()
                    for c in coords: points.InsertNextPoint(c)
                    line = vtk.vtkPolyLine()
                    line.GetPointIds().SetNumberOfIds(len(coords))
                    for i in range(len(coords)): line.GetPointIds().SetId(i, i)
                    cells = vtk.vtkCellArray()
                    cells.InsertNextCell(line)
                    polydata = vtk.vtkPolyData()
                    polydata.SetPoints(points)
                    polydata.SetLines(cells)
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(polydata)
                    highlight_actor = vtk.vtkActor()
                    highlight_actor.SetMapper(mapper)
                    highlight_actor.GetProperty().SetColor(0, 0.5, 1) 
                    highlight_actor.GetProperty().SetLineWidth(5)

                self.app.vtk_widget.renderer.AddActor(highlight_actor)
                self._selection_highlight_actors.append(highlight_actor)

            self._safe_main_render()
            print(f"🔵 Restored {len(self._selection_highlight_actors)} fence highlights")
            
        except Exception as e:
            print(f"⚠️ Failed to restore highlights: {e}")


    def showEvent(self, event):
        """Grab keyboard when dialog becomes visible and restore visuals"""
        super().showEvent(event)
        if self._connect_display_dialog():
            self.on_classes_changed()
        # self.grabKeyboard()
        print("⌨️ InsideFenceDialog: Grabbed keyboard")

        if hasattr(self, 'select_fence_btn'):
            self.select_fence_btn.setVisible(True)
        if hasattr(self, 'clear_fence_btn'):
            self.clear_fence_btn.setVisible(True)
        
        # Prune stale fences before restoring (catches any that slipped through cleanup)
        if hasattr(self, '_prune_stale_fences'):
            try:
                self._prune_stale_fences()
            except Exception:
                pass

        # ✅ BULLETPROOF: Restore visual highlights if data exists
        if hasattr(self, 'selected_fences') and self.selected_fences:
            print(f"🔄 Restoring {len(self.selected_fences)} existing fence highlights...")
            # We must trick the list widget into redrawing them
            if hasattr(self, '_highlight_selected_fences_in_3d'):
                # We don't have the QListWidget items here, so we manually call the core logic
                self._restore_highlights_from_data()

    def hideEvent(self, event):
        """Release keyboard when dialog is hidden"""
        # self.releaseKeyboard()
        super().hideEvent(event)
        print("⌨️ InsideFenceDialog: Released keyboard")


    def closeEvent(self, event):
        """Clean up event filter, highlights, and keyboard grab when dialog closes"""
        print("🧹 InsideFenceDialog closing - cleaning up...")
        
        # Close any open sub-dialog
        if hasattr(self, '_fence_selection_dialog') and self._fence_selection_dialog is not None:
            try:
                self._fence_selection_dialog.close()
            except (RuntimeError, ReferenceError):
                pass
            self._fence_selection_dialog = None
            
        # ✅ BULLETPROOF: Actually remove the event filter so we don't hijack keys
        try:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().removeEventFilter(self)
            print("   ✅ Global event filter removed")
        except Exception as e:
            print(f"   ⚠️ Event filter removal failed: {e}")

        # Release keyboard grab
        #     self.releaseKeyboard()
        
        # ✅ BULLETPROOF: Use RemoveViewProp for hover highlight
        if hasattr(self, '_hover_highlight_actor') and self._hover_highlight_actor:
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(self._hover_highlight_actor)
                self._hover_highlight_actor = None
                print("   ✅ Hover highlight removed")
            except Exception as e:
                print(f"   ⚠️ Hover highlight removal failed: {e}")
        
        # ✅ BULLETPROOF: Use RemoveViewProp for selection highlights
        if hasattr(self, '_selection_highlight_actors'):
            try:
                for actor in self._selection_highlight_actors:
                    self.app.vtk_widget.renderer.RemoveViewProp(actor) 
                self._selection_highlight_actors = []
                print("   ✅ Selection highlights removed")
            except Exception as e:
                print(f"   ⚠️ Selection highlights removal failed: {e}")
        
        # Refresh view
        if self._safe_main_render():
            print("   ✅ View refreshed")
        else:
            print("   ⚠️ View refresh skipped (render window not ready)")
        
        print("✅ InsideFenceDialog cleanup complete")
        
        # Disconnect display dialog signal
        if self._connected_display_dialog is not None:
            try:
                self._connected_display_dialog.classes_loaded.disconnect(self.on_classes_changed)
            except Exception:
                pass
            self._connected_display_dialog = None

        self.selected_fences = []
        self.permanent_fence_mode = False

        # Call parent closeEvent
        super().closeEvent(event)

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        body = layout

        self.info_banner = QLabel("Convert points inside the selected fence.")
        self.info_banner.setStyleSheet("font-size: 9pt; color: gray;")
        body.addWidget(self.info_banner)

        # Draw or Select Fence
        self.fence_count_badge = QLabel() # kept for compatibility
        fence_title = QLabel("Draw or Select Fence:")
        fence_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(fence_title)

        btn_row = QHBoxLayout()
        self.select_fence_btn = QPushButton("Select Fence")
        _set_compact_button_role(self.select_fence_btn, "primary")
        self.select_fence_btn.clicked.connect(self.select_fence)

        self.clear_fence_btn = QPushButton("Clear All")
        _set_compact_button_role(self.clear_fence_btn)
        self.clear_fence_btn.clicked.connect(self.clear_fence_selection)
        btn_row.addWidget(self.select_fence_btn, 3)
        btn_row.addWidget(self.clear_fence_btn, 1)
        body.addLayout(btn_row)

        self.fence_status = QLabel()
        _set_dialog_status(self.fence_status, "No fence selected", "neutral")

        # Height Filter
        height_title = QLabel("Height Filter:")
        height_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(height_title)

        self.height_filter_enabled = QCheckBox("Enable Height Filter")
        self.height_filter_enabled.toggled.connect(self.on_height_filter_toggled)
        body.addWidget(self.height_filter_enabled)

        self.height_filter_container = QWidget()
        h_layout = QVBoxLayout(self.height_filter_container)
        h_layout.setContentsMargins(0, 0, 0, 0)
        h_layout.setSpacing(4)

        self.height_mode_combo = QComboBox()
        self.height_mode_combo.addItem("Within Range", "within")
        self.height_mode_combo.addItem("Above Height", "above")
        self.height_mode_combo.addItem("Below Height", "below")
        self.height_mode_combo.currentIndexChanged.connect(self.on_height_mode_changed)
        h_layout.addWidget(self.height_mode_combo)

        min_row = QHBoxLayout()
        self.min_height_label = QLabel("Min:")
        self.min_height_label.setObjectName("dialogCaption")
        self.min_height_spin = QDoubleSpinBox()
        self.min_height_spin.setRange(-1000, 10000)
        self.min_height_spin.setDecimals(2)
        self.min_height_spin.setSingleStep(1.0)
        self.min_height_spin.setValue(0.0)
        self.min_height_spin.setSuffix(" m")
        min_row.addWidget(self.min_height_label)
        min_row.addWidget(self.min_height_spin)
        h_layout.addLayout(min_row)

        max_row = QHBoxLayout()
        self.max_height_label = QLabel("Max:")
        self.max_height_label.setObjectName("dialogCaption")
        self.max_height_spin = QDoubleSpinBox()
        self.max_height_spin.setRange(-1000, 10000)
        self.max_height_spin.setDecimals(2)
        self.max_height_spin.setSingleStep(1.0)
        self.max_height_spin.setValue(100.0)
        self.max_height_spin.setSuffix(" m")
        max_row.addWidget(self.max_height_label)
        max_row.addWidget(self.max_height_spin)
        h_layout.addLayout(max_row)

        self.height_stats_label = QLabel("Enable height filter and select classes to analyze")
        self.height_stats_label.setObjectName("dialogCaption")
        h_layout.addWidget(self.height_stats_label)

        analyze_btn = QPushButton("Auto-Analyze Heights")
        _set_compact_button_role(analyze_btn)
        analyze_btn.clicked.connect(self.auto_analyze_heights)
        h_layout.addWidget(analyze_btn)

        self.height_filter_container.setVisible(False)
        body.addWidget(self.height_filter_container)

        # Select Classes
        classes_title = QLabel("Select Classes:")
        classes_title.setStyleSheet("font-weight: bold; font-size: 9.5pt; margin-top: 6px;")
        body.addWidget(classes_title)

        self.selected_classes_label = QLabel()
        _set_dialog_status(self.selected_classes_label, "Selected: None", "neutral")
        body.addWidget(self.selected_classes_label)

        from_lbl = QLabel("From class (Ctrl+Click for multiple):")
        from_lbl.setObjectName("dialogCaption")
        body.addWidget(from_lbl)

        self.from_list = QListWidget()
        self.from_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.from_list.setMinimumHeight(90)
        self.from_list.setStyleSheet("QListWidget::item { border-bottom: none; padding: 4px; }")
        self.from_list.itemSelectionChanged.connect(self.on_from_selection_changed)
        body.addWidget(self.from_list)

        clear_sel_btn = QPushButton("Clear Selection")
        _set_compact_button_role(clear_sel_btn)
        clear_sel_btn.clicked.connect(self.clear_from_selection)
        body.addWidget(clear_sel_btn)

        to_lbl = QLabel("Convert to class:")
        to_lbl.setObjectName("dialogCaption")
        body.addWidget(to_lbl)

        self.to_combo = QComboBox()
        self.to_combo.setMinimumHeight(32)
        body.addWidget(self.to_combo)

        self.preview_label = QLabel("Configure the fence and classes, then convert.")
        self.preview_label.setObjectName("dialogCaption")
        self.preview_label.setWordWrap(True)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addWidget(self.preview_label, 1)

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setMinimumWidth(140)
        _set_compact_button_role(self.convert_btn, "primary")
        self.convert_btn.clicked.connect(self.perform_conversion)
        footer.addWidget(self.convert_btn)
        layout.addLayout(footer)

    def on_height_filter_toggled(self, checked):
        """
        ✅ BULLETPROOF UI TOGGLE: 
        .toggled emits a boolean, not an int. Do not compare it to Qt.CheckState.
        """
        is_checked = bool(checked)
        
        print(f"🔍 DEBUG: Height filter toggled! State = {is_checked}")
        
        if hasattr(self, 'height_filter_container'):
            self.height_filter_container.setVisible(is_checked)
            print(f"🔍 DEBUG: Container visibility set to: {is_checked}")
            
            if is_checked:
                self.on_height_mode_changed(0)  # Initialize visibility
        
    def on_height_mode_changed(self, index):
        """Update visibility of height inputs based on mode"""
        mode = self.height_mode_combo.currentData()  # ✅ Now this will work
        
        if mode == "within":
            # Show both min and max
            self.min_height_label.setText("Min:")
            self.min_height_label.setVisible(True)
            self.min_height_spin.setVisible(True)
            self.max_height_label.setVisible(True)
            self.max_height_spin.setVisible(True)
        elif mode == "above":
            # Show only min (as threshold)
            self.min_height_label.setText("Threshold:")
            self.min_height_label.setVisible(True)
            self.min_height_spin.setVisible(True)
            self.max_height_label.setVisible(False)
            self.max_height_spin.setVisible(False)
        elif mode == "below":
            # Show only max (as threshold)
            self.min_height_label.setVisible(False)
            self.min_height_spin.setVisible(False)
            self.max_height_label.setText("Threshold:")
            self.max_height_label.setVisible(True)
            self.max_height_spin.setVisible(True)
    
    def auto_analyze_heights(self):
        """Analyze height distribution of selected classes within fence"""
        if not self.selected_fences:  # ← CHANGED: plural
            QMessageBox.warning(self, "No Fence", "Please select at least one fence first")
            return
        
        # Use first fence for height analysis
        fence_coords = self.selected_fences[0]['coords']  # ← ADD THIS LINE
        
        from_classes = self._get_selected_from_classes()
        if not from_classes:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return
        
        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        
        if classification is None or xyz is None:
            QMessageBox.warning(self, "No Data", "No point cloud data available")
            return
        
        try:
            # Get fence coordinates
          
            if isinstance(fence_coords, list):
                fence_coords = np.array(fence_coords)
            
            # Get From class points
            from_class_mask = np.isin(classification, from_classes)
            from_class_xyz = xyz[from_class_mask]
            
            if len(from_class_xyz) == 0:
                self.height_stats_label.setText("❌ No points found in selected classes")
                return
            
            # Check which points are inside fence
            inside_mask = self._points_inside_polygon(from_class_xyz, fence_coords)
            inside_points = from_class_xyz[inside_mask]
            
            if len(inside_points) == 0:
                self.height_stats_label.setText("❌ No points found inside fence")
                return
            
            # Calculate height statistics
            heights = inside_points[:, 2]
            min_h = np.min(heights)
            max_h = np.max(heights)
            mean_h = np.mean(heights)
            median_h = np.median(heights)
            
            # Update spinboxes with reasonable defaults
            self.min_height_spin.setValue(float(min_h))
            self.max_height_spin.setValue(float(max_h))
            
            # Display statistics
            stats_text = (
                f"📊 Height Analysis ({len(inside_points):,} points inside fence):\n"
                f"Min: {min_h:.2f}m | Max: {max_h:.2f}m\n"
                f"Mean: {mean_h:.2f}m | Median: {median_h:.2f}m"
            )
            
            self.height_stats_label.setText(stats_text)
            self.height_stats_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 9px;
                    font-weight: bold;
                    padding: 4px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
            
            print(f"✅ Height analysis complete: {len(inside_points):,} points")
            print(f"   Min: {min_h:.2f}m, Max: {max_h:.2f}m, Mean: {mean_h:.2f}m")
            
        except Exception as e:
            print(f"❌ Height analysis failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Analysis Failed", str(e))


    def perform_redo(self):
        if not hasattr(self.app, 'redo_classification'):
            return
        
        print("🔄 InsideFenceDialog: Performing CLASSIFICATION redo...")
        
        try:
            # ✅ redo_classification handles ALL refresh internally
            self.app.redo_classification()
            
            # ✅ Only update cross-sections and stats — NO main view refresh
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception:
                        pass

            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()
            
            self.preview_label.setText("↷ Redo performed")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #ff9800;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #3e2723;
                    border-radius: 3px;
                }
            """)
            print("✅ Classification redo performed")
        except Exception as e:
            print(f"❌ Redo failed: {e}")
            QMessageBox.warning(self, "Redo Failed", f"Could not redo: {str(e)}")  
        
    def _clear_fence_highlights(self):
        """Remove selection/hover highlight actors — keep classified (cyan) actors intact"""
        print("   🧹 Clearing fence highlights...")
        
        # Remove hover highlight (yellow)
        if hasattr(self, '_hover_highlight_actor') and self._hover_highlight_actor:
            try:
                self.app.vtk_widget.renderer.RemoveActor(self._hover_highlight_actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(self._hover_highlight_actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(self._hover_highlight_actor)
            except Exception: pass
            self._hover_highlight_actor = None
        
        # Remove selection highlights (blue) only
        if hasattr(self, '_selection_highlight_actors'):
            for actor in self._selection_highlight_actors:
                try:
                    self.app.vtk_widget.renderer.RemoveActor(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveActor2D(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveViewProp(actor)
                except Exception: pass
            self._selection_highlight_actors = []
        # Render to update view
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        
        print("   ✅ Fence highlights cleared")
    
    
    def select_fence(self):
        """Allow user to select fences from digitize manager AND curve tool."""
        print("\n" + "=" * 60)
        print("🔷 SELECT_FENCE() called")
        print("=" * 60)

        self._conversion_completed = False
        self._fences_from_selection = False

        if getattr(self, '_fence_selection_dialog', None) is not None:
            try:
                self._fence_selection_dialog.close()
            except Exception:
                pass
            self._fence_selection_dialog = None
            return

        if hasattr(self, "select_fence_btn"):
            self.select_fence_btn.setVisible(True)
        if hasattr(self, "clear_fence_btn"):
            self.clear_fence_btn.setVisible(True)

        _show_fence_selection_dialog(
            self,
            "Select Fence(s)",
            "Select one or more fences to use for conversion.",
        )

    def clear_fence_selection(self):
        """Clear all selected fences"""
        self._conversion_completed = False
        self.selected_fences = []
        self.permanent_fence_mode = False
        self._fences_from_selection = False

        if hasattr(self, 'select_fence_btn'):
            self.select_fence_btn.setVisible(True)
        if hasattr(self, 'clear_fence_btn'):
            self.clear_fence_btn.setVisible(True)
        
        _set_dialog_status(self.fence_status, "No fence selected", "neutral")
        
        # ✅ ADDED: Clear visual highlights too
        self._clear_fence_highlights()
        self.update_fence_display() 
        print("🗑️ Cleared all fence selections and highlights")
        
    def _highlight_fence_on_hover(self, item):
        """Temporarily highlight fence in 3D view when hovering over it in list"""
        if not item:
            return
        
        try:
            shape = item.data(Qt.UserRole)
            coords = shape.get('coords', [])
            
            # Remove previous hover highlight
            if hasattr(self, '_hover_highlight_actor') and self._hover_highlight_actor:
                try:
                    self.app.vtk_widget.renderer.RemoveActor(self._hover_highlight_actor)
                except Exception:
                    pass
            
            # Create temporary yellow highlight
            
            # Use digitizer's method if available, otherwise create inline
            if hasattr(self.app, 'digitizer'):
                self._hover_highlight_actor = self.app.digitizer._make_polyline_actor(
                    coords,
                    color=(1, 1, 0),  # Yellow
                    width=6
                )
            else:
                # Fallback: create basic actor
                import vtk
                points = vtk.vtkPoints()
                for c in coords:
                    points.InsertNextPoint(c)
                
                line = vtk.vtkPolyLine()
                line.GetPointIds().SetNumberOfIds(len(coords))
                for i in range(len(coords)):
                    line.GetPointIds().SetId(i, i)
                
                cells = vtk.vtkCellArray()
                cells.InsertNextCell(line)
                
                polydata = vtk.vtkPolyData()
                polydata.SetPoints(points)
                polydata.SetLines(cells)
                
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(polydata)
                
                self._hover_highlight_actor = vtk.vtkActor()
                self._hover_highlight_actor.SetMapper(mapper)
                self._hover_highlight_actor.GetProperty().SetColor(1, 1, 0)
                self._hover_highlight_actor.GetProperty().SetLineWidth(6)
            
            self.app.vtk_widget.renderer.AddActor(self._hover_highlight_actor)
            self.app.vtk_widget.render()
            
        except Exception as e:
            print(f"⚠️ Hover highlight failed: {e}")  
            
            
    def update_fence_display(self):
        """Update the fence status display and button text."""
        count = len(self.selected_fences) if self.selected_fences else 0
        if hasattr(self, "fence_count_badge") and self.fence_count_badge:
            self.fence_count_badge.setText(str(count))
            
        if hasattr(self, "select_fence_btn") and self.select_fence_btn:
            if count > 0:
                self.select_fence_btn.setText(f"Select Fence ({count})")
            else:
                self.select_fence_btn.setText("Select Fence")

    def _points_inside_polygon(self, points, polygon_coords):
        from matplotlib.path import Path
        if isinstance(polygon_coords, list):
            poly_xy = np.array([(c[0], c[1]) for c in polygon_coords])
        else:
            poly_xy = polygon_coords[:, :2]
        points_xy = points[:, :2]
        min_x, min_y = np.min(poly_xy, axis=0)
        max_x, max_y = np.max(poly_xy, axis=0)
        bbox_mask = (points_xy[:, 0] >= min_x) & (points_xy[:, 0] <= max_x) & \
                    (points_xy[:, 1] >= min_y) & (points_xy[:, 1] <= max_y)
        inside = np.zeros(len(points), dtype=bool)
        if np.any(bbox_mask):
            poly_path = Path(poly_xy)
            inside[bbox_mask] = poly_path.contains_points(points_xy[bbox_mask])
        return inside

    def _calculate_conversion(self, from_classes, to_class, preview=False):
        """Calculate and optionally apply conversion for InsideFenceDialog"""
        classification = self.app.data.get("classification")
        xyz = self.app.data.get("xyz")
        
        if classification is None or xyz is None:
            return None
            
        # Get From class mask
        if from_classes is None:
            from_class_mask = (classification != to_class)
        else:
            from_class_mask = np.isin(classification, from_classes)
            
        from_class_count = np.sum(from_class_mask)
        if from_class_count == 0:
            return 0
            
        # Get indices and coordinates of eligible points
        from_class_indices = np.where(from_class_mask)[0]
        from_class_xyz = xyz[from_class_mask]
        
        # 1. FENCE FILTER
        # Check which points are inside any of the selected fences
        combined_inside = np.zeros(len(from_class_xyz), dtype=bool)
        for fence in self.selected_fences:
            fence_coords = fence['coords']
            inside = self._points_inside_polygon(from_class_xyz, fence_coords)
            combined_inside |= inside
            
        if not np.any(combined_inside):
            return 0
            
        # Filter down indices and coordinates to only those inside the fence
        inside_indices = from_class_indices[combined_inside]
        inside_xyz = from_class_xyz[combined_inside]
        
        # 2. HEIGHT FILTER
        if self.height_filter_enabled.isChecked():
            mode = self.height_mode_combo.currentData()
            heights = inside_xyz[:, 2]
            
            if mode == "within":
                min_h = self.min_height_spin.value()
                max_h = self.max_height_spin.value()
                height_mask = (heights >= min_h) & (heights <= max_h)
            elif mode == "above":
                min_h = self.min_height_spin.value()
                height_mask = heights >= min_h
            elif mode == "below":
                max_h = self.max_height_spin.value()
                height_mask = heights <= max_h
            else:
                height_mask = np.ones(len(heights), dtype=bool)
                
            if not np.any(height_mask):
                return 0
                
            final_convert_indices = inside_indices[height_mask]
        else:
            final_convert_indices = inside_indices
            
        converted_count = len(final_convert_indices)
        if converted_count == 0:
            return 0
            
        if preview:
            return converted_count
            
        # Create final mask for shader update
        final_mask = np.zeros(len(classification), dtype=bool)
        final_mask[final_convert_indices] = True
        
        old_classes = classification[final_mask].copy()
        
        # Perform change in-place
        classification[final_mask] = to_class
        self.app._just_did_conversion = True
        
        # Save undo
        new_classes_arr = np.full(np.sum(final_mask), to_class, dtype=classification.dtype)
        undo_step = {
            "mask": final_mask.copy(),
            "oldclasses": old_classes,
            "old_classes": old_classes,
            "newclasses": new_classes_arr,
            "new_classes": new_classes_arr
        }
        
        if hasattr(self.app, 'undostack'):
            self.app.undostack.append(undo_step)
        elif hasattr(self.app, 'undo_stack'):
            self.app.undo_stack.append(undo_step)
            
        if hasattr(self.app, 'redostack'):
            self.app.redostack.clear()
        elif hasattr(self.app, 'redo_stack'):
            self.app.redo_stack.clear()
            
        from gui.memory_manager import trim_undo_stack
        trim_undo_stack(self.app)
        
        # Update VTK actors in-place
        from gui.unified_actor_manager import fast_classify_update
        fast_classify_update(self.app, final_mask, int(to_class))
        
        # Emit classification_finished to sync section views
        try:
            self.app.classification_finished.emit(final_mask)
        except Exception:
            pass
            
        return converted_count, final_mask

    def add_fences_from_selection(self, fence_shapes):
        """Accept a list of drawing dicts and add them as fences.

        Called by SelectionModeDialog._on_add_to_byclass() when the user
        selects drawings and clicks 'Add to By Class'.  Each shape dict
        must have at least a 'coords' key with a list/array of (x,y) or
        (x,y,z) points.

        Duplicate detection is based on the shape's Python id() to avoid
        re-adding the same drawing twice.

        When fences come from the selection tool, the Select Fence(s) and
        Clear All buttons are hidden so the user cannot accidentally
        re-enter the fence picker (which would cause undo/clear issues).
        """
        if not fence_shapes:
            return

        existing_ids = set()
        for f in self.selected_fences:
            if f.get('source') == 'curve_tool':
                existing_ids.add(id(f.get('curve_data', f)))
            else:
                existing_ids.add(id(f))

        added = 0
        for shape in fence_shapes:
            coords = shape.get('coords', [])
            if not coords:
                continue

            fence_id = id(shape)
            if fence_id in existing_ids:
                continue

            s = dict(shape)
            if s.get('type') in [
                    'line', 'smart_line', 'polyline', 'smartline', 'curve']:
                c = s.get('coords', [])
                if isinstance(c, list):
                    c = np.array(c)
                if len(c) > 0 and not np.array_equal(c[0], c[-1]):
                    s['coords'] = (np.vstack([c, c[0]])
                                   if isinstance(c, np.ndarray)
                                   else list(c) + [list(c[0])])

            self.selected_fences.append(s)
            existing_ids.add(fence_id)
            added += 1

        self._fences_from_selection = True

        # Keep manual fence picker visible so user can select/clear fences
        pass

        self.update_fence_display()

        if hasattr(self, '_restore_highlights_from_data'):
            try:
                self._restore_highlights_from_data()
            except Exception:
                pass

        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

        if added:
            total = len(self.selected_fences)
            print(f"🔷 Added {added} fence(s) from selection ({total} total)")
            _set_dialog_status(
                self.fence_status,
                f"{added} fence(s) added from selection ({total} total)",
                "success",
            )
            
            
    def _highlight_selected_fences_in_3d(self, fence_list):
        """Highlight selected fences in blue in the 3D view"""
        try:
            # Remove old selection highlights
            if hasattr(self, '_selection_highlight_actors'):
                for actor in self._selection_highlight_actors:
                    try:
                        self.app.vtk_widget.renderer.RemoveActor(actor)
                    except Exception:
                        pass
            
            self._selection_highlight_actors = []
            
            # Get selected items
            selected_items = fence_list.selectedItems()
            
            if not selected_items:
                self.app.vtk_widget.render()
                return
            
            # Create blue highlight for each selected fence
            for item in selected_items:
                shape = item.data(Qt.UserRole)
                coords = shape.get('coords', [])
                
                # Create blue highlight actor
                if hasattr(self.app, 'digitizer'):
                    highlight_actor = self.app.digitizer._make_polyline_actor(
                        coords,
                        color=(0, 0.5, 1),  # Blue
                        width=5
                    )
                else:
                    # Fallback: create basic actor
                    import vtk
                    points = vtk.vtkPoints()
                    for c in coords:
                        points.InsertNextPoint(c)
                    
                    line = vtk.vtkPolyLine()
                    line.GetPointIds().SetNumberOfIds(len(coords))
                    for i in range(len(coords)):
                        line.GetPointIds().SetId(i, i)
                    
                    cells = vtk.vtkCellArray()
                    cells.InsertNextCell(line)
                    
                    polydata = vtk.vtkPolyData()
                    polydata.SetPoints(points)
                    polydata.SetLines(cells)
                    
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(polydata)
                    
                    highlight_actor = vtk.vtkActor()
                    highlight_actor.SetMapper(mapper)
                    highlight_actor.GetProperty().SetColor(0, 0.5, 1)  # Blue
                    highlight_actor.GetProperty().SetLineWidth(5)
                
                self.app.vtk_widget.renderer.AddActor(highlight_actor)
                self._selection_highlight_actors.append(highlight_actor)
            
            self.app.vtk_widget.render()
            print(f"🔵 Highlighted {len(selected_items)} selected fence(s) in blue")
            
        except Exception as e:
            print(f"⚠️ Selection highlight failed: {e}")

    # ==================== UNDO/REDO ====================
    def perform_undo(self):
        if getattr(self, '_undo_in_progress', False):
            return
        
        self._undo_in_progress = True
        try:
            if not hasattr(self.app, 'undo_classification'):
                return
            
            print("🔄 InsideFenceDialog: Performing CLASSIFICATION undo...")
            
            # ✅ undo_classification handles ALL refresh internally
            # Do NOT trigger any additional refresh after this call
            self.app.undo_classification()
            
            # ✅ Only update cross-sections and stats — NO main view refresh
            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception:
                        pass

            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()
            
            self.preview_label.setText("↶ Undo performed")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #ff9800;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 6px;
                    background-color: #3e2723;
                    border-radius: 3px;
                }
            """)
            print("✅ Classification undo performed")
        except Exception as e:
            print(f"❌ Undo failed: {e}")
            QMessageBox.warning(self, "Undo Failed", f"Could not undo: {str(e)}")
        finally:
            self._undo_in_progress = False

    # ==================== CLASS MANAGEMENT ====================

    def populate_classes(self):
        """Populate class lists from Display Mode"""
        print(f"\n🔄 Populating InsideFenceDialog classes...")
        
        self.from_list.clear()
        self.to_combo.clear()
        class_list = _collect_conversion_classes(self.app)
        
        if not class_list:
            print("⚠️ No classes found")
            return
        
        # Populate From list
        any_item = QListWidgetItem("Any Class")
        any_item.setData(Qt.UserRole, None)
        self.from_list.addItem(any_item)

        for cls in class_list:
            code = cls['code']
            lvl = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            
            icon = self.make_color_icon(color)
            
            item = QListWidgetItem(icon, text)
            item.setData(Qt.UserRole, code)
            self.from_list.addItem(item)
        
        # Populate To combo
        for cls in class_list:
            code = cls['code']
            lvl = cls['lvl']
            desc = cls['desc']
            color = cls['color']
            
            text = f"{code} - {lvl}" if lvl and lvl.strip() else f"{code}"
            if desc:
                text += f" ({desc})"
            
            icon = self.make_color_icon(color)
            self.to_combo.addItem(icon, text, code)
        
        print(f"✅ Populated InsideFenceDialog with {len(class_list)} classes")

    def on_from_selection_changed(self):
        """Update preview when From selection changes"""
        selected_items = self.from_list.selectedItems()
        if selected_items:
            codes = [item.data(Qt.UserRole) for item in selected_items]
            if any(code is None for code in codes):
                _set_dialog_status(self.selected_classes_label, "Selected From: Any Class", "success")
            else:
                _set_dialog_status(
                    self.selected_classes_label,
                    f"Selected From: {', '.join(map(str, codes))}",
                    "success",
                )
        else:
            _set_dialog_status(self.selected_classes_label, "Selected: None", "neutral")

    def clear_from_selection(self):
        """Clear all From class selections"""
        self.from_list.clearSelection()
        self.on_from_selection_changed()

    def _get_selected_from_classes(self):
        """Get list of selected From class codes"""
        selected_items = self.from_list.selectedItems()
        if not selected_items:
            return []
        codes = [item.data(Qt.UserRole) for item in selected_items]
        if any(code is None for code in codes):
            return None
        return codes

    def on_classes_changed(self):
        """Preserve selections when classes change"""
        print("\n" + "="*60)
        print("🔄 INSIDE FENCE DIALOG: Detected PTC change from Display Mode")
        print("="*60)
        
        # Save current selections
        old_from_classes = self._get_selected_from_classes()
        old_to = self.to_combo.currentData()
        
        # ✅ BUG FIX: You were using the deprecated single-fence variable!
        has_fences = len(getattr(self, 'selected_fences', [])) > 0 
        
        print(f"   📋 Saving selections:")
        print(f"     From: {old_from_classes}")
        print(f"     To: {old_to}")
        print(f"     Fence: {'Selected' if has_fences else 'None'}")
        
        # Rebuild lists
        self.populate_classes()
        
        # Restore To selection
        restored_count = 0
        if old_to is not None:
            idx = self.to_combo.findData(old_to)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)
                print(f"     ✅ Restored To: Class {old_to}")
                restored_count += 1
        
        # Restore From selections
        if old_from_classes is None:
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                if item.data(Qt.UserRole) is None:
                    item.setSelected(True)
                    restored_count += 1
                    break
            print("     ✅ Restored From: Any Class")
        elif old_from_classes:
            for code in old_from_classes:
                for i in range(self.from_list.count()):
                    item = self.from_list.item(i)
                    if item.data(Qt.UserRole) == code:
                        item.setSelected(True)
                        restored_count += 1
                        break
            print(f"     ✅ Restored {len(old_from_classes)} From classes")
        
        if has_fences:
            print(f"     ✅ Fence state preserved")
            restored_count += 1

        # ✅ BULLETPROOF FIX: The Display Mode wiped the VTK Renderer. 
        # We MUST force the Digitizer to push the shapes back to the GPU!
        if hasattr(self.app, 'digitizer'):
            print("   🛠️ Restoring physical fences to VTK view...")
            self.app.digitizer.rebind_drawings()
            
        # ✅ Restore the blue selection highlights on top of the fences
        if hasattr(self, '_restore_highlights_from_data'):
            print("   🛠️ Restoring blue selection highlights...")
            self._restore_highlights_from_data()
            
        print(f"✅ InsideFenceDialog updated ({restored_count} settings restored)")
        print("="*60 + "\n")
        
        self.on_from_selection_changed()
        if hasattr(self.app, 'statusBar'):
            self.app.statusBar().showMessage(
                "✅ Fences and classes restored after display update",
                2000
            )
    # ==================== PREVIEW & CONVERSION ====================

    def preview_conversion(self):
        """Preview how many points would be converted"""
        # ✅ CHANGED: Check plural fences
        if not self.selected_fences:
            QMessageBox.warning(self, "No Fence", "Please select at least one fence first")
            return
        
        from_classes = self._get_selected_from_classes()
        to_class = self.to_combo.currentData()
        
        if from_classes == []:
            QMessageBox.warning(self, "No Selection", "Please select at least one From class")
            return
        
        if to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select To class")
            return
        
        if from_classes is not None and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", 
                            "To class cannot be one of the selected From classes")
            return
        
        try:
            affected_count = self._calculate_conversion(
                from_classes, to_class, preview=True
            )
            
            if affected_count is None:
                return
            
            # ✅ CHANGED: Build message for multiple fences
            fence_count = len(self.selected_fences)
            fence_types = [f['type'] for f in self.selected_fences]
            fence_desc = f"{fence_count} fence(s)" if fence_count > 1 else self.selected_fences[0]['type']
            
            classes_str = "Any Class" if from_classes is None else ", ".join(map(str, from_classes))
            
            preview_msg = f"📊 Preview: {affected_count:,} points would be converted\n"
            preview_msg += f"(Classes {classes_str} inside {fence_desc}"
            
            if self.height_filter_enabled.isChecked():
                mode = self.height_mode_combo.currentData()
                if mode == "within":
                    preview_msg += f", heights {self.min_height_spin.value():.2f}m - {self.max_height_spin.value():.2f}m"
                elif mode == "above":
                    preview_msg += f", heights > {self.min_height_spin.value():.2f}m"
                elif mode == "below":
                    preview_msg += f", heights < {self.max_height_spin.value():.2f}m"
            
            preview_msg += ")"
            
            self.preview_label.setText(preview_msg)
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #2196f3;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1a237e;
                    border-radius: 3px;
                }
            """)
            
        except Exception as e:
            print(f"❌ Preview failed: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Preview Failed", str(e))

    def perform_conversion(self):
        """
        ✅ FIXED: Fast GPU injection when target class is visible (~100ms)
        Only rebuilds mesh when target class is HIDDEN from shading.
        """
        if self._conversion_in_progress:
            QMessageBox.information(
                self,
                "Conversion Running",
                "A fence conversion is already running. Please wait for it to finish."
            )
            return

        if not self.selected_fences:
            QMessageBox.warning(self, "No Fence", "Please select at least one fence first")
            return
        
        from_classes = self._get_selected_from_classes()
        to_class = self.to_combo.currentData()
        
        if from_classes == [] or to_class is None:
            QMessageBox.warning(self, "No Selection", "Please select both From and To classes")
            return
        
        if from_classes is not None and to_class in from_classes:
            QMessageBox.warning(self, "Invalid Selection", "To class cannot be in From classes")
            return

        fence_desc = f"{len(self.selected_fences)} fence(s)" if len(self.selected_fences) > 1 else self.selected_fences[0]['type']
        msg = f"Convert points inside {fence_desc} to class {to_class}?"
        
        if QMessageBox.question(self, "Confirm", msg, QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return

        self._conversion_in_progress = True
        if hasattr(self, "convert_btn") and self.convert_btn:
            self.convert_btn.setEnabled(False)

        try:
            result = self._calculate_conversion(from_classes, to_class, preview=False)
            
            if not result:
                QMessageBox.information(self, "No Points", "No points found inside the fence criteria.")
                return

            if isinstance(result, tuple):
                converted_count, final_mask = result
            else:
                converted_count = result
                final_mask = None

            if not converted_count:
                return

            # ════════════════════════════════════════════════════════════
            # REFRESH LOGIC
            # ════════════════════════════════════════════════════════════
            display_mode = getattr(self.app, 'display_mode', None)
            
            if display_mode == "shaded_class" and final_mask is not None:
                import time
                t0 = time.perf_counter()
                
                # ✅ Get ACTUAL shading visibility (what the mesh was built with)
                shading_vis = None
                try:
                    from gui.shading_display import get_cache
                    cache = get_cache()
                    shading_vis = getattr(cache, 'visible_classes_set', None)
                    if shading_vis is not None and len(shading_vis) > 0:
                        shading_vis = shading_vis.copy()
                except Exception:
                    pass
                
                if shading_vis is None or len(shading_vis) == 0:
                    shading_vis = getattr(self.app, '_shading_visible_classes', None)
                    if shading_vis is not None and len(shading_vis) > 0:
                        shading_vis = shading_vis.copy()
                
                if shading_vis is None or len(shading_vis) == 0:
                    shading_vis = {
                        int(c) for c, e in self.app.class_palette.items()
                        if e.get("show", True)
                    }
                
                target_visible = int(to_class) in shading_vis
                source_visible = (
                    any(int(fc) in shading_vis for fc in from_classes)
                    if from_classes is not None else None
                )
                
                print(f"⚡ SHADING MODE: target={to_class} visible={target_visible}, "
                    f"source visible={source_visible}")
                
                if from_classes is None:
                    print("🔄 Any Class selected — rebuilding shaded mesh")
                    self._shading_force_rebuild(shading_vis)

                elif target_visible and source_visible:
                    # ══════════════════════════════════════════════════
                    # FAST PATH: Both source and target in mesh → color swap only
                    # ══════════════════════════════════════════════════
                    try:
                        from gui.shading_display import refresh_shaded_after_classification_fast
                        refresh_shaded_after_classification_fast(self.app, changed_mask=final_mask)
                        elapsed = (time.perf_counter() - t0) * 1000
                        print(f"⚡ Fast GPU injection: {elapsed:.0f}ms")
                    except Exception as e:
                        print(f"⚠️ Fast injection failed ({e}), rebuilding...")
                        self._shading_force_rebuild(shading_vis)
                
                elif not target_visible and source_visible:
                    # ══════════════════════════════════════════════════
                    # REBUILD: Source was visible, target is hidden
                    # Points must be REMOVED from mesh geometry
                    # ══════════════════════════════════════════════════
                    print(f"🙈 Target class {to_class} HIDDEN — must rebuild to remove geometry")
                    self._shading_force_rebuild(shading_vis)
                
                elif target_visible and not source_visible:
                    # ══════════════════════════════════════════════════
                    # REBUILD: Source was hidden, target is visible
                    # Points must be ADDED to mesh geometry
                    # ══════════════════════════════════════════════════
                    print(f"🙈 Source classes hidden, target visible — must rebuild to add geometry")
                    self._shading_force_rebuild(shading_vis)
                
                else:
                    # ══════════════════════════════════════════════════
                    # SKIP: Both hidden — mesh unchanged visually
                    # ══════════════════════════════════════════════════
                    print(f"⏭️ Both source and target hidden — no visual change needed")
            
            elif display_mode == "shaded_class" and final_mask is None:
                print("⚠️ No change mask — forcing rebuild")
                self._shading_force_rebuild(None)
            
            else:
                # Standard Point Cloud Refresh
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)

            # ════════════════════════════════════════════════════════════
            # CLEANUP
            # ════════════════════════════════════════════════════════════
            self.preview_label.setText(f"✅ Converted {converted_count:,} points")
            self.preview_label.setStyleSheet("""
                QLabel {
                    color: #4caf50;
                    font-size: 10px;
                    font-weight: bold;
                    padding: 8px;
                    background-color: #1b5e20;
                    border-radius: 3px;
                }
            """)
            self._clear_fence_highlights()
            # ✅ Re-highlight fences in a distinct color (cyan) to show classified area
            self._highlight_classified_fences()
 
            if hasattr(self.app, 'digitizer') and self.app.digitizer:
                try:
                    self.app.digitizer.rebind_drawings()
                    print("   🖊️ Drawing actors rebound to overlay after shading rebuild")
                except Exception as _rb_err:
                    print(f"   ⚠️ rebind_drawings failed: {_rb_err}")
 
            # Keep the fence in the dialog so the user can remove it manually
            # through the dedicated Clear Classified Fences popup.
            self._fences_from_selection = False
            if hasattr(self, 'select_fence_btn'):
                self.select_fence_btn.setVisible(True)
            if hasattr(self, 'clear_fence_btn'):
                self.clear_fence_btn.setVisible(True)
            self.update_fence_display()
            _set_dialog_status(
                self.fence_status,
                "Classified. Clear it from the fence popup when ready.",
                "success",
            )
            self._conversion_completed = True
 
            if self.ribbon_parent:
                self.ribbon_parent.update_status(f"Converted {converted_count:,} points", "success")

            if hasattr(self.app, 'section_vtks') and self.app.section_vtks:
                for view_idx in list(self.app.section_vtks.keys()):
                    try:
                        if hasattr(self.app, '_refresh_single_section_view'):
                            self.app._refresh_single_section_view(view_idx)
                    except Exception:
                        pass

            if hasattr(self.app, 'point_count_widget'):
                self.app.point_count_widget.schedule_update()

            QMessageBox.information(self, "Success", f"Converted {converted_count:,} points.")

        except Exception as e:
            print(f"❌ Conversion Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._conversion_in_progress = False
            if hasattr(self, "convert_btn") and self.convert_btn:
                self.convert_btn.setEnabled(True)


    def _shading_force_rebuild(self, shading_vis):
        """Helper: Force full shading mesh rebuild preserving visibility"""
        try:
            from gui.shading_display import clear_shading_cache, update_shaded_class
            
            if shading_vis:
                for c in self.app.class_palette:
                    self.app.class_palette[c]["show"] = (int(c) in shading_vis)
            
            clear_shading_cache("fence conversion requires geometry change")
            update_shaded_class(
                self.app,
                getattr(self.app, "last_shade_azimuth", 45.0),
                getattr(self.app, "last_shade_angle", 45.0),
                getattr(self.app, "shade_ambient", 0.2),
                force_rebuild=True
            )
        except Exception as e:
            print(f"❌ Rebuild failed: {e}")
            from gui.class_display import update_class_mode
            update_class_mode(self.app, force_refresh=True)

    def _highlight_classified_fences(self):
        """Recolor the existing fence drawing actors to cyan — keeps them pickable/deletable"""
        if not self.selected_fences:
            return

        self._classified_fence_actors = []  # track which drawing actors we recolored

        def _coords_key(coords):
            try:
                pts = [tuple(c[:2]) for c in (coords or [])]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                return tuple(pts)
            except Exception:
                return ()

        def _source_drawing(fence):
            digitizer = getattr(self.app, 'digitizer', None)
            drawings = list(getattr(digitizer, 'drawings', []) or [])
            if any(drawing is fence for drawing in drawings):
                return fence
            actor = fence.get('actor') if isinstance(fence, dict) else None
            if actor is not None:
                for drawing in drawings:
                    if drawing.get('actor') is actor:
                        return drawing
            fence_key = _coords_key(fence.get('coords', []) if isinstance(fence, dict) else [])
            if fence_key:
                for drawing in drawings:
                    if _coords_key(drawing.get('coords', [])) == fence_key:
                        return drawing
            return fence

        for fence in self.selected_fences:
            drawing = _source_drawing(fence)
            actor = drawing.get('actor') if isinstance(drawing, dict) else None
            if drawing and isinstance(drawing, dict):
                drawing['classified_fence'] = True  # tag the original digitizer drawing
            if actor:
                try:
                    self._classified_fence_actors.append(actor)
                except Exception as e:
                    print(f"⚠️ Storing classified actor failed: {e}")
        self._safe_main_render()
        print(f"✅ Highlighted {len(self._classified_fence_actors)} classified fence(s) in cyan")

    def _safe_main_render(self):
        """
        Render guard for dialog-triggered updates.
        Avoids crashing on stale/tearing-down render windows during heavy classify cycles.
        """
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return False
        try:
            rw = vtk_widget.GetRenderWindow() if hasattr(vtk_widget, "GetRenderWindow") else None
            if rw is None:
                return False
            vtk_widget.render()
            return True
        except Exception as e:
            print(f"⚠️ Safe render skipped: {e}")
            return False

    # ==================== HELPER METHODS ====================

    @staticmethod
    def make_color_icon(rgb):
        """Create a color icon from RGB tuple"""
        pix = QPixmap(20, 12)
        pix.fill(QColor(*rgb))
        return QIcon(pix)

    def naksha_dark_theme(self):
        """Return 'Obsidian & Teal' theme stylesheet"""
        return """
            QDialog, QWidget {
                background-color: #0a0a0a;
                color: #eeeeee;
                font-family: "Segoe UI";
            }
            /* Teal Section Headers */
            QLabel#header_label {
                color: #00c8aa;
                font-weight: bold;
                text-transform: uppercase;
                letter-spacing: 1px;
                font-size: 10px;
            }
            QListWidget, QComboBox, QDoubleSpinBox {
                background-color: #121212;
                color: #ffffff;
                border: 1px solid #222222;
                border-radius: 4px;
                padding: 5px;
            }
            QListWidget::item:selected {
                background-color: #00c8aa;
                color: #000000;
                border-radius: 3px;
            }
            /* ✅ ADD: Proper QComboBox dropdown styling */
            QComboBox::drop-down {
                border: none;
                width: 20px;
            }
            QComboBox::down-arrow {
                image: none;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 4px solid #888888;
                margin-right: 5px;
            }
            QComboBox QAbstractItemView {
                background-color: #1a1a1a;
                color: #ffffff;
                selection-background-color: #00c8aa;
                selection-color: #000000;
                border: 1px solid #333333;
            }
            QCheckBox {
                color: #aaaaaa;
                font-size: 10px;
            }
            QPushButton {
                background-color: #222222;
                color: #ffffff;
                border: 1px solid #333333;
                padding: 8px;
                border-radius: 4px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #333333;
            }
            /* Bright Teal Action Button */
            QPushButton#primary_btn {
                background-color: #00c8aa;
                color: #000000;
                font-weight: bold;
                border: none;
            }
            QPushButton#primary_btn:hover {
                background-color: #00e6c3;
            }
        """
class SyncViewsDialog(QDialog):
    """
    Synchronize Views dialog like MicroStation.
    Rows:
      View N: [No synch | Match]  [View 1..View 5]
    """
    def __init__(self, app, parent=None):
        super().__init__(parent or app)
        self.app = app
        self.setWindowTitle("Synchronize Views")
        self.setModal(False)
        self.setMinimumWidth(280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.rows = []
        self.num_views = 5  # You currently support View 1..View 5

        for v in range(1, self.num_views + 1):
            row_layout = QHBoxLayout()
            row_layout.setSpacing(6)

            label = QLabel(f"View {v}:")
            label.setFixedWidth(50)

            mode_combo = QComboBox()
            mode_combo.addItems(["No synch", "Match"])

            source_combo = QComboBox()
            for s in range(1, self.num_views + 1):
                source_combo.addItem(f"View {s}", s)

            row_layout.addWidget(label)
            row_layout.addWidget(mode_combo)
            row_layout.addWidget(source_combo)
            layout.addLayout(row_layout)

            row = {"view_num": v, "mode": mode_combo, "source": source_combo}
            self.rows.append(row)

            mode_combo.currentIndexChanged.connect(lambda idx, r=row: self._update_row_enabled(r))

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._apply_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load_from_app_state()
        for r in self.rows:
            self._update_row_enabled(r)

    def _update_row_enabled(self, row):
        is_match = (row["mode"].currentIndex() == 1)
        row["source"].setEnabled(is_match)

    def _load_from_app_state(self):
        """
        Load current settings from app.view_sync_map with semantics:

        view_sync_map[target_idx] = source_idx

        But in the UI, each row is:

        "View A : [mode] [View B]"

        and represents:

        View B = Match View A

        i.e. A = source, B = target.
        """
        match_map = getattr(self.app, "view_sync_map", {})

        for row in self.rows:
            source_view_num = row["view_num"]   # left "View A"
            source_idx = source_view_num - 1

            # Find any target that is set to match this source
            target_view_num = None
            for target_idx, src_idx in match_map.items():
                if src_idx == source_idx:
                    target_view_num = target_idx + 1
                    break

            if target_view_num is not None:
                # This source has at least one target; show "Match target_view_num"
                row["mode"].setCurrentIndex(1)  # Match
                i = row["source"].findData(target_view_num)
                if i >= 0:
                    row["source"].setCurrentIndex(i)
            else:
                # No target currently matches this source
                row["mode"].setCurrentIndex(0)  # No synch
                # (we leave source combo as-is; it doesn't matter when mode=No synch)
    def _apply_to_app(self):
        """
        Apply mappings using app.set_view_sync() with your dialog semantics:

        Row: "View A : Match View B"
        Means: View B = Match View A

        Fix:
        - Do NOT clear a target just because mode is "No synch" and the combo happens
        to be sitting on View 1 by default.
        - Only clear mappings that are actually affected.
        """
        existing = dict(getattr(self.app, "view_sync_map", {}) or {})

        desired = {}            # target_idx -> source_idx
        sources_no_sync = set() # source_idx that user set to "No synch" explicitly (in UI)

        # 1) Read UI into desired mapping
        for row in self.rows:
            source_view_num = int(row["view_num"])          # A (1-based)
            source_idx = source_view_num - 1                # A (0-based)
            mode = row["mode"].currentIndex()               # 0=No synch, 1=Match

            if mode == 0:
                sources_no_sync.add(source_idx)
                continue

            target_view_num = row["source"].currentData()   # B (1-based)
            if target_view_num is None:
                continue

            target_idx = int(target_view_num) - 1

            # Ignore self-sync (treat as no mapping)
            if target_idx == source_idx:
                continue

            desired[target_idx] = source_idx

        # 2) Decide what to clear (unique targets only)
        to_clear = set()

        for target_idx, src_idx in existing.items():
            if target_idx in desired and desired[target_idx] != src_idx:
                to_clear.add(target_idx)
                continue

            if (target_idx not in desired) and (src_idx in sources_no_sync):
                to_clear.add(target_idx)

        # 3) Apply clears
        for target_idx in sorted(to_clear):
            self.app.set_view_sync(target_idx + 1, None)

        # 4) Apply new mappings
        for target_idx, source_idx in desired.items():
            self.app.set_view_sync(target_idx + 1, source_idx + 1)

    def _apply_and_close(self):
        self._apply_to_app()
        self.accept()


class AIRibbon(QWidget):
    """Ribbon for AI Classification"""
    
    ai_classify_requested = Signal()
    
    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.build_ribbon()
        
    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        # AI Classification
        ai_section = RibbonSection("AI", self)
        ai_section.add_button(
            "Start",
            "🚀",
            self._start_classification,
            toggleable=False
        )
        layout.addWidget(ai_section)
        
        layout.addStretch()
    
    def _start_classification(self):
        """Trigger AI classification workflow"""
        try:
            app = getattr(self, 'app', None)
            if app is None:
                # Fallback if self.app is somehow missing
                app = getattr(self.parent(), 'app', None)
            if app is None:
                app = self.parent().parent().parent()
            
            # Check if LAZ file is loaded
            if not hasattr(app, 'data') or app.data is None:
                QMessageBox.warning(
                    self,
                    "No Data Loaded",
                    "Please load a LAZ file before running AI classification."
                )
                return
            
            from gui.ai_dialog import show_ai_classification_dialog
            show_ai_classification_dialog(app)
            
        except Exception as e:
            print(f"⚠️ AI classification failed: {e}")
            import traceback
            traceback.print_exc()

class VertexInsertSettingsDialog(QDialog):
    """Settings dialog for Vertex insertion tool"""

    def __init__(self, current_auto_drag=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vertex Tool Settings")
        self.setModal(True)
        self.resize(350, 180)

        from gui.theme_manager import get_dialog_stylesheet
        self.setStyleSheet(get_dialog_stylesheet())
       
        layout = QVBoxLayout()
        mode_group = QGroupBox("Insertion Mode")
        mode_layout = QVBoxLayout()
        self.insert_only_radio = QRadioButton("Insert only (click to place vertex)")
        self.insert_drag_radio = QRadioButton("Insert and drag (click, then move to position)")
       
        if current_auto_drag:
            self.insert_drag_radio.setChecked(True)
        else:
            self.insert_only_radio.setChecked(True)

        mode_layout.addWidget(self.insert_only_radio)
        mode_layout.addWidget(self.insert_drag_radio)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)

        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout) 
        self.setLayout(layout)
   
    def get_auto_drag_mode(self):
        """Return True if insert-and-drag mode is selected"""
        return self.insert_drag_radio.isChecked()

class VertexMoveSettingsDialog(QDialog):
    """Settings dialog for Vertex Move tool"""

    def __init__(self, current_mode='click', parent=None):
        super().__init__(parent)
        self.setWindowTitle("Vertex Move Settings")
        self.setModal(True)
        self.resize(350, 200)

        from gui.theme_manager import get_dialog_stylesheet
        self.setStyleSheet(get_dialog_stylesheet())
       
        layout = QVBoxLayout()
        mode_group = QGroupBox("Move Mode")
        mode_layout = QVBoxLayout()
        self.click_mode_radio = QRadioButton("Click to select, click again to place")
        self.drag_mode_radio = QRadioButton("Click and drag (hold mouse button)")
        if current_mode == 'drag':
            self.drag_mode_radio.setChecked(True)
        else:
            self.click_mode_radio.setChecked(True)
        mode_layout.addWidget(self.click_mode_radio)
        mode_layout.addWidget(self.drag_mode_radio)
        mode_group.setLayout(mode_layout)
        layout.addWidget(mode_group)
       
        # Buttons
        button_layout = QHBoxLayout()
        ok_btn = QPushButton("OK")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancel_btn")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(ok_btn)
        button_layout.addWidget(cancel_btn)
        layout.addLayout(button_layout)
        self.setLayout(layout)
   
    def get_move_mode(self):
        """Return 'click' or 'drag' based on selection"""
        return 'drag' if self.drag_mode_radio.isChecked() else 'click'
        
class PluginsRibbon(QWidget):
    """Ribbon for runtime plugin/tool loading."""

    def __init__(self, parent=None, app=None):
        super().__init__(parent)
        self.app = app
        self.build_ribbon()

    def build_ribbon(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.rebuild_ribbon()

    def rebuild_ribbon(self):
        layout = self.layout()
        if layout is None:
            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
        else:
            # Clear all existing widgets from the layout
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        # Re-add core plugins section with Manage button only
        section = RibbonSection("Plugins", self)
        section.add_button("Manage", "", self._manage_plugins, toggleable=False)
        layout.addWidget(section)

        # Dynamic addition of plugin-defined ribbon buttons
        app = getattr(self, "app", None) or self.window()
        pm = getattr(app, "plugin_manager", None)
        
        custom_plugins_section = None

        if pm:
            for name, info in pm.loaded_plugins.items():
                instance = info.get("instance")
                if hasattr(instance, "get_ribbon_button"):
                    try:
                        btn_cfg = instance.get_ribbon_button()
                        if btn_cfg and isinstance(btn_cfg, dict):
                            btn_label = btn_cfg.get("label", name)
                            btn_emoji = btn_cfg.get("emoji", "")
                            btn_callback = btn_cfg.get("callback")
                            btn_toggleable = btn_cfg.get("toggleable", False)

                            # Automatically create/add to the Custom Plugins section on demand
                            if custom_plugins_section is None:
                                custom_plugins_section = RibbonSection("Custom Plugins", self)
                                layout.addWidget(custom_plugins_section)

                            custom_plugins_section.add_button(btn_label, btn_emoji, btn_callback, toggleable=btn_toggleable)
                    except Exception as e:
                        print(f"Warning: Failed to add button for plugin '{name}': {e}")

        layout.addStretch()

    def _manage_plugins(self):
        try:
            from gui.plugin_loader_dialog import PluginLoaderDialog
            app = getattr(self, "app", None) or self.window()
            if not hasattr(app, "plugin_manager"):
                from gui.plugin_manager import PluginManager
                app.plugin_manager = PluginManager(app)
            dlg = PluginLoaderDialog(app.plugin_manager, parent=app)
            dlg.exec()
        except Exception as e:
            print(f"Warning: Manage plugins dialog failed: {e}")
