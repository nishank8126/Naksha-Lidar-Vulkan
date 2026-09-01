"""MicroStation-style "View Fields" table — a full spreadsheet of point data.

Shows every loaded point as a row and every attribute as a column, with
virtual (lazy) rendering so millions of points stay responsive.  The dialog is
non-modal so the user can still interact with the main 3D window; clicking a
row can highlight / zoom to that point in the 3D view.
"""

import os
import datetime

import numpy as np
import laspy
from PySide6.QtCore import (
    Qt,
    QAbstractTableModel,
    QEvent,
    QModelIndex,
    QObject,
    QThread,
    Signal,
    QItemSelection,
    QItemSelectionModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
    QLayout,
    QTableView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QCheckBox,
    QMenu,
    QMenuBar,
    QMessageBox,
    QFileDialog,
)

from gui.icon_provider import get_icon
from gui.theme_manager import ThemeColors, get_dialog_stylesheet
from gui.dialogs.view_fields_dialog import (
    IMAGE_FIELD_LABEL,
    IMAGE_SOURCE_KEY,
    find_image_dimension_name,
)


# ASPRS standard classification codes (0-18) plus a few extended values used
# by the app.  Anything not listed falls back to "Class <code>".
CLASS_NAMES = {
    0: "Unclassified",
    1: "Unclassified",
    2: "Ground",
    3: "Low Vegetation",
    4: "Medium Vegetation",
    5: "High Vegetation",
    6: "Building",
    7: "Low Point (noise)",
    8: "Reserved",
    9: "Water",
    10: "Rail",
    11: "Road Surface",
    12: "Reserved",
    13: "Wire - Guard",
    14: "Wire - Conductor",
    15: "Transmission Tower",
    16: "Wire-structure Connector",
    17: "Bridge Deck",
    18: "High Noise",
    19: "Overhead Structure",
    20: "Ignored Ground",
    21: "Snow",
    22: "Temporal Exclusion",
    51: "Noise",
    52: "Noise",
}


def _class_name(code):
    try:
        code = int(code)
    except (TypeError, ValueError):
        return str(code)
    return CLASS_NAMES.get(code, "Class %d" % code)


def _make_fence_overlay_actor(coords, color=(0.0, 1.0, 1.0), width=5):
    """
    Build a screen-space polyline actor tracing a fence shape in world X/Y,
    so the outline stays visible on top of the point cloud regardless of
    point elevation (Z) or camera angle — used to preview a fence on hover.
    """
    try:
        import vtk

        points = vtk.vtkPoints()
        points.SetDataTypeToDouble()
        for coord in coords:
            z = float(coord[2]) if len(coord) > 2 else 0.0
            points.InsertNextPoint(float(coord[0]), float(coord[1]), z)

        polyline = vtk.vtkPolyLine()
        polyline.GetPointIds().SetNumberOfIds(len(coords))
        for i in range(len(coords)):
            polyline.GetPointIds().SetId(i, i)

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
    except Exception as exc:
        print("[ViewFieldsTable] fence overlay build failed: %s" % exc)
        return None


def _points_in_polygon(points, polygon_coords):
    """Boolean mask of which points (N,3) fall inside a 2D polygon (X/Y only)."""
    from matplotlib.path import Path
    poly_xy = np.asarray(polygon_coords, dtype=float)[:, :2]
    points_xy = np.asarray(points)[:, :2]
    min_x, min_y = np.min(poly_xy, axis=0)
    max_x, max_y = np.max(poly_xy, axis=0)
    bbox_mask = (points_xy[:, 0] >= min_x) & (points_xy[:, 0] <= max_x) & \
                (points_xy[:, 1] >= min_y) & (points_xy[:, 1] <= max_y)
    inside = np.zeros(len(points_xy), dtype=bool)
    if np.any(bbox_mask):
        inside[bbox_mask] = Path(poly_xy).contains_points(points_xy[bbox_mask])
    return inside


# ---------------------------------------------------------------------------
# LAS GPS-time handling
# ---------------------------------------------------------------------------
# LAS Global Encoding bit 0 defines how the point-record GPS time is stored:
#   0 -> GPS Week Time (seconds into a GPS week; calendar week number absent)
#   1 -> Adjusted Standard GPS Time (standard GPS seconds - 1,000,000,000)
#
# A GPS Week Time value such as 307789 is NOT a date in January 1980.  Without
# a trustworthy GPS week number, converting it from the GPS epoch invents a
# calendar date.  TerraScan/MicroStation correctly shows '-' in this case.
_GPS_EPOCH = datetime.datetime(1980, 1, 6)
_GPS_WEEK_SECONDS = 7.0 * 24.0 * 60.0 * 60.0

# UTC dates at which GPS-UTC increased by one second.  GPS time itself has no
# leap seconds, so an absolute GPS timestamp is converted to UTC by subtracting
# the accumulated offset.  This matters only around midnight for the Date
# column, but using the real conversion avoids a second subtle false date.
_GPS_UTC_LEAP_EFFECTIVE_DATES = (
    datetime.datetime(1981, 7, 1),
    datetime.datetime(1982, 7, 1),
    datetime.datetime(1983, 7, 1),
    datetime.datetime(1985, 7, 1),
    datetime.datetime(1988, 1, 1),
    datetime.datetime(1990, 1, 1),
    datetime.datetime(1991, 1, 1),
    datetime.datetime(1992, 7, 1),
    datetime.datetime(1993, 7, 1),
    datetime.datetime(1994, 7, 1),
    datetime.datetime(1996, 1, 1),
    datetime.datetime(1997, 7, 1),
    datetime.datetime(1999, 1, 1),
    datetime.datetime(2006, 1, 1),
    datetime.datetime(2009, 1, 1),
    datetime.datetime(2012, 7, 1),
    datetime.datetime(2015, 7, 1),
    datetime.datetime(2017, 1, 1),
)


