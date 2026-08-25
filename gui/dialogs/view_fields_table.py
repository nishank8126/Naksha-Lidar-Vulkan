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
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QVBoxLayout,
    QHBoxLayout,
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


# GPS epoch (seconds since 1980-01-06 00:00:00 UTC) used by LAS gps_time.
_GPS_EPOCH = datetime.datetime(1980, 1, 6)


def _gps_to_date_string(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    dt = _GPS_EPOCH + datetime.timedelta(seconds=v)
    # Some writers store "adjusted" GPS time (large values); fall back to unix.
    if dt.year > 2100 or dt.year < 1980:
        try:
            dt = datetime.datetime.utcfromtimestamp(v)
        except (OverflowError, OSError, ValueError):
            return "%.6f" % v
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
        self._columns = self._build_columns()

    # -- column discovery -------------------------------------------------
    def _build_columns(self):
        cols = []
        present = set(self._data.keys())
        for header, kind, key in COLUMN_DEFS:
            # x/y/z are always derived from xyz even if not separate keys
            if key in ("x", "y", "z"):
                if "xyz" in self._data:
                    cols.append((header, kind, key))
                continue
            if key in present and self._data[key] is not None:
                cols.append((header, kind, key))
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
        cols = [c for c in self._build_columns() if c[0] in wanted]
        if not cols:
            return False
        self._columns = cols
        self.layoutChanged.emit()
        return True

    # -- required model API ----------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return int(self._order.shape[0])

    def columnCount(self, parent=QModelIndex()):
        return len(self._columns)

    def headerData(self, section, orientation, role):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self._columns[section][0]
        return None

    def _raw_value(self, header, kind, key, row):
        """Return the unformatted value for a (row, column)."""
        r = self._order[row]
        if key in ("x", "y", "z"):
            axis = {"x": 0, "y": 1, "z": 2}[key]
            return self._data["xyz"][r, axis]
        arr = self._data.get(key)
        if arr is None:
            return None
        return arr[r]

    def data(self, index, role=Qt.DisplayRole):
        if role != Qt.DisplayRole or not index.isValid():
            return None
        row, col = index.row(), index.column()
        header, kind, key = self._columns[col]

        if kind == K_DESC:
            code = self._raw_value(header, K_CODE, "classification", row)
            return _class_name(code)
        if kind == K_DATE:
            val = self._raw_value(header, K_TIME, "gps_time", row)
            return _gps_to_date_string(val) if val is not None else ""
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
            axis = {"x": 0, "y": 1, "z": 2}[key]
            return self._data["xyz"][:, axis]
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
        r = self._order[row]
        return self._data["xyz"][r]


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
                dims = {str(d).lower() for d in las.header.point_format.dimension_names}
                needed = [w for w in self._wanted if w in dims]
                if needed:
                    points = las.read()
                    for w in needed:
                        try:
                            result[w] = np.asarray(getattr(points, w))
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
        self.setMinimumSize(900, 420)

        self.setStyleSheet(get_dialog_stylesheet() + self._extra_stylesheet())

        layout = QVBoxLayout(self)
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

        dlg = ViewFieldsDialog(self.filename, parent=self)
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
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.verticalHeader().setVisible(False)
        self.table.clicked.connect(self._on_row_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._build_context_menu)
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
        wanted = [k for (_h, _kind, k) in COLUMN_DEFS
                  if k not in self.app_data or self.app_data.get(k) is None]
        wanted = [w for w in wanted if w not in ("x", "y", "z")]
        if not wanted or not self.filename:
            return
        n_points = len(self.app_data.get("xyz", []))
        if n_points > self.EXTRA_FIELD_LIMIT:
            print("[ViewFieldsTable] skipping extra field read (%d points)" % n_points)
            return
        self._loader = _ExtraFieldLoader(self.filename, wanted, self)
        self._loader.finished_loading.connect(self._on_extra_fields_loaded)
        self._loader.start()

    def _on_extra_fields_loaded(self, result):
        if result:
            for k, v in result.items():
                self.app_data[k] = v
            self.model.refresh_columns()

    # -- row interactions -------------------------------------------------
    def _on_row_clicked(self, index):
        if not index.isValid():
            return
        self._highlight_point(self.model.xyz_for_row(index.row()))

    def _highlight_point(self, xyz):
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
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputConnection(sphere.GetOutputPort())
            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            actor.GetProperty().SetColor(1.0, 0.2, 0.2)
            actor.GetProperty().SetRepresentationToWireframe()
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
            mn = xyz.min(axis=0)
            mx = xyz.max(axis=0)
            diag = float(np.linalg.norm(mx - mn))
            return max(diag / 5000.0, 1e-6)
        except Exception:
            return 1.0

    def _go_to_selected(self):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        row = sel.selectedRows()[0].row()
        xyz = self.model.xyz_for_row(row)
        if self._center_camera(xyz):
            self._highlight_point(xyz)

    def _center_camera(self, xyz):
        app = self.parent()
        if app is None or not hasattr(app, "vtk_widget"):
            return False
        try:
            import vtk
            renderer = app.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            focus = camera.GetFocalPoint()
            pos = camera.GetPosition()
            delta = (xyz[0] - focus[0], xyz[1] - focus[1], xyz[2] - focus[2])
            camera.SetFocalPoint(xyz[0], xyz[1], xyz[2])
            camera.SetPosition(pos[0] + delta[0], pos[1] + delta[1], pos[2] + delta[2])
            renderer.ResetCameraClippingRange()
            app.vtk_widget.render()
            return True
        except Exception as exc:
            print("[ViewFieldsTable] camera move failed: %s" % exc)
            return False

    # -- actions ----------------------------------------------------------
    def _copy_selected_row(self):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        row = sel.selectedRows()[0].row()
        cells = []
        for col in range(self.model.columnCount()):
            cells.append(str(self.model.data(self.model.index(row, col))))
        QMessageBox.information(self, "Row %d" % row, "\t".join(cells))

    def _identify_selected(self):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        row = sel.selectedRows()[0].row()
        pidx = self.model.point_index(row)
        app = self.parent()
        identify = getattr(app, "activate_identification_at_point", None)
        if callable(identify):
            try:
                identify(pidx)
                return
            except Exception as exc:
                print("[ViewFieldsTable] identify failed: %s" % exc)
        QMessageBox.information(
            self, "Identify",
            "Selected point index: %d\n(row %d in the current view)" % (pidx, row))

    def _sort_selected(self, order):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        cols = sel.selectedColumns()
        col = cols[0].column() if cols else 0
        self.table.sortByColumn(col, order)

    def _export_csv(self):
        sel = self.table.selectionModel()
        has_selection = sel is not None and sel.hasSelection()
        n_rows = self.model.rowCount()
        if has_selection:
            rows = sorted(idx.row() for idx in sel.selectedRows())
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
                    cells = [str(self.model.data(self.model.index(row, c)))
                             for c in range(self.model.columnCount())]
                    fh.write(",".join('"%s"' % c.replace('"', '""') for c in cells) + "\n")
            QMessageBox.information(self, "Export CSV",
                                    "Exported %s rows to %s" % (format(len(rows), ","), path))
        except Exception as exc:
            QMessageBox.critical(self, "Export CSV", "Export failed: %s" % exc)

    # -- context menu -----------------------------------------------------
    def _build_context_menu(self, pos):
        sel = self.table.selectionModel()
        if sel is None or not sel.hasSelection():
            return
        menu = QMenu(self)
        menu.addAction("Copy Row", self._copy_selected_row)
        menu.addAction("Export Selection to CSV", self._export_csv)
        menu.addSeparator()
        menu.addAction("Zoom to Point in 3D", self._go_to_selected)
        menu.addAction("Identify Point", self._identify_selected)
        menu.exec_(self.table.viewport().mapToGlobal(pos))