def _gps_standard_to_utc_datetime(standard_gps_seconds):
    """Convert absolute GPS seconds since 1980-01-06 to a UTC datetime."""
    try:
        seconds = float(standard_gps_seconds)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(seconds) or seconds < 0.0:
        return None

    gps_dt = _GPS_EPOCH + datetime.timedelta(seconds=seconds)
    utc_dt = gps_dt
    # Two passes are enough because the correction is at most a few seconds
    # and only changes the selected leap-second count at a boundary.
    for _ in range(2):
        leap_count = sum(utc_dt >= d for d in _GPS_UTC_LEAP_EFFECTIVE_DATES)
        utc_dt = gps_dt - datetime.timedelta(seconds=leap_count)
    return utc_dt


def _gps_to_date_string(value, gps_time_mode="gps_week"):
    """Return a trustworthy per-point date, or '-' when LAS cannot provide it."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(v):
        return "-"

    if gps_time_mode != "adjusted_standard":
        # GPS Week Time has only seconds-within-week.  The LAS point record does
        # not carry the GPS week number, so the calendar date is unknowable.
        return "-"

    dt = _gps_standard_to_utc_datetime(v + 1_000_000_000.0)
    if dt is None or dt.year < 1980 or dt.year > 2100:
        return "-"
    return dt.strftime("%d/%m/%Y")


# Column kinds understood by the model.
K_CODE = "code"        # classification code -> integer
K_DESC = "desc"        # classification code -> mapped name
K_RAW = "raw"          # source array, str(value)
K_FLOAT3 = "float3"    # source array, 3 decimals
K_INT0 = "int0"        # source array, 0 decimals
K_TIME = "time"        # gps_time, 6 decimals
K_DATE = "date"        # gps_time -> date string
K_RGB = "rgb"          # rgb (N,3) -> "R G B"
K_HSV = "hsv"          # rgb (N,3) -> "H S V"


# Ordered column definitions.  (header, kind, source_key)
COLUMN_DEFS = [
    ("Class", K_CODE, "classification"),
    ("Description", K_DESC, "classification"),
    ("Line", K_RAW, "point_source_id"),
    (IMAGE_FIELD_LABEL, K_RAW, IMAGE_SOURCE_KEY),
    ("Time", K_TIME, "gps_time"),
    ("Date", K_DATE, "gps_time"),
    ("Echo", K_RAW, "return_number"),
    ("Easting", K_FLOAT3, "x"),
    ("Northing", K_FLOAT3, "y"),
    ("Elevation", K_FLOAT3, "z"),
    ("Amplitude", K_RAW, "amplitude"),
    ("Intensity", K_INT0, "intensity"),
    ("Reflectance", K_RAW, "reflectance"),
    ("Deviation", K_RAW, "deviation"),
    ("Reliability", K_RAW, "reliability"),
    ("Color RGB", K_RGB, "rgb"),
    ("Color HSV", K_HSV, "rgb"),
    ("Angle", K_FLOAT3, "scan_angle"),
    ("Number of Returns", K_RAW, "number_of_returns"),
]


class PointTableModel(QAbstractTableModel):
    """Virtual table model — only formats rows that are currently visible."""

    def __init__(self, app_data, filename=None, parent=None):
        super().__init__(parent)
        self._data = app_data or {}
        self._filename = filename
        self._order = np.arange(len(self._data.get("xyz", [])), dtype=np.int64)
        self._visible_headers = None
        self._columns = self._build_columns()

    # -- column discovery -------------------------------------------------
    def _build_columns(self):
        cols = []
        present = set(self._data.keys())
        for header, kind, key in COLUMN_DEFS:
            if key == IMAGE_SOURCE_KEY:
                image_key = find_image_dimension_name(present)
                if image_key is not None and self._data.get(image_key) is not None:
                    cols.append((header, kind, image_key))
                continue
            # x/y/z are always derived from xyz even if not separate keys
            if key in ("x", "y", "z"):
                if "xyz" in self._data:
                    cols.append((header, kind, key))
                continue
            if key in present and self._data[key] is not None:
                cols.append((header, kind, key))
        if self._visible_headers is not None:
            cols = [
                column for column in cols
                if column[0] in self._visible_headers
            ]
        return cols

    def refresh_columns(self):
        self._columns = self._build_columns()
        n_points = len(self._data.get("xyz", []))
        self._order = np.arange(n_points, dtype=np.int64)
        self.layoutChanged.emit()

    def set_visible_columns(self, headers):
        """Restrict visible columns to the given header names.

        Returns True if at least one column matched, False otherwise.
        """
        wanted = set(headers)
        known_headers = {header for header, _kind, _key in COLUMN_DEFS}
        if not wanted or not wanted.intersection(known_headers):
            return False
        self._visible_headers = wanted
        self._columns = self._build_columns()
        self.layoutChanged.emit()
        return True

    # -- required model API ----------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        # Defensive: the main app can replace/clear its `data` dict (e.g.
        # loading a new file) while this non-modal table is still open, so
        # never report more rows than the live array actually has.
        xyz = self._data.get("xyz")
        if xyz is None:
            return 0
        return min(int(self._order.shape[0]), len(xyz))

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation, role):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._columns[section][0]
        return None

    def _raw_value(self, header, kind, key, row):
        """Return the unformatted value for a (row, column).

        Defensive against the underlying app data dict being replaced,
        cleared, or resized by the main app (e.g. loading a new file, or a
        delete operation) while this non-modal table is still open.
        """
        try:
            r = self._order[row]
        except IndexError:
            return None

        if key in ("x", "y", "z"):
            xyz = self._data.get("xyz")
            if xyz is None or r >= len(xyz):
                return None
            axis = {"x": 0, "y": 1, "z": 2}[key]
            return xyz[r, axis]

        arr = self._data.get(key)
        if arr is None or r >= len(arr):
            return None
        return arr[r]

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, col = index.row(), index.column()
        header, kind, key = self._columns[col]

        if role == Qt.ToolTipRole and kind == K_DATE:
            mode = self._data.get("_gps_time_mode")
            if mode is None:
                mode = "adjusted_standard" if self._data.get("_gps_time_adjusted", False) else "gps_week"
            if mode == "adjusted_standard":
                return (
                    "Date derived from LAS Adjusted Standard GPS Time "
                    "(Global Encoding bit 0 = 1)."
                )
            creation = self._data.get("_las_creation_date")
            extra = (" Header creation date: %s." % creation) if creation else ""
            return (
                "Calendar date unavailable: this file stores GPS Week Time. "
                "LAS does not store the GPS week number in each point, so "
                "NakshaAI does not invent a date." + extra +
                " The header date is file metadata, not a per-point acquisition date."
            )

        if role != Qt.DisplayRole:
            return None

        if kind == K_DESC:
            code = self._raw_value(header, K_CODE, "classification", row)
            return _class_name(code)
        if kind == K_DATE:
            val = self._raw_value(header, K_TIME, "gps_time", row)
            mode = self._data.get("_gps_time_mode")
            if mode is None:
                mode = "adjusted_standard" if self._data.get("_gps_time_adjusted", False) else "gps_week"
            return _gps_to_date_string(val, mode) if val is not None else "-"
        if kind == K_RGB:
            rgb = self._raw_value(header, K_RGB, "rgb", row)
            if rgb is None:
                return ""
            return "R%d G%d B%d" % (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        if kind == K_HSV:
            rgb = self._raw_value(header, K_HSV, "rgb", row)
            if rgb is None:
                return ""
            return self._rgb_to_hsv(rgb)

        val = self._raw_value(header, kind, key, row)
        if val is None:
            return ""
        if kind == K_FLOAT3:
            return "%.3f" % float(val)
        if kind == K_INT0:
            return "%.0f" % float(val)
        if kind == K_TIME:
            return "%.6f" % float(val)
        return str(val)

    @staticmethod
    def _rgb_to_hsv(rgb):
        r, g, b = (int(rgb[0]) / 255.0, int(rgb[1]) / 255.0, int(rgb[2]) / 255.0)
        mx, mn = max(r, g, b), min(r, g, b)
        diff = mx - mn
        if diff == 0:
            h = 0.0
        elif mx == r:
            h = (60.0 * ((g - b) / diff)) % 360.0
        elif mx == g:
            h = (60.0 * ((b - r) / diff)) + 120.0
        else:
            h = (60.0 * ((r - g) / diff)) + 240.0
        s = 0.0 if mx == 0 else diff / mx
        v = mx
        return "%d %d %d" % (round(h), round(s * 100), round(v * 100))

    # -- sorting ----------------------------------------------------------
    def _sort_key(self, col):
        header, kind, key = self._columns[col]
        if kind == K_DESC:
            return self._data.get("classification")
        if kind == K_DATE:
            return self._data.get("gps_time")
        if kind in (K_RGB, K_HSV):
            rgb = self._data.get("rgb")
            if rgb is None:
                return None
            return (rgb[:, 0].astype(np.float64) * 0.299 +
                    rgb[:, 1].astype(np.float64) * 0.587 +
                    rgb[:, 2].astype(np.float64) * 0.114)
        if key in ("x", "y", "z"):
            xyz = self._data.get("xyz")
            if xyz is None:
                return None
            axis = {"x": 0, "y": 1, "z": 2}[key]
            return xyz[:, axis]
        return self._data.get(key)

    def sort(self, column, order):
        key = self._sort_key(column)
        if key is None:
            return
        try:
            order_arr = np.argsort(key, kind="stable")
        except TypeError:
            order_arr = np.argsort(np.array(key, dtype=str), kind="stable")
        if order == Qt.DescendingOrder:
            order_arr = order_arr[::-1]
        self._order = order_arr
        self.layoutChanged.emit()

    # -- helpers for external callers ------------------------------------
    def point_index(self, row):
        """Return the original point index for a visible row."""
        return int(self._order[row])

    def row_for_point(self, point_index):
        """Return the visible row for an original point index, or None."""
        matches = np.flatnonzero(self._order == int(point_index))
        return int(matches[0]) if matches.size else None

    def xyz_for_row(self, row):
        try:
            r = self._order[row]
        except IndexError:
            return None
        xyz = self._data.get("xyz")
        if xyz is None or r >= len(xyz):
            return None
        return xyz[r]

    def class_code_for_row(self, row):
        """Return classification code for a visible row, or None."""
        try:
            point_index = int(self._order[row])
        except (IndexError, TypeError, ValueError):
            return None
        classification = self._data.get("classification")
        if classification is None or point_index < 0 or point_index >= len(classification):
            return None
        try:
            return int(classification[point_index])
        except (TypeError, ValueError):
            return None

    def rows_for_class(self, class_code):
        """Return visible table rows belonging to class_code as an int64 array."""
        classification = self._data.get("classification")
        if classification is None or self._order.size == 0:
            return np.empty(0, dtype=np.int64)
        try:
            ordered_codes = np.asarray(classification)[self._order]
            return np.flatnonzero(ordered_codes == int(class_code)).astype(np.int64, copy=False)
        except Exception:
            return np.empty(0, dtype=np.int64)

    def sort_by_class_code(self):
        """Stable class sort used only when a huge class selection is fragmented."""
        classification = self._data.get("classification")
        if classification is None:
            return False
        try:
            self.layoutAboutToBeChanged.emit()
            self._order = np.argsort(np.asarray(classification), kind="stable")
            self.layoutChanged.emit()
            return True
        except Exception:
            return False


class _ExtraFieldLoader(QThread):
    """Read optional LAS dimensions from the file in the background."""

    finished_loading = Signal(dict)

    def __init__(self, filename, wanted, parent=None):
        super().__init__(parent)
        self._filename = filename
        self._wanted = wanted

    def run(self):
        result = {}
        try:
            ext = os.path.splitext(self._filename)[1].lower()
            if ext not in (".las", ".laz"):
                self.finished_loading.emit(result)
                return
            with laspy.open(self._filename) as las:
                global_encoding = getattr(las.header, "global_encoding", 0)
                encoding_value = int(getattr(global_encoding, "value", global_encoding) or 0)
                adjusted = bool(encoding_value & 1)
                result["_gps_time_adjusted"] = adjusted  # backward compatibility
                result["_gps_time_mode"] = "adjusted_standard" if adjusted else "gps_week"

                creation_date = getattr(las.header, "creation_date", None)
                if creation_date:
                    try:
                        result["_las_creation_date"] = creation_date.isoformat()
                    except Exception:
                        result["_las_creation_date"] = str(creation_date)
                result["_las_system_identifier"] = str(
                    getattr(las.header, "system_identifier", "") or ""
                ).strip()
                result["_las_generating_software"] = str(
                    getattr(las.header, "generating_software", "") or ""
                ).strip()

                dimension_names = [
                    str(d) for d in las.header.point_format.dimension_names
                ]
                dimensions_by_lower = {
                    name.casefold(): name for name in dimension_names
                }
                needed = [
                    w for w in self._wanted
                    if w != IMAGE_SOURCE_KEY and w in dimensions_by_lower
                ]
                image_dimension = None
                if IMAGE_SOURCE_KEY in self._wanted:
                    image_dimension = find_image_dimension_name(dimension_names)

                if needed or image_dimension is not None:
                    points = las.read()
                    for w in needed:
                        try:
                            result[w] = np.asarray(points[dimensions_by_lower[w]])
                        except Exception:
                            pass
                    if image_dimension is not None:
                        try:
                            result[str(image_dimension).casefold()] = np.asarray(
                                points[str(image_dimension)]
                            )
                        except Exception:
                            pass
        except Exception as exc:
            print("[ViewFieldsTable] extra field load failed: %s" % exc)
        self.finished_loading.emit(result)


class ViewFieldsTableDialog(QDialog):
    """MicroStation-style point data table window."""

    EXTRA_FIELD_LIMIT = 25_000_000

    def __init__(self, filename, app_data, parent=None):
        super().__init__(parent)
        self.setProperty("themeStyledDialog", True)
        self.filename = filename
        self.app_data = app_data if isinstance(app_data, dict) else {}
        self._highlight_actor = None
        self._class_selected_rows = None
        self._programmatic_class_selection = False
        self._context_row = None

        # QDialogs suppress min/max buttons by default — re-add them so the
        # window can be maximized (button + double-click title bar).
        self.setWindowFlags(
            Qt.Window
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )

        n_points = len(self.app_data.get("xyz", []))
        title = os.path.basename(filename) if filename else "point cloud"
        self.setWindowTitle("%s - %s points" % (title, format(n_points, ",")))
        icon = get_icon("fields")
        if not icon.isNull():
            self.setWindowIcon(icon)
        self.resize(1400, 720)
        self.setMinimumSize(0, 0)
        self.setSizeGripEnabled(True)

        self.setStyleSheet(get_dialog_stylesheet() + self._extra_stylesheet())

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._build_menubar(layout)
        self._build_table(layout)

        self._load_extra_fields()

    def _extra_stylesheet(self):
        c = ThemeColors
        return """
        QTableView#fieldsTable {
            background-color: %s;
            gridline-color: %s;
            color: %s;
            border: none;
            font-size: 11px;
        }
        QTableView#fieldsTable::item:selected {
            background-color: #FFFFFF;
            color: #000000;
        }
        QHeaderView::section {
            background-color: %s;
            color: %s;
            border: 1px solid %s;
            padding: 4px 6px;
        }
        """ % (
            c.get("bg_input"), c.get("border_light"), c.get("text_primary"),
            c.get("bg_panel", c.get("bg_input")), c.get("text_primary"),
            c.get("border_light"),
        )

    def _build_menubar(self, layout):
        menubar = QMenuBar()
        menubar.setObjectName("fieldsMenuBar")

        # Direct action — clicking "Fields" opens the View Fields dialog
        # immediately (no dropdown submenu).
        menubar.addAction("Fields", self._choose_fields)

        # Delete menu — point removal operations.
        delete_menu = menubar.addMenu("Delete")
        delete_menu.addAction("By Class", self._delete_by_class)
        delete_menu.addAction("By Point", self._delete_by_point)
        delete_menu.addAction("By line", self._delete_by_line)
        delete_menu.addSeparator()
        delete_menu.addAction("Inside Fence", lambda: self._delete_by_fence(inside=True))
        delete_menu.addAction("Outside Fence", lambda: self._delete_by_fence(inside=False))

        layout.addWidget(menubar)

    # -- delete: shared confirm + apply ------------------------------------
    def _apply_delete_mask(self, mask, description):
        """Ask for confirmation, then remove the masked points from the loaded cloud."""
        app = self.parent()
        if app is None or not isinstance(getattr(app, "data", None), dict):
            QMessageBox.warning(self, "Delete", "No point cloud data loaded.")
            return

        mask = np.asarray(mask, dtype=bool)
        count = int(np.count_nonzero(mask))
        if count == 0:
            QMessageBox.information(self, "Delete", "No points matched — nothing to delete.")
            return

        reply = QMessageBox.question(
            self,
            "Confirm Delete",
            "Delete %s point(s) — %s?\n\nThis cannot be undone." % (
                format(count, ","), description),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        keep_mask = ~mask
        data = app.data
        mask_len = mask.shape[0]
        for key, arr in list(data.items()):
            if isinstance(arr, np.ndarray) and arr.ndim >= 1 and arr.shape[0] == mask_len:
                data[key] = arr[keep_mask]

        from gui.pointcloud_display import update_pointcloud
        update_pointcloud(app, getattr(app, "display_mode", "rgb"))

        if getattr(app, "point_count_widget", None):
            from gui.point_count_widget import refresh_point_statistics
            refresh_point_statistics(app)

        for stack_name in ("undo_stack", "undostack", "redo_stack", "redostack"):
            stack = getattr(app, stack_name, None)
            if stack is not None and hasattr(stack, "clear"):
                stack.clear()

        self.model.refresh_columns()
        QMessageBox.information(self, "Delete", "Deleted %s point(s)." % format(count, ","))

    def _pick_codes_dialog(self, title, label_text, items, on_hover=None):
        """
        items: list of (code, display_text). Returns list of chosen codes, or
        None if cancelled/empty.

        If on_hover is given, it is called with the code under the mouse as
        the user moves over the list (and with None once the mouse leaves
        the list), so callers can preview e.g. a fence shape in the 3D view.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setStyleSheet(get_dialog_stylesheet())
        dlg.resize(320, 380)

        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel(label_text))

        list_widget = QListWidget()
        for code, text in items:
            item = QListWidgetItem(text)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            item.setData(Qt.UserRole, code)
            list_widget.addItem(item)
        layout.addWidget(list_widget, 1)

        if on_hover is not None:
            list_widget.setMouseTracking(True)
            list_widget.viewport().setMouseTracking(True)
            list_widget.itemEntered.connect(lambda item: on_hover(item.data(Qt.UserRole)))

            class _LeaveFilter(QObject):
                def eventFilter(self_filter, obj, event):
                    if event.type() == QEvent.Leave:
                        on_hover(None)
                    return False

            leave_filter = _LeaveFilter(list_widget)
            list_widget.viewport().installEventFilter(leave_filter)
            dlg._leave_filter = leave_filter  # keep alive for the dialog's lifetime

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(buttons)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)

        try:
            if dlg.exec() != QDialog.Accepted:
                return None
        finally:
            if on_hover is not None:
                on_hover(None)

        chosen = [
            list_widget.item(i).data(Qt.UserRole)
            for i in range(list_widget.count())
            if list_widget.item(i).checkState() == Qt.Checked
        ]
        return chosen or None

    # -- delete: By Class ---------------------------------------------------
    def _delete_by_class(self):
        app = self.parent()
        classification = (getattr(app, "data", None) or {}).get("classification") if app else None
        if classification is None:
            QMessageBox.information(self, "Delete — By Class", "No classification data loaded.")
            return

        codes, counts = np.unique(classification, return_counts=True)
        items = [
            (int(c), "%d - %s (%s pts)" % (int(c), _class_name(int(c)), format(int(n), ",")))
            for c, n in zip(codes, counts)
        ]
        chosen = self._pick_codes_dialog(
            "Delete By Class", "Select classification code(s) to delete:", items)
        if not chosen:
            return

        mask = np.isin(classification, chosen)
        self._apply_delete_mask(mask, "class(es) %s" % ", ".join(str(c) for c in chosen))

    # -- delete: By Point -----------------------------------------------------
    def _delete_by_point(self):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            QMessageBox.information(
                self, "Delete — By Point", "Select a row in the table first.")
            return

        row = sel.selectedRows()[0].row()
        point_index = self.model.point_index(row)
        n_points = self.model.rowCount()
        mask = np.zeros(n_points, dtype=bool)
        mask[point_index] = True
        self._apply_delete_mask(mask, "the selected row")

    # -- delete: By Line ------------------------------------------------------
    def _delete_by_line(self):
        app = self.parent()
        data = getattr(app, "data", None) if app else None
        line_ids = data.get("point_source_id") if data else None
        if line_ids is None:
            QMessageBox.information(
                self, "Delete — By Line",
                "Line numbers (point_source_id) are not available for this file.")
            return

        codes, counts = np.unique(line_ids, return_counts=True)
        items = [
            (int(c), "Line %d (%s pts)" % (int(c), format(int(n), ",")))
            for c, n in zip(codes, counts)
        ]
        chosen = self._pick_codes_dialog(
            "Delete By Line", "Select line number(s) to delete:", items)
        if not chosen:
            return

        mask = np.isin(line_ids, chosen)
        self._apply_delete_mask(mask, "line(s) %s" % ", ".join(str(c) for c in chosen))

    # -- delete: Inside/Outside Fence -----------------------------------------
    def _delete_by_fence(self, inside=True):
        app = self.parent()
        xyz = (getattr(app, "data", None) or {}).get("xyz") if app else None
        if xyz is None:
            QMessageBox.information(self, "Delete — Fence", "No point cloud data loaded.")
            return

        digitizer = getattr(app, "digitizer", None)
        drawings = getattr(digitizer, "drawings", []) if digitizer else []
        fence_shapes = [
            d for d in drawings
            if d.get("type") in (
                "rectangle", "circle", "polygon", "freehand",
                "line", "smart_line", "polyline", "smartline",
            )
        ]
        if not fence_shapes:
            QMessageBox.information(
                self, "Delete — Fence",
                "No fence shape found. Draw a shape with the Draw tools first.")
            return

        items = [
            (i, "%s #%d" % (str(shape.get("type", "shape")).title(), i + 1))
            for i, shape in enumerate(fence_shapes)
        ]

        renderer = getattr(getattr(app, "vtk_widget", None), "renderer", None)
        hover_actor = [None]

        def clear_hover_highlight():
            if hover_actor[0] is not None and renderer is not None:
                try:
                    renderer.RemoveActor2D(hover_actor[0])
                except Exception:
                    pass
                hover_actor[0] = None
                try:
                    app.vtk_widget.render()
                except Exception:
                    pass

        def on_hover(code):
            clear_hover_highlight()
            if code is None or renderer is None:
                return
            coords = fence_shapes[code].get("coords")
            if not coords:
                return
            actor = _make_fence_overlay_actor(coords, color=(0.0, 1.0, 1.0), width=5)
            if actor is not None:
                renderer.AddViewProp(actor)
                hover_actor[0] = actor
                try:
                    app.vtk_widget.render()
                except Exception:
                    pass

        try:
            chosen = self._pick_codes_dialog(
                "Select Fence", "Select the fence shape(s) to use:", items,
                on_hover=on_hover)
        finally:
            clear_hover_highlight()

        if not chosen:
            return

        from gui.menu_sidebar_system import _prepare_fence_shape

        combined = np.zeros(len(xyz), dtype=bool)
        for i in chosen:
            shape = _prepare_fence_shape(fence_shapes[i])
            coords = shape.get("coords")
            if coords is None or len(coords) < 3:
                continue
            combined |= _points_in_polygon(xyz, coords)

        mask = combined if inside else ~combined
        label = "inside" if inside else "outside"
        self._apply_delete_mask(mask, "point(s) %s the selected fence" % label)

    def _choose_fields(self):
        """Open the original View Fields checklist and apply it to the table."""
        from gui.dialogs.view_fields_dialog import ViewFieldsDialog

        dlg = ViewFieldsDialog(
            self.filename,
            parent=self,
            available_dimensions=self.app_data.keys(),
        )
        if dlg.exec() != QDialog.Accepted:
            return
        selected = dlg.selected_fields()

        # Keep the main window's identify ribbon in sync with the choice.
        app = self.parent()
        if app is not None:
            app.selected_view_fields = selected

        if not self.model.set_visible_columns(selected):
            QMessageBox.information(
                self, "View Fields",
                "No matching columns to display for the selected fields.")

    def _build_table(self, layout):
        self.table = QTableView()
        self.table.setObjectName("fieldsTable")
        self.model = PointTableModel(self.app_data, self.filename)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.verticalHeader().setVisible(False)
        self.table.clicked.connect(self._on_row_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._build_context_menu)
        self.table.selectionModel().selectionChanged.connect(self._on_table_selection_changed)
        layout.addWidget(self.table, 1)

    # -- identification link ----------------------------------------------
    def highlight_point(self, point_index):
        """Select and scroll to the row for an original point index.

        Called by the app when the Identification tool picks a point so the
        table stays in sync with the 3D view.
        """
        if not self.isVisible():
            print("[ViewFieldsTable] highlight skipped: table window not visible")
            return
        row = self.model.row_for_point(point_index)
        if row is None:
            print("[ViewFieldsTable] highlight: point %r not found in table" % point_index)
            return
        from PySide6.QtCore import QItemSelection, QItemSelectionModel

        sel_model = self.table.selectionModel()
        if sel_model is None:
            return
        top = self.model.index(row, 0)
        bottom = self.model.index(row, max(self.model.columnCount() - 1, 0))
        sel_model.clearSelection()
        sel_model.select(
            QItemSelection(top, bottom),
            QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
        )
        self.table.setCurrentIndex(top)
        # Center the highlighted row in the viewport so it is always visible.
        self.table.scrollTo(top, QTableView.PositionAtCenter)
        self.table.setFocus(Qt.OtherFocusReason)
        self.table.viewport().update()
        print("[ViewFieldsTable] highlighted row %d for point %r" % (row, point_index))

    # -- extra fields -----------------------------------------------------
    def _load_extra_fields(self):
        wanted = []
        for _header, _kind, key in COLUMN_DEFS:
            if key == IMAGE_SOURCE_KEY:
                image_key = find_image_dimension_name(self.app_data.keys())
                if image_key is None or self.app_data.get(image_key) is None:
                    wanted.append(key)
            elif key not in self.app_data or self.app_data.get(key) is None:
                wanted.append(key)
        wanted = [w for w in wanted if w not in ("x", "y", "z")]
        if not self.filename:
            return
        n_points = len(self.app_data.get("xyz", []))
        if wanted and n_points > self.EXTRA_FIELD_LIMIT:
            print("[ViewFieldsTable] loading LAS header only (%d points)" % n_points)
            wanted = []
        self._loader = _ExtraFieldLoader(self.filename, wanted, self)
        self._loader.finished_loading.connect(self._on_extra_fields_loaded)
        self._loader.start()

    def _on_extra_fields_loaded(self, result):
        if result:
            for k, v in result.items():
                self.app_data[k] = v
            self.model.refresh_columns()

    # -- keep in sync with the main app --------------------------------
    def refresh_data(self, filename, app_data):
        """
        Re-point this (possibly still-open, non-modal) table at the app's
        current point cloud.

        The app replaces `app.data` with a brand-new dict on every (re)load
        rather than mutating the old one in place, so a table opened before
        a reload would otherwise keep showing/computing against the old,
        now-orphaned dict — this is what made the table look "stale" or
        blank across a reload.  Called from the same place the app already
        refreshes point-count statistics after any load/edit, so this stays
        in sync automatically without extra wiring at each load call site.
        """
        app_data = app_data if isinstance(app_data, dict) else {}

        if filename == self.filename and app_data is self.app_data:
            # Same dataset (e.g. after a delete, which mutates the same
            # dict in place) — just resync row/column counts; no need to
            # re-kick the background extra-field file read.
            self.model.refresh_columns()
            return

        self.filename = filename
        self.app_data = app_data
        self._class_selected_rows = None
        self._context_row = None
        self._cached_point_radius = None
        self.model._data = app_data
        self.model.refresh_columns()

        n_points = len(app_data.get("xyz", []))
        title = os.path.basename(filename) if filename else "point cloud"
        self.setWindowTitle("%s - %s points" % (title, format(n_points, ",")))

        self._load_extra_fields()

    # -- row interactions -------------------------------------------------
    def _on_table_selection_changed(self, _selected, _deselected):
        if not self._programmatic_class_selection:
            self._class_selected_rows = None

    def _selected_rows(self):
        """Return selected visible row numbers without forcing Qt to expand a class selection."""
        if self._class_selected_rows is not None:
            return self._class_selected_rows
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return np.empty(0, dtype=np.int64)
        return np.asarray(sorted({idx.row() for idx in sel.selectedRows()}), dtype=np.int64)

    def _on_row_clicked(self, index):
        if not index.isValid():
            return
        self._highlight_point(self.model.xyz_for_row(index.row()))

    def _highlight_point(self, xyz):
        if xyz is None:
            return
        app = self.parent()
        if app is None or not hasattr(app, "vtk_widget"):
            return
        try:
            import vtk
            renderer = app.vtk_widget.renderer
            if self._highlight_actor is not None:
                renderer.RemoveActor(self._highlight_actor)
                self._highlight_actor = None
            sphere = vtk.vtkSphereSource()
            sphere.SetCenter(float(xyz[0]), float(xyz[1]), float(xyz[2]))
            sphere.SetRadius(self._point_radius())
            sphere.SetThetaResolution(16)
            sphere.SetPhiResolution(12)
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.15, 0.15)
            actor.GetProperty().SetRepresentationToWireframe()
            actor.GetProperty().SetLineWidth(3.0)
            renderer.AddActor(actor)
            self._highlight_actor = actor
            app.vtk_widget.render()
        except Exception as exc:
            print("[ViewFieldsTable] highlight failed: %s" % exc)

    def _point_radius(self):
        xyz = self.app_data.get("xyz")
        if xyz is None or xyz.shape[0] < 2:
            return 1.0
        try:
            # Do not scan the whole array for every click.  Cache a dataset-size
            # marker radius; refresh_data clears it for a new cloud.
            cached = getattr(self, "_cached_point_radius", None)
            if cached is not None:
                return cached
            mn = np.nanmin(xyz, axis=0)
            mx = np.nanmax(xyz, axis=0)
            diag = float(np.linalg.norm(mx - mn))
            self._cached_point_radius = max(diag / 5000.0, 1e-4)
            return self._cached_point_radius
        except Exception:
            return 1.0

    def _primary_action_row(self):
        if self._context_row is not None and 0 <= int(self._context_row) < self.model.rowCount():
            return int(self._context_row)
        rows = self._selected_rows()
        return int(rows[0]) if rows.size else None

    def _go_to_selected(self):
        row = self._primary_action_row()
        if row is None:
            return
        xyz = self.model.xyz_for_row(row)
        if self._zoom_camera_to_point(xyz):
            self._highlight_point(xyz)

    def _zoom_camera_to_point(self, xyz):
        """Center the main camera and actually zoom in to the chosen point."""
        if xyz is None:
            return False
        app = self.parent()
        if app is None or not hasattr(app, "vtk_widget"):
            return False
        try:
            renderer = app.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            if camera is None:
                return False

            xyz = np.asarray(xyz, dtype=float)
            focus = np.asarray(camera.GetFocalPoint(), dtype=float)
            pos = np.asarray(camera.GetPosition(), dtype=float)
            view_vec = pos - focus
            distance = float(np.linalg.norm(view_vec))
            if not np.isfinite(distance) or distance <= 1e-9:
                view_vec = np.array([0.0, 0.0, 1.0], dtype=float)
                distance = 1.0
            else:
                view_vec /= distance

            radius = max(float(self._point_radius()), 1e-4)
            camera.SetFocalPoint(float(xyz[0]), float(xyz[1]), float(xyz[2]))

            if camera.GetParallelProjection():
                # Target roughly a few dozen marker radii across the viewport;
                # never zoom out if the user is already closer than that.
                current_scale = max(float(camera.GetParallelScale()), 1e-9)
                target_scale = max(radius * 20.0, 0.25)
                camera.SetParallelScale(min(current_scale, target_scale))
                new_pos = xyz + view_vec * distance
                camera.SetPosition(float(new_pos[0]), float(new_pos[1]), float(new_pos[2]))
            else:
                # Perspective: place the camera at a deterministic distance that
                # frames the marker while preserving the current view direction.
                angle = max(float(camera.GetViewAngle()), 1.0)
                half_angle = np.deg2rad(angle * 0.5)
                target_half_size = max(radius * 20.0, 0.25)
                target_distance = target_half_size / max(np.tan(half_angle), 1e-6)
                new_pos = xyz + view_vec * target_distance
                camera.SetPosition(float(new_pos[0]), float(new_pos[1]), float(new_pos[2]))

            renderer.ResetCameraClippingRange()
            app.vtk_widget.render()

            schedule_history = getattr(app, "_schedule_main_view_history_commit", None)
            if callable(schedule_history):
                schedule_history("fields_zoom_to_point", delay_ms=0)
            print(
                "[ViewFieldsTable] zoomed to point "
                "(%.3f, %.3f, %.3f)" % (xyz[0], xyz[1], xyz[2])
            )
            return True
        except Exception as exc:
            print("[ViewFieldsTable] camera zoom failed: %s" % exc)
            return False

    # -- actions ----------------------------------------------------------
    def _show_status(self, message, timeout_ms=3000):
        app = self.parent()
        try:
            status_bar = app.statusBar() if app is not None and hasattr(app, "statusBar") else None
            if status_bar is not None:
                status_bar.showMessage(message, timeout_ms)
                return
        except Exception:
            pass
        print("[ViewFieldsTable] %s" % message)

    def _copy_selected_row(self):
        rows = self._selected_rows()
        if rows.size == 0:
            return

        if rows.size > 100_000:
            reply = QMessageBox.question(
                self,
                "Copy Selected Rows",
                "Copy %s rows to the clipboard?\n\n"
                "This can use a large amount of memory. For very large selections, "
                "Export Selection to CSV is recommended." % format(int(rows.size), ","),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        lines = []
        for row in rows:
            row = int(row)
            cells = [
                str(self.model.data(self.model.index(row, col), Qt.DisplayRole) or "")
                for col in range(self.model.columnCount())
            ]
            lines.append("\t".join(cells))

        QApplication.clipboard().setText("\n".join(lines))
        self._show_status("Copied %s row(s) to clipboard" % format(int(rows.size), ","))

    def _identify_selected(self):
        row = self._primary_action_row()
        if row is None:
            return
        pidx = self.model.point_index(row)
        xyz = self.model.xyz_for_row(row)
        app = self.parent()

        identify_tool = getattr(app, "identification_tool", None) if app is not None else None
        identify_by_index = getattr(identify_tool, "identify_point_index", None)
        if callable(identify_by_index):
            try:
                result = identify_by_index(pidx)
                if result:
                    self._highlight_point(xyz)
                    self._show_status(
                        "Identified point %d — class %s" % (pidx, result.get("class_code", "?"))
                    )
                    return
            except Exception as exc:
                print("[ViewFieldsTable] identify failed: %s" % exc)

        QMessageBox.information(
            self,
            "Identify",
            "Unable to run the Identification backend for point index %d." % pidx,
        )

    def _select_by_class(self):
        row = self._context_row
        if row is None:
            current = self.table.currentIndex()
            row = current.row() if current.isValid() else None
        if row is None or row < 0:
            return

        class_code = self.model.class_code_for_row(row)
        if class_code is None:
            QMessageBox.information(self, "Select by Class", "Classification is not available.")
            return

        rows = self.model.rows_for_class(class_code)
        if rows.size == 0:
            return

        # Convert row numbers to contiguous QItemSelection ranges.  If the
        # current table order makes one class extremely fragmented, stable-sort
        # by Class first so millions of selected rows do not become millions of
        # tiny Qt selection objects.
        def _runs(values):
            if values.size == 0:
                return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
            breaks = np.flatnonzero(np.diff(values) != 1)
            starts = np.r_[0, breaks + 1]
            ends = np.r_[breaks, values.size - 1]
            return values[starts], values[ends]

        run_starts, run_ends = _runs(rows)
        if run_starts.size > 5000 and self.model.sort_by_class_code():
            rows = self.model.rows_for_class(class_code)
            run_starts, run_ends = _runs(rows)
            self._show_status("Table sorted by Class for efficient class selection")

        last_col = max(self.model.columnCount() - 1, 0)
        selection = QItemSelection()
        for start_row, end_row in zip(run_starts, run_ends):
            selection.select(
                self.model.index(int(start_row), 0),
                self.model.index(int(end_row), last_col),
            )

        sel_model = self.table.selectionModel()
        self._programmatic_class_selection = True
        try:
            sel_model.select(
                selection,
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
            )
            first = self.model.index(int(rows[0]), 0)
            self.table.setCurrentIndex(first)
            self.table.scrollTo(first, QTableView.PositionAtCenter)
            self._class_selected_rows = rows
        finally:
            self._programmatic_class_selection = False

        self._show_status(
            "Selected %s row(s) in class %d (%s)" % (
                format(int(rows.size), ","), class_code, _class_name(class_code)
            )
        )

    def _sort_selected(self, order):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        cols = sel.selectedColumns()
        col = cols[0].column() if cols else 0
        self.table.sortByColumn(col, order)

    def _export_csv(self):
        selected_rows = self._selected_rows()
        n_rows = self.model.rowCount()
        if selected_rows.size:
            rows = selected_rows
            default_name = "selected_points.csv"
        else:
            if n_rows > 1_000_000:
                QMessageBox.warning(
                    self, "Export CSV",
                    "This would export %s rows. Exporting the full table "
                    "to CSV can be very large; select a subset of rows to "
                    "export instead." % format(n_rows, ","))
                return
            rows = range(n_rows)
            default_name = "point_fields.csv"

        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", default_name, "CSV Files (*.csv)")
        if not path:
            return
        try:
            headers = [self.model.headerData(c, Qt.Horizontal, Qt.DisplayRole)
                       for c in range(self.model.columnCount())]
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(",".join('"%s"' % h.replace('"', '""') for h in headers) + "\n")
                for row in rows:
                    row = int(row)
                    cells = [str(self.model.data(self.model.index(row, c), Qt.DisplayRole) or "")
                             for c in range(self.model.columnCount())]
                    fh.write(",".join('"%s"' % c.replace('"', '""') for c in cells) + "\n")
            QMessageBox.information(
                self, "Export CSV",
                "Exported %s rows to %s" % (format(len(rows), ","), path)
            )
        except Exception as exc:
            QMessageBox.critical(self, "Export CSV", "Export failed: %s" % exc)

    # -- context menu -----------------------------------------------------
    def _build_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid():
            return

        self._context_row = index.row()
        sel_model = self.table.selectionModel()

        # A right-click must operate on the row under the cursor, not an old
        # selection from somewhere else.  Preserve an existing multi-selection
        # when the user right-clicks inside it.
        row_is_selected = sel_model.isRowSelected(index.row(), QModelIndex())
        if not row_is_selected:
            self._class_selected_rows = None
            sel_model.select(
                self.model.index(index.row(), 0),
                QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
            )
            self.table.setCurrentIndex(self.model.index(index.row(), 0))

        class_code = self.model.class_code_for_row(index.row())

        menu = QMenu(self)
        copy_label = "Copy Row" if self._selected_rows().size <= 1 else "Copy Selected Rows"
        menu.addAction(copy_label, self._copy_selected_row)
        menu.addAction("Export Selection to CSV", self._export_csv)

        if class_code is not None:
            menu.addSeparator()
            menu.addAction(
                "Select by Class — %d (%s)" % (class_code, _class_name(class_code)),
                self._select_by_class,
            )

        menu.addSeparator()
        menu.addAction("Zoom to Point in 3D", self._go_to_selected)
        menu.addAction("Identify Point", self._identify_selected)
        menu.exec_(self.table.viewport().mapToGlobal(pos))
