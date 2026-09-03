import os
import random
import vtk
import time
import numpy as np
import pyvista as pv
from pyvistaqt import QtInteractor
from .cross_section.interactor_classify import ClassificationInteractor
# Add this with your other imports at the top
from gui.brush_size_dialog import activate_brush_tool_with_dialog, show_brush_size_dialog
from .vtk_safety import VTKSafetyManager, safe_render, _validate_vtk_widget, safe_vtk_operation

from PySide6.QtWidgets import (
    QMainWindow, QFileDialog, QMessageBox, QWidget, QVBoxLayout, QInputDialog,
    QDockWidget, QTreeWidget, QTreeWidgetItem, QMenu, QColorDialog, QSplitter, QComboBox,
    QPushButton, QLabel, QSizePolicy, QHBoxLayout,
    QDialog, QStatusBar
)
from PySide6.QtCore import QObject, QEvent, Qt, QTimer, QSettings, Signal, QThread
from PySide6.QtWidgets import QSlider
# Add with your other gui imports (around line 20-30)
from gui.point_count_widget import PointCountWidget, refresh_point_statistics

from PySide6.QtGui import QAction, QColor, QCursor, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from gui.menu_sidebar_system import RibbonManager

from .session_manager import SESSION  
from gui.minimize_chip import MinimizableDialogMixin, close_all_chips


# ✅ imports from your project
from .views import set_view
from .cross_section.cut_section_controller import CutSectionController
from .cross_section.interactor_slice import CrossSectionInteractor
from .cross_section.interactor_classify import ClassificationInteractor
from .cross_section.section_controller import SectionController
from .data_loader import load_lidar_file
from .pointcloud_display import update_pointcloud
from .shading_display import update_shaded_class, ShadingControlPanel, clear_shading_cache
from .display_mode import DisplayModeDialog
from .save_pointcloud import save_pointcloud
from .clear_project import clear_project, clear_point_cloud
from .shortcut_manager import ShortcutManager
from .global_shortcuts import GlobalShortcutFilter
from gui.digitize_tools import DigitizeManager
from .classification_fast import UltraFastClassifier  # ✅ CORRECT - relative import
from PySide6.QtWidgets import QApplication
from .spatial_index import build_spatial_index_auto
from pyproj import CRS  

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None

def patch_pyvistaqt_close():
    """
    Fix PyVistaQt's close() method to handle already-deleted timers.
    This prevents AttributeError/RuntimeError during app shutdown.
    """
    try:
        from pyvistaqt import QtInteractor
        
        # Store original close method
        original_close = QtInteractor.close
        
        def safe_close(self):
            """Safe close that handles deleted timers"""
            try:
                # Stop timer safely
                if hasattr(self, 'render_timer') and self.render_timer is not None:
                    try:
                        self.render_timer.stop()
                    except RuntimeError:
                        # Timer already deleted - this is OK
                        pass
                    self.render_timer = None
            except Exception:
                pass
            
            # Call original close (skip timer part)
            try:
                # Manually do what original close does (without timer)
                if hasattr(self, 'iren') and self.iren:
                    self.iren.close()
            except Exception:
                pass
        
        # Replace close method
        QtInteractor.close = safe_close
        print("✅ PyVistaQt close() method patched")
        
    except Exception as e:
        print(f"⚠️ PyVistaQt patch failed (not critical): {e}")

# Apply patch immediately
patch_pyvistaqt_close()

# # ---------------- Main Application Window ----------------

_SHUTDOWN_THREAD_GUARD = []


class Disable3DDoubleClickFilter(QObject):
    """
    Blocks double-click on the MAIN VTK interactor so app won't auto-switch to 3D.
    3D view remains accessible only via Views -> 3D button.
    """
    def __init__(self, app):
        super().__init__()
        self.app = app

    def eventFilter(self, obj, event):
        if event.type() == QEvent.MouseButtonDblClick:
            # block only left double click (safe)
            try:
                if event.button() == Qt.LeftButton:
                    event.accept()
                    return True
            except Exception:
                event.accept()
                return True
        return False


class MainWheelZoomEventFilter(QObject):
    """
    Route main-canvas 2D wheel input through the immediate zoom controller.

    A VTK observer callback cannot set the abort flag on
    ``vtkRenderWindowInteractor``.  Consuming the Qt wheel event before
    ``QtInteractor.wheelEvent`` guarantees one camera mutation per physical
    event. Native 3D wheel behavior is left untouched.
    """

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        # Keep ownership in the Qt filter as well as on the app.  If a queued
        # move reports that the button is already up, the app finishes the pan
        # immediately and this flag still lets us swallow the later physical
        # release instead of leaking an unmatched release into VTK.
        self._owns_main_pan = False
        self._owned_main_pan_button = None

    @staticmethod
    def _left_button_is_owned_by_tool(app):
        """Mirror the established swapper guard before claiming left drag."""
        if getattr(app, "active_classify_tool", None) is not None:
            return True
        if getattr(app, "cross_section_active", False):
            return True
        digitizer = getattr(app, "digitizer", None)
        if digitizer is not None and getattr(digitizer, "active_tool", None) is not None:
            return True
        element_select = getattr(digitizer, "_element_select_tool", None) if digitizer is not None else None
        if element_select is not None and getattr(element_select, "_active", False):
            return True
        measurement = getattr(app, "measurement_tool", None)
        if measurement is not None and getattr(measurement, "is_measuring", False):
            return True
        # These tools require the original left click for picking. Do not let
        # configured left-button navigation claim it as a camera pan.
        for tool_name in (
            "identification_tool",
            "point_sync_tool",
            "snt_layer_pick_tool",
        ):
            tool = getattr(app, tool_name, None)
            if tool is not None and getattr(tool, "active", False):
                return True
        return False

    def eventFilter(self, obj, event):
        app = self.app
        if app is None:
            return False
        event_type = event.type()

        # VTK can consume a canvas key press before the application-wide
        # shortcut filter sees it. Keep Escape reliable for click-identify
        # modes at the canvas boundary, while leaving every other Escape
        # action untouched when no such mode is active.
        if event_type == QEvent.KeyPress:
            try:
                is_plain_escape = (
                    event.key() == Qt.Key_Escape
                    and event.modifiers() == Qt.NoModifier
                )
            except Exception:
                is_plain_escape = False
            if is_plain_escape:
                deactivate = getattr(
                    app,
                    "_deactivate_active_identification_tools_for_escape",
                    None,
                )
                if callable(deactivate) and deactivate():
                    event.accept()
                    return True
            return False

        # Main 2D middle-pan is owned at the Qt boundary. This prevents the
        # same physical drag from reaching both the digitizer's manual camera
        # path and VTK's interactor style.
        if event_type in (
            QEvent.MouseButtonPress,
            QEvent.MouseMove,
            QEvent.MouseButtonRelease,
        ):
            if getattr(app, "_shutdown_in_progress", False):
                return False
            if getattr(app, "is_3d_mode", False):
                return False
            if getattr(app, "active_classify_tool", None) is not None:
                return False

            active = bool(getattr(app, "_qt_main_pan_active", False))
            try:
                if event_type == QEvent.MouseButtonPress:
                    pressed_button = event.button()
                    left_pan_enabled = bool(
                        getattr(app, "_left_pan_shortcut_active", False)
                    )
                    if pressed_button == Qt.MiddleButton:
                        # Physical middle is always pan, irrespective of which
                        # configurable primary pan button is selected.
                        pan_button = Qt.MiddleButton
                    elif pressed_button == Qt.LeftButton and left_pan_enabled:
                        pan_button = Qt.LeftButton
                    else:
                        return False
                    if (
                        pan_button == Qt.LeftButton
                        and self._left_button_is_owned_by_tool(app)
                    ):
                        return False
                    handler = getattr(app, "_handle_fast_main_pan_press", None)
                    handled = bool(
                        handler(event.position(), obj.width(), obj.height())
                    ) if callable(handler) else False
                    if handled:
                        self._owns_main_pan = True
                        self._owned_main_pan_button = pan_button
                elif event_type == QEvent.MouseMove:
                    if not active:
                        # A right-click grid load can block Qt long enough for
                        # VTK to miss the matching release and remain in Dolly.
                        # Repair it before this hover move reaches QtInteractor.
                        if event.buttons() == Qt.NoButton:
                            repair = getattr(
                                app, "_repair_stale_main_interactor_drag", None
                            )
                            if callable(repair) and repair():
                                try:
                                    event.accept()
                                except Exception:
                                    pass
                                return True
                        return False
                    pan_button = self._owned_main_pan_button
                    if pan_button is None:
                        return False
                    pan_button_down = bool(event.buttons() & pan_button)
                    handler = getattr(app, "_handle_fast_main_pan_move", None)
                    handled = bool(
                        handler(
                            event.position(),
                            obj.width(),
                            obj.height(),
                            middle_down=pan_button_down,
                        )
                    ) if callable(handler) else False
                else:
                    pan_button = self._owned_main_pan_button
                    if pan_button is None:
                        return False
                    if event.button() != pan_button or not (
                        active or self._owns_main_pan
                    ):
                        return False
                    if active:
                        handler = getattr(app, "_handle_fast_main_pan_release", None)
                        handled = bool(handler()) if callable(handler) else False
                    else:
                        handled = True
                    self._owns_main_pan = False
                    self._owned_main_pan_button = None
            except Exception:
                handled = False

            if handled:
                try:
                    event.accept()
                except Exception:
                    pass
                return True
            return False

        if event_type != QEvent.Wheel:
            return False
        # A PRJ fly-to owns the camera until its final frame.  Touchpads and
        # high-resolution wheels can leave momentum events queued while a
        # large LAS is loading; applying those events during the animation
        # overwrites its target scale and leaves the cloud tiny/off target.
        if getattr(app, "_prj_fly_camera_active", False):
            try:
                event.accept()
            except Exception:
                pass
            return True
        if getattr(app, "_shutdown_in_progress", False):
            try:
                event.accept()
            except Exception:
                pass
            return True
        if getattr(app, "is_3d_mode", False):
            return False

        try:
            delta = float(event.angleDelta().y())
            if delta == 0.0:
                delta = float(event.pixelDelta().y())
        except Exception:
            return False
        if delta == 0.0:
            return False

        display_position = None
        try:
            vtk_widget = getattr(app, "vtk_widget", None)
            render_window = vtk_widget.GetRenderWindow() if vtk_widget is not None else None
            render_size = render_window.GetSize() if render_window is not None else None
            position = event.position()
            from gui.zoom_navigation import qt_position_to_vtk_display
            display_position = qt_position_to_vtk_display(
                position.x(),
                position.y(),
                obj.width(),
                obj.height(),
                render_size[0],
                render_size[1],
            )
        except Exception:
            # A missing position should not disable zoom. The controller can
            # still use the established VTK event position or center fallback.
            display_position = None

        handler = getattr(app, "_handle_fast_main_wheel", None)
        if not callable(handler) or not handler(delta, display_position=display_position):
            return False

        try:
            event.accept()
        except Exception:
            pass
        return True


class CanvasCursorEventFilter(QObject):
    """Keep the custom tool cursor scoped to VTK canvas widgets only."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app

    def eventFilter(self, obj, event):
        if self.app is None or getattr(self.app, "_shutdown_in_progress", False):
            return False
        if obj is None or not _qt_object_is_valid(obj):
            return False
        event_type = event.type()

        if event_type == QEvent.Enter:
            self.app._set_canvas_widget_cursor(obj, active=True)
            self.app._move_axis_guides(obj, event)
        elif event_type in (QEvent.MouseMove, QEvent.HoverMove):
            self.app._set_canvas_widget_cursor(obj, active=True)
            self.app._move_axis_guides(obj, event)
        elif event_type == QEvent.Leave:
            self.app._set_canvas_widget_cursor(obj, active=False)
            self.app._hide_axis_guides()
        elif event_type in (
            QEvent.CursorChange,
            QEvent.FocusIn,
            QEvent.Show,
            QEvent.WindowActivate,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.Wheel,
        ):
            if not getattr(self.app, "_cursor_state", False):
                return False
            if event_type == QEvent.CursorChange:
                try:
                    if bool(obj.property("canvasCursorMutating")):
                        return False
                except Exception:
                    return False
            self.app._schedule_canvas_cursor_refresh(obj)

        return False


class _StableFooterHost(QWidget):
    """Host widget that keeps status footer children in fixed regions."""

    def __init__(self, status_bar):
        super().__init__(status_bar)
        self._status_bar = status_bar

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._status_bar is not None:
            self._status_bar._layout_footer_widgets()


class _ClickableFileLabel(QLabel):
    """Elided footer filename that emits a click for full-path details."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = "File: -"
        self.setCursor(Qt.PointingHandCursor)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def setFullText(self, text):
        self._full_text = str(text or "File: -")
        self._refresh_elided_text()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_elided_text()

    def sizeHint(self):
        hint = super().sizeHint()
        hint.setWidth(hint.width() + 12)
        return hint

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def _refresh_elided_text(self):
        available = max(0, self.width() - 8)
        QLabel.setText(
            self,
            self.fontMetrics().elidedText(self._full_text, Qt.ElideMiddle, available),
        )


class StableFooterStatusBar(QStatusBar):
    """Keep footer controls stable while status text changes."""

    _MESSAGE_WIDTH = 340
    _FOOTER_GAP = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_message = ""
        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self.clearMessage)
        self.setSizeGripEnabled(False)

        self._footer_host = _StableFooterHost(self)
        self._footer_host.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        self._message_label = QLabel("", self._footer_host)
        self._message_label.setObjectName("statusMessageLabel")
        self._message_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._message_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

        self._left_widget = QWidget(self._footer_host)
        self._left_widget.setObjectName("statusLeftWidget")
        self._left_layout = QHBoxLayout(self._left_widget)
        self._left_layout.setContentsMargins(0, 0, 0, 0)
        self._left_layout.setSpacing(4)

        self._center_widget = QWidget(self._footer_host)
        self._center_widget.setObjectName("statusCenterWidget")
        self._center_layout = QHBoxLayout(self._center_widget)
        self._center_layout.setContentsMargins(0, 0, 0, 0)
        self._center_layout.setSpacing(4)

        self._right_widget = QWidget(self._footer_host)
        self._right_widget.setObjectName("statusRightWidget")
        self._right_layout = QHBoxLayout(self._right_widget)
        self._right_layout.setContentsMargins(0, 0, 0, 0)
        self._right_layout.setSpacing(4)

        super().addPermanentWidget(self._footer_host, 1)
        self._refresh_message_label()
        QTimer.singleShot(0, self._layout_footer_widgets)

    def addWidget(self, widget, stretch=0):
        if widget is None:
            return
        widget.setParent(self._center_widget)
        self._center_layout.addWidget(widget, stretch, Qt.AlignVCenter)
        self._layout_footer_widgets()

    def addLeftWidget(self, widget, stretch=0):
        if widget is None:
            return
        widget.setParent(self._left_widget)
        self._left_layout.addWidget(widget, stretch, Qt.AlignVCenter)
        self._layout_footer_widgets()

    def addPermanentWidget(self, widget, stretch=0):
        if widget is None:
            return
        widget.setParent(self._right_widget)
        self._right_layout.addWidget(widget, stretch, Qt.AlignVCenter)
        self._layout_footer_widgets()

    def messageLabel(self):
        return self._message_label

    def showMessage(self, message, timeout=0):
        text = "" if message is None else str(message)

        self._message_timer.stop()
        if text and timeout:
            self._message_timer.start(max(0, int(timeout)))

        if text == self._current_message:
            self._refresh_message_label()
            return

        self._current_message = text
        self._refresh_message_label()
        self.messageChanged.emit(self._current_message)

    def clearMessage(self):
        self._message_timer.stop()
        if not self._current_message and not self._message_label.text() and not self._message_label.toolTip():
            return

        self._current_message = ""
        self._refresh_message_label()
        self.messageChanged.emit("")

    def currentMessage(self):
        return self._current_message

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._layout_footer_widgets()

    def _layout_footer_widgets(self):
        if not hasattr(self, "_footer_host") or self._footer_host is None:
            return

        rect = self._footer_host.contentsRect()
        if rect.width() <= 0 or rect.height() <= 0:
            return

        self._right_widget.adjustSize()
        self._center_widget.adjustSize()
        self._left_widget.adjustSize()

        gap = self._FOOTER_GAP
        right_width = min(self._right_widget.sizeHint().width(), rect.width())
        right_x = rect.right() - right_width + 1
        self._right_widget.setGeometry(right_x, rect.top(), right_width, rect.height())

        center_width = min(self._center_widget.sizeHint().width(), rect.width())
        min_center_x = rect.left()
        max_center_x = right_x - gap - center_width
        center_x = rect.left() + max(0, (rect.width() - center_width) // 2)

        if max_center_x < min_center_x:
            center_x = min_center_x
        else:
            center_x = max(min_center_x, min(center_x, max_center_x))

        self._center_widget.setGeometry(center_x, rect.top(), center_width, rect.height())

        left_capacity = max(0, center_x - rect.left() - gap)
        left_width = min(self._left_widget.sizeHint().width(), 600, left_capacity)
        self._left_widget.setGeometry(rect.left(), rect.top(), left_width, rect.height())

        message_x = rect.left() + left_width + (gap if left_width else 0)
        message_capacity = max(0, center_x - gap - message_x)
        message_width = min(self._MESSAGE_WIDTH, message_capacity)
        self._message_label.setGeometry(message_x, rect.top(), message_width, rect.height())
        self._refresh_message_label()

    def _refresh_message_label(self):
        full_text = self._current_message
        available_width = max(0, self._message_label.width() - 6)
        display_text = self._message_label.fontMetrics().elidedText(
            full_text,
            Qt.ElideRight,
            available_width,
        )
        self._message_label.setText(display_text)
        self._message_label.setToolTip(full_text)


class _VTKCrosshair:
    """VTK-native crosshair drawn as a 2D overlay actor in display coords.

    Uses the same coordinate space as interactor.GetEventPosition(),
    so there is zero Qt ↔ VTK coordinate translation.
    """

    def __init__(self, renderer):
        self._renderer = renderer

        # 4 points → 2 line segments (H + V)
        self._points = vtk.vtkPoints()
        self._points.SetNumberOfPoints(4)
        for i in range(4):
            self._points.SetPoint(i, 0, 0, 0)

        lines = vtk.vtkCellArray()
        # horizontal line: point 0 → 1
        lines.InsertNextCell(2)
        lines.InsertCellPoint(0)
        lines.InsertCellPoint(1)
        # vertical line: point 2 → 3
        lines.InsertNextCell(2)
        lines.InsertCellPoint(2)
        lines.InsertCellPoint(3)

        self._polydata = vtk.vtkPolyData()
        self._polydata.SetPoints(self._points)
        self._polydata.SetLines(lines)

        mapper = vtk.vtkPolyDataMapper2D()
        mapper.SetInputData(self._polydata)

        self._actor = vtk.vtkActor2D()
        self._actor.SetMapper(mapper)
        self._actor.GetProperty().SetColor(0.70, 0.73, 0.76)
        self._actor.GetProperty().SetOpacity(0.18)
        self._actor.GetProperty().SetLineWidth(1)
        self._actor.VisibilityOff()

        renderer.AddActor2D(self._actor)

    # --- public API ---------------------------------------------------

    def update(self, x, y, w, h):
        """Move crosshair to (x, y) in VTK display pixels.  w, h = viewport size."""
        self._points.SetPoint(0, 0, y, 0)      # H left
        self._points.SetPoint(1, w, y, 0)       # H right
        self._points.SetPoint(2, x, 0, 0)       # V bottom
        self._points.SetPoint(3, x, h, 0)       # V top
        self._points.Modified()
        self._actor.VisibilityOn()

    def hide(self):
        self._actor.VisibilityOff()

    @property
    def visible(self):
        return bool(self._actor.GetVisibility())

    def set_theme(self, is_dark):
        prop = self._actor.GetProperty()
        if is_dark:
            prop.SetColor(0.70, 0.73, 0.76)
            prop.SetOpacity(0.18)
        else:
            prop.SetColor(0.10, 0.10, 0.12)
            prop.SetOpacity(0.16)

    def remove(self):
        try:
            self._renderer.RemoveActor2D(self._actor)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════
#  ADD THIS CLASS RIGHT BEFORE "class NakshaApp(QMainWindow):"
#  (after the _VTKCrosshair class, around line 178)
# ═══════════════════════════════════════════════════════════════════════

class _BackupWorker(QThread):
    """
    Writes LAZ backup in a background thread.
    All numpy data is snapshotted BEFORE starting so the main thread
    is only blocked for < 1ms during the snapshot.
    """
    finished_ok = Signal(str)
    failed = Signal(str)

    def __init__(self, snapshot, path, las_version, point_format,
                 crs_wkt=None, crs_epsg=None, drawing_data=b"",
                 source_path=None, import_options=None):
        super().__init__()
        self.setObjectName("NakshaBackupWorker")
        self._snapshot = snapshot
        self._path = path
        self._las_version = las_version
        self._point_format = point_format
        self._crs_wkt = crs_wkt
        self._crs_epsg = crs_epsg
        self._drawing_data = drawing_data
        self._source_path = source_path
        self._import_options = import_options

    def run(self):
        try:
            import laspy
            from .save_pointcloud import (
                _attach_drawings_storage,
                _atomic_write_las,
                _try_build_preserved_las,
            )

            xyz = self._snapshot["xyz"]
            n = xyz.shape[0]
            classes_u8 = self._snapshot["classes"]
            rgb16 = self._snapshot["rgb"]
            intensity16 = self._snapshot["intensity"]

            preserved_las, preserve_reason = _try_build_preserved_las(
                source_path=self._source_path,
                target_path=self._path,
                xyz=xyz,
                classes_u8=classes_u8,
                rgb16=rgb16,
                intensity16=intensity16,
                import_options=self._import_options,
                requested_version=self._las_version,
                crs_wkt=self._crs_wkt,
                crs_epsg=self._crs_epsg,
            )
            if preserved_las is not None:
                if self._drawing_data:
                    _attach_drawings_storage(
                        preserved_las,
                        self._path,
                        self._drawing_data,
                        "Digitized drawings",
                        verbose=False,
                    )
                _atomic_write_las(preserved_las, self._path)
                self.finished_ok.emit(self._path)
                return

            major, minor = map(int, self._las_version.split("."))
            header = laspy.LasHeader(
                point_format=self._point_format,
                version=f"{major}.{minor}"
            )

            try:
                import pyproj
                if self._crs_wkt:
                    header.parse_crs(pyproj.CRS.from_wkt(self._crs_wkt))
                elif self._crs_epsg:
                    header.parse_crs(pyproj.CRS.from_epsg(self._crs_epsg))
            except Exception:
                pass

            las = laspy.LasData(header)
            las.x = xyz[:, 0].copy()
            las.y = xyz[:, 1].copy()
            las.z = xyz[:, 2].copy()

            if self._las_version == "1.4":
                las.classification = np.clip(classes_u8, 0, 255).astype(np.uint8, copy=False)
            else:
                max_cls = int(classes_u8.max()) if classes_u8.size else 0
                if max_cls > 31:
                    # Preserve the full 8-bit class code in LAS 1.2 backups
                    # using the same packed-byte layout as the main save path.
                    las.classification = (classes_u8 & 0x1F).astype(np.uint8, copy=False)
                    las.synthetic = ((classes_u8 >> 5) & 1).astype(bool)
                    las.key_point = ((classes_u8 >> 6) & 1).astype(bool)
                    las.withheld  = ((classes_u8 >> 7) & 1).astype(bool)
                else:
                    las.classification = np.clip(classes_u8, 0, 31).astype(np.uint8, copy=False)

            if rgb16 is not None:
                las.red = rgb16[:, 0].copy()
                las.green = rgb16[:, 1].copy()
                las.blue = rgb16[:, 2].copy()
            if intensity16 is not None:
                las.intensity = intensity16.copy()

            if self._drawing_data:
                _attach_drawings_storage(
                    las,
                    self._path,
                    self._drawing_data,
                    "Digitized drawings",
                    verbose=False,
                )

            _atomic_write_las(las, self._path)
            self.finished_ok.emit(self._path)

        except Exception as e:
            self.failed.emit(str(e))

class NakshaApp(QMainWindow):
    # Phase 4: Global signal bus
    classification_finished = Signal(object)  # emits changed_mask (numpy array or None)

    def __init__(self):

       

        super().__init__()
        self.setContextMenuPolicy(Qt.NoContextMenu)
        try:
            from gui.app_icon import apply_window_icon, resolve_app_icon_path

            if apply_window_icon(self):
                print(f"✅ Window icon loaded: {resolve_app_icon_path()}")
            else:
                print("⚠️ Window icon not found in known icon locations")
        except Exception as e:
            print(f"⚠️ Failed to set window icon: {e}")
            import traceback
            traceback.print_exc()
 
        # ===== GPU SUPPORT INITIALIZATION =====
        from gui.gpu_support import init_gpu_support
        gpu_support = init_gpu_support()
        print(f"🖥️ Rendering backend: {gpu_support.rendering_backend}")
 
        # ===== GPU RENDER OPTIMIZATION =====
        from gui.gpu_render_manager import GPURenderManager
        self.gpu_render_manager = GPURenderManager(self)
        self.gpu_render_manager.install()

        # Phase 4: Global Signal Bus — connect classification_finished
        self.classification_finished.connect(self._on_classification_finished)
 
        # ✅ NEW: Auto-configure render delay based on GPU
        recommended_delay = gpu_support.get_recommended_render_delay()
        self.gpu_render_manager.set_render_delay(recommended_delay)
        print(f"⏱️  Render delay: {recommended_delay}ms (auto-configured for {gpu_support.gpu_name})")
 
        # ✅ Load theme FIRST
        try:
            from gui.theme_manager import ThemeManager
            saved_theme = ThemeManager.load_saved_theme()
            ThemeManager.apply_theme(self, saved_theme)
        except Exception as e:
            print(f"⚠️ Failed to apply theme: {e}")
 
        # Settings
        settings = QSettings("NakshaAI", "LidarApp")
        self.settings = settings 
        self.default_cut_width = settings.value("cut_section_width", 2.0, type=float)
        self.zoom_behavior = settings.value("view_zoom_behavior", "cursor", type=str)
        if self.zoom_behavior not in {"center", "cursor", "picked_point"}:
            self.zoom_behavior = "cursor"
        self.panning_button = settings.value("panning_button", "scroll", type=str)
        # Escape/tool exit must never arm left-click pan. Only the configured
        # Pan tool shortcut changes this state.
        self._left_pan_shortcut_active = False

        # ── Smooth (eased) mouse-wheel zoom state ──────────────────────────
        # Each wheel notch nudges a *target* zoom multiplier; a short-lived
        # timer eases the camera toward it in bounded steps so zoom stays
        # responsive even when the main actor contains tens of millions of
        # points.
        self._zoom_target_factor = 1.0      # accumulated product of pending notches
        self._zoom_anim_timer = None        # QTimer driving the easing loop
        self._zoom_anim_active = False
        self._zoom_anchor_pending = None    # (anchor_world) for picked_point mode
        from gui.zoom_navigation import DEFAULT_WHEEL_ZOOM_FACTOR
        self._zoom_notch_factor = DEFAULT_WHEEL_ZOOM_FACTOR
        self._zoom_anchor_points = {}
        self._canvas_axis_guides_enabled = settings.value("canvas_axis_guides_enabled", False, type=bool)
        self._vtk_crosshair = None              # _VTKCrosshair instance (lazy)
        self._canvas_axis_color_key = None      # cached theme key
        
        # Initialize cross-section line preferences with defaults FIRST
        self.cross_line_color = (1.0, 0.0, 1.0)  # Default magenta (normalized RGB)
        self.cross_line_width = 3
        self.cross_line_style = "solid"
        
        # Then load saved preferences from QSettings if they exist
        saved_color = settings.value("cross_line_color", None)
        if saved_color:
            try:
                from PySide6.QtGui import QColor
                qc = QColor(saved_color)
                self.cross_line_color = (qc.redF(), qc.greenF(), qc.blueF())
            except Exception as e:
                pass
        
        saved_width = settings.value("cross_line_width", None)
        if saved_width is not None:
            self.cross_line_width = int(saved_width)
        
        saved_style = settings.value("cross_line_style", None)
        if saved_style:
            self.cross_line_style = str(saved_style)
       
        # ===== SET WINDOW TITLE WITH GPU INFO =====
        if gpu_support.gpu_available:
            self.setWindowTitle(f"NakshaAI-Lidar [GPU: {gpu_support.gpu_name}]")
        else:
            self.setWindowTitle("NakshaAI-Lidar [CPU Mode]")
       
        self.resize(1400, 900)  # Bigger window
        # ✅ ADD THESE 3 LINES HERE:
        self._canvas_cursor_filter = CanvasCursorEventFilter(self)
        self._canvas_tool_cursor = self._create_canvas_tool_cursor()
        self._cursor_state = False
        self._active_tools = set()
        # Storage initialization
        self.data = None
        # Latest generated scalar elevation products, keyed by "DTM" / "DSM".
        # The cached GeoTIFFs back the Edit-ribbon preview and export actions.
        self.elevation_models = {}
        self._elevation_model_jobs = {}
        self.display_mode = "rgb"
        self.section_controller = SectionController(self)
        self.active_mode = None          
        self.last_classify_tool = None
        self.section_dock = None
        self.sec_vtk = None
        self.current_view = "top"
        self.class_palette = {}
        self.view_palettes = {}     # per-view palette isolation

        self.is_3d_mode = False
        self.cut_section_controller = CutSectionController(self)
        self._suppress_main_view_updates = False  #-----------------------------------------------code added by bala--------
        
        # CRS & Classification state
        # ``canvas_crs`` is the ONE authoritative CRS describing VTK world XY.
        # ``project_crs_*`` / ``crs`` are kept for backwards compatibility and
        # are kept in sync by gui.crs_manager - they are NOT authoritative.
        self.canvas_crs = None
        self.canvas_crs_info = {}
        self.project_crs_epsg = None
        self.project_crs_wkt = None
        self.crs = None
        self.active_classify_tool = None
        self.from_classes = None
        self.to_class = None
        self.last_shade_azimuth = 45.0
        self.last_shade_angle = 45.0
        self.shading_sharpness_angle = 45.0
        self.shade_coverage_target = 0.70
        self.shading_dock = None
        self.shading_panel = None
        self.last_shade_max_edge = 100.0
        self.point_border_percent = 0
        self.layers = []
        self.dxf_attachments = []  # Store DXF attachment metadata
        self.dxf_actors = []  
        ## bd
        self.dwg_attachments = []   # Store DWG attachment metadata
        self.dwg_actors      = []   # Store DWG VTK actor groups
        self.dwg_dialog      = None # DWG dialog reference
 
        # ── SNT state (mirrors DWG pattern exactly) ──────────────
        self.snt_attachments = []        # Store SNT attachment metadata
        self.snt_actors = []             # Store SNT VTK actor groups
        self.snt_dialog = None           # SNT dialog reference
 
        self.classify_interactors = {}  # {view_idx: ClassificationInteractor}

        
        self.view_sync_map = {}
        self._syncing_camera = False

        # ===== VTK WIDGETS SETUP =====
        self.frame = QWidget()
        self.layout = QVBoxLayout(self.frame)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self.vtk_widget = QtInteractor(self.frame)
        self._setup_interactor_swapper(self.vtk_widget.interactor)
        self.layout.addWidget(self.vtk_widget.interactor)
        # ✅ Disable double-click switching to 3D on MAIN viewer only
        if not hasattr(self, "_disable_3d_dblclick_filter"):
            self._disable_3d_dblclick_filter = Disable3DDoubleClickFilter(self)

        self.vtk_widget.interactor.installEventFilter(self._disable_3d_dblclick_filter)
        self._main_wheel_zoom_filter = MainWheelZoomEventFilter(self)
        self.vtk_widget.interactor.installEventFilter(self._main_wheel_zoom_filter)
        self._register_canvas_cursor_widget(self.vtk_widget.interactor)
        self.frame.setLayout(self.layout)

        from gui.theme_manager import ThemeManager
        bg_color = ThemeManager.canvas_background_for_theme()
        bg_rgb = ThemeManager.canvas_background_rgb()
        self.vtk_widget.set_background(bg_color)
        self.vtk_widget.renderer.SetBackground(*bg_rgb)
        self.vtk_widget.renderer.SetBackground2(*bg_rgb)
        self.vtk_widget.renderer.GradientBackgroundOff()
        self.cross_interactor = None
        self.cross_section_active = False  # track if cross-section tool is active
        self.section_locate_enabled = True
        
        try:
            iren = self.vtk_widget.interactor
            iren.AddObserver("KeyPressEvent", self._on_main_interactor_keypress)
        except Exception as e:
            print(f"⚠️ Failed to install ESC observer: {e}")
        

        self.section_frame = QWidget()
        self.section_layout = QVBoxLayout(self.section_frame)
        self.section_layout.setContentsMargins(0, 0, 0, 0)
        self.section_layout.setSpacing(0)
        self.sec_vtk = QtInteractor(self.section_frame)
        self._setup_interactor_swapper(
            self.sec_vtk.interactor,
            preserve_physical_middle_pan=True,
        )
        self.sec_vtk.set_background(bg_color)
        self.sec_vtk.renderer.SetBackground(*bg_rgb)
        self.sec_vtk.renderer.SetBackground2(*bg_rgb)
        self.sec_vtk.renderer.GradientBackgroundOff()
        self.section_layout.addWidget(self.sec_vtk.interactor)
        self.section_frame.setLayout(self.section_layout)
        self._install_section_wheel_zoom(self.sec_vtk)

        # Splitter setup (main + optional section view)
        self.splitter = QSplitter()
        self.splitter.addWidget(self.frame)
        self.splitter.addWidget(self.section_frame)
        self.splitter.setSizes([1400, 0])  # allocate all space to main view
        self.section_frame.hide()          # hide right panel initially
        self.setCentralWidget(self.splitter)

        # VTK widget exists now: ensure GPU render hooks that were deferred
        # during early app bootstrap are installed.
        if hasattr(self, "gpu_render_manager") and self.gpu_render_manager is not None:
            self.gpu_render_manager.install()
        
        # ✅ Load backup settings and start timer

        # Set default view
        set_view(self, "top")

        # Extra docks
        self.section_docks = {}     # key = view_index (0-3)
        self.section_vtks = {}      # QtInteractor objects per view
        self.cross_action = None
        self.current_saturation = 1.0   # 100% = normal
        self.current_sharpness = 1.0  # Amplifier removed — always 1.0 (no scaling)

        # ── MicroStation-style display stretch defaults ─────────────────
        self.current_saturation = 1.0
        self.current_sharpness = 1.0

        self.elevation_clip_low = 1.0
        self.elevation_clip_high = 99.0
        self.elevation_color_ramp = None
        self.surface_color_ramp = None

        self._elevation_cache_key = None
        self._elevation_cache_colors = None

        self.intensity_clip_low = 0.5
        self.intensity_clip_high = 99.8
        self.intensity_gamma = 4.0

        self.depth_clip_low = 1.0
        self.depth_clip_high = 99.0

        self.depth_color_scheme = "grayscale"
        self.depth_gamma = 1.0

        self._load_display_settings()

        self.elevation_color_ramp = None

        self.classify_interactors = {}
 
        # ===== 2D LOCK =====
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
        interactor = self.vtk_widget.interactor
        style = vtkInteractorStyleImage()
        interactor.SetInteractorStyle(style)

        cam = self.vtk_widget.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetFocalPoint(0, 0, 0)
        cam.SetPosition(0, 0, 1)
        cam.SetViewUp(0, 1, 0)
        self.vtk_widget.renderer.ResetCamera()
        self.vtk_widget.render()
        self._main_view_2d_locked = True
        # self.vtk_widget.interactor.AddObserver("RightButtonPressEvent", self.on_grid_label_right_click)
        print("✅ Grid label detection enabled (right-click)")
        

        print("🔒 Main viewer locked to 2D Plan View")

        

        # ========================================================================
        # 1. CREATE shortcuts dict FIRST
        # ========================================================================
        self.shortcuts = {}
        print("🔍 Initializing shortcuts dict")

        # ========================================================================
        # 2. REGISTER Shift+1, Shift+2 FIRST (before filter)
        # ========================================================================
        self.shortcuts[('shift', '!')] = {'tool': 'cut_section', 'from': None, 'to': None}
        self.shortcuts[('shift', '@')] = {'tool': 'cut_section_nested', 'from': None, 'to': None}
        print("✅ Shift+1='cut_section', Shift+2='cut_section_nested' REGISTERED")

        # ========================================================================
        # 3. CREATE GlobalShortcutFilter AFTER shortcuts exist
        # ========================================================================
        from .global_shortcuts import GlobalShortcutFilter

        self.short_cut_filter = GlobalShortcutFilter(self)
        print("✅ GlobalShortcutFilter created")

        # ========================================================================
        # 4. INSTALL filter SAFELY
        # ========================================================================
        self.installEventFilter(self.short_cut_filter)
        if hasattr(self, 'vtk_widget') and self.vtk_widget and hasattr(self.vtk_widget, 'interactor'):
            self.vtk_widget.interactor.installEventFilter(self.short_cut_filter)
            print("✅ Filter installed on VTK interactor")
        else:
            print("⚠️ VTK widget not ready - will retry")
            QTimer.singleShot(1000, self._install_vtk_filter)

        print("🔍 ALL SHORTCUTS:", {k: v['tool'] for k, v in self.shortcuts.items()})
              
        if not hasattr(self, 'brush_radius'):
                    self.brush_radius = 1.0  # World units (for classification)
            
        if not hasattr(self, 'brush_preview_px'):
            self.brush_preview_px = 20.0  # Pixel size for preview cursor
    
        if not hasattr(self, 'brush_shape'):
            self.brush_shape = "circle"
# ===================================================================================================================================
        from PySide6.QtCore import QMutex
        self._render_mutex = QMutex()
        print("✅ Render mutex initialized")

        # ===== INITIALIZE DIGITIZER =====
        from gui.digitize_tools import DigitizeManager
        renderer = self.vtk_widget.renderer
        self.digitizer = DigitizeManager(self, renderer, self.vtk_widget.interactor)
        self._install_canvas_axis_render_observer()
        print("✅ Digitizer initialized")   
        
        # ===== GRID LABEL HYPERLINK SYSTEM =====
        from gui.grid_label_system import add_grid_label_system_to_app
        add_grid_label_system_to_app(self)
        print("✅ Grid label hyperlink system initialized")

        from gui.select_rectangle_tool import SelectRectangleTool
        self.select_rectangle_tool = SelectRectangleTool(self)
        print("✅ Select Rectangle tool initialized")           
        
        # ----------------------------------------------------------------------------------------------------------------------------------
        from gui.measurement_tools import MeasurementTool
        self.measurement_tool = MeasurementTool(self.digitizer)
        print("✅ Measurement tool initialized")

        from gui.identification_tool import IdentificationTool
        self.identification_tool = IdentificationTool(self)
        print("✅ Identification tool initialized")

        from gui.point_sync_tool import PointSyncTool
        self.point_sync_tool = PointSyncTool(self)
        print("✅ Point sync tool initialized")

        from gui.snt_layer_pick_tool import SNTLayerPickTool
        self.snt_layer_pick_tool = SNTLayerPickTool(self)
        print("✅ SNT layer pick tool initialized")

        from gui.zoom_rectangle_tool import ZoomRectangleTool
        self.zoom_rectangle_tool = ZoomRectangleTool(self)
        print("✅ Zoom rectangle tool initialized")

        from gui.curve_tools import CurveTool
        self.curve_tool = CurveTool(self)
        print("✅ Curve tool initialized") 

        # ===== CREATE MENUS FIRST =====
        self._create_menus()

        # ===== THEN CREATE RIBBON MANAGER =====
        self.ribbon_manager = RibbonManager(self)

        # ===== INIT PLUGIN MANAGER & LOAD PLUGINS =====
        from gui.plugin_manager import PluginManager
        self.plugin_manager = PluginManager(self)
        try:
            self.plugin_manager.load_all()
        except Exception as e:
            print(f"⚠️ Failed to load startup plugins: {e}")
                
        # ========================================
        # TEST BORDER CONNECTION - after ribbon_manager setup
        # ========================================
        # ✅ Performance monitoring


        self._perf_timers = {}

        def start_timer(name):
            """Start timing an operation."""
            self._perf_timers[name] = time.time()

        def end_timer(name):
            """End timing and print result."""
            if name in self._perf_timers:
                elapsed = (time.time() - self._perf_timers[name]) * 1000
                print(f"⏱️ {name}: {elapsed:.1f}ms")
                del self._perf_timers[name]

        # Attach to app
        self.start_timer = start_timer
        self.end_timer = end_timer

        self._setup_sidebar_layout()
        self._connect_sidebar_actions()

        # ===== STATUS BAR (Ring App Style) =====
        self.status = StableFooterStatusBar(self)
        self.setStatusBar(self.status)
        self.status.setStyleSheet("""
            QStatusBar {
                max-height: 22px;
            }
            QStatusBar::item { 
                border: none; 
            } 
            QLabel { 
                font-weight: normal; 
                font-size: 11px; 
                padding: 0 2px; 
            } 
            QComboBox { 
                font-size: 11px;
                padding: 0 4px;
                max-height: 18px;
            }
            QLabel#statusMessageLabel {
                padding: 0 4px 0 2px;
            }
        """)

        from PySide6.QtWidgets import QComboBox, QCheckBox, QToolButton
        from PySide6.QtCore import QSize

        # Loaded source filename (left). The full path is available on click.
        self.loaded_file_footer_label = _ClickableFileLabel(self.status)
        self.loaded_file_footer_label.setObjectName("loadedFileFooterLabel")
        self.loaded_file_footer_label.setMinimumWidth(180)
        self.loaded_file_footer_label.setMaximumWidth(600)
        self.loaded_file_footer_label.clicked.connect(self._show_loaded_file_paths)
        self.status.addLeftWidget(self.loaded_file_footer_label, 1)

        # 1. Total Points (Centered)
        self.total_points_label = QLabel("Total Points: 0")
        self.total_points_label.setAlignment(Qt.AlignCenter)
        self.status.addWidget(self.total_points_label, 1)
        self._refresh_footer_file_label()

        # 2. Synced point target toggle
        self.point_sync_footer_btn = QToolButton(self.status)
        self.point_sync_footer_btn.setObjectName("statusTargetButton")
        self.point_sync_footer_btn.setCheckable(True)
        self.point_sync_footer_btn.setAutoRaise(True)
        self.point_sync_footer_btn.setFixedSize(22, 20)
        self.point_sync_footer_btn.setIconSize(QSize(16, 16))
        self.point_sync_footer_btn.setCursor(Qt.PointingHandCursor)
        self.point_sync_footer_btn.setFocusPolicy(Qt.NoFocus)
        self.point_sync_footer_btn.setToolTip("Sync point target across Main / Cross / Cut views")
        self.point_sync_footer_btn.setStyleSheet("""
            QToolButton {
                border: 1px solid transparent;
                border-radius: 4px;
                padding: 0px;
                margin: 0 2px;
            }
            QToolButton:hover {
                background: rgba(255, 255, 255, 0.08);
            }
            QToolButton:checked {
                background: rgba(255, 255, 255, 0.14);
                border-color: rgba(255, 255, 255, 0.18);
            }
        """)
        self.point_sync_footer_btn.toggled.connect(self._toggle_point_sync_from_footer)
        self.status.addPermanentWidget(self.point_sync_footer_btn)

        self._update_point_sync_footer_icon()
        self._update_point_sync_footer_state_visual(False)

        # 3. SNT layer pick toggle + result text
        self.snt_layer_pick_footer_btn = QToolButton(self.status)
        self.snt_layer_pick_footer_btn.setObjectName("statusSntLayerPickButton")
        self.snt_layer_pick_footer_btn.setCheckable(True)
        self.snt_layer_pick_footer_btn.setAutoRaise(True)
        self.snt_layer_pick_footer_btn.setFixedSize(22, 20)
        self.snt_layer_pick_footer_btn.setIconSize(QSize(16, 16))
        self.snt_layer_pick_footer_btn.setCursor(Qt.PointingHandCursor)
        self.snt_layer_pick_footer_btn.setFocusPolicy(Qt.NoFocus)
        self.snt_layer_pick_footer_btn.setToolTip("Identify layer on main-view click")
        self.snt_layer_pick_footer_btn.toggled.connect(self._toggle_snt_layer_pick_from_footer)
        self.status.addPermanentWidget(self.snt_layer_pick_footer_btn)

        self.snt_layer_pick_footer_label = QLabel("Layer: -")
        self.snt_layer_pick_footer_label.setObjectName("sntLayerPickFooterLabel")
        self.snt_layer_pick_footer_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.snt_layer_pick_footer_label.setMinimumWidth(210)
        self.status.addPermanentWidget(self.snt_layer_pick_footer_label)

        self._update_snt_layer_pick_footer_icon()
        self._update_snt_layer_pick_footer_state_visual(False)

        # 4. Cursor-following axis guides
        axis_tooltip = "Show full-canvas X/Y guides that follow the cursor"
        self.axis_guides_widget = QWidget(self.status)
        axis_layout = QHBoxLayout(self.axis_guides_widget)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        axis_layout.setSpacing(4)
        self.axis_guides_label = QLabel("Axis")
        self.axis_guides_label.setToolTip(axis_tooltip)
        self.axis_guides_toggle = QCheckBox()
        self.axis_guides_toggle.setText("")
        self.axis_guides_toggle.setToolTip(axis_tooltip)
        self.axis_guides_toggle.setFixedSize(14, 14)
        self.axis_guides_toggle.setStyleSheet("""
            QCheckBox {
                margin: 0px;
                padding: 0px;
            }
            QCheckBox::indicator {
                width: 10px;
                height: 10px;
                margin: 0px;
            }
        """)
        self.axis_guides_toggle.setChecked(bool(self._canvas_axis_guides_enabled))
        self.axis_guides_toggle.toggled.connect(self._on_canvas_axis_guides_toggled)
        axis_layout.addWidget(self.axis_guides_label)
        axis_layout.addWidget(self.axis_guides_toggle)
        self.status.addPermanentWidget(self.axis_guides_widget)

        # 5. Magnifier (Right)
        self.magnifier_label = QLabel("Magnifier")
        self.magnifier_combo = QComboBox()
        self.magnifier_combo.addItems(["50%", "100%", "200%", "400%"])
        self.magnifier_combo.setCurrentText("100%")
        self.magnifier_combo.setEditable(True)
        self.magnifier_combo.currentTextChanged.connect(self._on_magnifier_changed)
        self.status.addPermanentWidget(self.magnifier_label)
        self.status.addPermanentWidget(self.magnifier_combo)

        # Globe Icon and EPSG projection label (far right side, QGIS style)
        self.epsg_widget = QWidget(self.status)
        self.epsg_widget.setObjectName("epsgWidget")
        
        epsg_layout = QHBoxLayout(self.epsg_widget)
        epsg_layout.setContentsMargins(0, 0, 0, 0)
        epsg_layout.setSpacing(2)
        
        self.epsg_icon_label = QLabel()
        self.epsg_icon_label.setObjectName("epsgIconLabel")
        self.epsg_icon_label.setToolTip("Coordinate Reference System projection code (EPSG)")
        
        self.epsg_prefix_label = QLabel("EPSG:")
        self.epsg_prefix_label.setObjectName("epsgPrefixLabel")
        self.epsg_prefix_label.setToolTip("Coordinate Reference System projection code (EPSG)")
        self.epsg_prefix_label.setStyleSheet("padding: 0px; margin: 0px;")
        
        self.epsg_label = QLabel("-")
        self.epsg_label.setObjectName("epsgLabel")
        self.epsg_label.setToolTip("Coordinate Reference System projection code (EPSG)")
        
        epsg_layout.addWidget(self.epsg_icon_label)
        epsg_layout.addWidget(self.epsg_prefix_label)
        epsg_layout.addWidget(self.epsg_label)
        epsg_layout.addStretch(0)
        
        self._update_epsg_style()

        self.status.addPermanentWidget(self.epsg_widget)

        # Hook up mouse scrolling to update the magnifier UI smoothly
        def _sync_magnifier_from_scroll(obj, event):
            if not hasattr(self, "_current_zoom_level"):
                self._current_zoom_level = 100.0
            
            # VTK's default mouse wheel zoom factor is approximately exactly 1.1 per chunk
            factor = 1.1 if event == "MouseWheelForwardEvent" else (1.0 / 1.1)
            self._current_zoom_level *= factor
            
            # Clamp limits (e.g., 10% to 5000%)
            self._current_zoom_level = max(10.0, min(self._current_zoom_level, 5000.0))
            
            zoom_int = int(round(self._current_zoom_level))
            self.magnifier_combo.blockSignals(True)
            self.magnifier_combo.setCurrentText(f"{zoom_int}%")
            self.magnifier_combo.blockSignals(False)

        try:
            self.vtk_widget.interactor.AddObserver("MouseWheelForwardEvent", _sync_magnifier_from_scroll, 0.5)
            self.vtk_widget.interactor.AddObserver("MouseWheelBackwardEvent", _sync_magnifier_from_scroll, 0.5)
        except Exception as e:
            print(f"⚠️ Failed to bind mouse wheel to magnifier: {e}")

        try:
            self.vtk_widget.interactor.AddObserver("MouseWheelForwardEvent", self._on_main_mouse_wheel, 10.0)
            self.vtk_widget.interactor.AddObserver("MouseWheelBackwardEvent", self._on_main_mouse_wheel, 10.0)
            self.vtk_widget.interactor.AddObserver("LeftButtonPressEvent", self._on_main_left_press_2d_guard, 50.0)
            self.vtk_widget.interactor.AddObserver("LeftButtonDoubleClickEvent", self._on_main_left_double_click_guard, 100.0)
            self.vtk_widget.interactor.AddObserver("LeftButtonPressEvent", self._on_main_left_click_for_zoom_anchor, 0.1)
        except Exception as e:
            print(f"Failed to install custom wheel zoom handler: {e}")

        try:
            self._init_main_view_history()
        except Exception as e:
            print(f"Failed to initialize main view history: {e}")

        self._update_window_title(None, None)
        self.status.showMessage("Ready", 3000)

        # ===== SHORTCUTS (global install — filter already created above) =====
        try:
            app = QApplication.instance()
            if app is not None:
                app.installEventFilter(self.short_cut_filter)
        except Exception:
            pass

        # Temporary test shortcut - Remove after testing
        self.shortcut_backup = QShortcut(QKeySequence("Ctrl+B"), self)
        self.shortcut_backup.activated.connect(self.open_backup_settings)
        print("🧪 Press Ctrl+B to open Backup Settings")

        # self._shortcut_alt_f4 = QShortcut(QKeySequence("Alt+F4"), self)
        # self._shortcut_alt_f4.setContext(Qt.ApplicationShortcut)
        # self._shortcut_alt_f4.activated.connect(self._on_alt_f4_blocked)

        self._soak_telemetry = None
        self.shortcut_soak_telemetry = QShortcut(QKeySequence("Ctrl+Shift+T"), self)
        self.shortcut_soak_telemetry.activated.connect(self.open_soak_telemetry)
        print("🧪 Press Ctrl+Shift+T to open Soak Telemetry")
        self._start_soak_telemetry_on_startup()

        # ── GIS overlay layers (imported .shp / .tif / .geojson) ─────────────
        # Managed by the Overlay Control Center dock (Global-Mapper style).
        self.gis_layers = []                 # registry of imported overlay layers
        self._gis_layers_dock = None         # lazily created QDockWidget
        self.shortcut_gis_layers = QShortcut(QKeySequence("Alt+C"), self)
        self.shortcut_gis_layers.activated.connect(self.toggle_gis_layers_panel)
        print("🗺️ Press Alt+C to open the Overlay Control Center")

        self.shortcut_gdb = QShortcut(QKeySequence("Alt+G"), self)
        self.shortcut_gdb.activated.connect(self.toggle_gdb_panel)
        print("🗄️ Press Alt+G to open the GDB import panel")

        # Allow GIS files to be dropped straight onto the window (Global-Mapper style).
        self.setAcceptDrops(True)

        # Undo/Redo
        self.undo_stack = []
        self.redo_stack = []
        self._last_changed_mask = None
        self._last_changed_indices = None
        self.classification_revision = 0
        self._max_undo_steps = 30  # Global cap for classification undo/redo
        # Aliases — point to the SAME list objects (never reassign, always use .clear())
        self.undostack = self.undo_stack
        self.redostack = self.redo_stack
        print("✅ Undo/Redo stacks initialized (0 steps)")

        self._pending_view_updates = set()
        self._update_debounce_timer = QTimer(self)
        self._update_debounce_timer.setSingleShot(True)
        self._update_debounce_timer.timeout.connect(self._execute_pending_updates)
        self._last_changed_mask = None
        self._last_changed_indices = None

        self.last_auto_save_time = 0
        self.auto_backup_timer = QTimer(self)
        self.auto_backup_timer.timeout.connect(self._auto_backup)
        self._backup_worker_running = False
        self._active_backup_worker = None
        # ✅ Load settings and start timer with user's interval
        self._load_backup_settings()

        # Color thread
        self.color_thread = QThread()
        self.color_thread.setObjectName("NakshaColorThread")
        self.color_thread.start()
        self.color_workers = []
        print("✅ Application initialized successfully")  

        #Added by bala
        # ============================================================
        # MEMORY / SESSION MAINTENANCE (VERY IMPORTANT)
        # ============================================================
        self.__session_maintenance_timer = QTimer(self)
        self.__session_maintenance_timer.setInterval(30_000)  # 30s — SESSION only acts every 120s
        self.__session_maintenance_timer.timeout.connect(self._on_session_tick)

        QTimer.singleShot(
            0,
            self.__session_maintenance_timer.start
        )                                                              #####

        # ===== MEMORY LEAK GUARD =====
        try:
            from .memory_manager import MemoryLeakGuard

            self._mem_guard = MemoryLeakGuard(self)
            self._mem_guard.start()
        except Exception as e:
            print(f"⚠️ MemoryLeakGuard init failed (non-critical): {e}")
            self._mem_guard = None



    def update_total_points_label(self, total_points=None):
        if not hasattr(self, "total_points_label"):
            return

        if total_points is None:
            total_points = 0
            data = getattr(self, "data", None)
            xyz = data.get("xyz") if isinstance(data, dict) else None
            if xyz is not None:
                total_points = len(xyz)

        self.total_points_label.setText(f"Total Points: {total_points:,}")
        self._refresh_footer_file_label()

    def _footer_loaded_source_paths(self):
        """Return de-duplicated loaded source paths in display priority order."""
        paths = []
        seen = set()

        for store_name in ("snt_attachments", "dxf_attachments"):
            for attachment in getattr(self, store_name, []) or []:
                if not isinstance(attachment, dict):
                    continue
                value = (
                    attachment.get("full_path")
                    or attachment.get("filepath")
                    or attachment.get("file_path")
                    or attachment.get("filename")
                )
                if not value:
                    continue
                path = os.path.normpath(str(value))
                key = os.path.normcase(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(path)

        if not paths:
            value = getattr(self, "loaded_file", None) or getattr(self, "last_save_path", None)
            if value:
                paths.append(os.path.normpath(str(value)))
        return paths

    def _refresh_footer_file_label(self):
        label = getattr(self, "loaded_file_footer_label", None)
        if label is None:
            return
        paths = self._footer_loaded_source_paths()
        names = [os.path.basename(path) or path for path in paths]
        display = f"File: {', '.join(names)}" if names else "File: -"
        label.setFullText(display)
        label.setToolTip("\n".join(paths) if paths else "No file loaded")

    def _show_loaded_file_paths(self):
        paths = self._footer_loaded_source_paths()
        if not paths:
            QMessageBox.information(self, "Loaded File", "No file is currently loaded.")
            return
        heading = "Loaded file path:" if len(paths) == 1 else "Loaded file paths:"
        QMessageBox.information(self, "Loaded File Path", f"{heading}\n\n" + "\n".join(paths))

    def open_snt_files_from_shell(self, file_paths: list):
        """Open .snt files passed from Windows shell double-click.
        
        Opens the SNT dialog, adds the files, and auto-attaches them.
        """
        from pathlib import Path
        from PySide6.QtWidgets import QMessageBox

        valid_paths = []
        for p in file_paths:
            try:
                pp = Path(p)
                if pp.is_file() and pp.suffix.lower() == ".snt":
                    valid_paths.append(str(pp))
            except Exception:
                continue

        if not valid_paths:
            return

        from gui.snt_attachment import show_snt_attachment_dialog
        dlg = show_snt_attachment_dialog(self)

        QTimer.singleShot(300, lambda paths=valid_paths, d=dlg: self._shell_load_snt_files(paths, d))

    def _shell_load_snt_files(self, file_paths: list, dlg):
        """Load SNT files into dialog and auto-attach."""
        from pathlib import Path
        from gui.snt_attachment import SNTFileItem, SNTLoadWorker

        if dlg is None or not hasattr(dlg, 'file_list_layout'):
            return

        if dlg._load_worker is not None and dlg._load_worker.isRunning():
            dlg._load_worker.cancel()
            dlg._load_worker.wait(3000)

        existing_keys = set()
        for item in dlg._iter_snt_file_items():
            try:
                existing_keys.add(str(item.snt_path.resolve()).lower())
            except Exception:
                existing_keys.add(str(item.snt_path).lower())

        new_paths = []
        for p in file_paths:
            try:
                key = str(Path(p).resolve()).lower()
            except Exception:
                key = str(p).lower()
            if key not in existing_keys:
                new_paths.append(p)

        if not new_paths:
            dlg._attach_all()
            return

        from PySide6.QtWidgets import QProgressDialog
        from PySide6.QtCore import QCoreApplication
        from gui.theme_manager import get_progress_dialog_stylesheet

        progress = QProgressDialog(
            "Loading SNT file..." if len(new_paths) == 1 else "Loading SNT files...",
            "Cancel", 0, 0 if len(new_paths) == 1 else len(new_paths), dlg)
        progress.setWindowTitle("Loading SNT Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.setStyleSheet(get_progress_dialog_stylesheet())
        progress.show()
        QCoreApplication.processEvents()

        worker = SNTLoadWorker(new_paths)
        dlg._load_worker = worker

        indeterminate = (len(new_paths) == 1)
        total = len(new_paths)

        def on_progress(value, message, is_indet):
            try:
                if progress is not None and not progress.wasCanceled():
                    progress.setLabelText(message)
                    if not is_indet:
                        progress.setValue(value)
            except RuntimeError:
                pass

        def on_file_loaded(item_data, _):
            try:
                snt_path = Path(item_data["snt_path"])
                snt_key = dlg._path_key(snt_path)
                if any(dlg._path_key(item.snt_path) == snt_key for item in dlg._iter_snt_file_items()):
                    return
                item = SNTFileItem(snt_path, parent=dlg)
                item.remove_requested.connect(dlg._remove_item)
                dlg.file_list_layout.insertWidget(len(dlg.snt_items), item)
                dlg.snt_items.append(item)
                if "parsed" in item_data:
                    item.cached_parsed = item_data["parsed"]
                    item.update_entity_count(item_data["entity_count"])
                else:
                    item.count_label.setText("Error")
            except RuntimeError:
                pass

        def on_finished():
            try:
                dlg._load_worker = None
                if progress is not None:
                    if not indeterminate and not progress.wasCanceled():
                        progress.setValue(total)
                    progress.close()
                dlg._update_file_count()
                dlg._attach_all()
            except RuntimeError:
                pass

        def on_error(msg):
            try:
                dlg._load_worker = None
                if progress is not None:
                    progress.close()
                QMessageBox.critical(dlg, "Load Failed", msg)
            except RuntimeError:
                pass

        worker.progress.connect(on_progress)
        worker.file_loaded.connect(on_file_loaded)
        worker.finished.connect(on_finished)
        worker.error.connect(on_error)
        worker.start()

    def _create_canvas_tool_cursor(self):
        """Create a custom tool cursor for canvas-only interactions."""
        size = 36
        center = size // 2
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, True)

        outline_pen = QPen(QColor(18, 22, 30, 220), 3)
        outline_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(outline_pen)
        painter.drawLine(center, 2, center, 12)
        painter.drawLine(center, size - 3, center, size - 13)
        painter.drawLine(2, center, 12, center)
        painter.drawLine(size - 3, center, size - 13, center)
        painter.drawEllipse(center - 4, center - 4, 8, 8)

        highlight_pen = QPen(QColor(255, 255, 255, 245), 1)
        highlight_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(highlight_pen)
        painter.drawLine(center, 2, center, 12)
        painter.drawLine(center, size - 3, center, size - 13)
        painter.drawLine(2, center, 12, center)
        painter.drawLine(size - 3, center, size - 13, center)
        painter.drawEllipse(center - 4, center - 4, 8, 8)

        center_pen = QPen(QColor(255, 255, 255, 255), 3)
        center_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(center_pen)
        painter.drawPoint(center, center)
        painter.end()

        return QCursor(pixmap, center, center)

    def _iter_canvas_widgets(self):
        """Yield each live VTK interactor that should show the shared tool cursor."""
        seen = set()

        def _yield_widget(widget):
            if widget is None or not _qt_object_is_valid(widget):
                return
            widget_id = id(widget)
            if widget_id in seen:
                return
            seen.add(widget_id)
            yield widget

        main_interactor = getattr(getattr(self, "vtk_widget", None), "interactor", None)
        yield from _yield_widget(main_interactor)

        for vtk_widget in (getattr(self, "section_vtks", None) or {}).values():
            yield from _yield_widget(getattr(vtk_widget, "interactor", None))

        cut_vtk = getattr(getattr(self, "cut_section_controller", None), "cut_vtk", None)
        yield from _yield_widget(getattr(cut_vtk, "interactor", None))

    def _register_canvas_cursor_widget(self, widget):
        """Install the shared cursor filter on a canvas interactor once."""
        if widget is None or not _qt_object_is_valid(widget):
            return False

        try:
            if not bool(widget.property("canvasCursorFilterInstalled")):
                widget.installEventFilter(self._canvas_cursor_filter)
                widget.setProperty("canvasCursorFilterInstalled", True)
            if widget.property("canvasCursorMutating") is None:
                widget.setProperty("canvasCursorMutating", False)
            widget.setMouseTracking(True)
            self._set_canvas_widget_cursor(widget, active=bool(widget.underMouse()))
            return True
        except Exception as e:
            print(f"Failed to register canvas cursor widget: {e}")
            return False

    def _install_section_wheel_zoom(self, vtk_widget):
        """Install fast section zoom without removing the legacy VTK fallback."""
        try:
            from gui.cross_section.section_zoom import install_section_wheel_zoom

            return install_section_wheel_zoom(self, vtk_widget)
        except Exception as e:
            # Section creation must never fail because an optional navigation
            # enhancement could not be installed.  The existing VTK style will
            # continue to provide wheel zoom in this case.
            print(f"Section wheel zoom install skipped: {e}")
            return None

    def _schedule_canvas_cursor_refresh(self, widget):
        """Re-apply the canvas cursor after Qt/VTK tries to change it."""
        if widget is None or not _qt_object_is_valid(widget):
            return
        if getattr(self, "_shutdown_in_progress", False):
            return
        if not getattr(self, "_cursor_state", False):
            return

        try:
            if bool(widget.property("canvasCursorRefreshPending")):
                return

            widget.setProperty("canvasCursorRefreshPending", True)

            def _refresh():
                try:
                    if getattr(self, "_shutdown_in_progress", False):
                        return
                    if widget is None or not _qt_object_is_valid(widget):
                        return
                    if hasattr(widget, "isVisible") and not widget.isVisible():
                        return
                    if hasattr(widget, "testAttribute") and not widget.testAttribute(Qt.WA_WState_Created):
                        return
                    self._set_canvas_widget_cursor(widget, active=bool(widget.underMouse()))
                finally:
                    try:
                        if widget is not None and _qt_object_is_valid(widget):
                            widget.setProperty("canvasCursorRefreshPending", False)
                    except Exception:
                        pass

            QTimer.singleShot(0, _refresh)
        except Exception:
            pass

    def _set_canvas_widget_cursor(self, widget, active):
        """Apply the custom tool cursor only while the pointer is over a canvas."""
        if widget is None or not _qt_object_is_valid(widget):
            return
        if getattr(self, "_shutdown_in_progress", False):
            return

        try:
            if hasattr(widget, "isVisible") and not widget.isVisible():
                return
            if hasattr(widget, "testAttribute") and not widget.testAttribute(Qt.WA_WState_Created):
                return
            desired_custom_cursor = bool(self._cursor_state and active)
            current_state = widget.property("canvasCursorAppliedState")
            if current_state is not None and bool(current_state) == desired_custom_cursor:
                return

            widget.setProperty("canvasCursorMutating", True)
            try:
                if desired_custom_cursor:
                    widget.setCursor(self._canvas_tool_cursor)
                else:
                    widget.unsetCursor()
                widget.setProperty("canvasCursorAppliedState", desired_custom_cursor)
            finally:
                widget.setProperty("canvasCursorMutating", False)
        except Exception:
            pass

    def _refresh_canvas_tool_cursors(self):
        """Force-refresh all registered canvas cursors."""
        for widget in self._iter_canvas_widgets():
            self._schedule_canvas_cursor_refresh(widget)

    def _get_main_canvas_widget(self):
        return getattr(getattr(self, "vtk_widget", None), "interactor", None)

    def _accudraw_canvas_right_click_in_progress(self):
        """
        True while AccuDraw is active.

        AccuDraw uses right-click as a drawing command.
        So while AccuDraw is active, Surface / Shading / Display settings
        must not open from canvas right-click or delayed context events.
        """
        try:
            digitizer = getattr(self, "digitizer", None)
            accudraw_tool = getattr(digitizer, "accudraw_tool", None) if digitizer else None

            if accudraw_tool is not None and getattr(accudraw_tool, "active", False):
                return True

            if digitizer is not None and getattr(digitizer, "active_tool", None) == "accudraw":
                return True

            consumed_until = max(
                float(getattr(self, "_accudraw_right_click_consumed_until", 0.0) or 0.0),
                float(getattr(digitizer, "_accudraw_right_click_consumed_until", 0.0) or 0.0) if digitizer else 0.0,
            )

            if time.monotonic() < consumed_until:
                return True

        except Exception:
            pass

        return False

    # ------------------------------------------------------------------
    # VTK-native crosshair (vtkActor2D in display coords — like MicroStation)
    # ------------------------------------------------------------------

    def _install_canvas_axis_render_observer(self):
        """Install VTK observer so the crosshair redraws with each render."""
        pass  # observer installed lazily in _ensure_crosshair

    def _ensure_crosshair(self):
        """Create the VTK crosshair actor if it doesn't exist yet."""
        ch = getattr(self, "_vtk_crosshair", None)
        if ch is not None:
            return ch
        try:
            renderer = self.vtk_widget.renderer
        except Exception:
            return None
        ch = _VTKCrosshair(renderer)
        self._vtk_crosshair = ch
        self._canvas_axis_color_key = None
        self._apply_axis_style()
        return ch

    def _apply_axis_style(self):
        ch = getattr(self, "_vtk_crosshair", None)
        if not ch:
            return
        try:
            from gui.theme_manager import ThemeManager
            bg = ThemeManager.canvas_background_for_theme()
        except Exception:
            bg = "black"
        dark = str(bg).lower() != "white"
        if dark == getattr(self, "_canvas_axis_color_key", None):
            return
        self._canvas_axis_color_key = dark
        ch.set_theme(dark)

    def _move_axis_guides(self, widget, event):
        if getattr(self, "_shutdown_in_progress", False):
            return
        if not getattr(self, "_canvas_axis_guides_enabled", False):
            return
        if widget is not self._get_main_canvas_widget():
            return

        ch = self._ensure_crosshair()
        if not ch:
            return

        try:
            # Get VTK display coordinates — exact pixel position, no Qt offset
            iren = self.vtk_widget.interactor
            rw = iren.GetRenderWindow()
            if rw is None:
                return
            x, y = iren.GetEventPosition()
            w, h = rw.GetSize()
            ch.update(x, y, w, h)

            # ✅ Skip the extra crosshair render while a camera interaction
            # (pan/zoom/rotate) is active — the render manager already drives
            # the repaint on every interaction tick, so a second rw.Render()
            # here would double the per-move cost. The crosshair still follows
            # because ch.update() ran above; it repaints on the next render.
            mgr = getattr(self, "gpu_render_manager", None)
            if mgr is not None and (
                getattr(mgr, "_interaction_active", False)
                or getattr(mgr, "_pan_in_progress", False)
            ):
                return

            # Throttle: schedule a single deferred render instead of
            # calling rw.Render() on every mouse-move (huge perf win).
            if not getattr(self, "_axis_render_pending", False):
                self._axis_render_pending = True
                def _do_axis_render():
                    self._axis_render_pending = False
                    try:
                        if getattr(self, "_shutdown_in_progress", False):
                            return
                        # Route through the render manager so this shares the
                        # same throttle/debounce as every other main-view render.
                        if mgr is not None and getattr(mgr, "request_render", None):
                            mgr.request_render(self.vtk_widget)
                        else:
                            rw.Render()
                    except Exception:
                        pass
                QTimer.singleShot(16, _do_axis_render)  # ~60 FPS cap
        except Exception:
            pass

    def _hide_axis_guides(self):
        if getattr(self, "_shutdown_in_progress", False):
            return
        ch = getattr(self, "_vtk_crosshair", None)
        if ch and ch.visible:
            ch.hide()
            try:
                self.vtk_widget.interactor.GetRenderWindow().Render()
            except Exception:
                pass

    def _shutdown_canvas_cursor_system(self):
        """
        Stop cursor/crosshair activity before VTK teardown.
        Prevents queued cursor refresh callbacks from touching stale widgets.
        """
        try:
            self._cursor_state = False
            if hasattr(self, "_active_tools") and isinstance(self._active_tools, set):
                self._active_tools.clear()
            self._axis_render_pending = False
        except Exception:
            pass

        try:
            ch = getattr(self, "_vtk_crosshair", None)
            if ch is not None:
                try:
                    ch.hide()
                except Exception:
                    pass
        except Exception:
            pass

        for widget in self._iter_canvas_widgets():
            if widget is None or not _qt_object_is_valid(widget):
                continue
            try:
                widget.setProperty("canvasCursorRefreshPending", False)
                widget.setProperty("canvasCursorMutating", True)
                widget.unsetCursor()
                widget.setProperty("canvasCursorAppliedState", False)
                widget.setProperty("canvasCursorMutating", False)
            except Exception:
                pass
            try:
                if bool(widget.property("canvasCursorFilterInstalled")):
                    widget.removeEventFilter(self._canvas_cursor_filter)
                    widget.setProperty("canvasCursorFilterInstalled", False)
            except Exception:
                pass
            try:
                widget.setUpdatesEnabled(False)
            except Exception:
                pass

        try:
            main_vtk = getattr(self, "vtk_widget", None)
            if main_vtk is not None:
                setattr(main_vtk, "_naksha_skip_render", True)
            for vw in (getattr(self, "section_vtks", None) or {}).values():
                try:
                    setattr(vw, "_naksha_skip_render", True)
                except Exception:
                    pass
            cut_vtk = getattr(getattr(self, "cut_section_controller", None), "cut_vtk", None)
            if cut_vtk is not None:
                setattr(cut_vtk, "_naksha_skip_render", True)
        except Exception:
            pass

    def _on_canvas_axis_guides_toggled(self, checked):
        checked = bool(checked)
        self._canvas_axis_guides_enabled = checked
        if hasattr(self, "settings") and self.settings is not None:
            self.settings.setValue("canvas_axis_guides_enabled", checked)

        # Activate / deactivate the crosshair cursor alongside the guides.
        # Uses "axis_guides" as tool name so it stacks with classification
        # tool cursors — cursor stays active while either source is on.
        self.set_cross_cursor_active(checked, tool_name="axis_guides")

        if not checked:
            self._hide_axis_guides()
            if hasattr(self, "status"):
                self.status.showMessage("Canvas axis guides disabled", 2000)
        else:
            if hasattr(self, "status"):
                self.status.showMessage("Canvas axis guides enabled", 2000)

    # Backward-compatible stubs
    def _hide_canvas_axis_guides(self):
        self._hide_axis_guides()

    def _hide_canvas_axis_guides_for_widget(self, widget):
        if widget is self._get_main_canvas_widget():
            self._hide_axis_guides()

    def _refresh_canvas_axis_overlay_style(self):
        self._canvas_axis_color_key = None
        self._apply_axis_style()

    def _refresh_canvas_axis_guides_if_needed(self):
        if not getattr(self, "_canvas_axis_guides_enabled", False):
            self._hide_axis_guides()

    def set_cross_cursor_active(self, active: bool, tool_name: str = None):
        if not hasattr(self, '_cursor_state'):
            self._cursor_state = False
            self._active_tools = set()

        if active:
            if tool_name:
                self._active_tools.add(tool_name)
            self._cursor_state = True
        else:
            if tool_name:
                self._active_tools.discard(tool_name)
            else:
                self._active_tools.clear()
            self._cursor_state = bool(self._active_tools)

        widgets_updated = 0
        errors = []

        for widget in self._iter_canvas_widgets():
            try:
                if self._register_canvas_cursor_widget(widget):
                    self._set_canvas_widget_cursor(widget, active=bool(widget.underMouse()))
                    widgets_updated += 1
            except Exception as e:
                errors.append(str(e))

        state_text = "ACTIVE" if self._cursor_state else "INACTIVE"
        tool_info = f" (tool: {tool_name})" if tool_name else ""

        print(f"Canvas cursor {state_text}{tool_info} - {widgets_updated} widgets updated")

        if self._active_tools:
            print(f"   Active tools: {', '.join(sorted(self._active_tools))}")

        if errors:
            print("   Cursor update errors:")
            for error in errors:
                print(f"      - {error}")

        if widgets_updated == 0:
            print("   WARNING: No widgets updated - cursor may not change")

        self._refresh_canvas_tool_cursors()

    def _cancel_cross_section_tool_only(self):
        """Deactivate cross-section drawing tool but keep cross-section windows/views intact."""
        try:
            if getattr(self, "cross_interactor", None) is not None:
                try:
                    if hasattr(self.cross_interactor, "cancel"):
                        self.cross_interactor.cancel()
                except Exception:
                    pass

            iren = self.vtk_widget.interactor

            # Restore previous style if available, otherwise fall back to 2D image style
            prev = getattr(self, "previous_interactor_style", None)
            if prev is not None:
                iren.SetInteractorStyle(prev)
            else:
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                iren.SetInteractorStyle(vtkInteractorStyleImage())

        except Exception as e:
            print(f"⚠️ Failed to restore interactor style: {e}")

        # Clear any ongoing preview lines
        if hasattr(self, 'section_controller') and self.section_controller:
            try:
                self.section_controller.clear_preview(force_overlay_destroy=True)
                
                # Also reset the slice state in interactor if it exists
                if hasattr(self, 'cross_interactor') and self.cross_interactor:
                    self.cross_interactor.P1 = None
                    self.cross_interactor.P2 = None
                    self.cross_interactor.slice_state = 0
            except Exception as e:
                pass

        # Reset tool state (do NOT clear section_vtks/section_docks)
        self.cross_interactor = None
        self.cross_section_active = False
        self._section_locate_display = None
        self._section_locate_view = None
        try:
            sc = getattr(self, 'section_controller', None)
            if sc is not None and hasattr(sc, 'clear_locate_state'):
                sc.clear_locate_state()
        except Exception:
            pass
        # ✅ FIX: Also clear classify interactor's locate line in cross-section views
        try:
            ci = getattr(self, 'classify_interactor', None)
            if ci is not None and hasattr(ci, '_clear_locate_state'):
                ci._clear_locate_state()
            elif ci is not None and hasattr(ci, '_clear_all_previews'):
                ci._clear_all_previews()
        except Exception:
            pass
        try:
            self.set_cross_cursor_active(False, "cross_section")
        except TypeError:
            self.set_cross_cursor_active(False)
        except Exception:
            pass

        try:
            if getattr(self, "cross_action", None) is not None:
                self.cross_action.setChecked(False)
        except Exception:
            pass

        if hasattr(self, "statusBar"):
            self.statusBar().showMessage("Cross-section tool deactivated (views kept)", 2000)

    def _install_vtk_filter(self):
        """Delayed VTK filter install"""
        if (hasattr(self, 'short_cut_filter') and
            hasattr(self, 'vtk_widget') and
            self.vtk_widget.interactor):
            self.vtk_widget.interactor.installEventFilter(self.short_cut_filter)
            print("✅ VTK filter installed (delayed)")

    #Added by bala
    def _on_session_tick(self):
        try:
            # MemoryLeakGuard already performs periodic GC/trim.
            # Avoid running a second maintenance loop in parallel.
            if getattr(self, "_mem_guard", None) is not None:
                return
            SESSION.maintenance()
        except Exception:
            pass                       #####

    def clear_project(app):
        """
        Safely clear the current project from the NakshaApp instance.
        Resets all viewers, layers, drawings, and temporary states.
        ✅ FIXED: Now properly closes all cross-section windows
        """
        reply = QMessageBox.question(
            app,
            "Confirm Clear Project",
            "Are you sure you want to clear the current project?\n"
            "All unsaved data, drawings, and layers will be removed.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.No:
            return

        try:
            print("\n" + "="*60)
            print("🧹 CLEARING PROJECT")
            print("="*60)

            # --- Hide the classification count label in the top bar ---
            if hasattr(app, "_classify_count_label"):
                app._classify_count_label.hide()
            if hasattr(app, "_classify_count_timer"):
                app._classify_count_timer.stop()

            # --- Clear main viewer ---
            if hasattr(app, "vtk_widget") and app.vtk_widget:
                app.vtk_widget.clear()
                app.vtk_widget.render()
                print("✅ Main viewer cleared")

            # --- ✅ Close ALL cross-section dock windows (Views 1-4) ---
            if hasattr(app, "section_docks") and app.section_docks:
                for view_idx, dock in list(app.section_docks.items()):
                    try:
                        print(f"   Closing Cross Section View {view_idx + 1}...")
                        dock.setVisible(False)  # Hide first
                        dock.close()            # Then close
                        dock.deleteLater()      # Schedule deletion
                        print(f"   ✅ Closed Cross Section View {view_idx + 1}")
                    except Exception as e:
                        print(f"   ⚠️ Failed to close View {view_idx + 1}: {e}")
                
                # Clear the dictionaries
                app.section_docks.clear()
                print("✅ All cross-section docks closed")

            # --- ✅ Clear cross-section VTK widgets ---
            if hasattr(app, "section_vtks") and app.section_vtks:
                for view_idx, vtk_widget in list(app.section_vtks.items()):
                    try:
                        vtk_widget.clear()
                        vtk_widget.render()
                        print(f"✅ Cleared VTK widget for View {view_idx + 1}")
                    except Exception as e:
                        print(f"⚠️ Failed to clear VTK View {view_idx + 1}: {e}")
                
                app.section_vtks.clear()

            # --- Clear legacy section view ---
            if hasattr(app, "sec_vtk") and app.sec_vtk:
                app.sec_vtk.clear()
                app.sec_vtk.render()
                print("✅ Legacy section view cleared")

            # --- ✅ Clear section controller ---
            if hasattr(app, "section_controller") and app.section_controller:
                try:
                    app.section_controller.clear()
                    app.section_controller.active_view = None
                    app.section_controller.current_vtk = None
                    if hasattr(app.section_controller, 'view_vtks'):
                        app.section_controller.view_vtks.clear()
                    print("✅ Section controller cleared")
                except Exception as e:
                    print(f"⚠️ Section controller clear failed: {e}")

            # --- ✅ Clear cut section if active ---
            if hasattr(app, "cut_section_controller") and app.cut_section_controller:
                try:
                    if hasattr(app.cut_section_controller, 'clear'):
                        app.cut_section_controller.clear()
                    app.cut_section_controller.cut_points = None
                    print("✅ Cut section cleared")
                except Exception as e:
                    print(f"⚠️ Cut section clear failed: {e}")

            # --- Clear layers ---
            if hasattr(app, "layers"):
                app.layers.clear()
                print("✅ Layers cleared")

            # --- Clear digitizer drawings ---
            if hasattr(app, "digitizer"):
                app.digitizer.clear_drawings()
                print("✅ Drawings cleared")

            # --- ✅ Clear stored section data ---
            for i in range(4):
                for attr in [f"section_{i}_core_points", f"section_{i}_buffer_points",
                            f"section_{i}_core_mask", f"section_{i}_buffer_mask"]:
                    if hasattr(app, attr):
                        try:
                            delattr(app, attr)
                        except Exception:
                            pass
            print("✅ Section data cleared")

            # --- ✅ Close Display Mode dialog ---
            if hasattr(app, "display_dialog") and app.display_dialog:
                try:
                    app.display_dialog.close()
                    app.display_dialog = None
                    print("✅ Display Mode dialog closed")
                except Exception:
                    pass

            if hasattr(app, "display_mode_dialog") and app.display_mode_dialog:
                try:
                    app.display_mode_dialog.close()
                    app.display_mode_dialog = None
                except Exception:
                    pass

            # --- ✅ Close Class Picker ---
            if hasattr(app, "class_picker") and app.class_picker:
                try:
                    app.class_picker.close()
                    print("✅ Class Picker closed")
                except Exception:
                    pass
                finally:
                    app.class_picker = None

            # --- Reset classification and data ---
            try:
                from .unified_actor_manager import reset_uam
                reset_uam(app)
            except Exception as e:
                print(f"⚠️ UAM reset failed: {e}")

            app.data = None
            # Reset the authoritative canvas CRS and every compatibility field
            # so a previous project's CRS can never leak into the next one.
            try:
                from gui.crs_manager import clear_canvas_crs
                clear_canvas_crs(app)
            except Exception as e:
                print(f"[CRS] clear_canvas_crs failed: {e}")
                app.project_crs_epsg = None
                app.project_crs_wkt = None
            app.loaded_file = None
            app.last_save_path = None
            app.class_palette = {}
            app._main_global_indices = None
            app._main_global_mask = None
            app._main_lod_step = None

            # --- ✅ Clear view palettes ---

            if hasattr(app, "view_palettes"):
                app.view_palettes.clear()

            # --- ✅ Clear undo/redo ---
            if hasattr(app, "undo_stack"):
                app.undo_stack.clear()
            if hasattr(app, "redo_stack"):
                app.redo_stack.clear()

            # --- ✅ Deactivate classification tools ---
            app.active_classify_tool = None

            # --- ✅ Deactivate footer click tools ---
            try:
                if hasattr(app, "point_sync_tool") and app.point_sync_tool is not None:
                    app.point_sync_tool.deactivate()
            except Exception:
                pass
            try:
                if hasattr(app, "snt_layer_pick_tool") and app.snt_layer_pick_tool is not None:
                    app.snt_layer_pick_tool.deactivate()
            except Exception:
                pass
            try:
                if hasattr(app, "_set_snt_layer_pick_footer_text"):
                    app._set_snt_layer_pick_footer_text(None, None, None)
            except Exception:
                pass

            # --- Reset window title ---
            app._update_window_title(None, None)
            app.statusBar().showMessage("🧹 Project cleared successfully.", 3000)
            
            print("="*60)
            print("✅ PROJECT CLEARED SUCCESSFULLY")
            print("="*60 + "\n")

        except Exception as e:
            print(f"⚠️ Error while clearing project: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(app, "Error", f"Failed to clear project:\n{e}")

    def _update_epsg_style(self):
        if not hasattr(self, "epsg_label") or self.epsg_label is None:
            return
        try:
            from gui.theme_manager import ThemeColors
            accent = ThemeColors.get("accent") or "#3b5bdb"
            text_muted = ThemeColors.get("text_muted") or "#888888"
            bg_button = ThemeColors.get("bg_button") or "#262a33"
            border = ThemeColors.get("border") or "#2a2d35"
        except Exception:
            accent = "#3b5bdb"
            text_muted = "#888888"
            bg_button = "#262a33"
            border = "#2a2d35"

        self.epsg_label.setStyleSheet(f"""
            QLabel#epsgLabel {{
                color: {accent};
                background-color: {bg_button};
                border: 1px solid {border};
                border-radius: 4px;
                padding: 1px 5px;
                font-weight: bold;
                font-size: 10px;
            }}
        """)
        
        if hasattr(self, "epsg_icon_label") and self.epsg_icon_label is not None:
            try:
                from gui.gis.icons import pixmap as gpixmap
                self.epsg_icon_label.setPixmap(gpixmap("globe", text_muted, 14))
            except Exception:
                pass

    def update_epsg_display(self):
        if not hasattr(self, "epsg_label"):
            return

        # Show the AUTHORITATIVE CANVAS CRS.
        # A GIS layer's own source CRS must NOT masquerade as the canvas CRS -
        # that created a false impression that the project/canvas CRS was known
        # (and silently misreported it for mixed-CRS projects).
        epsg = None
        try:
            from gui.crs_manager import get_canvas_crs
            canvas = get_canvas_crs(self)
            if canvas is not None:
                code = canvas.to_epsg()
                epsg = str(code) if code else None
        except Exception:
            epsg = None

        # Backwards compatibility only: if no canvas CRS object is available,
        # fall back to the legacy project field (never to a GIS layer).
        if not epsg and getattr(self, "project_crs_epsg", None):
            epsg = str(self.project_crs_epsg)

        if epsg:
            self.epsg_label.setText(epsg)
            self.epsg_label.show()
            if hasattr(self, "epsg_icon_label") and self.epsg_icon_label:
                self.epsg_icon_label.show()
            if hasattr(self, "epsg_prefix_label") and self.epsg_prefix_label:
                self.epsg_prefix_label.show()
        else:
            self.epsg_label.setText("-")

    def _update_settings_icon(self):
        import os
        from pathlib import Path
        from gui.icon_provider import _load_icon_from_file
        from gui.theme_manager import ThemeColors

        icon_path = Path(os.path.join(os.path.dirname(__file__), "icons", "gear.svg"))
        if hasattr(self, "settings_btn"):
            self.settings_btn.setIcon(
                _load_icon_from_file(icon_path, 20, color=ThemeColors.get("icon_primary"))
            )
        self._update_point_sync_footer_icon()
        self._update_snt_layer_pick_footer_icon()
        self._update_epsg_style()

    def _update_point_sync_footer_icon(self):
        import os
        from pathlib import Path
        from gui.icon_provider import _load_icon_from_file
        from gui.theme_manager import ThemeColors

        icon_path = Path(os.path.join(os.path.dirname(__file__), "icons", "target.svg"))
        if hasattr(self, "point_sync_footer_btn"):
            is_enabled = bool(self.point_sync_footer_btn.isChecked())
            icon_color = (
                ThemeColors.get("text_on_active") if is_enabled
                else (
                    ThemeColors.get("text_primary")
                    if not ThemeColors.is_light()
                    else ThemeColors.get("icon_primary")
                )
            )
            self.point_sync_footer_btn.setIcon(
                _load_icon_from_file(icon_path, 16, color=icon_color)
            )

    def _update_snt_layer_pick_footer_icon(self):
        import os
        from pathlib import Path
        from gui.icon_provider import _load_icon_from_file
        from gui.theme_manager import ThemeColors

        icon_path = Path(os.path.join(os.path.dirname(__file__), "icons", "layer_pick.svg"))
        if hasattr(self, "snt_layer_pick_footer_btn"):
            is_enabled = bool(self.snt_layer_pick_footer_btn.isChecked())
            icon_color = (
                ThemeColors.get("text_on_active") if is_enabled
                else (
                    ThemeColors.get("text_primary")
                    if not ThemeColors.is_light()
                    else ThemeColors.get("icon_primary")
                )
            )
            self.snt_layer_pick_footer_btn.setIcon(
                _load_icon_from_file(icon_path, 16, color=icon_color)
            )

    def _refresh_activity_bar_theme(self):
        from pathlib import Path
        from gui.icon_provider import _load_icon_from_file
        from gui.theme_manager import ThemeColors

        if not hasattr(self, "_activity_bar") or not hasattr(self, "_btn_layers"):
            return

        is_checked = bool(self._btn_layers.isChecked())
        is_light = ThemeColors.is_light()
        accent = ThemeColors.get("accent")
        accent_hover = ThemeColors.get("accent_hover")
        idle_bg = ThemeColors.get("bg_button") if is_light else ThemeColors.get("bg_secondary")
        hover_bg = ThemeColors.get("bg_button_hover") if is_light else ThemeColors.get("bg_tertiary")
        idle_border = ThemeColors.get("border") if is_light else ThemeColors.get("border_light")
        hover_border = ThemeColors.get("border_light") if is_light else ThemeColors.get("text_secondary")
        icon_color = (
            ThemeColors.get("text_on_active") if is_checked
            else (
                ThemeColors.get("icon_primary") if is_light
                else ThemeColors.get("text_primary")
            )
        )

        icon_path = Path(os.path.join(os.path.dirname(__file__), "gis", "icons", "stack-minus.svg"))
        self._btn_layers.setIcon(_load_icon_from_file(icon_path, 16, color=icon_color))

        self._activity_bar.setStyleSheet(f"""
            QWidget#ActivityBar {{
                background: {ThemeColors.get("bg_secondary")};
                border-right: 1px solid {ThemeColors.get("border")};
            }}
        """)
        self._btn_layers.setStyleSheet(f"""
            QToolButton#ActivityBtn {{
                background: {accent if is_checked else idle_bg};
                border: 1px solid {accent if is_checked else idle_border};
                border-radius: 6px;
                padding: 0px;
            }}
            QToolButton#ActivityBtn:hover {{
                background: {accent_hover if is_checked else hover_bg};
                border-color: {accent_hover if is_checked else hover_border};
            }}
            QToolButton#ActivityBtn:pressed {{
                background: {accent_hover};
                border-color: {accent_hover};
            }}
            QToolButton#ActivityBtn:checked {{
                background: {accent};
                border-color: {accent};
            }}
        """)

    def _sync_activity_bar(self):
        if not hasattr(self, "_activity_bar") or not hasattr(self, "_btn_layers"):
            return

        reg = getattr(self, "gis_layers", None)
        has_layers = bool(reg) if isinstance(reg, list) else False

        panel = getattr(self, "_gis_layers_panel", None)
        panel_visible = bool(panel is not None and panel.isVisible())
        should_show = has_layers or panel_visible

        self._activity_bar.setVisible(should_show)

        was_blocked = self._btn_layers.blockSignals(True)
        self._btn_layers.setChecked(panel_visible)
        self._btn_layers.blockSignals(was_blocked)

        state_text = "Hide" if panel_visible else "Show"
        self._btn_layers.setToolTip(f"{state_text} Overlay Control Center (Alt+C)")
        self._btn_layers.setStatusTip(f"{state_text} Overlay Control Center")
        self._refresh_activity_bar_theme()

    def _show_activity_bar_context_menu(self, pos):
        source = self.sender()
        menu = QMenu(self)

        panel = getattr(self, "_gis_layers_panel", None)
        panel_visible = bool(panel is not None and panel.isVisible())
        reg = getattr(self, "gis_layers", None)
        has_layers = bool(reg) if isinstance(reg, list) else False

        toggle_action = menu.addAction(
            "Hide Overlay Control Center" if panel_visible else "Show Overlay Control Center"
        )
        import_action = menu.addAction("Import Overlay...")
        zoom_action = menu.addAction("Zoom To GIS Layers")
        zoom_action.setEnabled(has_layers)
        gdb_action = menu.addAction("Open GDB Import Panel")
        menu.addSeparator()
        refresh_action = menu.addAction("Refresh Activity Bar")

        global_pos = source.mapToGlobal(pos) if hasattr(source, "mapToGlobal") else QCursor.pos()
        chosen = menu.exec(global_pos)
        if chosen is None:
            return

        if chosen == toggle_action:
            self.toggle_gis_layers_panel()
        elif chosen == import_action:
            try:
                from gui.gis.gis_layers import unified_import_overlay
                unified_import_overlay(self)
            except Exception as exc:
                print(f"⚠️ Overlay import failed: {exc}")
                import traceback
                traceback.print_exc()
        elif chosen == zoom_action:
            try:
                from gui.gis.gis_layers import zoom_to_gis_entries
                zoom_to_gis_entries(self, list(reg or []))
            except Exception as exc:
                print(f"⚠️ GIS zoom failed: {exc}")
                import traceback
                traceback.print_exc()
        elif chosen == gdb_action:
            self.toggle_gdb_panel()
        elif chosen == refresh_action:
            self._sync_activity_bar()

    def _update_point_sync_footer_state_visual(self, enabled):
        from gui.theme_manager import ThemeColors

        accent = ThemeColors.get("accent_alt")
        accent_hover = ThemeColors.get("accent_hover")
        button_hover = ThemeColors.get("bg_button_hover")
        button_idle = ThemeColors.get("bg_button")
        border = ThemeColors.get("border")
        border_light = ThemeColors.get("border_light")
        is_light = ThemeColors.is_light()

        if hasattr(self, "point_sync_footer_btn"):
            if enabled:
                self.point_sync_footer_btn.setStyleSheet(f"""
                    QToolButton {{
                        border: 1px solid {accent};
                        border-radius: 4px;
                        padding: 0px;
                        margin: 0 2px;
                        background: {accent};
                    }}
                    QToolButton:hover {{
                        background: {accent_hover};
                        border-color: {accent_hover};
                    }}
                    QToolButton:checked {{
                        background: {accent};
                        border-color: {accent};
                    }}
                """)
                self.point_sync_footer_btn.setToolTip("Point target sync is enabled")
                self.point_sync_footer_btn.setStatusTip("Point target sync is enabled")
            else:
                idle_bg = button_idle if is_light else ThemeColors.get("bg_secondary")
                idle_border = border if is_light else border_light
                hover_bg = button_hover if is_light else ThemeColors.get("bg_tertiary")
                hover_border = border_light if is_light else ThemeColors.get("text_secondary")

                self.point_sync_footer_btn.setStyleSheet(f"""
                    QToolButton {{
                        border: 1px solid {idle_border};
                        border-radius: 4px;
                        padding: 0px;
                        margin: 0 2px;
                        background: {idle_bg};
                    }}
                    QToolButton:hover {{
                        background: {hover_bg};
                        border-color: {hover_border};
                    }}
                    QToolButton:pressed {{
                        background: {hover_bg};
                        border-color: {accent};
                    }}
                """)
                self.point_sync_footer_btn.setToolTip("Point target sync is disabled")
                self.point_sync_footer_btn.setStatusTip("Point target sync is disabled")

        self._update_point_sync_footer_icon()

    def _update_snt_layer_pick_footer_state_visual(self, enabled):
        from gui.theme_manager import ThemeColors

        accent = ThemeColors.get("accent")
        accent_hover = ThemeColors.get("accent_hover")
        button_hover = ThemeColors.get("bg_button_hover")
        button_idle = ThemeColors.get("bg_button")
        border = ThemeColors.get("border")
        border_light = ThemeColors.get("border_light")
        is_light = ThemeColors.is_light()

        if hasattr(self, "snt_layer_pick_footer_btn"):
            if enabled:
                self.snt_layer_pick_footer_btn.setStyleSheet(f"""
                    QToolButton {{
                        border: 1px solid {accent};
                        border-radius: 4px;
                        padding: 0px;
                        margin: 0 2px;
                        background: {accent};
                    }}
                    QToolButton:hover {{
                        background: {accent_hover};
                        border-color: {accent_hover};
                    }}
                    QToolButton:checked {{
                        background: {accent};
                        border-color: {accent};
                    }}
                """)
                self.snt_layer_pick_footer_btn.setToolTip("Layer identifier is enabled")
                self.snt_layer_pick_footer_btn.setStatusTip("Layer identifier is enabled")
            else:
                idle_bg = button_idle if is_light else ThemeColors.get("bg_secondary")
                idle_border = border if is_light else border_light
                hover_bg = button_hover if is_light else ThemeColors.get("bg_tertiary")
                hover_border = border_light if is_light else ThemeColors.get("text_secondary")

                self.snt_layer_pick_footer_btn.setStyleSheet(f"""
                    QToolButton {{
                        border: 1px solid {idle_border};
                        border-radius: 4px;
                        padding: 0px;
                        margin: 0 2px;
                        background: {idle_bg};
                    }}
                    QToolButton:hover {{
                        background: {hover_bg};
                        border-color: {hover_border};
                    }}
                    QToolButton:pressed {{
                        background: {hover_bg};
                        border-color: {accent};
                    }}
                """)
                self.snt_layer_pick_footer_btn.setToolTip("Layer identifier is disabled")
                self.snt_layer_pick_footer_btn.setStatusTip("Layer identifier is disabled")

        self._update_snt_layer_pick_footer_icon()

    def _sync_point_sync_footer_button(self, enabled):
        if not hasattr(self, "point_sync_footer_btn"):
            return

        was_blocked = self.point_sync_footer_btn.blockSignals(True)
        self.point_sync_footer_btn.setChecked(bool(enabled))
        self.point_sync_footer_btn.blockSignals(was_blocked)
        self._update_point_sync_footer_state_visual(bool(enabled))

    def _sync_snt_layer_pick_footer_button(self, enabled):
        if not hasattr(self, "snt_layer_pick_footer_btn"):
            return

        was_blocked = self.snt_layer_pick_footer_btn.blockSignals(True)
        self.snt_layer_pick_footer_btn.setChecked(bool(enabled))
        self.snt_layer_pick_footer_btn.blockSignals(was_blocked)
        self._update_snt_layer_pick_footer_state_visual(bool(enabled))

    def _set_snt_layer_pick_footer_text(self, layer_name=None, snt_name=None, entity_count=None):
        if not hasattr(self, "snt_layer_pick_footer_label"):
            return

        if not layer_name:
            self.snt_layer_pick_footer_label.setText("Layer: -")
            self.snt_layer_pick_footer_label.setToolTip("Enable layer identifier and click features in main view")
            self.snt_layer_pick_footer_label.setStatusTip("No layer selected")
            return


        layer_text = str(layer_name).strip()
        file_text = os.path.basename(str(snt_name)) if snt_name else ""

        count_text = ""
        if isinstance(entity_count, int) and entity_count >= 0:
            count_text = f" ({entity_count:,})"

        prefix = "Layer"
        if file_text:
            display = f"{prefix}: {layer_text}{count_text} | {file_text}"
        else:
            display = f"{prefix}: {layer_text}{count_text}"

        self.snt_layer_pick_footer_label.setText(display)
        self.snt_layer_pick_footer_label.setToolTip(display)
        self.snt_layer_pick_footer_label.setStatusTip(display)

    def _toggle_point_sync_from_footer(self, enabled):
        if not hasattr(self, "point_sync_tool") or self.point_sync_tool is None:
            self._sync_point_sync_footer_button(False)
            return

        self._update_point_sync_footer_state_visual(bool(enabled))

        if enabled:
            if hasattr(self, "snt_layer_pick_tool") and self.snt_layer_pick_tool is not None:
                if getattr(self.snt_layer_pick_tool, "active", False):
                    self.snt_layer_pick_tool.deactivate()
            self.point_sync_tool.activate()
            self.statusBar().showMessage("🎯 Point target sync enabled", 2000)
        else:
            self.point_sync_tool.deactivate()
            self.statusBar().showMessage("🎯 Point target sync disabled", 2000)

    def _toggle_snt_layer_pick_from_footer(self, enabled):
        if not hasattr(self, "snt_layer_pick_tool") or self.snt_layer_pick_tool is None:
            self._sync_snt_layer_pick_footer_button(False)
            return

        self._update_snt_layer_pick_footer_state_visual(bool(enabled))

        has_gis = False
        try:
            from gui.gis.gis_layers import _registry
            has_gis = len(_registry(self)) > 0
        except Exception:
            pass

        has_data = bool(getattr(self, "snt_attachments", None) or getattr(self, "dxf_attachments", None) or has_gis)
        if enabled and not has_data:
            self._sync_snt_layer_pick_footer_button(False)
            self.statusBar().showMessage("Attach SNT, DXF, or GIS data first, then enable layer identifier", 2500)
            return

        if enabled:
            self.snt_layer_pick_tool.activate()
            self.statusBar().showMessage("🎯 Layer identifier enabled (click SNT, DXF, or GIS features in main view)", 2500)
        else:
            self.snt_layer_pick_tool.deactivate()
            self._set_snt_layer_pick_footer_text(None, None, None)
            self.statusBar().showMessage("🎯 Layer identifier disabled", 2000)

    def open_global_settings(self):
        """Open the consolidated global settings dialog."""
        from gui.global_settings_dialog import GlobalSettingsDialog

        if not hasattr(self, "_global_settings_dialog") or self._global_settings_dialog is None:
            self._global_settings_dialog = GlobalSettingsDialog(self, self)

        self._global_settings_dialog.refresh_theme()
        self._global_settings_dialog.refresh_summaries()
        self._global_settings_dialog.show()
        self._global_settings_dialog.raise_()
        self._global_settings_dialog.activateWindow()

    def _create_menus(self):
        """Top bar with buttons that open ribbons below it, with highlight states."""
        from PySide6.QtWidgets import QHBoxLayout, QPushButton, QWidget, QVBoxLayout, QToolButton
        from PySide6.QtCore import QSize

        # Hide the default menu bar
        self.menuBar().hide()

        # --- Create top bar ---
        top_bar = QWidget()
        top_bar.setObjectName("TopBar")
        top_bar.setFixedHeight(34)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(4, 2, 4, 2)
        top_layout.setSpacing(2)

        # --- Helper to create top buttons ---
        self.menu_buttons = {}  # store references for highlighting
        def add_menu_button(label, ribbon_name):
            btn = QPushButton(label)
            btn.setObjectName("menuButton")
            btn.setFixedHeight(26)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.clicked.connect(lambda: self._handle_menu_click(ribbon_name))
            self.menu_buttons[ribbon_name] = btn
            top_layout.addWidget(btn)

        # --- Add all menu buttons ---
        add_menu_button("File", "file")
        add_menu_button("Edit", "edit")
        add_menu_button("View", "view")
        add_menu_button("Tools", "tools")
        add_menu_button("Classify", "classify")
        add_menu_button("Display", "display")
        add_menu_button("Measure", "measure")
        add_menu_button("Identify", "identify")
        add_menu_button("By Class", "by_class")
        add_menu_button("Draw", "draw")
        add_menu_button("AI", "ai")
        add_menu_button("Block", "block")
        add_menu_button("Plugins", "plugins")

        # --- Centered classification count label (exact middle of top bar) ---
        from PySide6.QtWidgets import QLabel
        from PySide6.QtCore import QTimer

        self._classify_count_label = QLabel("")
        self._classify_count_label.setObjectName("ClassifyCountLabel")
        self._classify_count_label.setAlignment(Qt.AlignCenter)
        self._classify_count_label.setStyleSheet(
            "color: #FFD54F; font-weight: bold; font-size: 11px; "
            "background: transparent; padding: 0 8px;"
        )
        self._classify_count_label.hide()
        self._classify_count_timer = QTimer(self)
        self._classify_count_timer.setSingleShot(True)
        self._classify_count_timer.setInterval(3000)
        self._classify_count_timer.timeout.connect(self._classify_count_label.hide)
        # stretch=1 makes this label occupy the free middle space -> centered
        top_layout.addWidget(self._classify_count_label, 1)

        top_layout.addStretch()

        try:
            from gui.point_count_widget import PointCountWidget
            self.point_count_widget = PointCountWidget(self)
            self.point_count_widget.set_app(self)
            
            # Create standalone Stats button to align with theme toggle
            self.stats_btn = QPushButton("Stats")
            self.stats_btn.setObjectName("menuButton")
            self.stats_btn.setFixedHeight(26)
            self.stats_btn.setCursor(Qt.PointingHandCursor)
            self.stats_btn.setFocusPolicy(Qt.NoFocus)
            self.stats_btn.clicked.connect(lambda: self.point_count_widget.toggle_panel(self.stats_btn))
            top_layout.addWidget(self.stats_btn)
            
            print("📊 Embedded Point Stats button inside Top Menu")
        except Exception as e:
            print(f"⚠️ Failed to init Point Stats: {e}")
            self.point_count_widget = None

        self.settings_btn = QToolButton()
        self.settings_btn.setObjectName("settingsBtn")
        self.settings_btn.setFixedSize(34, 26)
        self.settings_btn.setIconSize(QSize(20, 20))
        self.settings_btn.setToolTip("Global Settings")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setFocusPolicy(Qt.NoFocus)
        self.settings_btn.clicked.connect(self.open_global_settings)
        top_layout.addWidget(self.settings_btn)

        self._update_settings_icon()

        # --- Ribbon container (below top bar) ---
        self.ribbon_container = QWidget()  # ✅ Changed from sidebar_container
        self.ribbon_container.setObjectName("RibbonContainer")
        self.ribbon_container.setFixedHeight(0)  # hidden until needed

        # --- Left activity bar (VS Code-style sidebar toggle) ---
        from PySide6.QtGui import QPixmap, QPainter, QColor, QPen, QIcon

        def _svg_to_icon(svg_path, size, color_hex):
            """Load an SVG, recolor it, and return QIcon."""
            try:
                with open(svg_path, "r") as f:
                    svg = f.read()
                svg = svg.replace("#000000", color_hex)
                from PySide6.QtSvg import QSvgRenderer
                from PySide6.QtCore import QByteArray
                renderer = QSvgRenderer(QByteArray(svg.encode()))
                pm = QPixmap(size, size)
                pm.fill(Qt.transparent)
                p = QPainter(pm)
                renderer.render(p)
                p.end()
                return QIcon(pm)
            except Exception:
                return QIcon()

        _icon_path = os.path.join(os.path.dirname(__file__),
                                   "gis", "icons", "stack-minus.svg")

        self._activity_bar = QWidget()
        self._activity_bar.setObjectName("ActivityBar")
        self._activity_bar.setFixedWidth(28)
        ab_layout = QVBoxLayout(self._activity_bar)
        ab_layout.setContentsMargins(1, 4, 1, 4)
        ab_layout.setSpacing(2)

        self._btn_layers = QToolButton()
        self._btn_layers.setObjectName("ActivityBtn")
        self._btn_layers.setFixedSize(24, 24)
        self._btn_layers.setIconSize(QSize(16, 16))
        self._btn_layers.setToolTip("Toggle Overlay Control Center (Alt+C)")
        self._btn_layers.setCursor(Qt.PointingHandCursor)
        self._btn_layers.setCheckable(True)
        self._btn_layers.clicked.connect(self.toggle_gis_layers_panel)

        self._activity_bar.setContextMenuPolicy(Qt.CustomContextMenu)
        self._btn_layers.setContextMenuPolicy(Qt.CustomContextMenu)
        self._activity_bar.customContextMenuRequested.connect(self._show_activity_bar_context_menu)
        self._btn_layers.customContextMenuRequested.connect(self._show_activity_bar_context_menu)

        ab_layout.addWidget(self._btn_layers)
        ab_layout.addStretch()

        self._sync_activity_bar()

        # --- Combine: [activity_bar | splitter] horizontal, then vertical ---
        main_row = QWidget()
        main_row_layout = QHBoxLayout(main_row)
        main_row_layout.setContentsMargins(0, 0, 0, 0)
        main_row_layout.setSpacing(0)
        main_row_layout.addWidget(self._activity_bar)
        main_row_layout.addWidget(self.splitter, 1)

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(top_bar)
        layout.addWidget(self.ribbon_container)  # ✅ Changed
        layout.addWidget(main_row, 1)
        self.setCentralWidget(container)

    def _capture_main_camera(self):
        """Capture current main-view camera (zoom/pan/orientation)."""
        try:
            renderer = self.vtk_widget.renderer
            cam = renderer.GetActiveCamera()
        except Exception:
            return None
        return {
            "pos": cam.GetPosition(),
            "fp": cam.GetFocalPoint(),
            "up": cam.GetViewUp(),
            "ps": cam.GetParallelScale(),
            "pp": 1 if cam.GetParallelProjection() else 0,
            "va": cam.GetViewAngle(),
        }

    def _run_preserving_camera(self, render_fn):
        """Run a render while preserving the current main-view zoom/pan."""
        from PySide6.QtCore import QTimer

        cam_state = self._capture_main_camera()
        self._preserve_view = True
        try:
            render_fn()
        finally:
            self._preserve_view = False

        # Restore camera immediately and once more after event loop flush
        self._restore_main_camera(cam_state)
        QTimer.singleShot(0,  lambda s=cam_state: self._restore_main_camera(s))
        QTimer.singleShot(50, lambda s=cam_state: self._restore_main_camera(s))

    def _restore_main_camera(self, state):
        """Restore previously captured main-view camera."""
        if not state or not hasattr(self, "vtk_widget") or not self.vtk_widget:
            return
        try:
            renderer = self.vtk_widget.renderer
            cam = renderer.GetActiveCamera()
            cam.SetPosition(*state["pos"])
            cam.SetFocalPoint(*state["fp"])
            cam.SetViewUp(*state["up"])
            if state.get("pp", 1):
                cam.ParallelProjectionOn()
                cam.SetParallelScale(state.get("ps", cam.GetParallelScale()))
            else:
                cam.ParallelProjectionOff()
                cam.SetViewAngle(state.get("va", cam.GetViewAngle()))
            renderer.ResetCameraClippingRange()
            self.vtk_widget.render()
        except Exception:
            pass

    # ── Interaction LOD (delegates to unified_actor_manager) ──────────────────
    # These are called by GPURenderManager during pan/zoom/rotate so the main
    # cloud draws a precomputed coarse subset while interacting and restores
    # full detail on release. They are safe no-ops when LOD isn't available
    # (small clouds) or already in the requested state.
    def apply_lod_factor(self, factor: float = 0.25):
        try:
            from gui.unified_actor_manager import apply_main_lod
            return apply_main_lod(self, factor)
        except Exception:
            return False

    def restore_full_detail_cloud(self):
        try:
            from gui.unified_actor_manager import restore_main_full_detail
            return restore_main_full_detail(self)
        except Exception:
            return False
    def _init_main_view_history(self):
        """
        Main view camera history.
        Left Arrow / Back button     = previous pan/zoom/3D view
        Right Arrow / Forward button = next pan/zoom/3D view
        """

        if getattr(self, "_main_view_history_ready", False):
            return

        self._view_back_stack = []
        self._view_forward_stack = []
        self._view_history_current_state = None
        self._view_history_restoring = False
        # Keep only a small rolling history so repeated pan/zoom sessions
        # don't accumulate a large camera-state cache in memory.
        self._view_history_max_steps = 15
        self._view_history_pending_reason = "view_change"

        self._view_history_timer = QTimer(self)
        self._view_history_timer.setSingleShot(True)
        self._view_history_timer.timeout.connect(self._flush_main_view_history_timer)

        self._main_view_history_observers = []

        try:
            interactor = self.vtk_widget.interactor

            self._main_view_history_observers.extend([
                interactor.AddObserver(
                    "StartInteractionEvent",
                    self._on_main_view_interaction_start,
                    0.01,
                ),
                interactor.AddObserver(
                    "EndInteractionEvent",
                    self._on_main_view_interaction_end,
                    0.01,
                ),
                interactor.AddObserver(
                    "LeftButtonReleaseEvent",
                    self._on_main_view_interaction_end,
                    0.01,
                ),
                interactor.AddObserver(
                    "MiddleButtonReleaseEvent",
                    self._on_main_view_interaction_end,
                    0.01,
                ),
                interactor.AddObserver(
                    "RightButtonReleaseEvent",
                    self._on_main_view_interaction_end,
                    0.01,
                ),
                interactor.AddObserver(
                    "MouseWheelForwardEvent",
                    self._on_main_view_wheel_history,
                    0.01,
                ),
                interactor.AddObserver(
                    "MouseWheelBackwardEvent",
                    self._on_main_view_wheel_history,
                    0.01,
                ),
            ])
        except Exception as e:
            print(f"View history observer install failed: {e}")

        self._main_view_history_ready = True
        QTimer.singleShot(0, self._prime_main_view_history)

        print("Main view history initialized: Back/Forward view navigation ready")

    def _prime_main_view_history(self):
        state = self._capture_main_camera()
        if state is not None:
            self._view_history_current_state = self._copy_camera_state(state)

    def _copy_camera_state(self, state):
        if not state:
            return None

        return {
            "pos": tuple(float(v) for v in state.get("pos", (0.0, 0.0, 1.0))),
            "fp": tuple(float(v) for v in state.get("fp", (0.0, 0.0, 0.0))),
            "up": tuple(float(v) for v in state.get("up", (0.0, 1.0, 0.0))),
            "ps": float(state.get("ps", 1.0)),
            "pp": int(state.get("pp", 1)),
            "va": float(state.get("va", 30.0)),
        }

    def _camera_states_equal(self, a, b, eps=1e-6):
        if not a or not b:
            return False

        for key in ("pos", "fp", "up"):
            av = a.get(key)
            bv = b.get(key)

            if av is None or bv is None or len(av) != len(bv):
                return False

            for x, y in zip(av, bv):
                if abs(float(x) - float(y)) > eps:
                    return False

        if abs(float(a.get("ps", 0.0)) - float(b.get("ps", 0.0))) > eps:
            return False

        if int(a.get("pp", 1)) != int(b.get("pp", 1)):
            return False

        if abs(float(a.get("va", 0.0)) - float(b.get("va", 0.0))) > eps:
            return False

        return True

    def _push_view_back_state(self, state):
        if not state:
            return

        state = self._copy_camera_state(state)

        if self._view_back_stack and self._camera_states_equal(self._view_back_stack[-1], state):
            return

        self._view_back_stack.append(state)

        max_steps = int(getattr(self, "_view_history_max_steps", 15))
        if len(self._view_back_stack) > max_steps:
            self._view_back_stack.pop(0)

    def _flush_main_view_history_timer(self):
        reason = getattr(self, "_view_history_pending_reason", "view_change")
        self._commit_main_view_history(reason)

    def _schedule_main_view_history_commit(self, reason="view_change", delay_ms=120):
        if getattr(self, "_view_history_restoring", False):
            return

        # A Qt-owned main pan updates the camera on every mouse move.  Do not
        # let release/interaction callbacks from the VTK side arm history in
        # the middle of that physical drag; the final camera is committed once
        # by _handle_fast_main_pan_release().
        if (
            getattr(self, "_main_view_history_pan_active", False)
            and reason == "pan_or_view_change"
        ):
            return

        if not hasattr(self, "_view_history_timer"):
            return

        self._view_history_pending_reason = reason

        try:
            self._view_history_timer.stop()
            self._view_history_timer.start(int(delay_ms))
        except Exception:
            pass

    def _commit_main_view_history(self, reason="view_change"):
        """
        Store current camera after pan, zoom, fit view, 2D/3D view switch, or restore-safe navigation.
        """

        if getattr(self, "_view_history_restoring", False):
            return

        new_state = self._capture_main_camera()
        if new_state is None:
            return

        new_state = self._copy_camera_state(new_state)
        old_state = getattr(self, "_view_history_current_state", None)

        if old_state is None:
            self._view_history_current_state = new_state
            return

        if self._camera_states_equal(old_state, new_state):
            return

        self._push_view_back_state(old_state)
        self._view_history_current_state = new_state
        self._view_forward_stack.clear()

        print(
            f"View history saved ({reason}) | "
            f"back={len(self._view_back_stack)}, forward={len(self._view_forward_stack)}"
        )

    def _on_main_view_interaction_start(self, obj, evt):
        if getattr(self, "_view_history_restoring", False):
            return

        if getattr(self, "_view_history_current_state", None) is None:
            self._prime_main_view_history()

    def _on_main_view_interaction_end(self, obj, evt):
        if getattr(self, "_view_history_restoring", False):
            return

        self._schedule_main_view_history_commit("pan_or_view_change", delay_ms=80)

    def _on_main_view_wheel_history(self, obj, evt):
        if getattr(self, "_view_history_restoring", False):
            return

        self._schedule_main_view_history_commit("mouse_wheel_zoom", delay_ms=120)

    def go_to_previous_main_view(self):
        """
        Restore previous main-view pan/zoom/2D/3D camera state.
        """

        if getattr(self, "_view_history_restoring", False):
            return False

        if not getattr(self, "_view_back_stack", None):
            try:
                self.statusBar().showMessage("No previous view", 1200)
            except Exception:
                pass
            return False

        current_state = self._copy_camera_state(self._capture_main_camera())
        if current_state is None:
            return False

        target_state = self._view_back_stack.pop()
        self._view_forward_stack.append(current_state)

        self._apply_main_view_history_state(target_state)
        self._view_history_current_state = self._copy_camera_state(target_state)

        try:
            self.statusBar().showMessage("Previous view", 1200)
        except Exception:
            pass

        print(
            f"Previous view | "
            f"back={len(self._view_back_stack)}, forward={len(self._view_forward_stack)}"
        )

        return True

    def go_to_next_main_view(self):
        """
        Restore next main-view pan/zoom/2D/3D camera state.
        """

        if getattr(self, "_view_history_restoring", False):
            return False

        if not getattr(self, "_view_forward_stack", None):
            try:
                self.statusBar().showMessage("No next view", 1200)
            except Exception:
                pass
            return False

        current_state = self._copy_camera_state(self._capture_main_camera())
        if current_state is None:
            return False

        target_state = self._view_forward_stack.pop()
        self._view_back_stack.append(current_state)

        self._apply_main_view_history_state(target_state)
        self._view_history_current_state = self._copy_camera_state(target_state)

        try:
            self.statusBar().showMessage("Next view", 1200)
        except Exception:
            pass

        print(
            f"Next view | "
            f"back={len(self._view_back_stack)}, forward={len(self._view_forward_stack)}"
        )

        return True

    def _apply_main_view_history_state(self, state):
        """
        Restore a saved main-view camera state.

        Important:
        - 3D history state must stay in TrackballCamera + Perspective mode.
        - 2D history state must stay in ImageStyle + Parallel mode.
        """

        if not state:
            return False

        self._view_history_restoring = True

        try:
            renderer = self.vtk_widget.renderer
            interactor = self.vtk_widget.interactor
            camera = renderer.GetActiveCamera()

            # Clear old 2D lock observer before restoring any camera state.
            try:
                short_filter = getattr(self, "short_cut_filter", None)
                if short_filter and hasattr(short_filter, "_clear_main_camera_lock_observer"):
                    short_filter._clear_main_camera_lock_observer(camera)
            except Exception:
                pass

            # pp = 0 means perspective projection, so this is a 3D view state.
            target_is_3d = int(state.get("pp", 1)) == 0

            self._restore_main_camera(state)

            if target_is_3d:
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera

                interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
                camera.ParallelProjectionOff()

                self.is_3d_mode = True
                self._main_view_2d_locked = False
                self.current_view = "3d"

                try:
                    renderer.ResetCameraClippingRange()
                    self.vtk_widget.render()
                except Exception:
                    pass

                print("View history restored in 3D mode")
                return True

            # 2D restore path
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            interactor.SetInteractorStyle(vtkInteractorStyleImage())
            camera.ParallelProjectionOn()

            self.is_3d_mode = False
            self._main_view_2d_locked = True

            if getattr(self, "current_view", None) == "3d":
                self.current_view = "top"

            try:
                self._refresh_2d_lock()
            except Exception:
                pass

            try:
                self.ensure_main_view_2d_interaction(
                    preserve_camera=True,
                    reason="view_history_restore",
                )
            except Exception:
                pass

            print("View history restored in 2D mode")
            return True

        finally:
            self._view_history_restoring = False

    def _apply_mode_without_camera_reset(self, render_callable):
        """
        Run a render function (e.g., update_pointcloud) while preventing any camera reset.
        - Temporarily overrides QtInteractor methods that reset camera.
        - Restores user camera immediately, after EndEvent, and via timers.
        """
        from PySide6.QtCore import QTimer

        cam_state = self._capture_main_camera()

        widget = self.vtk_widget
        plotter = getattr(widget, "plotter", None)

        # Save originals
        orig_widget_reset = getattr(widget, "reset_camera", None)
        orig_widget_clear = getattr(widget, "clear", None)
        orig_widget_add_points = getattr(widget, "add_points", None)
        orig_widget_add_mesh = getattr(widget, "add_mesh", None)

        orig_plot_add_points = getattr(plotter, "add_points", None) if plotter else None
        orig_plot_add_mesh = getattr(plotter, "add_mesh", None) if plotter else None
        orig_plot_clear = getattr(plotter, "clear", None) if plotter else None

        # Guards
        def no_reset_camera(*args, **kwargs):
            return None

        def clear_preserving_cam(*args, **kwargs):
            res = None
            try:
                if orig_widget_clear:
                    res = orig_widget_clear(*args, **kwargs)
            finally:
                self._restore_main_camera(cam_state)
            return res

        def add_points_no_reset(*args, **kwargs):
            kwargs["reset_camera"] = False
            kwargs.setdefault("render", False)
            return orig_widget_add_points(*args, **kwargs)

        def add_mesh_no_reset(*args, **kwargs):
            kwargs["reset_camera"] = False
            kwargs.setdefault("render", False)
            return orig_widget_add_mesh(*args, **kwargs)

        def plot_add_points_no_reset(*args, **kwargs):
            kwargs["reset_camera"] = False
            kwargs.setdefault("render", False)
            return orig_plot_add_points(*args, **kwargs)

        def plot_add_mesh_no_reset(*args, **kwargs):
            kwargs["reset_camera"] = False
            kwargs.setdefault("render", False)
            return orig_plot_add_mesh(*args, **kwargs)

        def plot_clear_preserving_cam(*args, **kwargs):
            res = None
            try:
                if orig_plot_clear:
                    res = orig_plot_clear(*args, **kwargs)
            finally:
                self._restore_main_camera(cam_state)
            return res

        # Apply patches
        try:
            if orig_widget_reset:
                widget.reset_camera = no_reset_camera
            if orig_widget_clear:
                widget.clear = clear_preserving_cam
            if orig_widget_add_points:
                widget.add_points = add_points_no_reset
            if orig_widget_add_mesh:
                widget.add_mesh = add_mesh_no_reset

            if plotter:
                if orig_plot_add_points:
                    plotter.add_points = plot_add_points_no_reset
                if orig_plot_add_mesh:
                    plotter.add_mesh = plot_add_mesh_no_reset
                if orig_plot_clear:
                    plotter.clear = plot_clear_preserving_cam

            # Global guard other code can check
            self._freeze_camera = True

            # Execute the render/update
            render_callable()

        finally:
            # Remove guard and restore methods
            self._freeze_camera = False

            if orig_widget_reset:
                widget.reset_camera = orig_widget_reset
            if orig_widget_clear:
                widget.clear = orig_widget_clear
            if orig_widget_add_points:
                widget.add_points = orig_widget_add_points
            if orig_widget_add_mesh:
                widget.add_mesh = orig_widget_add_mesh

            if plotter:
                if orig_plot_add_points:
                    plotter.add_points = orig_plot_add_points
                if orig_plot_add_mesh:
                    plotter.add_mesh = orig_plot_add_mesh
                if orig_plot_clear:
                    plotter.clear = orig_plot_clear

        # Restore camera immediately
        self._restore_main_camera(cam_state)

        # Also restore after render completes
        try:
            rw = widget.interactor.GetRenderWindow()
            tag = [None]
            def on_end(obj, evt):
                try:
                    self._restore_main_camera(cam_state)
                finally:
                    if tag[0] is not None:
                        rw.RemoveObserver(tag[0])
                        tag[0] = None
            tag[0] = rw.AddObserver("EndEvent", on_end)
        except Exception:
            pass

        # Timed backups in case of queued updates
        QTimer.singleShot(0,  lambda s=cam_state: self._restore_main_camera(s))
        QTimer.singleShot(80, lambda s=cam_state: self._restore_main_camera(s))
        QTimer.singleShot(180,lambda s=cam_state: self._restore_main_camera(s))


    # ------------------Active Buttons---------
    def _update_ribbon_container_height(self):
        """Fit the ribbon container to the currently visible ribbon."""
        if not hasattr(self, "ribbon_manager") or not hasattr(self, "ribbon_container"):
            return

        current_name = self.ribbon_manager.current_ribbon
        if not current_name:
            self.ribbon_container.setFixedHeight(0)
            return

        ribbon = self.ribbon_manager.ribbons.get(current_name)
        if ribbon is None:
            self.ribbon_container.setFixedHeight(0)
            return

        container_layout = self.ribbon_container.layout()
        if container_layout is not None:
            container_layout.activate()

        if ribbon.layout() is not None:
            ribbon.layout().activate()

        margins = container_layout.contentsMargins() if container_layout is not None else None
        top_margin = margins.top() if margins is not None else 0
        bottom_margin = margins.bottom() if margins is not None else 0

        content_height = max(
            ribbon.sizeHint().height(),
            ribbon.minimumSizeHint().height(),
            ribbon.minimumHeight(),
        )
        self.ribbon_container.setFixedHeight(content_height + top_margin + bottom_margin)

    def _handle_menu_click(self, ribbon_name):
        """Toggle ribbon and adjust container height"""
 
        #         self.byclass_dialog = ByClassDialog(self)  # pass main app as parent
        #     self.byclass_dialog.show()
        #     self.byclass_dialog.raise_()
        #     self.byclass_dialog.activateWindow()
        if not hasattr(self, 'ribbon_manager'):
            return
 
        self.ribbon_manager.toggle_ribbon(ribbon_name)
       
        # Show/hide ribbon container
        self._update_ribbon_container_height()
       
        self._sync_tools_for_ribbon_tab(self.ribbon_manager.current_ribbon)
        self._highlight_active_button(ribbon_name)

    def _sync_tools_for_ribbon_tab(self, ribbon_name):
        """Keep all tool tabs mutually exclusive when switching tabs."""
        if ribbon_name == "draw":
            self._enter_draw_tab_mode()
            self._clear_ribbon_active_buttons("classify")
            self._clear_ribbon_active_buttons("measure")
            self._clear_ribbon_active_buttons("identify")
        elif ribbon_name == "classify":
            self._enter_classify_tab_mode()
            self._clear_ribbon_active_buttons("draw")
            self._clear_ribbon_active_buttons("measure")
            self._clear_ribbon_active_buttons("identify")
        elif ribbon_name == "measure":
            self._enter_measure_tab_mode()
            self._clear_ribbon_active_buttons("draw")
            self._clear_ribbon_active_buttons("classify")
            self._clear_ribbon_active_buttons("identify")
        elif ribbon_name == "identify":
            self._enter_identify_tab_mode()
            self._clear_ribbon_active_buttons("draw")
            self._clear_ribbon_active_buttons("classify")
            self._clear_ribbon_active_buttons("measure")
        elif ribbon_name == "block":
            self._clear_ribbon_active_buttons("draw")
            self._clear_ribbon_active_buttons("classify")
            self._clear_ribbon_active_buttons("measure")
            self._clear_ribbon_active_buttons("identify")

    def _get_live_class_picker(self):
        """Return live ClassPicker, clear stale Qt wrappers if needed."""
        picker = getattr(self, "class_picker", None)
        if picker is None:
            return None
        try:
            if not _qt_object_is_valid(picker):
                self.class_picker = None
                return None
            # Access a QObject method to detect deleted C++ side.
            picker.objectName()
            return picker
        except (RuntimeError, ReferenceError, AttributeError):
            self.class_picker = None
            return None
        except Exception:
            self.class_picker = None
            return None

    def _class_picker_is_visible(self) -> bool:
        picker = self._get_live_class_picker()
        if picker is None:
            return False
        try:
            return bool(picker.isVisible())
        except (RuntimeError, ReferenceError, AttributeError):
            self.class_picker = None
            return False
        except Exception:
            return False

    def _hide_class_picker_safely(self):
        picker = self._get_live_class_picker()
        if picker is None:
            return
        try:
            picker.hide()
        except (RuntimeError, ReferenceError, AttributeError):
            self.class_picker = None
        except Exception:
            pass

    def _enter_draw_tab_mode(self):
        """Enable draw tooling and stop any active classification/measure/identify session."""
        classification_active = bool(
            getattr(self, "active_classify_tool", None)
            or self._class_picker_is_visible()
            or getattr(self, "classify_interactor", None)
            or getattr(self, "classify_interactors", None)
            or getattr(self, "cut_classify_interactor", None)
        )

        if classification_active:
            try:
                self.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification for Draw tab: {e}")

        # Deactivate measurement tool
        if hasattr(self, "measurement_tool"):
            try:
                self.measurement_tool.deactivate()
            except Exception as e:
                print(f"⚠️ Failed to deactivate measurement tool for Draw tab: {e}")

        # Deactivate identification tool (via ribbon so button states also reset)
        self._deactivate_identify_tab_tools()

        if hasattr(self, "digitizer") and self.digitizer:
            self.digitizer.enabled = True
            print("✅ Draw tab active - digitizer enabled")

    def _enter_classify_tab_mode(self):
        """Disable draw/measure/identify tooling so classification tools own interaction."""
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer.set_tool(None)
            except Exception as e:
                print(f"⚠️ Failed to deactivate draw tool for Classify tab: {e}")

            self.digitizer.enabled = False
            print("🚫 Classify tab active - digitizer disabled")

        curve_tool = getattr(self, "curve_tool", None)
        if curve_tool:
            try:
                if getattr(curve_tool, "active", False) and hasattr(curve_tool, "_cancel_curve"):
                    if hasattr(curve_tool, "suspend"):
                        curve_tool.suspend()
                    else:
                        curve_tool._cancel_curve()
                elif getattr(curve_tool, "_select_mode", False):
                    curve_tool.deactivate_select_mode()
            except Exception as e:
                print(f"⚠️ Failed to deactivate curve tool for Classify tab: {e}")

        # Deactivate measurement tool
        if hasattr(self, "measurement_tool"):
            try:
                self.measurement_tool.deactivate()
            except Exception as e:
                print(f"⚠️ Failed to deactivate measurement tool for Classify tab: {e}")

        # Deactivate identification tool (via ribbon so button states also reset)
        self._deactivate_identify_tab_tools()

    def _enter_measure_tab_mode(self):
        """Deactivate draw/classify/identify tools so measurement tools own interaction."""
        # Deactivate element selection tool first (it has its own VTK observers)
        try:
            if hasattr(self, "digitizer") and self.digitizer:
                self.digitizer.deactivate_element_select_tool()
        except Exception as e:
            print(f"⚠️ Failed to deactivate element select for Measure tab: {e}")

        # Deactivate draw tools
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer.set_tool(None)
            except Exception as e:
                print(f"⚠️ Failed to deactivate draw tool for Measure tab: {e}")
            self.digitizer.enabled = False

        # Deactivate curve tool
        curve_tool = getattr(self, "curve_tool", None)
        if curve_tool:
            try:
                if getattr(curve_tool, "active", False) and hasattr(curve_tool, "_cancel_curve"):
                    if hasattr(curve_tool, "suspend"):
                        curve_tool.suspend()
                    else:
                        curve_tool._cancel_curve()
                elif getattr(curve_tool, "_select_mode", False):
                    curve_tool.deactivate_select_mode()
            except Exception as e:
                print(f"⚠️ Failed to deactivate curve tool for Measure tab: {e}")

        # Deactivate classification
        if getattr(self, "active_classify_tool", None):
            try:
                self.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification for Measure tab: {e}")

        # Deactivate identification tool (via ribbon so button states also reset)
        self._deactivate_identify_tab_tools()

        print("📏 Measure tab active - other tools disabled")

    def _enter_identify_tab_mode(self):
        """Deactivate draw/classify/measure tools so identification tools own interaction."""
        # Deactivate element selection tool first (it has its own VTK observers)
        try:
            if hasattr(self, "digitizer") and self.digitizer:
                self.digitizer.deactivate_element_select_tool()
        except Exception as e:
            print(f"⚠️ Failed to deactivate element select for Identify tab: {e}")

        # Deactivate draw tools
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer.set_tool(None)
            except Exception as e:
                print(f"⚠️ Failed to deactivate draw tool for Identify tab: {e}")
            self.digitizer.enabled = False

        # Deactivate curve tool
        curve_tool = getattr(self, "curve_tool", None)
        if curve_tool:
            try:
                if getattr(curve_tool, "active", False) and hasattr(curve_tool, "_cancel_curve"):
                    if hasattr(curve_tool, "suspend"):
                        curve_tool.suspend()
                    else:
                        curve_tool._cancel_curve()
                elif getattr(curve_tool, "_select_mode", False):
                    curve_tool.deactivate_select_mode()
            except Exception as e:
                print(f"⚠️ Failed to deactivate curve tool for Identify tab: {e}")

        # Deactivate classification
        if getattr(self, "active_classify_tool", None):
            try:
                self.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification for Identify tab: {e}")

        # Deactivate measurement tool
        if hasattr(self, "measurement_tool"):
            try:
                self.measurement_tool.deactivate()
            except Exception as e:
                print(f"⚠️ Failed to deactivate measurement tool for Identify tab: {e}")

        print("🔍 Identify tab active - other tools disabled")

    def _deactivate_identify_tab_tools(self):
        """Deactivate all Identify-tab tools and reset their ribbon button states."""
        if not hasattr(self, "ribbon_manager"):
            return
        try:
            self.ribbon_manager.ribbons['identify'].deactivate_all_tools()
        except Exception as e:
            print(f"⚠️ Failed to deactivate identify tab tools: {e}")

    def _deactivate_active_identification_tools_for_escape(self) -> bool:
        """Turn off active click-identification modes on a single Escape."""
        deactivated = False
        for tool_name in (
            "identification_tool",
            "point_sync_tool",
            "snt_layer_pick_tool",
        ):
            tool = getattr(self, tool_name, None)
            if tool is None or not getattr(tool, "active", False):
                continue
            try:
                tool.deactivate()
                deactivated = True
                print(f"   ✅ {tool_name} deactivated by ESC")
            except Exception as exc:
                print(f"   ⚠️ ESC could not deactivate {tool_name}: {exc}")

        if deactivated:
            try:
                ribbon_manager = getattr(self, "ribbon_manager", None)
                identify_ribbon = (
                    getattr(ribbon_manager, "ribbons", {}).get("identify")
                    if ribbon_manager is not None else None
                )
                if identify_ribbon is not None and hasattr(identify_ribbon, "_deactivate_identify"):
                    identify_ribbon._deactivate_identify()
            except Exception:
                pass
        return deactivated

    def _clear_ribbon_active_buttons(self, ribbon_name):
        """Clear sticky button states for a ribbon whose tools were just deactivated."""
        if not hasattr(self, "ribbon_manager"):
            return

        ribbon = self.ribbon_manager.ribbons.get(ribbon_name)
        if ribbon is None:
            return

        from gui.menu_sidebar_system import RibbonSection

        for section in ribbon.findChildren(RibbonSection):
            if section.active_button:
                section.active_button.setChecked(False)
                section.active_button = None

    def _highlight_active_button(self, active_name):
        """Highlight currently active menu button via QSS checked state."""
        for name, btn in self.menu_buttons.items():
            ribbon = self.ribbon_manager.ribbons.get(name)
            is_visible = ribbon and ribbon.isVisible()

            if name == active_name and is_visible:
                btn.setChecked(True)
            else:
                btn.setChecked(False)

    def open_selection_mode(self):
        from gui.selection_popup import SelectionModeDialog
        active_tools = getattr(self, "_active_tools", set())

        if getattr(self, "cross_section_active", False) or "cross_section" in active_tools:
            try:
                print("🛑 Switching from cross-section to element selection")
                self.deactivate_cross_section_tool()
            except Exception:
                try:
                    self.set_cross_cursor_active(False, "cross_section")
                except Exception:
                    pass

        if getattr(self, "cut_section_mode_on", False) or "cut_section" in active_tools:
            try:
                print("🛑 Switching from cut-section to element selection")
                self._deactivate_pending_cut_section_tool("switching to element selection")
            except Exception:
                try:
                    self.set_cross_cursor_active(False, "cut_section")
                except Exception:
                    pass

        # Stand down classification tool the same way cross/cut-section are stood down.
        classify_tool = getattr(self, "active_classify_tool", None)
        if classify_tool and classify_tool not in ("cross_section", "cut_section"):
            try:
                print(f"🛑 Switching from {classify_tool} classification to element selection")
                self.deactivate_classification_tool(preserve_cross_section=True)
            except Exception as e:
                print(f"⚠️ Failed to deactivate classification before element select: {e}")

        # Stand down identification tool so its click observer doesn't fight the
        # element-select observer. Use the ribbon path so its button state resets too.
        # Defensive: ribbon_manager.ribbons may be missing during startup/teardown.
        identify_tool = getattr(self, "identification_tool", None)
        if identify_tool is not None and getattr(identify_tool, "active", False):
            try:
                rm = getattr(self, "ribbon_manager", None)
                ribbons = getattr(rm, "ribbons", None) if rm is not None else None
                identify_ribbon = ribbons.get("identify") if isinstance(ribbons, dict) else None
                if identify_ribbon is not None and hasattr(identify_ribbon, "deactivate_all_tools"):
                    identify_ribbon.deactivate_all_tools()
                else:
                    identify_tool.deactivate()
                print("🛑 Identification tool deactivated for element selection")
            except Exception as e:
                print(f"⚠️ Failed to deactivate identification before element select: {e}")

        dlg = getattr(self, "_element_selection_dialog", None)
        if dlg is None:
            dlg = SelectionModeDialog(self)
            self._element_selection_dialog = dlg
        if hasattr(dlg, "restore_from_chip"):
            dlg.restore_from_chip()
        elif dlg.windowState() & Qt.WindowMinimized:
            dlg.showNormal()
        else:
            dlg.show()
        dlg.raise_()
        dlg.activateWindow()
    # ------------------------------------------------------------
    # CUT SECTION BUFFER WIDTH PROMPT
    # ------------------------------------------------------------
    def _prompt_cut_width(self):
        """Popup for global persistent cut section width."""
        settings = QSettings("NakshaAI", "LidarApp")
        current_val = getattr(self, "default_cut_width", 2.0)
        val, ok = QInputDialog.getDouble(
            self, "Set Default Cut Width",
            f"Current cut width: ±{current_val:.2f} m",
            current_val, 0.05, 10.0, 2
        )
        if ok:
            self.default_cut_width = val
            settings.setValue("cut_section_width", val)
            settings.sync()
            print(f"💾 Saved persistent default cut width: ±{val:.2f} m")
            self.statusBar().showMessage(f"✅ Default cut width set to ±{val:.2f} m", 3000)

    def open_next_cross_section_view(self):
        """
        ✅ AUTO-INCREMENT VIEW OPENING FOR SHORTCUT
        - Opens View 1 if none are open
        - Opens next available view (2, 3, 4) if views already exist
        - Shows message if all 4 views are open
        """
        from PySide6.QtWidgets import QMessageBox
        
        # Restrict to top view only
        if getattr(self, "current_view", None) != "top":
            QMessageBox.warning(self, "Cross Section", "Cross Section works only in Top View.")
            return

        # ✅ Sync palette FIRST
        if hasattr(self, 'display_dialog') and self.display_dialog:
            print(f"\n{'='*60}")
            print(f"🔄 SYNCING PALETTE BEFORE CROSS-SECTION")
            dialog = self.display_dialog
            current_slot = dialog.current_slot
            if hasattr(dialog, 'view_palettes') and 0 in dialog.view_palettes:
                # Keep app.class_palette canonical to MAIN VIEW (slot 0).
                # Cross-section slots (1..4) must remain isolated in view_palettes.
                self.class_palette = {}
                for code, info in dialog.view_palettes[0].items():
                    self.class_palette[code] = {
                        "show": bool(info.get("show", False)),
                        "description": str(info.get("description", "")),
                        "lvl": str(info.get("lvl", "")),
                        "color": tuple(info.get("color", (128, 128, 128))),
                        "weight": float(info.get("weight", 1.0))
                    }
                print(f"  ✅ Synced {len(self.class_palette)} MAIN-view classes from Display Mode")
                if current_slot != 0:
                    print(f"  ℹ️ Active slot is {current_slot}; preserved class_palette from slot 0")
            elif hasattr(dialog, 'view_palettes') and current_slot in dialog.view_palettes:
                # Do not overwrite class_palette from non-main slots.
                print(f"  ℹ️ Slot 0 palette unavailable; keeping existing class_palette unchanged")
            print(f"{'='*60}\n")
        
        # ✅ Ensure dictionaries exist
        if not hasattr(self, "section_docks"):
            self.section_docks = {}
        if not hasattr(self, "section_vtks"):
            self.section_vtks = {}
        
        # ✅ Find next available view (0-3 = Views 1-4)
        next_view = None
        for i in range(4):
            if i not in self.section_docks or not self.section_docks[i].isVisible():
                next_view = i
                break
        
        # ✅ All 4 views already open
        if next_view is None:
            QMessageBox.information(
                self, 
                "All Views Open", 
                "All 4 cross-section views are already active.\n\n"
                "Close one to open a new view."
            )
            self.statusBar().showMessage("⚠️ All 4 cross-section views already open", 3000)
            return

        # Cross-section owns the main-canvas interaction while it is active.
        # Preserve an unfinished curve, but do not auto-resume it behind the
        # cross-section interactor. Do this only once activation can proceed.
        self._suspend_curve_tool_safely(
            "switching to cross-section",
            resume_after_switch=False,
        )
        
        # ✅ Open the next view directly
        print(f"✅ Auto-opening Cross Section View {next_view + 1}")
        self._open_specific_cross_section_view(next_view)
        
        self.statusBar().showMessage(
            f"✅ Cross Section View {next_view + 1} opened - Draw line on main view", 
            3000
        )

    def _deactivate_point_pick_tools(self):
        """
        Turn OFF every click-to-pick point tool (Identify / Point Sync / SNT
        layer pick) so none of them collide with Cross/Cut Section on the
        shared section/cut views. Also resets the Identify ribbon toggle
        button so the UI stays consistent.
        Returns True if any tool was active and got disabled.
        """
        disabled = False
        for tool_name in ("identification_tool", "point_sync_tool", "snt_layer_pick_tool"):
            tool = getattr(self, tool_name, None)
            if tool is None or not getattr(tool, "active", False):
                continue
            try:
                tool.deactivate()
            except Exception:
                pass
            disabled = True

        # Reset the Identify ribbon toggle button if present.
        try:
            ribbon_manager = getattr(self, "ribbon_manager", None)
            identify_ribbon = (
                getattr(ribbon_manager, "ribbons", {}).get("identify")
                if ribbon_manager is not None else None
            )
            if identify_ribbon is not None and hasattr(identify_ribbon, "_deactivate_identify"):
                identify_ribbon._deactivate_identify()
        except Exception:
            pass

        if disabled:
            print("🚫 Point-pick tools auto-disabled (Cross/Cut Section activated)")
        return disabled

    def enable_cross_section_mode(self):   #Added by bala
        # Check if we're in top view (for main viewer)
        if getattr(self, "current_view", None) != "top":
            QMessageBox.warning(self, "Cut Section", "Main view must be in Top View")
            return

        # ✅ MUTUAL EXCLUSION: Point-pick tools (Identify / Point Sync / SNT
        # pick) collide with Cross Section on the same views, so disable them
        # automatically when Cross Section is activated (no popup needed).
        self._deactivate_point_pick_tools()

        # Cross-section and Curve are mutually exclusive canvas tools. Keep
        # any unfinished curve available for a later manual Curve activation,
        # but prevent the display-mode auto-resume callback from re-enabling it.
        self._suspend_curve_tool_safely(
            "switching to cross-section",
            resume_after_switch=False,
        )

        # Deactivate any active digitize tool before enabling cross-section
        self._deactivate_digitize_tool()

        # ✅ Stand down the temp fence tool — its main-view VTK observers
        # would otherwise keep capturing clicks during cross-section drawing.
        try:
            _tft = getattr(self, "temp_fence_tool", None)
            if _tft is not None and getattr(_tft, "active", False):
                _tft.deactivate()
                if getattr(self, "active_classify_tool", None) == "temp_fence":
                    self.active_classify_tool = None
                print("🚧 Temp fence stood down for cross-section mode")
        except Exception as _e:
            print(f"⚠️ Temp fence stand-down failed: {_e}")


        # ✅ Create NON-BLOCKING view selector (only once)
        if not hasattr(self, '_view_selector_dialog') or self._view_selector_dialog is None:
            from gui.theme_manager import get_dialog_stylesheet

            class _CrossSectionViewSelectorDialog(MinimizableDialogMixin, QDialog):
                def __init__(self, parent):
                    super().__init__(parent)
                    self._init_minimize_state()

            self._view_selector_dialog = _CrossSectionViewSelectorDialog(self)
            self._view_selector_dialog.setProperty("themeStyledDialog", True)
            self._view_selector_dialog.setWindowTitle("Target Cross View")
            self._view_selector_dialog.setStyleSheet(get_dialog_stylesheet())
            self._view_selector_dialog.setWindowFlags(
                Qt.Window |
                Qt.WindowMinimizeButtonHint |
                Qt.WindowTitleHint |
                Qt.WindowCloseButtonHint |
                Qt.WindowSystemMenuHint
            )
            self._view_selector_dialog.setWindowModality(Qt.NonModal)

            layout = QVBoxLayout()
            layout.setContentsMargins(10, 8, 10, 10)  # tighter padding
            layout.setSpacing(0)                       # no gap needed with single widget

            # Dropdown only — no empty label
            combo = QComboBox()
            combo.addItems(["View 1", "View 2", "View 3", "View 4"])
            combo.setCurrentIndex(0)
            combo.setEditable(True)
            combo.lineEdit().setAlignment(Qt.AlignCenter)
            combo.lineEdit().setReadOnly(True)  # prevents typing, keeps alignment
            combo.setMinimumWidth(150)
            layout.addWidget(combo)

            self._view_selector_dialog.view_combo = combo
            self._view_selector_dialog.setLayout(layout)
            self._view_selector_dialog.setFixedSize(180, 58)  # ✅ tight: just title bar + combo
           
            # Connect dropdown change
            def on_view_changed(index):
                print(f"🔄 Target view changed to: View {index + 1}")
                self.section_controller.active_view = index
                self.statusBar().showMessage(
                    f"✅ Target: View {index + 1} - Draw line on main view",
                    3000
                )
            combo.currentIndexChanged.connect(on_view_changed)
           
            # Save geometry on close
            def save_geometry_on_close(event):
                self.settings.setValue("ViewSelectorDialog_geometry",
                                    self._view_selector_dialog.saveGeometry())
                event.accept()
            self._view_selector_dialog.closeEvent = save_geometry_on_close
           
            # Restore geometry
            saved_geo = self.settings.value("ViewSelectorDialog_geometry")
            if saved_geo:
                self._view_selector_dialog.restoreGeometry(saved_geo)
            else:
                # Position at top-right of main window
                self._view_selector_dialog.move(
                    self.x() + self.width() - 320,
                    self.y() + 100
                )

        from gui.theme_manager import get_dialog_stylesheet
        self._view_selector_dialog.setStyleSheet(get_dialog_stylesheet())
       
        # ✅ ALWAYS SHOW AND BRING TO FRONT (even if already open!)
        if getattr(self._view_selector_dialog, "_is_minimized_to_chip", False):
            self._view_selector_dialog.restore_from_chip()
        else:
            self._view_selector_dialog.show()
        self._view_selector_dialog.raise_()
        self._view_selector_dialog.activateWindow()  # ✅ Force focus
       
        # Get selected index
        if hasattr(self._view_selector_dialog, 'view_combo'):
            selected_index = self._view_selector_dialog.view_combo.currentIndex()
        else:
            selected_index = 0
       
        # Set active view in controller
        self.section_controller.active_view = selected_index
        print(f"✅ Cross-section mode enabled - Target: View {selected_index + 1}")
       
        # Attach main interactor
        self._attach_cross_section_interactor()
        self.set_cross_cursor_active(True, "cross_section")
        self.statusBar().showMessage(
            f"✅ Ready: Draw line → View {selected_index + 1} (change dropdown to switch)",
            5000
        )

    def _open_specific_cross_section_view(self, view_index):
        """
        ✅ INTERNAL METHOD - Creates/activates a specific cross-section view
        Called by both:
        - enable_cross_section_mode() (manual dialog selection)
        - open_next_cross_section_view() (auto-increment shortcut)
        """
        from PySide6.QtWidgets import QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QPushButton
        from pyvistaqt import QtInteractor
        from .cross_section.interactor_slice import CrossSectionInteractor

        # Ensure dictionaries exist
        if not hasattr(self, "section_docks"):
            self.section_docks = {}
        if not hasattr(self, "section_vtks"):
            self.section_vtks = {}

        # --------------------------------------------------------
        # 1. Handle Existing Dock (Reuse)
        # --------------------------------------------------------
        if view_index in self.section_docks:
            dock = self.section_docks[view_index]
            
            # Ensure visible and active
            if not dock.isVisible():
                dock.show()
            dock.raise_()
            dock.activateWindow()
            
            # Update controller state
            self.section_controller.active_view = view_index
            if view_index in self.section_vtks:
                self.section_controller.current_vtk = self.section_vtks[view_index]
                
            print(f"🔁 View {view_index + 1} already open. Activated.")
            
            # ✅ CRITICAL: Still need to attach main interactor even for existing views
            self._attach_cross_section_interactor()
            return

        # --------------------------------------------------------
        # 2. Create New Dock (Only if not exists)
        # --------------------------------------------------------
        # Create new dock
        dock = QDockWidget(f"Cross Section {view_index + 1}", self)
        dock.setObjectName(f"CrossSectionDock_{view_index}")  # CRITICAL for persistence
        dock.setContextMenuPolicy(Qt.NoContextMenu)

        frame = QWidget()
        layout = QVBoxLayout(frame)
        
        # Create VTK widget
        vtk_widget = QtInteractor(frame)
        self._setup_interactor_swapper(
            vtk_widget.interactor,
            preserve_physical_middle_pan=True,
        )
        from gui.theme_manager import ThemeManager
        bg_color = ThemeManager.canvas_background_for_theme()
        bg_rgb = ThemeManager.canvas_background_rgb()
        vtk_widget.set_background(bg_color)
        vtk_widget.renderer.SetBackground(*bg_rgb)
        vtk_widget.renderer.SetBackground2(*bg_rgb)
        vtk_widget.renderer.GradientBackgroundOff()
        layout.addWidget(vtk_widget.interactor)
        
        dock.setWidget(frame)
        
        # Add to main window FIRST (required for geometry operations)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)
        
        # Set floating and window flags BEFORE geometry restoration
        dock.setFloating(True)
        dock.setWindowFlags(
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        
        dock.setFeatures(
            QDockWidget.DockWidgetClosable |
            QDockWidget.DockWidgetFloatable
        )
        
        dock.setAllowedAreas(Qt.NoDockWidgetArea)
        
        # ✅ CORRECT: Restore individual dock geometry
        dock_geo_key = f"CrossSectionDock_{view_index}_geometry"
        saved_geometry = self.settings.value(dock_geo_key)
        
        if saved_geometry is not None:
            # Restore saved position and size
            dock.restoreGeometry(saved_geometry)
            print(f"✅ Restored dock {view_index + 1} geometry (position + size)")
        else:
            # First time - use default offset position
            offset = 40 * len(self.section_docks)
            dock.move(self.x() + self.width() - 400 + offset, self.y() + 120 + offset)
            dock.resize(500, 400)
            print(f"→ No saved geometry - using default position for dock {view_index + 1}")
        
        dock.show()
        ThemeManager.apply_native_window_theme(dock)
        
        # Custom close event handler
        original_close_event = dock.closeEvent

        def safe_close_event(event):
            """
            ✅ FIXED: Proper VTK cleanup before closing to prevent handle errors
            Shows confirmation dialog ONLY for Alt+F4, allows normal close otherwise
            """
            try:
                # During app-wide shutdown, the main closeEvent owns all VTK cleanup.
                # Avoid per-dock prompts/teardown here to prevent double-finalize races.
                if getattr(self, "_shutdown_in_progress", False):
                    print(f"🚪 App shutdown: accepting close for View {view_index + 1}")
                    event.accept()
                    return

                # ✅ NEW: Check if close was triggered by Alt+F4 (or window X button)
                from PySide6.QtWidgets import QMessageBox
               
                # Check if this is a spontaneous event (user-initiated like Alt+F4 or X button)
                # vs programmatic close
                if event.spontaneous():
                    reply = QMessageBox.question(
                        self,
                        "Close Cross-Section View?",
                        f"Are you sure you want to close Cross-Section View {view_index + 1}?\n\n"
                        "The view can be reopened from Tools → Cross Section.",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No
                    )
                   
                    if reply == QMessageBox.No:
                        print(f"❌ User cancelled closing View {view_index + 1}")
                        event.ignore()
                        return
                   
                    print(f"🚪 User confirmed closing View {view_index + 1} - cleaning up...")
                else:
                    # Programmatic close - no confirmation needed
                    print(f"🚪 Programmatically closing View {view_index + 1} - cleaning up...")

                # Detach per-view identify/point-sync hooks before VTK teardown.
                try:
                    identify_tool = getattr(self, "identification_tool", None)
                    if identify_tool is not None and hasattr(identify_tool, "deactivate_for_section"):
                        identify_tool.deactivate_for_section(view_index)
                except Exception as e:
                    print(f"   ⚠️ Identify section observer cleanup warning: {e}")

                try:
                    point_sync_tool = getattr(self, "point_sync_tool", None)
                    if point_sync_tool is not None and hasattr(point_sync_tool, "deactivate_for_section"):
                        point_sync_tool.deactivate_for_section(view_index)
                except Exception as e:
                    print(f"   ⚠️ Point sync section observer cleanup warning: {e}")

                # ✅ CRITICAL: Stop all VTK rendering FIRST
                try:
                    # 1. Clear the VTK widget completely
                    if view_index in self.section_vtks:
                        vtk_widget = self.section_vtks[view_index]
                       
                        # Stop any active render timers
                        if hasattr(vtk_widget, 'render_timer'):
                            try:
                                vtk_widget.render_timer.stop()
                                vtk_widget.render_timer.deleteLater()
                                vtk_widget.render_timer = None
                            except Exception:
                                pass
                       
                        # Finalize the render window (releases GPU resources)
                        try:
                            if not getattr(vtk_widget, "_naksha_view_finalized", False):
                                render_window = vtk_widget.GetRenderWindow()
                                if render_window:
                                    try:
                                        if hasattr(render_window, "SetAbortRender"):
                                            render_window.SetAbortRender(1)
                                    except Exception:
                                        pass
                                    try:
                                        if hasattr(render_window, "SetMapped"):
                                            render_window.SetMapped(False)
                                    except Exception:
                                        pass
                                    render_window.Finalize()
                                    vtk_widget._naksha_view_finalized = True
                                    print(f"   ✅ VTK render window finalized")
                        except Exception as e:
                            print(f"   ⚠️ Render window finalize warning: {e}")
                       
                        # Clear the renderer
                        try:
                            if hasattr(vtk_widget, 'renderer'):
                                vtk_widget.renderer.RemoveAllViewProps()
                                print(f"   ✅ Renderer cleared")
                        except Exception as e:
                            print(f"   ⚠️ Renderer clear warning: {e}")
                       
                        # Set render window to None (breaks the connection)
                        try:
                            vtk_widget.SetRenderWindow(None)
                            print(f"   ✅ VTK widget disconnected from render window")
                        except Exception as e:
                            print(f"   ⚠️ SetRenderWindow warning: {e}")
               
                except Exception as e:
                    print(f"   ⚠️ VTK cleanup error: {e}")
               
                # ✅ Save geometry BEFORE hiding
                from PySide6.QtCore import QSettings
                settings = QSettings("NakshaAI", "LidarApp")
               
                try:
                    settings.setValue(f"CrossSectionDock_{view_index}_geometry", dock.saveGeometry())
                    print(f"   💾 Dock {view_index + 1} geometry saved")
                except Exception as e:
                    print(f"   ⚠️ Geometry save failed: {e}")
               
                # ✅ NOW safe to hide and close
                dock.hide()
               
                # Remove camera sync observer BEFORE removing vtk reference so the
                # stale entry doesn't block reinstall when the dialog is reopened.
                self._remove_camera_sync_observer(view_index)

                # Remove from tracking (but don't delete the widget yet - let Qt handle it)
                if hasattr(self, 'section_docks') and view_index in self.section_docks:
                    del self.section_docks[view_index]
                if hasattr(self, 'section_vtks') and view_index in self.section_vtks:
                    del self.section_vtks[view_index]
               
                # Schedule deletion for next event loop (safe cleanup)
                from PySide6.QtCore import QTimer
                QTimer.singleShot(100, lambda: dock.deleteLater())
               
                print(f"   ✅ View {view_index + 1} cleanup complete")
               
                # Accept the close event
                event.accept()
               
            except Exception as e:
                print(f"⚠️ Close event error: {e}")
                event.accept()  # Still allow close on error
 
        dock.closeEvent = safe_close_event
        
        # Register in app
        self.section_docks[view_index] = dock
        self.section_vtks[view_index] = vtk_widget
        self.section_controller.active_view = view_index
        self.section_controller.current_vtk = vtk_widget
        
        if getattr(self, "active_classify_tool", None):
            try:
                from gui.cross_section.interactor_classify import ClassificationInteractor
                old = self.classify_interactors.get(view_index)
                if old and hasattr(old, 'cleanup'):
                    old.cleanup()
                wrapper = ClassificationInteractor(self, vtk_widget.interactor)
                vtk_widget.interactor.SetInteractorStyle(wrapper.style)
                self.classify_interactors[view_index] = wrapper
                print(f"✅ Classification interactor attached to NEW View {view_index + 1}")
            except Exception as e:
                print(f"⚠️ Failed to attach classification on new view {view_index + 1}: {e}")
        else:
            # No active tool - install right-click reactivation observer on plain style
            try:
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                if not hasattr(self, '_section_right_click_observers'):
                    self._section_right_click_observers = {}
                interactor = vtk_widget.interactor
                self._apply_plain_section_2d_style(interactor, vtk_widget)
                app_ref = self
                def _make_handler(app):
                    def _handler(obj, event):
                        print(f"🖱️ Right-click detected in cross-section view")
                        active_tool = getattr(app, "active_classify_tool", None)
                        section_has_classifier = bool(getattr(app, "classify_interactors", None))
                        if active_tool is None or not section_has_classifier:
                            last_tool = getattr(app, "last_classify_tool", None)
                            if last_tool and hasattr(app, "set_classify_tool"):
                                try:
                                    app.from_classes = getattr(app, "last_classify_from_classes", None)
                                    app.to_class = getattr(app, "last_classify_to_class", None)
                                    app._right_click_reactivating = True
                                    try:
                                        app.set_classify_tool(last_tool)
                                    finally:
                                        app._right_click_reactivating = False
                                except Exception as e:
                                    print(f"   ⚠️ Right-click reactivate failed: {e}")
                    return _handler
                # Remove old observer if any
                old_tag = self._section_right_click_observers.get(view_index)
                if old_tag is not None:
                    try:
                        interactor.RemoveObserver(old_tag)
                    except Exception:
                        pass
                tag = interactor.AddObserver("RightButtonPressEvent", _make_handler(app_ref), 1.0)
                self._section_right_click_observers[view_index] = tag

                # Left-click observer: MicroStation locate when cross-section tool active
                def _make_left_handler(app, vidx):
                    def _left_handler(obj, event):
                        if not getattr(app, 'cross_section_active', False):
                            return
                        if not getattr(app, 'section_locate_enabled', True):
                            return
                        sc = getattr(app, 'section_controller', None)
                        if sc is None:
                            return
                        sc.active_view = vidx
                        x, y = obj.GetEventPosition()
                        sc._do_section_locate(obj, x, y)
                    return _left_handler
                interactor.AddObserver("LeftButtonPressEvent", _make_left_handler(app_ref, view_index), 1.0)

                # Mouse-move observer: rubber-band preview while in section view
                def _make_move_handler(app, vidx):
                    def _move_handler(obj, event):
                        if not getattr(app, 'section_locate_enabled', True):
                            return
                        locate_disp = getattr(app, '_section_locate_display', None)
                        locate_view = getattr(app, '_section_locate_view', None)
                        if locate_disp is None or locate_view != vidx:
                            return
                        sc = getattr(app, 'section_controller', None)
                        if sc is None:
                            return
                        x, y = obj.GetEventPosition()
                        sc._draw_locate_rubber_band(x, y)
                    return _move_handler
                interactor.AddObserver("MouseMoveEvent", _make_move_handler(app_ref, view_index), 1.0)

                print(f"✅ Right-click observer added to NEW View {view_index + 1}")
            except Exception as e:
                print(f"⚠️ Failed to add right-click observer on new view {view_index + 1}: {e}")
        
        # Store in controller for global access
        if not hasattr(self.section_controller, 'view_vtks'):
            self.section_controller.view_vtks = {}
        self.section_controller.view_vtks[view_index] = vtk_widget
        
        print(f"✅ Registered View {view_index + 1}: dock={dock}, vtk={vtk_widget}")
        print(f"   Active view set to: {self.section_controller.active_view}")
        
        def on_dock_activated():
            self.section_controller.active_view = view_index
            print(f"✅ Dock {view_index + 1} activated -> Active view set")
        
        dock.visibilityChanged.connect(
            lambda visible: on_dock_activated() if visible else None
        )

        def _on_top_level_changed(is_floating):
            """Reinstall right-click observers when dock state changes (floating <-> tabified)."""
            if not is_floating:
                print(f"📎 View {view_index + 1} docked/tabified - reinstalling right-click observers")
                try:
                    self._reinstall_section_right_click_observer(view_index)
                except Exception as e:
                    print(f"   ⚠️ Failed to reinstall observer: {e}")

        dock.topLevelChanged.connect(_on_top_level_changed)
        
        # Install shortcut filter for new dock
        if hasattr(self, "_shortcut_filter"):
            vtk_widget.interactor.installEventFilter(self._shortcut_filter)
        self._register_canvas_cursor_widget(vtk_widget.interactor)
        self._install_section_wheel_zoom(vtk_widget)
        
        if hasattr(self, 'identification_tool') and self.identification_tool.active:
            self.identification_tool.activate_for_section(vtk_widget, view_index)
            print(f"🔍 Auto-activated identification for view {view_index + 1}")

        if hasattr(self, 'point_sync_tool') and self.point_sync_tool.active:
            self.point_sync_tool.activate_for_section(vtk_widget, view_index)
            print(f"🎯 Auto-activated point sync for view {view_index + 1}")

        # --------------------------------------------------------
        # 3. Attach Main Interactor (For new docks)
        # --------------------------------------------------------
        self._attach_cross_section_interactor()

        # ✅ FIX: Restore section data if this view was previously computed
        # When the dialog is closed and reopened, a new VTK widget is created but
        # the stored section data (section_{view_index}_core_points, etc.) still
        # exists on self. Re-render it so the dialog doesn't appear empty.
        core_pts = getattr(self, f"section_{view_index}_core_points", None)
        if core_pts is not None:
            try:
                from gui.unified_actor_manager import build_section_unified_actor
                build_section_unified_actor(
                    self,
                    view_index,
                    view=getattr(self, 'cross_view_mode', 'side')
                )
                vtk_widget.render()
                print(f"✅ Restored section data for View {view_index + 1}")
                self.statusBar().showMessage(
                    f"✅ Cross Section View {view_index + 1} restored", 5000
                )
            except Exception as e:
                print(f"⚠️ Failed to restore section data for View {view_index + 1}: {e}")
                self.statusBar().showMessage(
                    "✏️ Draw a line on the Plan View to create cross-section", 5000
                )
        else:
            self.statusBar().showMessage(
                "✏️ Draw a line on the Plan View to create cross-section",
                5000
            )

        print(f"✅ Cross Section View {view_index + 1} ready")

                # Install camera sync observer for ANY sync relationship (target OR source)
        if hasattr(self, 'view_sync_map'):
            is_target = view_index in self.view_sync_map
            is_source = any(src == view_index for src in self.view_sync_map.values())
            if is_target or is_source:
                self._install_realtime_camera_observer(view_index, vtk_widget)
                paired = set()
                if is_target:
                    paired.add(self.view_sync_map[view_index])
                for t, s in self.view_sync_map.items():
                    if s == view_index:
                        paired.add(t)
                for peer_idx in paired:
                    if peer_idx in self.section_vtks:
                        self._install_realtime_camera_observer(peer_idx, self.section_vtks[peer_idx])

    def _attach_cross_section_interactor(self):
        """
        ✅ HELPER: Attaches CrossSectionInteractor to main view
        Shared by both new and existing view activation
        """
        from .cross_section.interactor_slice import CrossSectionInteractor
        
        try:
            main_interactor = self.vtk_widget.interactor
            
            # Save the old interactor to restore later
            if not hasattr(self, 'previous_interactor_style'):
                self.previous_interactor_style = main_interactor.GetInteractorStyle()
                print(f"💾 Saved previous interactor style: {self.previous_interactor_style}")
            
            # Create and attach the cross-section interactor
            self.cross_interactor = CrossSectionInteractor(self, main_interactor)
            main_interactor.SetInteractorStyle(self.cross_interactor)
            self.cross_interactor.section_controller = self.section_controller
            
            # Mark tool as active
            self.cross_section_active = True
            
            # ✅ Create mock cross_action for compatibility
            class MockAction:
                def __init__(self):
                    self._checked = True
                
                def isChecked(self):
                    return self._checked
                
                def setChecked(self, checked):
                    self._checked = checked
            
            self.cross_action = MockAction()
            print("✅ Created mock cross_action for interactor compatibility")
            
            # Restore interactor after cross-section is drawn
            def restore_interactor_after_draw():
                if hasattr(self, 'previous_interactor_style') and self.previous_interactor_style:
                    try:
                        main_interactor.SetInteractorStyle(self.previous_interactor_style)
                        print(f"🔄 Restored previous interactor style")
                    except Exception as e:
                        print(f"⚠️ Failed to restore interactor: {e}")
                        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                        main_interactor.SetInteractorStyle(vtkInteractorStyleImage())
                
                self.cross_section_active = False
                self.cross_interactor = None
                print("🛑 Cross-section tool finished (auto-restore)")
            
            self.cross_interactor.on_section_complete = restore_interactor_after_draw
            
            print("🧭 CrossSectionInteractor attached to MAIN viewer (with auto-restore)")
            
        except Exception as e:
            print(f"⚠️ Failed to attach CrossSectionInteractor: {e}")
            import traceback
            traceback.print_exc()
        
        # Install shortcut filter for main widget
        if hasattr(self, "_shortcut_filter"):
            self.vtk_widget.interactor.installEventFilter(self._shortcut_filter)

    def toggle_cross_section_mode(self, checked):
        """
        Toggle cross-section drawing mode.
        ✅ FIXED: Automatically activates an existing open view if none is selected.
        """
        if checked:
            # 1. Check if a view is already active
            active_view = getattr(self.section_controller, 'active_view', None)
            
            # 2. If NO view is active, but we have open docks, pick the first one!
            if active_view is None and hasattr(self, 'section_docks') and self.section_docks:
                # Get the first available view index (e.g., 1, 2, 3, or 4)
                first_view_idx = sorted(list(self.section_docks.keys()))[0]
                
                # Force this view to be active
                self.section_controller.active_view = first_view_idx
                self.section_controller.current_vtk = self.section_vtks[first_view_idx]
                
                print(f"⚠️ No active view selected. Auto-activating View {first_view_idx}")
                
                # Optional: Bring that dock to front so user knows which one it is
                self.section_docks[first_view_idx].raise_()
                self.section_docks[first_view_idx].activateWindow()

            # 3. Now enable the mode as usual
            self.enable_cross_section_mode()
            
        else:
            self.deactivate_cross_section_tool()
        
        # ════════════════════════════════════════════════════════════════════════════════
    # COMPLETE BIDIRECTIONAL SYNC - CAMERA + SECTION DATA
    # ════════════════════════════════════════════════════════════════════════════════

    def _on_section_updated(self, view_idx: int):
        """
        Call AFTER a cross-section is computed in view_idx.
        Syncs section geometry to matched views and mirrors the source view's
        camera/zoom when sync is active.
        """
        # view, do not bounce another sync cycle back out from it.
        is_a_source = any(src == view_idx for src in self.view_sync_map.values())
        if not is_a_source and getattr(self, '_sync_count', 0) > 0:
            return

        core = getattr(self, f"section_{view_idx}_core_points", None)
        buf = getattr(self, f"section_{view_idx}_buffer_points", None)
        
        # Also check global section data as fallback
        if core is None:
            core = getattr(self, "section_core_points", None)
            buf = getattr(self, "section_buffer_points", None)
        
        if core is None:
            print("⚠️ _on_section_updated: no section data available")
            return

        # Sync section data to matched views
        self._sync_section_from(view_idx)


    def _get_synced_view_targets(self, view_idx: int):
        """Return every view currently linked to view_idx."""
        targets = set()
        if not hasattr(self, 'view_sync_map') or not self.view_sync_map:
            return targets

        for target_idx, src in self.view_sync_map.items():
            if src == view_idx and target_idx != view_idx:
                targets.add(target_idx)

        peer_idx = self.view_sync_map.get(view_idx)
        if peer_idx is not None and peer_idx != view_idx:
            targets.add(peer_idx)

        return targets


    def _remember_section_camera_state(self, view_idx: int):
        """Snapshot the current section-view camera so later sync only follows user movement."""
        if not hasattr(self, 'section_vtks') or view_idx not in self.section_vtks:
            return

        state = self._get_camera_state_fast(self.section_vtks[view_idx])
        if state is None:
            return

        if not hasattr(self, '_last_camera_states'):
            self._last_camera_states = {}
        self._last_camera_states[view_idx] = dict(state)


    def _sync_section_from(self, source_idx: int):
        """
        Copy section data from source view to ALL targets that match it.
        ✅ BIDIRECTIONAL: Works with bidirectional sync
        ✅ SYNCS: Points, masks, indices, and source camera/zoom
        """
        import numpy as np
        
        # Re-entrancy guard
        if getattr(self, '_sync_count', 0) > 0:
            return
        
        self._sync_count = getattr(self, '_sync_count', 0) + 1
        
        try:
            # Get source data - try view-specific first, then global
            core = getattr(self, f"section_{source_idx}_core_points", None)
            buf = getattr(self, f"section_{source_idx}_buffer_points", None)
            
            # Fallback to global section data if the source view has not been
            # promoted into its per-view storage yet.
            if core is None:
                core = getattr(self, "section_core_points", None)
                buf = getattr(self, "section_buffer_points", None)

                if core is not None:
                    setattr(self, f"section_{source_idx}_core_points", core)
                    setattr(self, f"section_{source_idx}_buffer_points", buf)
            
            if core is None:
                print(f"⚠️ _sync_section_from: no source data for View {source_idx + 1}")
                return
            
            # All attributes to sync
            attrs = [
                'core_points', 'buffer_points', 
                'core_mask', 'buffer_mask',
                'core_indices', 'buffer_indices', 'indices', 
                'points_transformed', 'combined_mask', 
                'P1', 'P2', 'half_width'
            ]
            
            # Collect source attributes
            source_attrs = {}
            for attr in attrs:
                # Try view-specific first
                val = getattr(self, f"section_{source_idx}_{attr}", None)
                # Fallback to global
                if val is None:
                    val = getattr(self, f"section_{attr}", None)
                source_attrs[attr] = val
            
            source_cam = None
            if hasattr(self, 'section_vtks') and source_idx in self.section_vtks:
                source_cam = self._get_camera_state_fast(self.section_vtks[source_idx])

            # Sync only to views that explicitly match this source.
            views_to_sync = set()
            for target_idx, src in self.view_sync_map.items():
                if src == source_idx and target_idx != source_idx:
                    views_to_sync.add(target_idx)
            
            # Sync to each target view
            for target_idx in views_to_sync:
                if target_idx == source_idx:
                    continue
                if not hasattr(self, 'section_vtks') or target_idx not in self.section_vtks:
                    continue
                if not self.section_controller:
                    continue
                
                vtk_widget = self.section_vtks[target_idx]
                
                # Copy ALL attributes to target. Arrays are cloned so a later
                # in-place mutation in one view cannot silently poison another.
                import numpy as _np
                for attr, value in source_attrs.items():
                    if isinstance(value, _np.ndarray):
                        setattr(self, f"section_{target_idx}_{attr}", value.copy())
                    else:
                        setattr(self, f"section_{target_idx}_{attr}", value)
                
                # Use SOURCE view mode so the synced target mirrors the source.
                view_mode = "side"
                try:
                    btns = getattr(self, "section_view_buttons", {}).get(source_idx, {})
                    if btns.get("front") and btns["front"].isChecked():
                        view_mode = "front"
                except Exception:
                    pass
                
                print(f"   🔗 Syncing section: View {target_idx + 1} from View {source_idx + 1} (mode={view_mode})")
                
                # Save current active view
                prev_active = getattr(self.section_controller, "active_view", None)
                prev_vtk = getattr(self.section_controller, "current_vtk", None)
                
                try:
                    # Clear and re-plot
                    vtk_widget.clear()
                    self.section_controller.active_view = target_idx
                    self.section_controller.current_vtk = vtk_widget
                    
                    # Plot section with source data
                    self.section_controller._plot_section(
                        core,
                        buf if buf is not None else np.empty((0, 3)),
                        view=view_mode
                    )

                    # Keep button state aligned when per-view mode buttons exist.
                    try:
                        target_btns = getattr(self, "section_view_buttons", {}).get(target_idx, {})
                        side_btn = target_btns.get("side")
                        front_btn = target_btns.get("front")
                        if side_btn is not None and front_btn is not None:
                            side_block = side_btn.blockSignals(True)
                            front_block = front_btn.blockSignals(True)
                            side_btn.setChecked(view_mode == "side")
                            front_btn.setChecked(view_mode == "front")
                            side_btn.blockSignals(side_block)
                            front_btn.blockSignals(front_block)
                    except Exception:
                        pass

                    # Sync palette from SOURCE -> TARGET before apply so the target
                    # view cannot keep stale visibility/color overrides.
                    try:
                        target_palette = self._sync_palette_between_section_views(
                            source_idx, target_idx
                        )
                        self._debug_log_cross_section_palette_state(
                            context=f"pre-sync-apply v{target_idx + 1}<-v{source_idx + 1}",
                            target_class=getattr(self, "_last_classified_to_class", None),
                            gpu_slot=target_idx + 1,
                        )
                        if target_palette and hasattr(self.section_controller, '_auto_apply_view_palette'):
                            self.section_controller._auto_apply_view_palette(
                                target_idx, target_palette
                            )
                            # Keep both synced views unlocked after rebuild so
                            # classification-triggered sync cannot be blocked.
                            self._reset_section_palette_locks(
                                [source_idx, target_idx],
                                reason=f"post-sync-apply v{target_idx + 1}<-v{source_idx + 1}",
                            )
                        self._debug_log_cross_section_palette_state(
                            context=f"post-sync-apply v{target_idx + 1}<-v{source_idx + 1}",
                            target_class=getattr(self, "_last_classified_to_class", None),
                            gpu_slot=target_idx + 1,
                        )
                    except Exception as e:
                        print(f"   ⚠️ Palette apply failed: {e}")

                    # Mirror the SOURCE camera/zoom after the target view finishes
                    # rebuilding and applying its own palette.
                    try:
                        if source_cam:
                            self._apply_camera_immediate(vtk_widget, source_cam)
                            if hasattr(self, '_last_camera_states'):
                                self._last_camera_states[target_idx] = source_cam.copy()
                        if hasattr(self.section_controller, '_force_section_camera_2d'):
                            self.section_controller._force_section_camera_2d(
                                vtk_widget, target_idx
                            )
                    except Exception as e:
                        print(f"   ⚠️ Post-sync source camera apply failed: {e}")
                    
                    print(f"   ✅ Section synced to View {target_idx + 1}")
                    
                finally:
                    # Restore active view
                    if prev_active is not None:
                        self.section_controller.active_view = prev_active
                    if prev_vtk is not None:
                        self.section_controller.current_vtk = prev_vtk

            # Prime snapshots with the post-sync camera states so later sync
            # reacts only to real user pan/zoom, not this redraw.
            self._remember_section_camera_state(source_idx)
            for target_idx in views_to_sync:
                self._remember_section_camera_state(target_idx)
        
        except Exception as e:
            print(f"⚠️ Section sync error: {e}")
            import traceback
            traceback.print_exc()
        
        finally:
            self._sync_count = max(0, self._sync_count - 1)


    # Keep the safe version as an alias
    def _sync_section_from_safe(self, source_idx: int):
        """Alias for _sync_section_from for backward compatibility."""
        self._sync_section_from(source_idx)


    def set_view_sync(self, target_view_num: int, source_view_num):
        """
        Configure sync between views.
        ✅ BIDIRECTIONAL: Both views can drive sync
        ✅ REAL-TIME: Immediate synchronization
        ✅ SYNCS: Both camera AND section data
        """
        target_idx = target_view_num - 1

        self._init_camera_sync_state()

        if source_view_num is None:
            # Clear sync for this target
            if target_idx in self.view_sync_map:
                del self.view_sync_map[target_idx]
            
            # Remove observer only if no relationships remain
            has_any_sync = (
                target_idx in self.view_sync_map or 
                any(src == target_idx for src in self.view_sync_map.values())
            )
            if not has_any_sync:
                self._remove_camera_sync_observer(target_idx)
            
            print(f"🔕 Sync cleared: View {target_view_num}")
            return

        source_idx = source_view_num - 1

        if source_idx == target_idx:
            print(f"⚠️ Ignoring self-sync for View {target_view_num}")
            if target_idx in self.view_sync_map:
                del self.view_sync_map[target_idx]
            return

        # Set the sync relationship (ALLOW BIDIRECTIONAL)
        self.view_sync_map[target_idx] = source_idx
        print(f"🔗 Sync set: View {target_view_num} = Match View {source_view_num}")

        # Check if bidirectional
        if source_idx in self.view_sync_map and self.view_sync_map[source_idx] == target_idx:
            print(f"🔄 Bidirectional sync enabled: View {target_view_num} ↔ View {source_view_num}")

        # Initial sync copies section geometry, then each view keeps its own
        # palette but mirrors the source camera/zoom. Real-time camera sync
        # begins after this.
        self._sync_section_from(source_idx)
        self._sync_camera_immediate(source_idx)
        self._remember_section_camera_state(source_idx)
        self._remember_section_camera_state(target_idx)
        
        # Install observers on BOTH views for bidirectional sync
        if hasattr(self, 'section_vtks'):
            if source_idx in self.section_vtks:
                self._install_realtime_camera_observer(source_idx, self.section_vtks[source_idx])
            if target_idx in self.section_vtks:
                self._install_realtime_camera_observer(target_idx, self.section_vtks[target_idx])


    def _init_camera_sync_state(self):
        """Initialize camera sync state."""
        if not hasattr(self, 'view_sync_map'):
            self.view_sync_map = {}
        if not hasattr(self, '_camera_observers'):
            self._camera_observers = {}
        if not hasattr(self, '_last_camera_states'):
            self._last_camera_states = {}
        if not hasattr(self, '_sync_count'):
            self._sync_count = 0

    def _reset_camera_sync_observers(self):
        """
        Safely remove all installed camera sync observers.

        Use this helper instead of clearing `_camera_observers` directly so
        underlying VTK callbacks are detached before references are dropped.
        """
        self._init_camera_sync_state()
        for view_idx in list(self._camera_observers.keys()):
            self._remove_camera_sync_observer(view_idx)
        self._camera_observers.clear()


    def _install_realtime_camera_observer(self, view_idx: int, vtk_widget):
        """
        Install REAL-TIME camera observer.
        ✅ BIDIRECTIONAL: Works for both views
        ✅ ZERO DELAY: Immediate sync
        """
        try:
            self._init_camera_sync_state()
            
            if view_idx in self._camera_observers:
                return
            
            if not _validate_vtk_widget(vtk_widget):
                return
            
            self._last_camera_states[view_idx] = self._get_camera_state_fast(vtk_widget)
            vtk_widget._sync_view_idx = view_idx
            
            def realtime_sync(obj, event):
                """Real-time bidirectional sync — throttled to 30fps."""
                import time as _time
                if getattr(self, '_sync_count', 0) > 0:
                    return
                if getattr(self, '_syncing_camera', False):
                    return
                
                # ✅ THROTTLE: 30fps max (33ms between syncs)
                now = _time.time()
                
                # 🚀 OPTIMIZATION: Disable real-time sync during classification dragging
                # the user is brushing in a section view causes extreme lag.
                if getattr(self, 'is_dragging', False):
                    return
                    
                last_sync = getattr(self, '_last_camera_sync_time', 0)
                if (now - last_sync) < 0.033:
                    return

                is_source = any(src == view_idx for src in self.view_sync_map.values())
                has_target = view_idx in self.view_sync_map

                if not is_source and not has_target:
                    return

                current = self._get_camera_state_fast(vtk_widget)
                if current is None:
                    return

                last = self._last_camera_states.get(view_idx)
                if last and not self._camera_changed_fast(last, current):
                    return
                
                self._last_camera_states[view_idx] = current
                self._last_camera_sync_time = now
                self._sync_camera_bidirectional(view_idx, current)
            
            interactor = vtk_widget.interactor
            observer_id = interactor.AddObserver("MouseMoveEvent", realtime_sync)
            end_observer_id = vtk_widget.GetRenderWindow().AddObserver("EndEvent", realtime_sync)
            
            self._camera_observers[view_idx] = {
                'interactor': interactor,
                'observer_id': observer_id,
                'end_observer_id': end_observer_id,
                'render_window': vtk_widget.GetRenderWindow(),
                'vtk_widget': vtk_widget
            }
            
            print(f"✅ Real-time camera observer installed for View {view_idx + 1}")
            
        except Exception as e:
            print(f"⚠️ Failed to install observer: {e}")
            import traceback
            traceback.print_exc()

    def _sync_camera_bidirectional(self, source_idx: int, source_state: dict):
        """
        Sync camera bidirectionally.
        """
        self._sync_count = getattr(self, '_sync_count', 0) + 1
        
        try:
            if not hasattr(self, 'section_vtks'):
                return
            
            views_to_sync = self._get_synced_view_targets(source_idx)
            
            for target_idx in views_to_sync:
                if target_idx == source_idx:
                    continue
                if target_idx not in self.section_vtks:
                    continue
                
                target_vtk = self.section_vtks[target_idx]
                self._last_camera_states[target_idx] = source_state.copy()
                self._apply_camera_immediate(target_vtk, source_state)
        
        except Exception as e:
            print(f"⚠️ Bidirectional sync error: {e}")
        
        finally:
            self._sync_count = max(0, self._sync_count - 1)


    def _remove_camera_sync_observer(self, view_idx: int):
        """Remove camera observer."""
        if not hasattr(self, '_camera_observers') or view_idx not in self._camera_observers:
            return
        
        try:
            info = self._camera_observers[view_idx]
            
            if 'interactor' in info and 'observer_id' in info:
                try:
                    info['interactor'].RemoveObserver(info['observer_id'])
                except Exception:
                    pass
            
            if 'render_window' in info and 'end_observer_id' in info:
                try:
                    info['render_window'].RemoveObserver(info['end_observer_id'])
                except Exception:
                    pass
            
            del self._camera_observers[view_idx]
            print(f"🔓 Observer removed from View {view_idx + 1}")
            
        except Exception as e:
            print(f"⚠️ Observer removal error: {e}")


    def _sync_camera_immediate(self, source_idx: int):
        """Initial camera sync."""
        if getattr(self, '_sync_count', 0) > 0:
            return
        
        self._sync_count = getattr(self, '_sync_count', 0) + 1
        
        try:
            if not hasattr(self, 'section_vtks') or source_idx not in self.section_vtks:
                return
            
            source_vtk = self.section_vtks[source_idx]
            source_state = self._get_camera_state_fast(source_vtk)
            
            if source_state is None:
                return
            
            for target_idx, src in self.view_sync_map.items():
                if src != source_idx:
                    continue
                if target_idx not in self.section_vtks:
                    continue
                
                target_vtk = self.section_vtks[target_idx]
                self._last_camera_states[target_idx] = source_state.copy()
                self._apply_camera_immediate(target_vtk, source_state)
        
        finally:
            self._sync_count = max(0, self._sync_count - 1)


    def _apply_camera_immediate(self, vtk_widget, camera_state):
        """Apply camera state immediately."""
        try:
            renderer = vtk_widget.renderer
            if renderer is None:
                return
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return
            
            camera.SetPosition(camera_state["position"])
            camera.SetFocalPoint(camera_state["focal_point"])
            camera.SetViewUp(camera_state["view_up"])
            camera.SetParallelScale(camera_state["parallel_scale"])
            camera.SetClippingRange(camera_state["clipping_range"])
            
# ✅ FIX: Cross-section views must ALWAYS be parallel/2D
            is_section_view = False
            if hasattr(self, 'section_vtks'):
                for _idx, _vtk in self.section_vtks.items():
                    if _vtk is vtk_widget:
                        is_section_view = True
                        break
            
            if is_section_view:
                camera.ParallelProjectionOn()
                up = camera.GetViewUp()
                if abs(up[0]) > 0.01 or abs(up[1]) > 0.01 or abs(up[2] - 1.0) > 0.01:
                    camera.SetViewUp(0.0, 0.0, 1.0)
            else:
                if camera_state.get("parallel_projection", True):
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
            
            renderer.ResetCameraClippingRange()
            
            render_window = vtk_widget.GetRenderWindow()
            if render_window:
                render_window.Render()
            
        except Exception:
            pass


    def _get_camera_state_fast(self, vtk_widget):
        """Get camera state fast."""
        try:
            renderer = vtk_widget.renderer
            if renderer is None:
                return None
            
            camera = renderer.GetActiveCamera()
            if camera is None:
                return None
            
            return {
                "position": camera.GetPosition(),
                "focal_point": camera.GetFocalPoint(),
                "view_up": camera.GetViewUp(),
                "parallel_scale": camera.GetParallelScale(),
                "parallel_projection": camera.GetParallelProjection(),
                "clipping_range": camera.GetClippingRange(),
            }
        except Exception:
            return None


    def _camera_changed_fast(self, state1, state2, tolerance=0.0001):
        """Fast camera comparison."""
        if state1 is None or state2 is None:
            return True
        
        try:
            s1 = state1["parallel_scale"]
            s2 = state2["parallel_scale"]
            if abs(s1 - s2) > tolerance * max(s1, s2, 1.0):
                return True
            
            fp1 = state1["focal_point"]
            fp2 = state2["focal_point"]
            
            dx = fp1[0] - fp2[0]
            dy = fp1[1] - fp2[1]
            dz = fp1[2] - fp2[2]
            dist_sq = dx*dx + dy*dy + dz*dz
            
            threshold = tolerance * max(s1, s2, 1.0)
            if dist_sq > threshold * threshold:
                return True
            
            return False
        except Exception:
            return True

    def disable_all_camera_sync(self):
        """Disable all sync."""
        if hasattr(self, '_camera_observers'):
            for view_idx in list(self._camera_observers.keys()):
                self._remove_camera_sync_observer(view_idx)
            self._camera_observers.clear()
        
        self._sync_count = 0
        
        if hasattr(self, 'view_sync_map'):
            self.view_sync_map.clear()
        
        print("✅ All camera sync disabled")
                  
    def cleanup_closed_section_views(self):
        """
        AGGRESSIVE cleanup of ALL section view references when cross-section closes.
        ✅ FIXED: Now properly removes widget references to stop false "cross-section active" detection
        """
        print("\n" + "="*60)
        print("🧹 CLEANUP: Starting section view cleanup...")
        print("="*60)
        
        view_indices = set()
        
        # 1. From section_vtks (CRITICAL - these cause "Cross-section active" false positive)
        if hasattr(self, 'section_vtks') and self.section_vtks:
            view_indices.update(self.section_vtks.keys())
            print(f"   Found in section_vtks: {list(self.section_vtks.keys())}")
        
        # 2. From section_docks
        if hasattr(self, 'section_docks') and self.section_docks:
            view_indices.update(self.section_docks.keys())
            print(f"   Found in section_docks: {list(self.section_docks.keys())}")
        
        # 3. From classify_interactors
        if hasattr(self, 'classify_interactors') and self.classify_interactors:
            view_indices.update(self.classify_interactors.keys())
            print(f"   Found in classify_interactors: {list(self.classify_interactors.keys())}")
        
        # 4. Scan for stored section data attributes
        data_attrs = ['P1', 'P2', 'half_width', 'core_points', 'buffer_points', 
                    'core_mask', 'buffer_mask']
        
        for attr in data_attrs:
            for i in range(10):  # Check views 0-9
                key = f"section_{i}_{attr}"
                if hasattr(self, key):
                    view_indices.add(i)
                    print(f"   Found stored data: {key}")
                    break  # Move to next attr
        
        if not view_indices:
            print("   ✅ No section views found - already clean")
            print("="*60 + "\n")
            return
        
        print(f"   📋 Will clean views: {sorted(view_indices)}")
        
        # ✅ CRITICAL FIX: Clean each view COMPLETELY
        for view_idx in sorted(view_indices):
            try:
                print(f"\n   🔧 Cleaning section view {view_idx}...")
                
                # 1. ✅ CRITICAL: Remove from section_vtks FIRST (this stops "Cross-section active" detection)
                if hasattr(self, 'section_vtks') and view_idx in self.section_vtks:
                    try:
                        # Clear the VTK widget
                        vtk_widget = self.section_vtks[view_idx]
                        if hasattr(vtk_widget, 'clear'):
                            vtk_widget.clear()
                    except Exception:
                        pass
                    
                    del self.section_vtks[view_idx]
                    print(f"      ✅ Removed section_vtks[{view_idx}]")
                
                # 2. Remove from section_docks
                if hasattr(self, 'section_docks') and view_idx in self.section_docks:
                    try:
                        dock = self.section_docks[view_idx]
                        dock.hide()  # Hide first
                    except Exception:
                        pass
                    
                    del self.section_docks[view_idx]
                    print(f"      ✅ Removed section_docks[{view_idx}]")
                
                # 3. Remove from classify_interactors
                if hasattr(self, 'classify_interactors') and view_idx in self.classify_interactors:
                    del self.classify_interactors[view_idx]
                    print(f"      ✅ Removed classify_interactors[{view_idx}]")
                
                # 4. Delete ALL stored section data
                for attr in data_attrs:
                    key = f"section_{view_idx}_{attr}"
                    if hasattr(self, key):
                        delattr(self, key)
                        print(f"      ✅ Deleted {key}")
                
                print(f"   ✅ Cross-section view {view_idx} cleanup complete")
                
            except Exception as e:
                print(f"   ⚠️ Cleanup error for view {view_idx}: {e}")
                import traceback
                traceback.print_exc()
        
        # 5. ✅ CRITICAL: Verify section_vtks is now empty
        if hasattr(self, 'section_vtks'):
            if self.section_vtks:
                print(f"\n   ⚠️ WARNING: section_vtks not empty: {list(self.section_vtks.keys())}")
                # Force clear
                self.section_vtks.clear()
                print(f"   🔨 FORCED CLEAR: section_vtks")
            else:
                print(f"\n   ✅ VERIFIED: section_vtks is empty")
        
        # 6. Reset classify_interactor if all cross-sections closed
        if not getattr(self, 'section_vtks', {}):
            if hasattr(self, 'classify_interactor'):
                self.classify_interactor = None
                print("   🔄 All cross-sections closed - reset classify_interactor")
        
        print("\n" + "="*60)
        print("✅ CLEANUP COMPLETE")
        print("="*60 + "\n")

    def deactivate_cross_section_tool(self):
        """Deactivate cross-section tool only (do not close cross-section windows)."""
        try:
            self._cancel_cross_section_tool_only()
            self.set_cross_cursor_active(False, "cross_section")  
            print("🛑 Cross-section tool deactivated")
        except Exception as e:
            print(f"⚠️ deactivate_cross_section_tool failed: {e}")

    # --------------------------------------------------------------
    def enable_section_point_picking(self):
        """Enable clicking to select/highlight points in the cut section view."""
        iren = self.app.sec_vtk.interactor
        old_id = getattr(self, "_section_pick_click_observer_id", None)
        if old_id is not None:
            try:
                iren.RemoveObserver(old_id)
            except Exception:
                pass
        self._section_pick_click_observer_id = iren.AddObserver("LeftButtonPressEvent", self._on_section_point_click)

    # --------------------------------------------------------------
    def _on_section_point_click(self, obj, evt):
        """Handle left-click to pick a point in the section/profile view."""
        x, y = self.app.sec_vtk.interactor.GetEventPosition()
        picker = vtk.vtkPointPicker()
        picker.Pick(x, y, 0, self.app.sec_vtk.renderer)
        pid = picker.GetPointId()
        if pid >= 0 and pid < len(self.app.section_points):
            picked_point = self.app.section_points[pid]
            print(f"✅ Selected section point: {picked_point}")
            self._highlight_section_point(pid)

    # --------------------------------------------------------------
    def _highlight_section_point(self, pid):
        """Visually highlight the selected point in the section view."""
        pt = self.app.section_points[pid]
        # Reuse a single highlight actor to avoid unbounded actor growth on repeated picks.
        old_actor = getattr(self, "_section_pick_highlight_actor", None)
        if old_actor is not None:
            try:
                self.app.sec_vtk.remove_actor(old_actor, reset_camera=False)
            except Exception:
                pass
            self._section_pick_highlight_actor = None

        cloud = pv.PolyData([pt])
        try:
            self._section_pick_highlight_actor = self.app.sec_vtk.add_points(
                cloud,
                color="red",
                point_size=12,
                render_points_as_spheres=True,
                reset_camera=False,
            )
        except TypeError:
            # Older PyVista versions may not accept reset_camera on add_points.
            self._section_pick_highlight_actor = self.app.sec_vtk.add_points(
                cloud,
                color="red",
                point_size=12,
                render_points_as_spheres=True,
            )
        self.app.sec_vtk.render()

    def enable_cut_section_mode(self):
        """
        Enable cut section mode.
        Cut section draws on the CROSS-SECTION window, NOT the main view.
        """
        # Check if we're in top view (for main viewer)
        if getattr(self, "current_view", None) != "top":
            QMessageBox.warning(self, "Cut Section", "Main view must be in Top View")
            return

        # ✅ MUTUAL EXCLUSION: Point-pick tools (Identify / Point Sync / SNT
        # pick) collide with Cut Section on the same views, so disable them
        # automatically when Cut Section is activated (it just yields).
        self._deactivate_point_pick_tools()

        # ✅ Do NOT touch the main interactor - cut section works on cross-section window!
        # Just activate the cut section controller
        self.cut_section_controller.activate()
        self.cut_section_mode_on = True
        self.set_cross_cursor_active(True, "cut_section")
        print("✅ Cut Section Mode enabled on cross-section window.")

    def toggle_cut_section_mode(self, checked):
        """Toggle cut section mode (for UI Actions)."""
        if checked:
            self.enable_cut_section_mode()
        else:
            if hasattr(self, 'cut_section_controller'):
                self.cut_section_controller.cancel_cut_section()
            self.cut_section_mode_on = False
            self.app.set_cross_cursor_active(False)
            self.set_cross_cursor_active(False, "cut_section") 
            print("🛑 Cut Section Mode disabled.")

    # ------------------ Hook into classification tools ------------------
    def on_classification_tool_start(self):
        """When classification tool activated."""
        if getattr(self, 'active_classify_tool', None) == "cut_section":
            if hasattr(self, 'cut_section_controller'):
                self.cut_section_controller.lock_for_classification()
        elif getattr(self, 'active_classify_tool', None) == "cross_section":
            if hasattr(self, 'section_controller'):
                self.section_controller.lock_for_classification()

    def on_classification_tool_end(self):
        """When classification tool finished or canceled."""
        if getattr(self, 'active_classify_tool', None) == "cut_section":
            if hasattr(self, 'cut_section_controller'):
                self.cut_section_controller.unlock_after_classification()
        elif getattr(self, 'active_classify_tool', None) == "cross_section":
            if hasattr(self, 'section_controller'):
                self.section_controller.unlock_after_classification()


    def toggle_view_mode(self, mode: str, preserve_camera: bool = False):
        """
        Switch between 2D plan view (locked top view) and full 3D orbit view.
        ✅ SIMPLE FIX: Backs up and restores section view actors

        preserve_camera:
            When True for 2D mode, keep the current camera orientation/position
            and only re-apply orthographic + interactor locking. This avoids
            a temporary top-view flash before a follow-up explicit view change.
        """
        from vtkmodules.vtkInteractionStyle import (
            vtkInteractorStyleImage,
            vtkInteractorStyleTrackballCamera,
        )
       
        # ✅ STEP 1: Backup all section view actors BEFORE any changes
        section_actors_backup = {}
        if hasattr(self, 'section_vtks') and self.section_vtks:
            for view_idx, vtk_widget in self.section_vtks.items():
                try:
                    if hasattr(vtk_widget, 'renderer') and vtk_widget.renderer:
                        actors = vtk_widget.renderer.GetActors()
                        actors.InitTraversal()
                        actor_list = []
                        for i in range(actors.GetNumberOfItems()):
                            actor = actors.GetNextActor()
                            if actor:
                                actor_list.append(actor)
                        section_actors_backup[view_idx] = actor_list
                        print(f"💾 Backed up {len(actor_list)} actors from View {view_idx + 1}")
                except Exception as e:
                    print(f"⚠️ Backup failed for View {view_idx + 1}: {e}")
 
        # ✅ STEP 2: Do the view mode switch
        if mode == "3d":
            self.is_3d_mode = True
            self._main_view_2d_locked = False
            print("🌀 Switching to 3D view (tools disabled)")
 
            self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
            cam = self.vtk_widget.renderer.GetActiveCamera()
            cam.ParallelProjectionOff()
            self.vtk_widget.render()
 
            if hasattr(self, "digitizer"):
                self.digitizer.disable_all_tools()
           
            self.active_classify_tool = None
            self.statusBar().showMessage("3D Mode: tools disabled", 4000)
 
        elif mode == "2d":
            self.is_3d_mode = False
            self._main_view_2d_locked = True
            view_label = "2D orthographic view" if preserve_camera else "2D Plan View"
            print(f"📐 Switching to {view_label} (tools enabled)")
 
            cam = self.vtk_widget.renderer.GetActiveCamera()
            cam.ParallelProjectionOn()
            if preserve_camera:
                if self.vtk_widget.renderer.VisibleActorCount() > 0:
                    self.vtk_widget.renderer.ResetCameraClippingRange()
                else:
                    pos = np.array(cam.GetPosition())
                    fp = np.array(cam.GetFocalPoint())
                    dist = max(1.0, np.linalg.norm(pos - fp))
                    cam.SetClippingRange(dist * 0.001, dist * 100.0)
            else:
                cam.SetFocalPoint(0, 0, 0)
                cam.SetPosition(0, 0, 1)
                cam.SetViewUp(0, 1, 0)
                self.vtk_widget.renderer.ResetCamera()
 
            self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleImage())
            self.vtk_widget.render()

            if hasattr(self, "digitizer") and self.digitizer:
                if not getattr(self, "active_classify_tool", None):
                    self.digitizer.enabled = True
                self.digitizer._check_and_update_renderers()

            self.statusBar().showMessage(
                "2D orthographic view locked (no 3D rotation)"
                if preserve_camera
                else "2D Plan View locked (no 3D rotation)",
                4000,
            )
        else:
            print(f"⚠️ Unknown view mode: {mode}")
            return
 
        # ✅ STEP 3: Restore section view actors if they were lost
        from PySide6.QtCore import QTimer
       
        def restore_actors():
            if not section_actors_backup:
                return
               
            for view_idx, actor_list in section_actors_backup.items():
                if view_idx not in self.section_vtks:
                    continue
                   
                vtk_widget = self.section_vtks[view_idx]
                if not vtk_widget or not hasattr(vtk_widget, 'renderer'):
                    continue
               
                try:
                    renderer = vtk_widget.renderer
                   
                    # Check current actor count
                    current_actors = renderer.GetActors()
                    current_count = current_actors.GetNumberOfItems() if current_actors else 0
                   
                    if current_count == 0 and len(actor_list) > 0:
                        print(f"🔄 Restoring {len(actor_list)} actors to View {view_idx + 1}")
                        for actor in actor_list:
                            try:
                                renderer.AddActor(actor)
                            except Exception:
                                pass
                        vtk_widget.render()
                        print(f"✅ View {view_idx + 1} restored")
                    else:
                        print(f"✅ View {view_idx + 1} OK ({current_count} actors)")
                       
                except Exception as e:
                    print(f"⚠️ Restore failed for View {view_idx + 1}: {e}")
       
        # Restore after a short delay to let any pending operations complete
        QTimer.singleShot(100, restore_actors)
        QTimer.singleShot(300, restore_actors)  # Double-check
        
    def open_shortcut_manager(self):
        self._shortcut_manager = ShortcutManager.open_manager(self)

        # Generic guard: True while any input popup (block creation, custom
        # backup, PRJ/Display sub-dialogs, etc.) is open. While set, runtime
        # tool shortcuts are suppressed so typed letters become text, not tools.
        # Dialogs set this via gui.popup_guard.set_input_popup_open().
        if not hasattr(self, "_input_popup_open"):
            self._input_popup_open = False
        try:
            self._shortcut_manager.applied.disconnect(self.update_shortcuts)
        except Exception:
            pass
        self._shortcut_manager.applied.connect(self.update_shortcuts)
        return self._shortcut_manager

    def update_shortcuts(self, shortcuts):
        if shortcuts:
            self.shortcuts = shortcuts
            print("✅ Shortcuts updated:", self.shortcuts)

    # def keyPressEvent(self, event):
    #     # Block Alt+F4 at the key press level
    #     if event.key() == Qt.Key_F4 and (event.modifiers() & Qt.AltModifier):
    #         event.accept()
    #         return
    #     # Let GlobalShortcutFilter handle all global shortcuts
    #     super().keyPressEvent(event)

    def keyPressEvent(self, event):
        # Block Alt+F4 only when it is not mapped to a user shortcut.
        if event.key() == Qt.Key_F4 and (event.modifiers() & Qt.AltModifier):
            shortcuts = getattr(self, "shortcuts", {})
            if ("alt", "F4") not in shortcuts:
                event.accept()
                return
        # Let GlobalShortcutFilter handle all global shortcuts
        super().keyPressEvent(event)

    # ═══════════════════════════════════════════════════════════════════
    # FILE LOADING  (main entry + worker slots)
    # ═══════════════════════════════════════════════════════════════════

    def open_file(self):
        """
        Load LiDAR file(s).

        Main-thread responsibilities:
          • auto-save, file dialog, memory check, import-option prompt
          • clear current project (VTK actors, state)
          • start background FileLoaderWorker

        Worker-thread responsibilities (FileLoaderWorker):
          • Phase 1 – read each file with load_lidar_file()
          • Phase 2 – pre-allocate merged numpy arrays
          • Phase 3 – fill / merge arrays (zero-copy)

        Back on main thread (_on_load_finished):
          • Phase 4+ – DEM, spatial index, palette, VTK render, title …
        """
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtWidgets import QMessageBox, QFileDialog
        from gui.progress_dialog import LoadingProgressDialog
        import os

        print(f"\n{'='*70}")
        print(f"📂 OPEN FILE - THREADED MODE")
        print(f"{'='*70}")

        # ── STEP 1: Auto-save current file ────────────────────────────
        if hasattr(self, "data") and self.data is not None:
            save_path = (
                getattr(self, "last_save_path", None)
                or getattr(self, "loaded_file", None)
            )
            # Never overwrite the source file when the current load is class-filtered
            # (partial data). Writing filtered points back would corrupt the original.
            _class_filtered = getattr(self, '_loaded_with_class_filter', False)
            if save_path and not _class_filtered:
                try:
                    print(f"\n💾 AUTO-SAVING CURRENT FILE")
                    print(f"   Path: {os.path.basename(save_path)}")
                    print(f"   Points: {len(self.data.get('xyz', [])):,}")

                    from gui.save_pointcloud import save_pointcloud_quick
                    result = save_pointcloud_quick(self, save_path)

                    if result:
                        print("✅ Saved successfully")
                        if hasattr(self, "statusBar"):
                            self.statusBar().showMessage(
                                f"💾 Saved: {os.path.basename(save_path)}", 2000
                            )
                            QCoreApplication.processEvents()
                    else:
                        print("⚠️ Save returned False")

                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    reply = QMessageBox.warning(
                        self, "Save Failed",
                        f"Failed to auto-save:\n\n{e}\n\nContinue without saving?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No,
                    )
                    if reply == QMessageBox.No:
                        return

        # ── STEP 2: File picker ────────────────────────────────────────
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "Select File(s)",
            "",
            "LiDAR Files (*.las *.laz);;All Files (*.las *.laz *.ply *.ptc *.prj)",
        )
        if not filenames:
            print("   User cancelled")
            return

        print(f"\n✅ USER SELECTED {len(filenames)} FILE(S)")
        for i, f in enumerate(filenames):
            print(f"   {i+1}. {os.path.basename(f)}")

        # ── STEP 2.5: Memory safety check ─────────────────────────────
        try:
            import psutil
            total_mb     = sum(
                os.path.getsize(f) for f in filenames if os.path.exists(f)
            ) / (1024 * 1024)
            estimated_mb = total_mb * 4
            available_mb = psutil.virtual_memory().available / (1024 * 1024)

            print(f"\n🧮 MEMORY CHECK:")
            print(f"   File size: {total_mb:.0f} MB")
            print(f"   Estimated memory needed: {estimated_mb:.0f} MB")
            print(f"   Available memory: {available_mb:.0f} MB")

            if estimated_mb > available_mb * 0.7:
                reply = QMessageBox.warning(
                    self, "⚠️ Memory Warning",
                    f"Loading these files may require ~{estimated_mb:.0f} MB.\n"
                    f"You have {available_mb:.0f} MB available.\n\n"
                    f"This may cause slowdowns or crashes.\n\n"
                    f"Recommendations:\n"
                    f"• Load files in smaller batches\n"
                    f"• Close other applications\n"
                    f"• Upgrade RAM for large datasets\n\n"
                    f"Continue anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply == QMessageBox.No:
                    print("   ⚠️ User cancelled due to memory warning")
                    return
                print("   ⚠️ User chose to continue despite warning")
            else:
                print("   ✅ Sufficient memory available")

        except ImportError:
            print("\n⚠️ psutil not installed — skipping memory check")
        except Exception as e:
            print(f"\n⚠️ Memory check failed: {e}")

        # ── STEP 2.75: Import options (prompt BEFORE clearing) ─────────
        batch_import_options = self._prompt_lidar_import_options_for_files(filenames)
        if batch_import_options is None:
            print("   User cancelled import setup")
            return

        # ── STEP 3: Handle special single-file types early ────────────
        #   (PTC / PRJ don't need the threaded loader)
        if len(filenames) == 1:
            fname = filenames[0]
            if fname.lower().endswith(".ptc"):
                print("📋 Loading PTC palette…")
                self._load_single_ptc(fname)
                return
            if fname.lower().endswith(".prj"):
                print("📋 Loading TerraScan PRJ…")
                self._load_terrascan_prj(fname)
                return

        # ── STEP 4: Clear current project ─────────────────────────────
        print(f"\n{'='*60}")
        print("🧹 CLEARING CURRENT PROJECT")
        print(f"{'='*60}")

        # Persist current file-specific display/PTC state before data clear.
        try:
            from gui.clear_project import _save_display_settings_before_clear
            _save_display_settings_before_clear(self)
            print("[RUNTIME-CHECK] pre-clear-save caller=open_file step=main-load")
        except Exception as e:
            print(f"⚠️ Display settings pre-save skipped: {e}")

        from gui.shading_display import clear_shading_cache
        clear_shading_cache(reason="new file")

        # Backup overlay actors (DXF + SNT) so they survive the clear
        renderer    = self.vtk_widget.renderer
        dxf_backup  = []
        snt_backup  = []

        if hasattr(self, "dxf_actors") and self.dxf_actors:
            for dxf_data in self.dxf_actors:
                for actor in dxf_data.get("actors", []):
                    dxf_backup.append(actor)
            for actor in dxf_backup:
                renderer.RemoveActor(actor)
            if dxf_backup:
                print(f"   💾 Backed up {len(dxf_backup)} DXF actors")

        if hasattr(self, "snt_actors") and self.snt_actors:
            for snt_data in self.snt_actors:
                for actor in snt_data.get("actors", []):
                    snt_backup.append(actor)
                    try:
                        renderer.RemoveActor(actor)
                    except Exception:
                        pass
            if snt_backup:
                print(f"   💾 Backed up {len(snt_backup)} SNT actors")

        # Clear VTK main view
        renderer.RemoveAllViewProps()
        for attr in ("actors", "_actors"):
            d = getattr(self.vtk_widget, attr, None)
            if isinstance(d, dict):
                d.clear()
        self.vtk_widget.render()
        print("   ✅ VTK cleared")

        # Clear cross-section views
        for vw in (getattr(self, "section_vtks", None) or {}).values():
            try:
                vw.renderer.RemoveAllViewProps()
                if hasattr(vw, "actors"):
                    vw.actors.clear()
                vw.render()
            except Exception:
                pass
        print("   ✅ Cross-sections cleared")

        # Clear cut section state/view to prevent stale cut index map on next file load.
        if hasattr(self, "cut_section_controller") and self.cut_section_controller:
            try:
                self.cut_section_controller.clear()
                print("   ✅ Cut section cleared")
            except Exception as e:
                print(f"   ⚠️ Cut section clear failed: {e}")
                try:
                    ctrl = self.cut_section_controller
                    ctrl.cut_points = None
                    ctrl._cut_index_map = None
                    ctrl.is_cut_view_active = False
                    print("   ✅ Applied fallback cut-state reset")
                except Exception:
                    pass

        # Memory-manager hook
        try:
            from gui.memory_manager import ObserverRegistry, release_data_arrays
            release_data_arrays(self)
            ObserverRegistry.release_all()
            mg = getattr(self, "_mem_guard", None)
            if mg is not None:
                mg.force_gc()
        except Exception as e:
            print(f"⚠️ Memory manager clear hook skipped: {e}")

        # Reset internal state
        self.data                       = None
        self.loaded_file                = None
        self.last_save_path             = None
        self.class_palette              = {}
        self._loaded_with_class_filter  = False
        # ✅ FIX: Clear stale Z-bounds cache so SNT actors are positioned correctly
        # relative to the next LAZ file. _get_snt_z_offset reads this cache first;
        # if it holds the previous file's z_max the SNT grid appears at the wrong height.
        self.data_bounds    = None

        for attr in ("view_palettes", "layers"):
            obj = getattr(self, attr, None)
            if isinstance(obj, (dict, list)):
                obj.clear()

        # Clear all per-view cross-section caches bound to previous dataset.
        # These masks/indices become invalid as soon as a new point cloud is loaded.
        try:
            import re
            stale_section_attrs = [
                name for name in list(vars(self).keys())
                if re.match(r"^section_\d+_", name) or re.match(r"^_section_\d+_", name)
            ]
            for name in stale_section_attrs:
                try:
                    delattr(self, name)
                except Exception:
                    pass

            for name in (
                "section_core_points",
                "section_buffer_points",
                "section_core_mask",
                "section_core_indices",
                "section_indices",
                "section_points",
                "_last_drawn_view_idx",
            ):
                if hasattr(self, name):
                    try:
                        delattr(self, name)
                    except Exception:
                        pass

            print(f"   ✅ Cleared {len(stale_section_attrs)} stale section cache attrs")
        except Exception as e:
            print(f"   ⚠️ Section cache clear skipped: {e}")

        for stack in (
            getattr(self, "undo_stack", None),
            getattr(self, "redo_stack", None),
        ):
            if stack is not None:
                stack.clear()

        if hasattr(self, "spatial_index"):
            self.spatial_index = None

        print("   ✅ All data cleared")
        QCoreApplication.processEvents()

        # Restore overlay actors immediately
        for actor in dxf_backup:
            renderer.AddActor(actor)
        # ✅ FIX: Reset _snt_z_offset on preserved SNT actors so the delta math in
        # _apply_z_offset_to_actor starts from zero for the incoming LAZ file.
        for actor in snt_backup:
            actor._snt_z_offset = 0.0
            renderer.AddActor(actor)
        if dxf_backup or snt_backup:
            self.vtk_widget.render()
            QCoreApplication.processEvents()
            print(f"   ✅ Restored {len(dxf_backup)} DXF + {len(snt_backup)} SNT actors")

        # ✅ FIX: Reinstall digitizer observers + ensure renderers after inline clear.
        # open_file() does its own clear instead of calling clear_project.py,
        # so ObserverRegistry.release_all() wipes all VTK interactor observers
        # but nothing reinstalls them. Without observers, digitize tools cannot
        # detect mouse events and drawings never appear.
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer._reinstall_all_observers()
                self.digitizer._check_and_update_renderers()
                print("   ✅ Digitizer observers + renderers restored (post-inline-clear)")
            except Exception as _dig_err:
                print(f"   ⚠️ Digitizer observer restore failed: {_dig_err}")

        print(f"{'='*60}")
        print("✅ CLEAR COMPLETE")
        print(f"{'='*60}\n")

        # ── STEP 5: Show progress dialog ───────────────────────────────
        label = (
            f"{len(filenames)} file(s)"
            if len(filenames) > 1
            else os.path.basename(filenames[0])
        )
        self._load_progress = LoadingProgressDialog(self, show_cancel=False)
        self._load_progress.set_filename(label)
        self.setWindowTitle("Loading LiDAR File…")   # show status in main window title bar
        self._load_progress.show()
        QCoreApplication.processEvents()

        # ── STEP 6: Start background worker ───────────────────────────
        #   Main thread is now FREE — the event loop keeps running.
        from gui.file_loader_worker import FileLoaderWorker

        worker = FileLoaderWorker(filenames, batch_import_options, parent=None)
        self._file_loader_worker = worker
        self._batch_import_options_last = batch_import_options  # read by _on_load_finished

        worker.progress.connect(self._on_load_progress)
        worker.points_counted.connect(self._on_load_points_counted)
        worker.finished.connect(self._on_load_finished)
        worker.error.connect(self._on_load_error)
        worker.cancelled.connect(self._on_load_cancelled)

        worker.start()

    # ── Worker → Main-thread slots ─────────────────────────────────────

    def _on_load_progress(self, percent: int, status: str):
        """Forward worker progress to the progress dialog (main thread)."""
        dlg = getattr(self, "_load_progress", None)
        if dlg:
            dlg.set_progress(percent)
            dlg.set_status(status)

    def _on_load_points_counted(self, total: int):
        """Update point-count label in progress dialog."""
        dlg = getattr(self, "_load_progress", None)
        if dlg and hasattr(dlg, "set_points_count"):
            dlg.set_points_count(total)

    def _on_load_error(self, message: str):
        """Handle worker error — always on main thread."""
        from PySide6.QtWidgets import QMessageBox
        dlg = getattr(self, "_load_progress", None)
        if dlg:
            try:
                dlg.finish_error(message)
            except Exception:
                pass
        QMessageBox.critical(self, "Load Error", message)
        self._file_loader_worker = None
        self._batch_import_options_last = None

    def _on_load_cancelled(self):
        """Worker was cancelled."""
        dlg = getattr(self, "_load_progress", None)
        if dlg:
            try:
                dlg.finish_error("Cancelled.")
            except Exception:
                pass
        self._file_loader_worker = None
        self._batch_import_options_last = None

    def _on_load_finished(self, result: dict):
        """
        Called on the MAIN THREAD after the worker finishes Phases 1-3.
        All VTK / Qt rendering lives here — identical logic to the original
        Phase 4+ block, preserving every detail.
        """
        import os
        import time
        from PySide6.QtCore import QCoreApplication
        from gui.point_count_widget import refresh_point_statistics

        t0  = time.time()
        dlg = getattr(self, "_load_progress", None)

        def _prog(pct, msg, force=True):
            if dlg:
                dlg.set_progress(pct)
                dlg.set_status(msg)
            QCoreApplication.processEvents()

        print(f"\n{'='*60}")
        print("📥 Phase 4+: Main-thread finalisation…")
        print(f"{'='*60}")

        # ── Phase 4: Store merged data (references — zero copy) ────────
        _prog(87, "Finalizing data…")

        self.data = {"xyz": result["xyz"], "classification": result["classification"]}
        if "rgb"       in result: self.data["rgb"]       = result["rgb"]
        if "intensity" in result: self.data["intensity"] = result["intensity"]
        self.data_bounds = None  # invalidate stale SNT z-offset cache for new dataset

        # Track whether this load was class-filtered so auto-save skips the file.
        _b = getattr(self, "_batch_import_options_last", None)
        self._loaded_with_class_filter = bool(
            isinstance(_b, dict) and _b.get("only_class") and _b.get("class_codes")
        )

        total_points = result["total_points"]
        first_file   = result["first_file"]
        filenames    = [fi["filename"] for fi in result["layer_info_list"]]
        num_files    = result["num_files"]

        import numpy as np
        print(f"\n✅ Final dataset ready: {total_points:,} points")
        xyz_mb = result["xyz"].nbytes / (1024**2)
        cls_mb = result["classification"].nbytes / (1024**2)
        rgb_mb = result.get("rgb",       np.array([])).nbytes / (1024**2)
        print(f"   Memory used: {xyz_mb + cls_mb + rgb_mb:.1f} MB")

        # ── CRS ────────────────────────────────────────────────────────
        if result.get("crs_epsg"):
            self.project_crs_epsg = result["crs_epsg"]
            self.project_crs_wkt  = result.get("crs_wkt")
            try:
                from pyproj import CRS
                self.crs = CRS.from_epsg(self.project_crs_epsg)
                print(f"   📐 CRS: {self.crs.name}")
            except Exception:
                pass

        # ── Layers panel ───────────────────────────────────────────────
        for fi in result["layer_info_list"]:
            layer = {
                "type"      : "laz_tile",
                "filename"  : fi["filename"],
                "xyz"       : self.data["xyz"],         # shared reference
                "classification": self.data["classification"],
                "rgb"       : self.data.get("rgb"),
                "intensity" : self.data.get("intensity"),
                "crs_epsg"  : fi.get("crs_epsg"),
                "visible"   : True,
            }
            if hasattr(self, "layers"):
                self.layers.append(layer)
            if hasattr(self, "layers_dock") and self.layers_dock:
                self.layers_dock.add_layer(layer)

        # ── DEM for shading ────────────────────────────────────────────
        _prog(88, "Building DEM…")
        try:
            from gui.shading_display import build_base_dem_mesh
            build_base_dem_mesh(self, percentile_filter=99.9, downsample=2)
        except Exception:
            pass

        # ── File paths ─────────────────────────────────────────────────
        self.loaded_file    = first_file
        self.last_save_path = first_file
        input_format_version = result.get("input_format_version")
        if isinstance(input_format_version, tuple) and len(input_format_version) >= 2:
            self.last_save_version = f"{int(input_format_version[0])}.{int(input_format_version[1])}"
        else:
            self.last_save_version = None
        if isinstance(self.data, dict):
            self.data["input_format_version"] = input_format_version
            self.data["las_version"] = self.last_save_version

        # ── Spatial index ──────────────────────────────────────────────
        if total_points > 50_000:
            _prog(90, "Building spatial index…")
            try:
                from gui.performance_optimizations import SpatialIndex
                self.spatial_index = SpatialIndex(self.data["xyz"])
                print("   ✅ Spatial index built")
            except Exception as e:
                print(f"   ⚠️ Spatial index failed: {e}")
                self.spatial_index = None

        # ── Restore display settings ───────────────────────────────────
        _prog(92, "Restoring settings…")
        try:
            from gui.display_mode import restore_display_settings_for_file
            restore_display_settings_for_file(self, first_file)
        except Exception:
            pass

        # A new dataset always starts with Structured borders in every view.
        # This intentionally runs after settings restore so an old Per-Point
        # preference cannot leak into the newly loaded file.
        from gui.unified_actor_manager import reset_border_logic_to_structured
        reset_border_logic_to_structured(self)

        self.display_mode = "class"
        if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
            if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                try:
                    self.display_mode_dialog.sync_with_app_state()
                except Exception:
                    pass

        # ── Palette ────────────────────────────────────────────────────
        _prog(94, "Loading palette…")
        palette_to_apply = self._get_palette_for_file(first_file)

        if palette_to_apply:
            _batch_opts = getattr(self, "_batch_import_options_last", None)

            # If user selected specific classes on load, hide all others in the palette.
            if isinstance(_batch_opts, dict) and _batch_opts.get("only_class"):
                from gui.display_mode import clone_palette
                _sel = set(int(c) for c in (_batch_opts.get("class_codes") or []))
                palette_to_apply = clone_palette(palette_to_apply)
                for _code, _entry in palette_to_apply.items():
                    _entry["show"] = (_code in _sel)
                print(f"   👁 Initial visibility: showing classes {sorted(_sel)}")

            visible_count = len([c for c, v in palette_to_apply.items() if v.get("show")])
            _prog(96, f"Rendering {visible_count} classes…")
            print(f"🎨 Applying palette with {visible_count} visible classes…")
            self.apply_class_map({
                "classes"     : palette_to_apply,
                "slot"        : 0,
                "color_mode"  : 0,
                "target_view" : 0,
            })

        # ── Drawings ───────────────────────────────────────────────────
        try:
            from gui.save_pointcloud import finalize_drawing_render
            finalize_drawing_render(self)
            print("✅ Drawings finalized after point cloud render")
        except Exception:
            pass

        # ── Finalise interactor + view ─────────────────────────────────
        _prog(98, "Finalizing…")
        try:
            from gui.pointcloud_display import force_interactor_ready
            force_interactor_ready(self, delay_ms=300)
        except Exception:
            pass

        self.toggle_view_mode("2d")

        # ── Window title ───────────────────────────────────────────────
        if num_files == 1:
            self._update_window_title(first_file, self.project_crs_epsg)
        else:
            self._update_window_title(
                f"{num_files} files ({total_points:,} pts)",
                self.project_crs_epsg,
            )

        # ── Auto-load drawings ─────────────────────────────────────────
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer.auto_load_drawings(first_file)
            except Exception:
                pass

        # ── Statistics ─────────────────────────────────────────────────
        if hasattr(self, "point_count_widget") and self.point_count_widget:
            try:
                refresh_point_statistics(self)
            except Exception:
                pass

        # ── Re-apply loaded PRJ dialog state (if present) ─────────────
        # Keep block-identifier context sticky across point-cloud reloads.
        if hasattr(self, "block_identifier_dialog") and self.block_identifier_dialog:
            try:
                prj_dlg = self.block_identifier_dialog
                prj_path = getattr(prj_dlg, "current_prj_path", None)
                prj_data = getattr(prj_dlg, "prj_data", None)
                if (not prj_data) and prj_path and os.path.exists(prj_path):
                    if hasattr(prj_dlg, "parse_prj_file"):
                        print(f"🔁 Restoring PRJ dialog data: {os.path.basename(prj_path)}")
                        prj_dlg.parse_prj_file(prj_path)
                if hasattr(prj_dlg, "reapply_hide_state"):
                    prj_dlg.reapply_hide_state()
            except Exception as _prj_restore_err:
                print(f"⚠️ PRJ dialog state restore skipped: {_prj_restore_err}")

        # ✅ FIX: Final digitizer health check after all load operations.
        # Must run AFTER toggle_view_mode, auto_load_drawings, and the
        # force_interactor_ready callback. Mirrors clear_project.py's
        # final restoration: _check_and_update_renderers() first (ensures
        # overlay renderers are in the window), then _reinstall_all_observers()
        # (ensures VTK interactor observers survive any intermediate wipe).
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer._check_and_update_renderers()
                self.digitizer._reinstall_all_observers()
                print("✅ Digitizer fully restored (post-load)")
            except Exception as _dig_err:
                print(f"⚠️ Digitizer restore failed: {_dig_err}")

        # ✅ FIX: Call fit_view() to properly position camera at data bounds.
        # toggle_view_mode("2d") resets camera to origin (0,0,0) then calls
        # ResetCamera(), but this doesn't match what fit_view() does — fit_view()
        # manually computes bounds from point cloud + DXF + SNT data and sets
        # the camera position, focal point, and parallel scale correctly.
        # Without this, digitize tool actors (3D vtkActor) are invisible because
        # the camera clipping range doesn't cover the real-world data coordinates
        # (~256000, 4779000). This is exactly what Shift+F does to fix the issue.
        try:
            self.fit_view()
            print("✅ Camera fitted to data bounds (post-load)")
        except Exception as _fit_err:
            print(f"⚠️ Post-load fit_view failed: {_fit_err}")

        # ── Done ───────────────────────────────────────────────────────
        total_time = time.time() - t0

        print(f"\n{'='*60}")
        print("✅ LOAD COMPLETE - THREADED MODE")

        print(f"   Files:       {num_files}")
        print(f"   Points:      {total_points:,}")
        print(f"   Time (main): {total_time:.1f}s")
        if total_time > 0:
            print(f"   Performance: {total_points / total_time:,.0f} pts/sec")
        print(f"{'='*60}\n")

        if dlg:
            try:
                dlg.finish_success(
                    f"Loaded {total_points:,} points in {total_time:.1f}s"
                )
            except Exception:
                pass

        self._file_loader_worker = None

    def attach_classification_to_main(self):
        """
        Attach ClassificationInteractor to the MAIN view (plan view).

        This lets all classification tools (rectangle, circle, brush, etc.)
        work directly on the main window.
        """
        try:
            from gui.cross_section.interactor_classify import ClassificationInteractor
        except ImportError:
            try:
                from .cross_section.interactor_classify import ClassificationInteractor
            except ImportError as e:
                print(f"⚠️ Cannot import ClassificationInteractor: {e}")
                return

        # Ensure main PyVista widget exists
        if not hasattr(self, 'vtk_widget') or self.vtk_widget is None:
            print("⚠️ No main VTK widget (vtk_widget) - cannot attach ClassificationInteractor to main view")
            return

        iren = self.vtk_widget.interactor

        # Cleanup old main-view interactor if any
        old = getattr(self, 'classify_interactor', None)
        if old and hasattr(old, 'cleanup'):
            old.cleanup()

        # Create a new ClassificationInteractor for the MAIN view
        wrapper = ClassificationInteractor(self, iren, mode="2d")

        # Replace current interactor style with classification style
        iren.SetInteractorStyle(wrapper.style)

        # Keep a reference so we can deactivate later if needed
        self.classify_interactor = wrapper

        print("✅ ClassificationInteractor attached to MAIN view")
 
 
    def _load_single_ptc(self, filename):
        """Load PTC palette file"""
        from gui.progress_dialog import LoadingProgressDialog
        from PySide6.QtCore import QCoreApplication
       
        progress = LoadingProgressDialog(self, show_cancel=False)
        progress.set_filename(filename)
        progress.show()
       
        progress.set_progress(30, "Loading class palette...")
        QCoreApplication.processEvents()
       
        palette = self._load_ptc_file(filename)
        if palette:
            from gui.unified_actor_manager import reset_border_logic_to_structured
            reset_border_logic_to_structured(self)
            palette = self._normalize_palette_weights(palette)
            
            progress.set_progress(80, "Applying palette...")
            QCoreApplication.processEvents()
            
            self.display_mode = "class"
            if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                    try:
                        self.display_mode_dialog.sync_with_app_state()
                    except Exception:
                        pass
            self.apply_class_map({
                "classes": palette,
                "slot": 0,
                "color_mode": 0
            })
           
            try:
                from gui.unified_actor_manager import sync_palette_to_gpu
                sync_palette_to_gpu(
                    self,
                    slot_idx=0,
                    palette=self.class_palette,
                    border=float(getattr(self, 'point_border_percent', 0) or 0.0),
                    render=True,
                )
                print("   ✅ Slot 0 GPU explicitly re-pushed after PTC load")
            except Exception as _ptc_push_err:
                print(f"   ⚠️ Slot 0 GPU push failed after PTC load: {_ptc_push_err}")

            from PySide6.QtCore import QSettings
            settings = QSettings("NakshaAI", "LidarApp")
            settings.setValue("last_ptc_path", filename)
           
            if hasattr(self, 'point_count_widget') and self.point_count_widget:
                refresh_point_statistics(self)
           
            progress.finish_success("Class palette loaded!")
        else:
            progress.finish_error("Failed to load palette")

    def _prompt_lidar_import_options_for_files(self, filenames):
        from gui.data_loader import prompt_lidar_import_options

        first_lidar = next(
            (
                str(path)
                for path in filenames
                if str(path).lower().endswith((".las", ".laz"))
            ),
            None,
        )
        if not first_lidar:
            return {}
        return prompt_lidar_import_options(first_lidar, parent=self)
 
 
    def _load_terrascan_prj(self, filename):
        """Load TerraScan PRJ project"""
        from gui.progress_dialog import LoadingProgressDialog
        from PySide6.QtCore import QCoreApplication

        from .data_loader import _parse_terrascan_prj
        laz_files = _parse_terrascan_prj(filename)

        import_options = self._prompt_lidar_import_options_for_files(laz_files)
        if import_options is None:
            return

        progress = LoadingProgressDialog(self, show_cancel=False)
        progress.set_filename(filename)
        progress.show()

        progress.set_progress(20, "Parsing TerraScan project...")
        QCoreApplication.processEvents()

        total_files = len(laz_files)
        for i, laz in enumerate(laz_files):
            percent = 20 + int((i / total_files) * 60)
            progress.set_progress(percent, f"Loading tile {i+1}/{total_files}...")
            QCoreApplication.processEvents()

            laz_data = load_lidar_file(
                laz,
                parent=None,
                import_options=import_options,
                prompt_user=False,
            )
            if laz_data:
                layer = {
                    "type": "laz_tile",
                    "filename": laz,
                    "xyz": laz_data["xyz"],
                    "crs_epsg": laz_data.get("crs_epsg"),
                    "visible": True,
                }
                self.layers.append(layer)
                if hasattr(self, 'layers_dock') and self.layers_dock:
                    self.layers_dock.add_layer(layer)
       
        progress.set_progress(90, "Rendering project...")
        QCoreApplication.processEvents()
        
        self.display_mode = "class"
        if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
            if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                try:
                    self.display_mode_dialog.sync_with_app_state()
                except Exception:
                    pass
        self._refresh_view(include_overlays=True)
        self._update_window_title(filename, self.project_crs_epsg)
        self.loaded_file = filename
        self.last_save_path = filename
        self._refresh_footer_file_label()
       
        # Set CRS
        if self.project_crs_epsg and not hasattr(self, 'crs'):
            try:
                from pyproj import CRS
                self.crs = CRS.from_epsg(self.project_crs_epsg)
                print(f"✅ Project CRS set: {self.crs.name}")
            except Exception as e:
                print(f"⚠️ Could not create CRS object: {e}")
       
        from .display_mode import restore_display_settings_for_file
        restore_display_settings_for_file(self, filename)
        from gui.unified_actor_manager import reset_border_logic_to_structured
        reset_border_logic_to_structured(self)
       
        if hasattr(self, 'point_count_widget') and self.point_count_widget:
            refresh_point_statistics(self)
       
        progress.finish_success(f"Loaded {total_files} tiles")
 
 
    def _load_single_lidar_file(self, filename):
        """Load single LAZ/LAS/PLY file"""
        from gui.progress_dialog import LoadingProgressDialog
        from PySide6.QtCore import QCoreApplication
        import time

        import_options = self._prompt_lidar_import_options_for_files([filename])
        if import_options is None:
            return

        progress = LoadingProgressDialog(self, show_cancel=False)
        progress.set_filename(filename)
        progress.show()
       
        def update_progress(percent, status):
            progress.set_progress(percent)
            progress.set_status(status)
            QCoreApplication.processEvents()
       
        print(f"\n{'='*60}")
        print(f"📂 Loading file: {os.path.basename(filename)}")
        print(f"{'='*60}")
       
        load_start = time.time()
       
        update_progress(15, "Opening file...")
       
        lidar_data = load_lidar_file(
            filename,
            parent=self,
            import_options=import_options,
            prompt_user=False,
        )
        if not lidar_data:
            print("❌ Failed to load LiDAR file")
            progress.finish_error("Failed to read file")
            return
       
        n_points = len(lidar_data.get('xyz', []))
        progress.set_points_count(n_points)
        print(f"📊 Data loaded: {n_points:,} points")
       
        update_progress(35, f"Processing {n_points:,} points...")
       
        # Set main data
        self.data = lidar_data
        self.data_bounds = None  # invalidate stale SNT z-offset cache for new dataset

        # Track whether this load was class-filtered so auto-save skips the file.
        _io = lidar_data.get("import_options") or {}
        self._loaded_with_class_filter = bool(
            _io.get("only_class") and _io.get("class_codes")
        )
 
        # Build spatial index
        if n_points > 50_000:
            try:
                update_progress(50, "Building spatial index...")
                from gui.performance_optimizations import SpatialIndex
                self.spatial_index = SpatialIndex(self.data["xyz"])
                print(f"✅ Spatial index ready")
            except Exception as e:
                print(f"⚠️ Spatial index failed: {e}")
                self.spatial_index = None
       
        # Set CRS - route through the authoritative canvas CRS manager so the
        # FIRST trustworthy georeferenced dataset establishes the canvas CRS and
        # later datasets are reprojected into it rather than replacing it.
        if lidar_data.get("crs_epsg"):
            try:
                from pyproj import CRS as _CRS
                from gui.crs_manager import ensure_canvas_crs
                _crs_obj = None
                try:
                    _crs_obj = _CRS.from_epsg(int(lidar_data["crs_epsg"]))
                except Exception:
                    if lidar_data.get("crs_wkt"):
                        try:
                            _crs_obj = _CRS.from_wkt(lidar_data["crs_wkt"])
                        except Exception:
                            _crs_obj = None
                if _crs_obj is not None:
                    ensure_canvas_crs(self, _crs_obj,
                                      source="LAZ/LAS header",
                                      dataset=filename)
                    print(f"Project CRS: {_crs_obj.name}")
                else:
                    print("Could not create CRS object from lidar metadata")
            except Exception as e:
                print(f"Could not set canvas CRS: {e}")
       
        self.loaded_file = filename
        self.last_save_path = filename
        self._refresh_footer_file_label()
       
        # Restore settings
        update_progress(55, "Checking for saved settings...")
        from .display_mode import restore_display_settings_for_file
        restore_display_settings_for_file(self, filename)
        from gui.unified_actor_manager import reset_border_logic_to_structured
        reset_border_logic_to_structured(self)
       
        # Set display mode
        self.display_mode = "class"
        if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
            if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                try:
                    self.display_mode_dialog.sync_with_app_state()
                except Exception:
                    pass
        
        # Get palette
        update_progress(80, "Loading palette...")
        palette_to_apply = self._get_palette_for_file(filename)
       
        # Apply palette
        if palette_to_apply:
            # If user selected specific classes on load, hide all others in the
            # palette. All points remain in memory — user can re-enable classes
            # via Display Mode at any time without reloading.
            if isinstance(import_options, dict) and import_options.get("only_class"):
                from gui.display_mode import clone_palette
                _sel = set(int(c) for c in (import_options.get("class_codes") or []))
                palette_to_apply = clone_palette(palette_to_apply)
                for _code, _entry in palette_to_apply.items():
                    _entry["show"] = (_code in _sel)
                print(f"   👁 Initial visibility: showing classes {sorted(_sel)}")
        
            visible_count = len([c for c, v in palette_to_apply.items() if v.get("show")])
            update_progress(85, f"Rendering {visible_count} visible classes...")
           
            self.apply_class_map({
                "classes": palette_to_apply,
                "slot": 0,
                "color_mode": 0,
                "target_view": 0
            })
       
        # Finalize
        update_progress(95, "Finalizing...")
       
        from .pointcloud_display import force_interactor_ready
        force_interactor_ready(self, delay_ms=300)
       
        self.toggle_view_mode("2d")
        self._update_window_title(filename, lidar_data.get("crs_epsg"))
       
        # Auto-load drawings
        if hasattr(self, "digitizer") and self.digitizer:
            try:
                self.digitizer.auto_load_drawings(filename)
            except Exception as e:
                print(f"⚠️ Failed to auto-load drawings: {e}")
       
        # Update statistics
        if hasattr(self, 'point_count_widget') and self.point_count_widget:
            refresh_point_statistics(self)
       
        total_time = time.time() - load_start
        print(f"{'='*60}")
        print(f"✅ LOAD COMPLETE: {os.path.basename(filename)}")
        print(f"   ⏱️ Time: {total_time:.1f}s")
        print(f"{'='*60}\n")
       
        progress.finish_success(f"Loaded {n_points:,} points in {total_time:.1f}s")
 
 
    def _get_palette_for_file(self, filename):
        """Helper to get appropriate palette for file"""
        import os
        from PySide6.QtCore import QSettings
        from gui.display_mode import clone_palette
        
        # Prefer slot-0 palette as canonical source, then fall back.
        source_palette = None
        if hasattr(self, "view_palettes") and isinstance(self.view_palettes, dict):
            slot0 = self.view_palettes.get(0)
            if isinstance(slot0, dict) and slot0:
                source_palette = slot0
        if source_palette is None:
            dlg = getattr(self, "display_mode_dialog", None)
            if dlg is not None and hasattr(dlg, "view_palettes"):
                slot0 = (dlg.view_palettes or {}).get(0)
                if isinstance(slot0, dict) and slot0:
                    source_palette = slot0
        if source_palette is None and hasattr(self, "class_palette") and self.class_palette:
            source_palette = self.class_palette

        if source_palette:
            print("🔄 Reusing existing/restored palette")
            palette = self._normalize_palette_weights(clone_palette(source_palette))

            # Ensure at least one class is visible
            if not any(v.get("show") for v in palette.values()):
                for code in palette:
                    palette[code]["show"] = True

            return palette

        # PTC must not be auto-loaded here. It is global state managed
        # exclusively by the Display Mode dialog. If no palette is in memory
        # the renderer falls back to TerraScan defaults until the user loads
        # a PTC manually.
        print("📋 No active PTC in memory — using TerraScan defaults")
        return None

        # # Try to load from last PTC
        # settings = QSettings("NakshaAI", "LidarApp")
        # last_ptc = settings.value("global_last_ptc_path", "") or settings.value("last_ptc_path", "")
        
        # if last_ptc and os.path.exists(last_ptc):
        #     print(f"📁 Loading PTC: {os.path.basename(last_ptc)}")
        #     from .display_mode import DisplayModeDialog
        #     dlg = DisplayModeDialog(self)
        #     dlg.load_classes_from_path(last_ptc)
        #     dlg.close()
           
        #     class_map = {}
        #     for row in range(dlg.table.rowCount()):
        #         show = dlg.table.cellWidget(row, 0).isChecked()
        #         code = int(dlg.table.item(row, 1).text())
        #         desc = dlg.table.item(row, 2).text()
        #         draw = dlg.table.item(row, 3).text()
        #         lvl = dlg.table.item(row, 4).text()
        #         color = dlg.table.item(row, 5).background().color().getRgb()[:3]
               
        #         class_map[code] = {
        #             "show": show,
        #             "description": desc,
        #             "draw": draw,
        #             "lvl": lvl,
        #             "color": color,
        #             "weight": 1.0
        #         }
           
        #     # Ensure at least one class is visible
        #     if not any(v.get("show") for v in class_map.values()):
        #         for code in class_map:
        #             class_map[code]["show"] = True
           
        #     return class_map
       
        # print("📋 Using TerraScan defaults")
        # return None
    # ============================================================
    # SIMPLER VERSION - Just add progress to existing code
    # ============================================================

    def show_loading_progress(parent, filename, total_steps=5):
        """
        Simple wrapper - show progress dialog during existing load.
        Call this BEFORE your existing load_lidar_file() call.
        """
        # Ensure the LoadingProgressDialog symbol is available (import locally)
        try:
            from gui.progress_dialog import LoadingProgressDialog
        except Exception as e:
            print(f"⚠️ Could not import LoadingProgressDialog: {e}")
            # Fallback minimal progress object to avoid crashes when the dialog class is missing
            class LoadingProgressDialog:
                def __init__(self, parent, show_cancel=False):
                    self._percent = 0
                    self._filename = ""
                def set_filename(self, fn):
                    self._filename = fn
                def show(self):
                    pass
                def set_progress(self, p):
                    self._percent = p
                def set_status(self, s):
                    pass
                def finish_success(self, msg=None):
                    pass
                def finish_error(self, msg=None):
                    pass

        progress = LoadingProgressDialog(parent, show_cancel=False)
        progress.set_filename(filename)
        progress.show()
        
        # Simulate progress (since existing loader doesn't report it)
        from PySide6.QtCore import QTimer, QCoreApplication
        
        step = [0]
        
        def advance():
            step[0] += 1
            percent = int((step[0] / total_steps) * 100)
            progress.set_progress(percent)
            
            statuses = [
                "Opening file...",
                "Reading point data...",
                "Processing colors...",
                "Loading classification...",
                "Building spatial index..."
            ]
            if step[0] <= len(statuses):
                progress.set_status(statuses[step[0] - 1])
            
            QCoreApplication.processEvents()      
        return progress, advance
    
    def _on_main_interactor_keypress(self, obj, evt):
        """
        Handle ESC key in main interactor.
        
        Priority order:
        1. Cancel active curve drawing (if in progress)
        2. Exit curve select mode (if active)
        3. Cancel cross-section mode (existing logic)
        4. Deactivate digitizer tools
        5. Deactivate measurement/other tools
        
        After ESC: right-click on grid labels works for loading LAZ.
        """
        key = obj.GetKeySym()

        if key in ("Left", "Right"):
            if key == "Left":
                handled = self.go_to_previous_main_view()
            else:
                handled = self.go_to_next_main_view()

            if handled:
                self._consume_vtk_event(obj)

            return

        if key == "Escape":
            print("🔑 ESC pressed — checking active tools...")
            # VTK windows do not always pass Escape through Qt's global event
            # filter, so persistent nested-cut placement is handled here too.
            handled = False
            cut_controller = getattr(self, 'cut_section_controller', None)
            cancel_nested_cut = getattr(
                cut_controller, 'cancel_persistent_cut_in_cut', None
            )
            if callable(cancel_nested_cut):
                handled = bool(cancel_nested_cut())

            if not handled:
                handled = self._deactivate_active_identification_tools_for_escape()

            # ══════════════════════════════════════════════════════
            # PRIORITY 1: Cancel active curve drawing
            # ══════════════════════════════════════════════════════
            if hasattr(self, 'curve_tool') and self.curve_tool:
                ct = self.curve_tool
                
                if ct.active:
                    if hasattr(ct, "suspend"):
                        ct.suspend()
                        print("   ✅ Curve drawing paused")
                    else:
                        ct._cancel_curve()
                        print("   ✅ Curve drawing cancelled")
                    handled = True
                
                elif getattr(ct, '_select_mode', False):
                    ct.deactivate_select_mode()
                    print("   ✅ Curve select mode deactivated")
                    handled = True

            # ══════════════════════════════════════════════════════
            # PRIORITY 2: Cancel cross-section mode (YOUR EXISTING LOGIC)
            # ══════════════════════════════════════════════════════
            if not handled:
                current_style = self.vtk_widget.interactor.GetInteractorStyle()

                if isinstance(current_style, CrossSectionInteractor):
                    print("🛑 ESC - deactivating cross-section")
                    self._cancel_cross_section_tool_only()
                    self.set_cross_cursor_active(False, "cross_section")
                    try:
                        self.vtk_widget.render()
                    except Exception:
                        pass
                    self.statusBar().showMessage("Cross-section mode canceled", 2000)
                    handled = True

                # Also check the cancel helper
                if getattr(self, "cross_interactor", None):
                    self._cancel_cross_section_tool_only()
                    print("🛑 Cross-section tool canceled (ESC) - views preserved")
                    handled = True

            # ══════════════════════════════════════════════════════
            # PRIORITY 3: Deactivate digitizer tools
            # ══════════════════════════════════════════════════════
            if not handled:
                if hasattr(self, 'digitizer') and self.digitizer:
                    try:
                        if hasattr(self.digitizer, 'deactivate_all'):
                            self.digitizer.deactivate_all()
                        elif hasattr(self.digitizer, 'current_tool') and self.digitizer.current_tool:
                            self.digitizer.set_tool(None)
                        print("   ✅ Digitizer tools deactivated")
                        handled = True
                    except Exception as e:
                        print(f"   ⚠️ Digitizer deactivate failed: {e}")

            # ══════════════════════════════════════════════════════
            # PRIORITY 4: Deactivate measurement / zoom tools
            # ══════════════════════════════════════════════════════
            if not handled:
                for tool_name in ('measurement_tool', 'select_rectangle_tool', 'zoom_rectangle_tool'):
                    tool = getattr(self, tool_name, None)
                    if tool and hasattr(tool, 'deactivate'):
                        try:
                            tool.deactivate()
                            print(f"   ✅ {tool_name} deactivated")
                            handled = True
                        except Exception:
                            pass

            # ══════════════════════════════════════════════════════
            # FINAL: Restore cursor and show status
            # ══════════════════════════════════════════════════════
            self.vtk_widget.setCursor(Qt.ArrowCursor)

            if handled:
                self.statusBar().showMessage(
                    "✅ Tools off — Right-click grid labels to load LAZ data", 3000
                )
            else:
                self.statusBar().showMessage("Ready", 2000)    
    #     """
    #     Handle ESC key in main interactor to cancel cross-section mode.
    #     """
   
    #         # Check what's currently active
           
    #         # If cross-section interactor is active, deactivate it
               
    #             # AGGRESSIVE CLEANUP: Remove ALL preview actors
               
    #             # Remove 2D centerline
    #                 renderer.RemoveActor2D(sc._centerline_actor_2d)
    #                 sc._centerline_actor_2d = None
    #                 sc._centerline_points_2d = None
    #                 sc._centerline_poly_2d = None
               
    #             # Remove 2D rectangle
    #                 renderer.RemoveActor2D(sc._rubber_actor_2d)
    #                 sc._rubber_actor_2d = None
    #                 sc._rubber_points_2d = None
    #                 sc._rubber_poly_2d = None
               
    #             # Remove 3D rubber band
    #                 renderer.RemoveActor(sc.rubber_actor)
    #                 sc.rubber_actor = None
    #                 sc.rubber_points = None
    #                 sc.rubber_poly = None
               
    #             # Reset interactor state
    #                 current_style.P1 = None
    #                 current_style.P2 = None
    #                 current_style.slice_state = 0
               
    #             # Restore camera control
    #             self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleImage())
               
    #             # Uncheck the cross-section button
    #                 self.cross_action.setChecked(False)
               
    #             self.cross_interactor = None
    #             self.cross_section_active = False
               
    #             # Force render
    #             self.vtk_widget.render()
               
                   
    #             # ✅ CRITICAL: Aggressive cleanup of all section views
    #                     self._cancel_cross_section_tool_only()
               
    #             self.statusBar().showMessage("Cross-section mode canceled", 2000)

    def _normalize_palette_weights(self, palette):
        """
        MODIFIED: Commented out the reset logic to allow user-defined 
        weights to persist after classification.
        """
        if not palette:
            return palette
            
        #     palette[code]["weight"] = 1.0  <-- COMMENT THIS OUT
            
        return palette

    def _load_ptc_file(self, path):
            """Parse TerraScan-style .ptc file and return a class_map dict."""
            try:
                with open(path, "r") as f:
                    lines = [ln.strip() for ln in f if ln.strip()]
            except Exception as e:
                self._show_display_mode_front_message(
                    QMessageBox.Critical,
                    "Error",
                    f"Failed to open .ptc file:\n{e}",
                )
                return None

            class_map = {}
            for i in range(0, len(lines), 2):
                try:
                    header = lines[i].split("\t")
                    detail = lines[i + 1].split("\t")
                    if len(header) < 2 or len(detail) < 5:
                        continue
                    code = int(header[0])
                    desc = header[1]
                    lvl = header[2] if len(header) > 2 else "0"
                    draw = detail[1]
                    rgb = [int(c) for c in detail[3].split(",")]
                    show = detail[4] == "1"
                    class_map[code] = {
                        "show": show,
                        "description": desc,
                        "draw": draw,
                        "lvl": lvl,
                        "color": tuple(rgb),
                    }
                except Exception as e:
                    print(f"⚠️ Skipping malformed entry at line {i}: {e}")
                    continue
            return class_map

    def _ensure_shading_controls_dock(self):
        dock = getattr(self, "shading_dock", None)
        panel = getattr(self, "shading_panel", None)

        if isinstance(dock, QDockWidget):
            try:
                self.removeDockWidget(dock)
            except Exception:
                pass
            try:
                dock.deleteLater()
            except Exception:
                pass
            dock = None
            panel = None

        if (dock is not None
                and panel is not None
                and _qt_object_is_valid(dock)
                and _qt_object_is_valid(panel)):
            return dock

        self.shading_dock = None
        self.shading_panel = None

        panel = ShadingControlPanel(self)
        panel.setParent(
            self,
            Qt.Tool | Qt.WindowTitleHint | Qt.WindowCloseButtonHint | Qt.CustomizeWindowHint
        )
        panel.setAttribute(Qt.WA_DeleteOnClose, False)
        panel.adjustSize()

        self.shading_dock = panel
        self.shading_panel = panel

        try:
            panel.ensurePolished()
            panel.adjustSize()
            panel.resize(panel.sizeHint())
        except Exception:
            pass

        return panel

    def _can_show_shading_controls(self):
        try:
            data = getattr(self, "data", None)
            mode = str(getattr(self, "display_mode", "") or "").lower()
            current_mode = str(getattr(self, "current_display_mode", "") or "").lower()

            if not isinstance(data, dict):
                return False

            xyz = data.get("xyz")
            classification = data.get("classification")
            if xyz is None or classification is None:
                return False

            if mode not in ("shaded_class", "shading", "shadingmode") and \
                    current_mode not in ("shaded_class", "shading", "shadingmode"):
                return False

            actor = getattr(self, "_shaded_mesh_actor", None)
            if actor is not None:
                try:
                    if hasattr(actor, "GetVisibility"):
                        return bool(actor.GetVisibility())
                except Exception:
                    return True
                return True

            try:
                renderer = getattr(getattr(self, "vtk_widget", None), "renderer", None)
                if renderer is not None:
                    actors = renderer.GetActors()
                    actors.InitTraversal()

                    for _ in range(actors.GetNumberOfItems()):
                        a = actors.GetNextActor()
                        if a is None:
                            continue

                        is_shading = bool(getattr(a, "_is_shading_mesh", False))
                        mode_tag = str(getattr(a, "_naksha_display_mode", "") or "").lower()
                        name_tag = str(getattr(a, "name", "") or "").lower()

                        if is_shading or mode_tag in ("shaded_class", "shading", "shadingmode") or "shad" in name_tag:
                            try:
                                return bool(a.GetVisibility())
                            except Exception:
                                return True
            except Exception:
                pass

            return True

        except Exception:
            return False

    def show_shading_controls(self):
        if self._accudraw_canvas_right_click_in_progress():
            print("AccuDraw right-click finish - Shading settings blocked")
            return False

        if not self._can_show_shading_controls():
            print("Shading right-click settings blocked - Shading mode/mesh is not active")
            return False

        dock = self._ensure_shading_controls_dock()
        if dock is None or not _qt_object_is_valid(dock):
            return False

        panel = getattr(self, "shading_panel", None)
        if panel is not None and _qt_object_is_valid(panel):
            sync = getattr(panel, "sync_from_app", None)
            if not callable(sync):
                sync = getattr(panel, "refresh_from_app", None)
            if callable(sync):
                sync()

        dock.show()
        dock.raise_()
        try:
            dock.activateWindow()
        except Exception:
            pass
        return True

    def _schedule_curve_tool_resume(self, reason="mode switch"):
        """
        Auto-resume the curve tool on the next Qt event-loop tick after a
        suspend(), mirroring execute_tool.py's _schedule_curve_tool_resume.
        Needed here too because set_display_mode() can be entered directly
        (ribbon buttons, the 3 settings-dialog "Apply" handlers) without
        ever going through execute_tool.py's version of this scheduling.
        """
        from PySide6.QtCore import QTimer

        def _do_resume():
            ct = getattr(self, 'curve_tool', None)
            if ct is None:
                return
            if getattr(ct, 'active', False):
                return
            if not getattr(ct, '_suspended_state', None):
                return
            if not getattr(self, '_draw_curve_context_active', False):
                return
            if hasattr(ct, 'resume'):
                ct.resume()
                print(f"   🔮 Curve tool auto-resumed after {reason}")

        QTimer.singleShot(0, _do_resume)

    def _suspend_curve_tool_safely(
        self,
        reason="switching tools",
        *,
        resume_after_switch=True,
    ):
        """
        Pause (not cancel) an in-progress curve drawing so app.curve_tool.points
        survives a display-mode switch — matching how SmartLine/Polyline already
        behave (their temp_points live on the persistent Digitizer and nobody
        clears them on mode switch).

        This is the SINGLE choke point for curve-tool safety on display-mode
        change: set_display_mode() is called from ~7 different places (keyboard
        shortcuts via execute_tool.py, the ribbon Elevation/Intensity/Depth
        buttons, and the 3 "Apply" settings dialogs) and only the keyboard-
        shortcut path had a suspend() guard before this fix — every other
        caller went straight into set_display_mode() with zero curve-tool
        awareness, which is what let ct._cancel_curve() wipe self.points
        depending on which UI element the user clicked.

        Safe to call redundantly (e.g. once from execute_tool.py, once here
        from set_display_mode itself): the second call sees ct.active is
        already False and returns immediately, so it's a harmless no-op.
        """
        # Entering an exclusive tool (for example cross-section) ends the
        # curve drawing context.  Clear this before touching CurveTool so any
        # already-queued display-mode resume callback also stands down.
        if not resume_after_switch:
            self._draw_curve_context_active = False

        ct = getattr(self, 'curve_tool', None)
        if ct is None:
            return

        is_active = getattr(ct, 'active', False)
        is_select_mode = getattr(ct, '_select_mode', False)

        if not is_active and not is_select_mode:
            return

        print(f"   🎨 Suspending curve tool ({reason})")

        if is_active:
            if hasattr(ct, "suspend"):
                ct.suspend()
                if resume_after_switch:
                    # Re-arm display-mode switches on the next event-loop
                    # tick. Exclusive tools deliberately leave curve mode
                    # suspended until the user selects Curve again.
                    self._schedule_curve_tool_resume(reason)
            else:
                # Defensive fallback only — should be unreachable once
                # CurveTool.suspend() exists, kept so an older/partial
                # CurveTool build degrades to the old cancel behavior
                # instead of hard-crashing.
                ct._cancel_curve()

        if is_select_mode:
            ct.deactivate_select_mode()

    def set_display_mode(self, mode):
        """
        MicroStation-style display mode switch.
        ALL modes write directly into the unified actor's RGB buffer.
        Zero actor rebuild. DXF/SNT grids always preserved.
        """
        if self.data is None:
            self._show_display_mode_front_message(
                QMessageBox.Warning,
                "No Data",
                "Please load a LiDAR file first.",
            )
            return

        # ✅ FIX: suspend (never cancel) any in-progress curve drawing before
        # the mode switch actually happens. Covers every caller of this
        # method, not just the keyboard-shortcut path.
        self._suspend_curve_tool_safely(f"switching to {mode} display mode")

        import time as _time
        mode = str(mode or "").lower().strip()
        t0 = _time.perf_counter()
        print(f"\n🎨 Display mode → {mode}")

        # Surface is a heavy mesh build. Skip duplicate Surface calls only when
        # the Display Mode class visibility has not changed.
        if mode == "surface" and str(getattr(self, "display_mode", "") or "").lower() == "surface":
            existing_surface_actor = getattr(self, "_surface_mesh_actor", None)
            if existing_surface_actor is not None:
                force_surface_rebuild = False

                try:
                    def _surface_signature_from_open_dialog():
                        dlg = (
                            getattr(self, "display_mode_dialog", None)
                            or getattr(self, "display_dialog", None)
                        )
                        table = getattr(dlg, "table", None) if dlg is not None else None

                        if table is not None and hasattr(table, "rowCount") and table.rowCount() > 0:
                            visible_codes = []
                            for row in range(table.rowCount()):
                                try:
                                    chk = table.cellWidget(row, 0)
                                    code_item = table.item(row, 1)

                                    if code_item is None:
                                        continue

                                    code = int(str(code_item.text()).strip())

                                    if chk is not None and hasattr(chk, "isChecked"):
                                        if chk.isChecked():
                                            visible_codes.append(code)
                                except Exception:
                                    continue

                            return tuple(sorted(visible_codes))

                        from gui.surface_mode import surface_visible_class_signature
                        return surface_visible_class_signature(self)

                    current_sig = _surface_signature_from_open_dialog()
                    previous_sig = getattr(self, "_surface_visible_class_signature", None)
                    try:
                        from gui.surface_mode import normalize_surface_quality
                        current_quality = normalize_surface_quality(getattr(self, "surface_quality", "normal"))
                    except Exception:
                        current_quality = str(getattr(self, "surface_quality", "normal") or "normal").lower()
                    previous_quality = getattr(self, "_surface_quality_signature", None)

                    if (
                        previous_sig is None
                        or current_sig != previous_sig
                        or previous_quality != current_quality
                    ):
                        force_surface_rebuild = True
                        print("🔁 Surface Display Mode signature changed — switching/rebuilding Surface mesh")
                        print(f"   classes old={previous_sig}")
                        print(f"   classes new={current_sig}")
                        print(f"   quality old={previous_quality} new={current_quality}")

                except Exception as _surface_sig_err:
                    print(f"⚠️ Surface signature check failed: {_surface_sig_err}")
                    force_surface_rebuild = False

                if not force_surface_rebuild:
                    try:
                        if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                            dlg = self.display_mode_dialog
                            if hasattr(dlg, "color_mode") and dlg.color_mode.currentIndex() != 6:
                                dlg.color_mode.blockSignals(True)
                                dlg.color_mode.setCurrentIndex(6)
                                dlg.color_mode.blockSignals(False)
                    except Exception:
                        pass

                    print("⏭️ Surface already active — skipped duplicate rebuild")
                    return

        self.display_mode = mode

        # Save camera
        saved_camera = None
        try:
            camera = self.vtk_widget.renderer.GetActiveCamera()
            if camera:
                saved_camera = {
                    'position': camera.GetPosition(),
                    'focal_point': camera.GetFocalPoint(),
                    'view_up': camera.GetViewUp(),
                    'view_angle': camera.GetViewAngle(),
                    'clipping_range': camera.GetClippingRange(),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection()
                }
        except Exception:
            pass

        # Close control docks
        for dock_name in ('shading_dock', 'class_dock'):
            dock = getattr(self, dock_name, None)
            if dock:
                try:
                    if isinstance(dock, QDockWidget):
                        self.removeDockWidget(dock)
                    else:
                        dock.hide()
                except Exception:
                    pass

                if dock_name == 'shading_dock' and not isinstance(dock, QDockWidget):
                    continue

                setattr(self, dock_name, None)
                if dock_name == 'shading_dock':
                    self.shading_panel = None

        # ═══════════════════════════════════════════════════════════════
        # SHADED CLASS — separate mesh pipeline (triangulated surface)
        # ═══════════════════════════════════════════════════════════════
        if mode == "shaded_class":
            if self.data.get("classification") is None:
                QMessageBox.warning(self, "No Classification",
                    "Shaded Classification requires class data.")
                mode = "class"
                self.display_mode = "class"
            else:
                # Shading owns all scene cleanup centrally inside
                # update_shaded_class().  Do not park Surface here as well;
                # duplicate caller cleanup caused the same Surface actor to be
                # processed twice on one mode switch.
                from .shading_display import update_shaded_class
                update_shaded_class(
                    self,
                    getattr(self, "last_shade_azimuth", 45.0),
                    getattr(self, "shading_sharpness_angle", 45.0),
                    getattr(self, "shade_ambient", 0.2),
                )
                dock = self._ensure_shading_controls_dock()
                if dock is not None:
                    dock.hide()
                try:
                    if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                        dlg = self.display_mode_dialog
                        # Switch dialog to Main View before updating color mode
                        if dlg.current_slot != 0:
                            dlg.slot_box.blockSignals(True)
                            dlg.slot_box.setCurrentIndex(0)
                            dlg.slot_box.blockSignals(False)
                            dlg.on_slot_changed(0)
                        if dlg.color_mode.currentIndex() != 1:
                            dlg.color_mode.blockSignals(True)
                            dlg.color_mode.setCurrentIndex(1)
                            dlg.color_mode.blockSignals(False)
                except Exception:
                    pass
                self._restore_camera_safe(saved_camera)
                return

        # ═══════════════════════════════════════════════════════════════
        # SURFACE MODE — separate mesh pipeline, no class-color overlay
        # ═══════════════════════════════════════════════════════════════
        if mode == "surface":
            try:
                from .pointcloud_display import update_pointcloud
                self.display_mode = "surface"
                update_pointcloud(self, "surface")
                try:
                    if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                        dlg = self.display_mode_dialog
                        if hasattr(dlg, "color_mode") and dlg.color_mode.currentIndex() != 6:
                            dlg.color_mode.blockSignals(True)
                            dlg.color_mode.setCurrentIndex(6)
                            dlg.color_mode.blockSignals(False)
                except Exception:
                    pass
                self._restore_camera_safe(saved_camera)
                return
            except Exception as _surface_err:
                print(f"⚠️ Surface display failed: {_surface_err}")
                mode = "elevation"
                self.display_mode = "elevation"

        # ═══════════════════════════════════════════════════════════════
        # ALL OTHER MODES: Write colors into unified actor RGB buffer
        # Zero rebuild. MicroStation instant switch.
        # ═══════════════════════════════════════════════════════════════

        # Remove Surface mesh if switching away from Surface mode.
        if mode != "surface":
            try:
                from gui.surface_mode import detach_surface_before_non_surface_mode
                detach_surface_before_non_surface_mode(self, requested_mode=mode)
            except Exception as _surface_cleanup_err:
                print(f"  ⚠️ Surface cleanup before {mode} skipped: {_surface_cleanup_err}")

        # Remove every shading-owned actor (base, edges, and fast-add patches)
        # while preserving cached geometry for a later fast shading restore.
        try:
            from .shading_display import detach_shading_before_non_shading_mode
            detach_shading_before_non_shading_mode(self)
        except Exception as _shading_detach_err:
            print(f"  ⚠️ Shading detach skipped: {_shading_detach_err}")

        # Ensure unified actor exists
        from gui.unified_actor_manager import (
            _get_unified_actor, build_unified_actor, _mark_actor_dirty,
            is_unified_actor_ready, fast_palette_refresh, UNIFIED_ACTOR_NAME
        )
        import numpy as np
        from vtkmodules.util import numpy_support

        actor = _get_unified_actor(self)
        if actor is None:
            # Build it for the first time
            palette = getattr(self, 'class_palette', {})
            border = float(getattr(self, 'point_border_percent', 0) or 0)
            build_unified_actor(self, palette=palette, border_percent=border)
            actor = _get_unified_actor(self)

        if actor is None:
            print("  ⚠️ Could not create unified actor")
            self._restore_camera_safe(saved_camera)
            return

        # Make unified actor visible (it may have been hidden by shading mode)
        actor.SetVisibility(True)

        rgb_ptr = getattr(actor, '_naksha_rgb_ptr', None)
        vtk_ca = getattr(actor, '_naksha_vtk_array', None)
        gi = getattr(self, '_main_global_indices', None)

        if rgb_ptr is None or vtk_ca is None:
            print("  ⚠️ RGB buffer not available — falling back to full rebuild")
            from .pointcloud_display import update_pointcloud
            update_pointcloud(self, mode)
            self._restore_camera_safe(saved_camera)
            return

        # Get visible data
        xyz = self.data["xyz"]
        classification = self.data.get("classification")
        vis_xyz = xyz[gi] if gi is not None else xyz
        vis_class = classification[gi] if (classification is not None and gi is not None) else classification

        # ── COMPUTE COLORS BASED ON MODE ──
        if mode == "class":
            # Classification colors from palette
            palette = getattr(self, 'class_palette', {})
            border = float(getattr(self, 'point_border_percent', 0) or 0)

            # Restore border for class mode
            if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                saved_border = self.display_mode_dialog.view_borders.get(0, 0)
                self.point_border_percent = float(saved_border)

            fast_palette_refresh(self, palette=palette, border_percent=border)
            print(f"  ✅ Class mode: palette applied ({len(palette)} classes)")

        elif mode == "rgb":
            rgb_data = self.data.get("rgb")
            if rgb_data is not None:
                vis_rgb = rgb_data[gi] if gi is not None else rgb_data
                np.copyto(rgb_ptr, vis_rgb[:len(rgb_ptr)])
            else:
                # No RGB data — show white
                rgb_ptr[:] = 200
            vtk_ca.Modified()
            _mark_actor_dirty(actor)
            self.vtk_widget.render()
            print(f"  ✅ RGB mode: direct color copy")

        elif mode == "intensity":
            from gui.pointcloud_display import _microstation_intensity_rgb

            intensity = self.data.get("intensity")
            if intensity is not None:
                vis_int = intensity[gi] if gi is not None else intensity

                colors_u8, clip_lo, clip_hi = _microstation_intensity_rgb(
                    vis_int,
                    low_pct=getattr(self, "intensity_clip_low", 0.5),
                    high_pct=getattr(self, "intensity_clip_high", 99.8),
                    gamma=getattr(self, "intensity_gamma", 1.35),
                    return_debug=True,
                )

                rgb_ptr[:, 0] = colors_u8[:len(rgb_ptr), 0]
                rgb_ptr[:, 1] = colors_u8[:len(rgb_ptr), 1]
                rgb_ptr[:, 2] = colors_u8[:len(rgb_ptr), 2]

                print(
                    f"  ✅ Intensity mode: raw [{float(vis_int.min()):.1f}, {float(vis_int.max()):.1f}] "
                    f"clip [{clip_lo:.1f}, {clip_hi:.1f}] "
                    f"gamma={getattr(self, 'intensity_gamma', 1.35):.2f}"
                )
            else:
                rgb_ptr[:] = 128
                print("  ⚠️ No intensity data — showing gray")

            vtk_ca.Modified()
            _mark_actor_dirty(actor)
            self.vtk_widget.render()

        elif mode == "elevation":
            from gui.pointcloud_display import _microstation_elevation_rgb
            import time as _time

            t_elev0 = _time.perf_counter()

            vis_z = vis_xyz[:, 2].astype(np.float32, copy=False)

            # Build cache key
            ramp = getattr(self, "elevation_color_ramp", None)
            if ramp:
                ramp_key = tuple(
                    (round(float(p), 6), int(c[0]), int(c[1]), int(c[2]))
                    for p, c in ramp
                )
            else:
                ramp_key = None

            if gi is None:
                vis_key = ("all", len(vis_z))
            else:
                # enough to invalidate when visible subset object changes
                vis_key = (id(gi), len(vis_z))

            z_min = float(vis_z.min()) if len(vis_z) else 0.0
            z_max = float(vis_z.max()) if len(vis_z) else 0.0

            cache_key = (
                id(self.data["xyz"]),
                vis_key,
                round(z_min, 4),
                round(z_max, 4),
                round(float(getattr(self, "elevation_clip_low", 1.0)), 3),
                round(float(getattr(self, "elevation_clip_high", 99.0)), 3),
                ramp_key,
            )

            if (
                self._elevation_cache_key == cache_key
                and self._elevation_cache_colors is not None
                and len(self._elevation_cache_colors) == len(rgb_ptr)
            ):
                colors_u8 = self._elevation_cache_colors
                print("  ⚡ Elevation cache hit")
            else:
                colors_u8, clip_lo, clip_hi = _microstation_elevation_rgb(
                    vis_z,
                    color_ramp=getattr(self, "elevation_color_ramp", None),
                    low_pct=getattr(self, "elevation_clip_low", 1.0),
                    high_pct=getattr(self, "elevation_clip_high", 99.0),
                    return_debug=True,
                )
                self._elevation_cache_key = cache_key
                self._elevation_cache_colors = colors_u8
                print(
                    f"  ✅ Elevation computed: raw Z [{z_min:.1f}, {z_max:.1f}] "
                    f"clip [{clip_lo:.1f}, {clip_hi:.1f}]"
                )

            # Fast bulk copy instead of per-channel assignment
            np.copyto(rgb_ptr[:len(colors_u8)], colors_u8[:len(rgb_ptr)])

            vtk_ca.Modified()
            _mark_actor_dirty(actor)
            self.vtk_widget.render()

            t_elev_ms = (_time.perf_counter() - t_elev0) * 1000.0
            print(f"  ⚡ Elevation mode apply: {t_elev_ms:.1f}ms")

        elif mode == "depth":
            # ✅ FIX: Read custom depth settings from app attributes
            from gui.pointcloud_display import _microstation_depth_rgb_from_camera
            
            # Use camera-based depth (true depth from camera perspective)
            camera = self.vtk_widget.renderer.GetActiveCamera()
            
            colors_u8 = _microstation_depth_rgb_from_camera(
                vis_xyz,
                camera,
                low_pct=getattr(self, "depth_clip_low", 1.0),
                high_pct=getattr(self, "depth_clip_high", 99.0),
                color_scheme=getattr(self, "depth_color_scheme", "grayscale"),
                gamma=getattr(self, "depth_gamma", 1.0),
            )
            
            rgb_ptr[:, 0] = colors_u8[:len(rgb_ptr), 0]
            rgb_ptr[:, 1] = colors_u8[:len(rgb_ptr), 1]
            rgb_ptr[:, 2] = colors_u8[:len(rgb_ptr), 2]
            
            vtk_ca.Modified()
            _mark_actor_dirty(actor)
            self.vtk_widget.render()
            
            clip_lo = getattr(self, "depth_clip_low", 1.0)
            clip_hi = getattr(self, "depth_clip_high", 99.0)
            scheme = getattr(self, "depth_color_scheme", "grayscale")
            gamma_val = getattr(self, "depth_gamma", 1.0)
            print(f"  ✅ Depth mode applied: clip={clip_lo:.1f}-{clip_hi:.1f}%, scheme={scheme}, gamma={gamma_val:.2f}")

        elif mode == "line":
            # Line is only a color presentation of the SAME point geometry.
            # Never rebuild/slice the actor for flight-line visibility: the
            # FlightVisible shader array handles show/hide independently.
            source_ids = self.data.get("point_source_id")
            if source_ids is None or len(source_ids) != len(xyz):
                print("  No usable Point Source ID data for Line mode")
            else:
                vis_lines = source_ids[gi] if gi is not None else source_ids
                vis_lines = np.asarray(vis_lines)
                line_colors = dict(getattr(self, "flight_line_colors", {}) or {})

                # LAS Point Source ID is uint16. A fixed 65,536-row RGB LUT
                # (~192 KiB) lets NumPy write directly into the persistent VTK
                # RGB buffer. The previous np.unique(..., return_inverse=True)
                # allocated a huge inverse array (100+ MB for ~13M points) on
                # every Line switch and added roughly a second of CPU time.
                try:
                    line_color_sig = tuple(sorted(
                        (int(raw_id), tuple(int(c) for c in color[:3]))
                        for raw_id, color in line_colors.items()
                    ))
                except Exception:
                    line_color_sig = tuple()

                line_lut = getattr(self, "_line_mode_rgb_lut", None)
                if (
                    not isinstance(line_lut, np.ndarray)
                    or line_lut.shape != (65536, 3)
                    or line_lut.dtype != np.uint8
                    or getattr(self, "_line_mode_rgb_lut_sig", None) != line_color_sig
                ):
                    ids = np.arange(65536, dtype=np.uint32)
                    line_lut = np.empty((65536, 3), dtype=np.uint8)
                    line_lut[:, 0] = ((ids * 67 + 53) & 255).astype(np.uint8)
                    line_lut[:, 1] = ((ids * 131 + 97) & 255).astype(np.uint8)
                    line_lut[:, 2] = ((ids * 193 + 181) & 255).astype(np.uint8)
                    for raw_line_id, color in line_colors.items():
                        try:
                            line_id = int(raw_line_id)
                            if 0 <= line_id <= 65535:
                                line_lut[line_id] = tuple(int(c) for c in color[:3])
                        except Exception:
                            continue
                    self._line_mode_rgb_lut = line_lut
                    self._line_mode_rgb_lut_sig = line_color_sig

                line_ids = np.asarray(vis_lines, dtype=np.intp)
                if (
                    line_ids.size == len(rgb_ptr)
                    and (line_ids.size == 0 or (line_ids.min() >= 0 and line_ids.max() <= 65535))
                ):
                    # Direct indexed write: no compact RGB temporary, no inverse array.
                    np.take(line_lut, line_ids, axis=0, out=rgb_ptr)
                else:
                    # Defensive fallback for malformed/non-LAS source IDs.
                    unique_lines, inverse = np.unique(vis_lines, return_inverse=True)
                    compact_lut = np.empty((len(unique_lines), 3), dtype=np.uint8)
                    for lut_idx, raw_line_id in enumerate(unique_lines):
                        line_id = int(raw_line_id)
                        compact_lut[lut_idx] = line_colors.get(
                            line_id,
                            (
                                (line_id * 67 + 53) % 256,
                                (line_id * 131 + 97) % 256,
                                (line_id * 193 + 181) % 256,
                            ),
                        )
                    np.copyto(rgb_ptr, compact_lut[inverse])

                vtk_ca.Modified()
                _mark_actor_dirty(actor)
                self.vtk_widget.render()
                configured_lines = max(
                    len(line_colors),
                    len(dict(getattr(self, "flight_line_visibility", {}) or {})),
                )
                print(
                    f"  ⚡ Line mode: existing unified actor recolored via LUT "
                    f"({configured_lines} configured flight lines)"
                )

        else:
            # Unknown mode — fallback to pointcloud_display
            try:
                from .pointcloud_display import update_pointcloud
                update_pointcloud(self, mode)
            except Exception as e:
                print(f"  ⚠️ Fallback display failed: {e}")

        if mode not in ("class", "shaded_class"):
            try:
                from gui.unified_actor_manager import _apply_border_once, _push_uniforms_direct
                _border_val = float(getattr(self, "point_border_percent", 0) or 0.0)
                if _border_val > 0.0:
                    _actor = actor if actor is not None else None
                    if _actor is None:
                        from gui.unified_actor_manager import _get_unified_actor
                        _actor = _get_unified_actor(self)
                    if _actor is not None:
                        _apply_border_once(_actor, _border_val)
                        _ctx = getattr(_actor, "_naksha_shader_ctx", None)
                        if _ctx is not None:
                            _push_uniforms_direct(_actor, _ctx)
                        print(f"  🔳 Border {_border_val}% preserved in {mode} mode")
            except Exception as _be:
                print(f"  ⚠️ Border preservation failed: {_be}")

        _MODE_TO_IDX = {
            "class":        0,
            "shaded_class": 1,
            "depth":        2,
            "intensity":    3,
            "rgb":          4,
            "elevation":    5,
            "surface":      6,
            "line":         7,
        }
        try:
            if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                dlg = self.display_mode_dialog
                # Switch dialog to Main View before updating color mode
                if dlg.current_slot != 0:
                    dlg.slot_box.blockSignals(True)
                    dlg.slot_box.setCurrentIndex(0)
                    dlg.slot_box.blockSignals(False)
                    dlg.on_slot_changed(0)
                target_idx = _MODE_TO_IDX.get(mode, -1)
                if target_idx >= 0 and dlg.color_mode.currentIndex() != target_idx:
                    dlg.color_mode.blockSignals(True)
                    dlg.color_mode.setCurrentIndex(target_idx)
                    dlg.color_mode.blockSignals(False)
        except Exception as _ce:
            pass  # dialog may not be open yet — not critical

        # Restore camera
        self._restore_camera_safe(saved_camera)

        # ✅ BULLETPROOF: Ensure overlays survive any mode switch
        self._ensure_overlay_actors()

        elapsed = (_time.perf_counter() - t0) * 1000
        print(f"  ⚡ Display switch complete: {elapsed:.0f}ms\n")


    def _handle_display_mode_change(self, mode):
        """
        Wrapper for display mode changes from View Ribbon.
        Intercepts Elevation and Intensity clicks to detect Shift key.
        
        Args:
            mode: Display mode string ('rgb', 'depth', 'intensity', 'elevation', etc.)
        """
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        
        # ✅ Special handling for Elevation mode
        if mode == 'elevation':
            modifiers = QApplication.keyboardModifiers()
            
            if modifiers & Qt.ShiftModifier:
                # Shift+Click → Open customization dialog
                self._open_elevation_settings()
                return  # Don't call set_display_mode
            else:
                # Normal click → keep the last applied ramp active
                # until the user changes it again.
                pass
        
        # ✅ NEW: Special handling for Intensity mode
        elif mode == 'intensity':
            modifiers = QApplication.keyboardModifiers()
            
            if modifiers & Qt.ShiftModifier:
                # Shift+Click → Open intensity settings dialog
                self._open_intensity_settings()
                return  # Don't call set_display_mode
            # Normal click → Just apply default intensity settings

        # ✅ NEW: Special handling for Depth mode
        elif mode == 'depth':
            modifiers = QApplication.keyboardModifiers()
            
            if modifiers & Qt.ShiftModifier:
                # Shift+Click → Open depth settings dialog
                self._open_depth_settings()
                return  # Don't call set_display_mode
            # Normal click → Just apply default depth settings
        
        # ✅ For all modes (including elevation/intensity after settings)
        self.set_display_mode(mode)


    #     """
    #     Open elevation color ramp customization dialog (Shift+Click on Elevation button).
    #     Allows users to customize the color gradient for elevation display.
    #     """
    #         QMessageBox.warning(self, "No Data", "Please load a LiDAR file first.")
        
        
    #     # Create dialog
        
    #     # Load existing custom ramp if user already customized it
    #         dialog.color_stops = list(self.elevation_color_ramp)
    #         dialog._load_current_ramp()
    #         dialog._update_preview()
        
    #     # Show dialog and wait for user
    #         # User clicked Apply - save custom ramp
    #         self.elevation_color_ramp = dialog.get_color_ramp()
            
    #         # Apply immediately
    #         self.set_display_mode('elevation')
            
    #         # User feedback
    #             self.statusBar().showMessage(
    #                 f"✨ Custom elevation gradient applied ({len(self.elevation_color_ramp)} colors)",
    #                 3000
    #             )


    def on_elevation_clicked(self):
        """
        Handle elevation button click.
        - Normal click: Apply default 5-color rainbow
        - Shift+Click: Open customization dialog
        """
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import Qt
        
        modifiers = QApplication.keyboardModifiers()
        
        if modifiers & Qt.ShiftModifier:
            # Shift+Click → Open customization dialog
            self._open_elevation_settings()
        else:
            # Normal click → Reapply the currently saved ramp if present.
            # Do not clear user edits here; the last applied elevation ramp
            # should remain active until the user changes it.
            self.set_display_mode('elevation')

    def _open_elevation_settings(self):
        """
        Open elevation color ramp customization dialog (Shift+Click on Elevation button).
        ✅ FIXED: Now passes app reference so settings persist across sessions.
        """
        if self.data is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "No Data", "Please load a LiDAR file first.")
            return
        
        from gui.elevation_settings_dialog import ElevationSettingsDialog
        
        # ✅ Pass self (app) as second parameter
        dialog = ElevationSettingsDialog(self, app=self)
        
        # Settings are already loaded in __init__ via app parameter
        
        from PySide6.QtWidgets import QDialog
        if dialog.exec() == QDialog.Accepted:
            # User clicked Apply - save custom ramp
            self.elevation_color_ramp = dialog.get_color_ramp()
            
            # ✅ OPTIONAL: Save to QSettings for persistence across app restarts
            try:
                from PySide6.QtCore import QSettings
                settings = QSettings("NakshaAI", "LidarApp")
                # Convert to serializable format
                ramp_data = [(float(pos), list(color)) for pos, color in self.elevation_color_ramp]
                settings.setValue("elevation_color_ramp", ramp_data)
                settings.sync()
                print(f"💾 Saved elevation ramp to settings")
            except Exception as e:
                print(f"⚠️ Failed to save elevation ramp: {e}")
            
            # Apply immediately
            self.set_display_mode('elevation')
            
            print(f"✅ Custom elevation ramp applied: {len(self.elevation_color_ramp)} color stops")
            if hasattr(self, 'statusBar'):
                self.statusBar().showMessage(
                    f"✨ Custom elevation gradient applied ({len(self.elevation_color_ramp)} colors)",
                    3000
                )
        else:
            print("⏭️ Elevation settings canceled")

    def _open_intensity_settings(self):
        """
        Open intensity display customization dialog (Shift+Click on Intensity button).
        ✅ NEW: Allows users to adjust brightness/darkness of intensity display.
        """
        if self.data is None or self.data.get("intensity") is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, 
                "No Intensity Data", 
                "This point cloud does not have intensity data.\n\n"
                "Intensity settings only work with LiDAR files that include intensity values."
            )
            return
        
        from gui.intensity_settings_dialog import IntensitySettingsDialog
        
        dialog = IntensitySettingsDialog(self, app=self)
        
        from PySide6.QtWidgets import QDialog
        if dialog.exec() == QDialog.Accepted:
            # User clicked Apply - save settings
            settings = dialog.get_settings()
            
            self.intensity_gamma = settings['gamma']
            self.intensity_clip_low = settings['clip_low']
            self.intensity_clip_high = settings['clip_high']
            
            # ✅ Save to QSettings for persistence across app restarts
            try:
                from PySide6.QtCore import QSettings
                qsettings = QSettings("NakshaAI", "LidarApp")
                qsettings.setValue("intensity_gamma", self.intensity_gamma)
                qsettings.setValue("intensity_clip_low", self.intensity_clip_low)
                qsettings.setValue("intensity_clip_high", self.intensity_clip_high)
                qsettings.sync()
                print(f"💾 Saved intensity settings: gamma={self.intensity_gamma:.2f}")
            except Exception as e:
                print(f"⚠️ Failed to save intensity settings: {e}")
            
            # Apply immediately
            self.set_display_mode('intensity')
            
            print(f"✅ Custom intensity settings applied: gamma={self.intensity_gamma:.2f}")
            if hasattr(self, 'statusBar'):
                self.statusBar().showMessage(
                    f"⚡ Intensity display updated (gamma={self.intensity_gamma:.2f})",
                    3000
                )
        else:
            print("⏭️ Intensity settings canceled")


    def _open_depth_settings(self):
        """
        Open depth display customization dialog (Shift+Click on Depth button).
        ✅ NEW: Allows users to adjust depth visualization settings.
        """
        if self.data is None:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(
                self, 
                "No Data", 
                "Please load a LiDAR file first to use depth display."
            )
            return
        
        from gui.depth_settings_dialog import DepthSettingsDialog
        
        dialog = DepthSettingsDialog(self, app=self)
        
        from PySide6.QtWidgets import QDialog
        if dialog.exec() == QDialog.Accepted:
            # User clicked Apply - save settings
            settings = dialog.get_settings()
            
            self.depth_clip_low = settings['depth_clip_low']
            self.depth_clip_high = settings['depth_clip_high']
            self.depth_color_scheme = settings['depth_color_scheme']
            self.depth_gamma = settings['depth_gamma']
            
            # ✅ Save to QSettings for persistence across app restarts
            try:
                from PySide6.QtCore import QSettings
                qsettings = QSettings("NakshaAI", "LidarApp")
                qsettings.setValue("depth_clip_low", self.depth_clip_low)
                qsettings.setValue("depth_clip_high", self.depth_clip_high)
                qsettings.setValue("depth_color_scheme", self.depth_color_scheme)
                qsettings.setValue("depth_gamma", self.depth_gamma)
                qsettings.sync()
                print(f"💾 Saved depth settings: scheme={self.depth_color_scheme}, gamma={self.depth_gamma:.2f}")
            except Exception as e:
                print(f"⚠️ Failed to save depth settings: {e}")
            
            # Apply immediately
            self.set_display_mode('depth')
            
            print(f"✅ Custom depth settings applied: {self.depth_color_scheme}, gamma={self.depth_gamma:.2f}")
            if hasattr(self, 'statusBar'):
                self.statusBar().showMessage(
                    f"📏 Depth display updated ({self.depth_color_scheme}, gamma={self.depth_gamma:.2f})",
                    3000
                )
        else:
            print("⏭️ Depth settings canceled")


    def _restore_camera_safe(self, saved_camera):
        """Restore camera position after display mode switch."""
        if saved_camera and hasattr(self, 'vtk_widget') and self.vtk_widget.renderer:
            try:
                camera = self.vtk_widget.renderer.GetActiveCamera()
                if camera:
                    camera.SetPosition(saved_camera['position'])
                    camera.SetFocalPoint(saved_camera['focal_point'])
                    camera.SetViewUp(saved_camera['view_up'])
                    camera.SetViewAngle(saved_camera['view_angle'])
                    camera.SetClippingRange(saved_camera['clipping_range'])
                    camera.SetParallelScale(saved_camera['parallel_scale'])
                    if saved_camera['parallel_projection']:
                        camera.ParallelProjectionOn()
                    else:
                        camera.ParallelProjectionOff()
                    self.vtk_widget.renderer.ResetCameraClippingRange()
                    self.vtk_widget.render()
            except Exception:
                pass

    def _ensure_overlay_actors(self):
        """
        BULLETPROOF: Ensure all DXF/SNT overlay actors are in the renderer.
        Called after any file load, grid switch, or display mode change.
        Checks what's actually in the renderer and re-adds anything missing.
        """
        if not hasattr(self, 'vtk_widget') or not self.vtk_widget:
            return
        try:
            renderer = self.vtk_widget.renderer
            # Collect all actors currently in renderer
            actors_in_renderer = set()
            vtk_actors = renderer.GetActors()
            vtk_actors.InitTraversal()
            for _ in range(vtk_actors.GetNumberOfItems()):
                a = vtk_actors.GetNextActor()
                if a:
                    actors_in_renderer.add(id(a))

            restored = 0
            for store_name in ('dxf_actors', 'snt_actors'):
                store = getattr(self, store_name, None)
                if store:
                    for entry in store:
                        for actor in entry.get('actors', []):
                            if id(actor) not in actors_in_renderer:
                                renderer.AddActor(actor)
                                restored += 1

            if restored > 0:
                # ✅ FIX: Reset clipping range after re-adding actors so SNT actors
                # at their Z-offset positions are not outside the view frustum.
                try:
                    renderer.ResetCameraClippingRange()
                except Exception:
                    pass
                self.vtk_widget.render()
                print(f"   ✅ _ensure_overlay_actors: restored {restored} missing actors")
        except Exception as e:
            print(f"   ⚠️ _ensure_overlay_actors failed: {e}")

    
    def open_display_mode(self):
        """Open Display Mode dialog with auto-restoration of saved settings"""
    
        dialog = getattr(self, 'display_mode_dialog', None)
        if dialog is not None:
            try:
                if _qt_object_is_valid(dialog):
                    return True
            except RuntimeError:
                pass
            self.display_mode_dialog = None

        if not hasattr(self, 'display_mode_dialog') or self.display_mode_dialog is None:
            from gui.display_mode import DisplayModeDialog
        
            print(f"\n{'='*60}")
            print(f"📂 CREATING DISPLAY MODE DIALOG")
            print(f"{'='*60}")
        
            # Create dialog (connection happens inside __init__)
            self.display_mode_dialog = DisplayModeDialog(self)
        
            # Connect signals
            self.display_mode_dialog.view_switched.connect(self.activate_dock_by_view)
        
            # Connect to Class Picker
            class_picker = self._get_live_class_picker()
            if class_picker is not None:
                self.display_mode_dialog.classes_loaded.connect(
                    class_picker.on_classes_changed
                )
        
            # Connect to Point Count Widget
            if hasattr(self, 'point_count_widget') and self.point_count_widget:
                self.point_count_widget.connect_to_display_mode(self.display_mode_dialog)
        
            print(f"{'='*60}\n")
        else:
            # ✅ CRITICAL: Dialog already exists - just refresh table from app's palette
            print(f"\n{'='*60}")
            print(f"🔄 REOPENING EXISTING DISPLAY MODE DIALOG")
            print(f"{'='*60}")
            
            # Sync table with current app state (preserves weights)
            if hasattr(self, 'class_palette') and self.class_palette:
                print(f"   🔄 Syncing table with app.class_palette...")
                
                for row in range(self.display_mode_dialog.table.rowCount()):
                    code = int(self.display_mode_dialog.table.item(row, 1).text())
                    
                    if code in self.class_palette:
                        # Update weight from app's palette
                        weight = self.class_palette[code].get('weight', 1.0)
                        weight_item = self.display_mode_dialog.table.item(row, 6)
                        if weight_item:
                            weight_item.setText(f"{weight:.2f}")
                        
                        # Update checkbox state
                        show = self.class_palette[code].get('show', True)
                        chk = self.display_mode_dialog.table.cellWidget(row, 0)
                        if chk:
                            chk.setChecked(show)
                
                print(f"   ✅ Table synced with existing weights")
            
            print(f"{'='*60}\n")
    
        # Sync color_mode combo with the app's current display mode (surface, class, etc.)
        if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
            self.display_mode_dialog.sync_with_app_state()

        # Show dialog
        self.display_mode_dialog.setModal(False)
        self.display_mode_dialog.show()
        self.display_mode_dialog.raise_()
        self.display_mode_dialog.activateWindow()
        return self.display_mode_dialog


    def apply_class_map(self, payload):
        """
        ⚡ ULTRA-OPTIMIZED: Instant apply button response for any file size.
        ✅ UNIFIED ACTOR PATH: GPU weight_lut + visibility_lut push (<1ms)
        ✅ Legacy fast path: GPU color buffer update (when unified actor absent)
        ✅ Async path: Non-blocking rebuilds for large files
        """

        if getattr(self, "_is_applying_class_map", False):
            return

        try:
            self._is_applying_class_map = True
            import numpy as np
            from vtkmodules.util import numpy_support
            from PySide6.QtCore import QTimer

            # --- 1. Extract Payload ---
            incoming_palette = payload.get("classes", {})
            target_view = payload.get("target_view", 0)
            color_mode = payload.get("color_mode", 0)
            border_value = payload.get("border_percent", getattr(self, "point_border_percent", 0))
            force_refresh = payload.get("force_refresh", False)
            border_only = payload.get("border_only", False)

            # --- 2. Update Internal State ---
            if not hasattr(self, 'view_palettes'):
                self.view_palettes = {}

            self.view_palettes[target_view] = {
                code: {
                    "show": bool(info.get("show", True)),
                    "color": tuple(info.get("color", (128, 128, 128))),
                    "weight": float(info.get("weight", 1.0)),
                    "description": str(info.get("description", ""))
                } for code, info in incoming_palette.items()
            }

            # ============================================================
            # CASE A: MAIN VIEW (View 0) - ULTRA OPTIMIZED
            # ============================================================
            if target_view == 0:
                from gui.display_mode import clone_palette
                self.class_palette = clone_palette(self.view_palettes[0])
                self.point_border_percent = border_value
            # ADD AFTER:
                # ── Keep dialog.view_palettes[0] in sync so DisplayMode preset shortcuts
                # don't reseed slot 0 GPU with stale ptc.ptc colours.
                dlg = getattr(self, 'display_mode_dialog', None)
                if dlg is not None and hasattr(dlg, 'view_palettes'):
                    dlg.view_palettes[0] = clone_palette(self.view_palettes[0])
                    print("   ✅ PRE-STEP: dialog.view_palettes[0] synced from app (slot 0 apply)")

                # Quick border-only path
                if border_only and not force_refresh:
                    self._update_border_only()
                    self._is_applying_class_map = False
                    return

                is_class_mode = (color_mode == 0)

                # ✅ UNIFIED ACTOR FAST PATH (highest priority)
                # sync_palette_to_gpu pushes BOTH the RGB color buffer AND
                # weight_lut / visibility_lut to the GPU shader in < 1 ms.
                # This is the ONLY path that correctly updates point weights.
                if is_class_mode and not force_refresh:
                    try:
                        from gui.unified_actor_manager import (
                            is_unified_actor_ready,
                            sync_palette_to_gpu,
                        )
                        if is_unified_actor_ready(self):
                            sync_palette_to_gpu(
                                self,
                                slot_idx=0,
                                palette=self.class_palette,
                                border=float(border_value),
                                render=True,
                            )
                            self._is_applying_class_map = False
                            return
                    except Exception as _unified_err:
                        print(f"⚠️ unified actor fast-path error, falling back: {_unified_err}")

                # ✅ LEGACY GPU COLOR BUFFER PATH (fallback when no unified actor)
                use_fast_path = (
                    is_class_mode and
                    not force_refresh and
                    hasattr(self, 'main_pc_actor') and
                    self.main_pc_actor and
                    hasattr(self, 'data') and
                    self.data is not None and
                    'classification' in self.data
                )

                if use_fast_path:
                    try:
                        _m = self.main_pc_actor.GetMapper()
                        polydata = _m.GetInput() if _m else None
                        vtk_colors = polydata.GetPointData().GetScalars() if polydata else None

                        if vtk_colors is not None:
                            class_arr = self.data["classification"]
                            max_c = int(class_arr.max())

                            lut = np.zeros((max_c + 1, 3), dtype=np.uint8)
                            for code, info in self.class_palette.items():
                                if code <= max_c:
                                    lut[code] = info.get("color", (128, 128, 128)) if info.get("show", True) else (0, 0, 0)

                            new_rgb = lut[class_arr.astype(int)]
                            vtk_ptr = numpy_support.vtk_to_numpy(vtk_colors)
                            np.copyto(vtk_ptr, new_rgb)
                            vtk_colors.Modified()

                            if border_value > 0:
                                try:
                                    from gui.class_display import apply_border_shader_ring
                                    for name, actor in self.vtk_widget.actors.items():
                                        if name.startswith('class_'):
                                            apply_border_shader_ring(actor, border_value)
                                except Exception as e:
                                    print(f"⚠️ Border application failed in fast path: {e}")

                            def deferred_render():
                                try:
                                    self.vtk_widget.render()
                                except Exception: pass

                            QTimer.singleShot(0, deferred_render)
                            self._is_applying_class_map = False
                            return

                    except Exception as e:
                        pass  # Fall through to rebuild

                # 🔄 REBUILD PATH
                point_count = len(self.data["xyz"]) if hasattr(self, 'data') and self.data and "xyz" in self.data else 0

                def deferred_rebuild():
                    try:
                        from gui.class_display import update_class_mode
                        update_class_mode(self, force_refresh=True)
                    except Exception as e:
                        print(f"⚠️ Rebuild error: {e}")

                if point_count > 500_000:
                    QTimer.singleShot(0, deferred_rebuild)
                    self._is_applying_class_map = False
                    return
                else:
                    deferred_rebuild()

            # ============================================================
            # CASE B: CROSS-SECTIONS (Views 1-4) - ASYNC OPTIMIZED
            # ============================================================
            elif 1 <= target_view <= 4:
                section_idx = target_view - 1
                if not hasattr(self, "view_borders"):
                    self.view_borders = {}
                self.view_borders[target_view] = border_value
            
                if border_only and not force_refresh:
                    self._update_section_border_only(section_idx, border_value)
                    self._is_applying_class_map = False
                    return
            
                # ← REMOVED: self.class_palette = self.view_palettes[target_view]
                # app.class_palette must always be the MAIN view palette.
                # Section palettes live in app.view_palettes[slot_idx] only.
            
                def deferred_section_update():
                    try:
                        self._refresh_single_section_view(section_idx, border_value)
                        if hasattr(self, 'section_interactors') and section_idx in self.section_interactors:
                            self.section_interactors[section_idx].setup_picker()
                    except Exception as e:
                        print(f"⚠️ Section update error: {e}")
            
                QTimer.singleShot(0, deferred_section_update)
                self._is_applying_class_map = False
                return  ##

            # ============================================================
            # CASE C: CUT SECTION (View 5) - ASYNC OPTIMIZED
            # ============================================================
            elif target_view == 5:
                ctrl = getattr(self, 'cut_section_controller', None)
                if ctrl and getattr(ctrl, 'is_cut_view_active', False):
                    ctrl.cut_palette = self.view_palettes[5]
                    if not hasattr(self, "view_borders"):
                        self.view_borders = {}
                    self.view_borders[5] = border_value

                    def deferred_cut_update():
                        try:
                            if hasattr(ctrl, '_refresh_cut_colors_fast'):
                                ctrl._refresh_cut_colors_fast()
                        except Exception as e:
                            print(f"⚠️ Cut section update error: {e}")

                    QTimer.singleShot(0, deferred_cut_update)
                    self._is_applying_class_map = False
                    return

        except Exception as e:
            print(f"❌ apply_class_map failed: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self._is_applying_class_map = False  ##


    # ============================================================
    # HELPER FUNCTIONS (keep your existing border-only methods)
    # ============================================================

    def _update_border_only(self):
        """Fast border-only update without full point cloud refresh."""
        if not hasattr(self, 'vtk_widget') or self.vtk_widget is None:
            return
        
        try:
            actors = self.vtk_widget.renderer.GetActors()
            actors.InitTraversal()
            
            border_factor = self.point_border_percent / 100.0
            
            actor = actors.GetNextActor()
            while actor:
                prop = actor.GetProperty()
                if prop:
                    if self.point_border_percent > 0:
                        prop.EdgeVisibilityOn()
                        prop.SetLineWidth(max(0.5, border_factor * 2))
                    else:
                        prop.EdgeVisibilityOff()
                actor = actors.GetNextActor()
            
            self.vtk_widget.render()
            
        except Exception as e:
            print(f"⚠️ Border update failed: {e}")


    def _update_section_border_only(self, section_idx, border_value):
        """Fast border-only update for cross-section views."""
        try:
            if not hasattr(self, 'section_vtks') or section_idx not in self.section_vtks:
                return
            
            vtk_widget = self.section_vtks[section_idx]
            if not vtk_widget or not hasattr(vtk_widget, 'renderer'):
                return
            
            actors = vtk_widget.renderer.GetActors()
            actors.InitTraversal()
            
            border_factor = border_value / 100.0
            
            actor = actors.GetNextActor()
            while actor:
                prop = actor.GetProperty()
                if prop:
                    if border_value > 0:
                        prop.EdgeVisibilityOn()
                        prop.SetLineWidth(max(0.5, border_factor * 2))
                    else:
                        prop.EdgeVisibilityOff()
                actor = actors.GetNextActor()
            
            vtk_widget.render()
            
        except Exception as e:
            print(f"⚠️ Section border update failed: {e}")
            
    def _refresh_active_cross_section_only(self):
        """
        Refresh ONLY the active cross-section view after classification.
        Uses the ACTIVE VIEW's palette (not global or other views).
        ✅ FIXED: Proper palette isolation per view
        """
        try:
            app = self.app if hasattr(self, 'app') else self
            
            # Get active view index (0-based: 0, 1, 2, 3)
            active_view = getattr(app.section_controller, 'active_view', None)
            
            if active_view is None:
                print("⚠️ No active view set")
                return
            
            # Convert to Display Mode slot (1-based: 1, 2, 3, 4)
            target_slot = active_view + 1
            
            print(f"\n{'='*60}")
            print(f"🔄 ISOLATED REFRESH: Cross-Section View {active_view} (Display Mode Slot {target_slot})")
            
            # Get the VTK widget for this view
            if not hasattr(app, 'section_vtks') or active_view not in app.section_vtks:
                print(f"   ⚠️ View {active_view} not found in section_vtks")
                print(f"{'='*60}\n")
                return
            
            vtk_widget = app.section_vtks[active_view]
            
            # ✅ CRITICAL: Get THIS VIEW's palette from Display Mode dialog
            view_palette = None
            
            if hasattr(app, 'display_mode_dialog'):
                dialog = app.display_mode_dialog
                
                # Check if this view has a stored palette in view_palettes
                if hasattr(dialog, 'view_palettes') and target_slot in dialog.view_palettes:
                    view_palette = dialog.view_palettes[target_slot]
                    print(f"   ✅ Using stored view_palettes[{target_slot}]")
                else:
                    # Build palette from Display Mode table for this slot
                    print(f"   🔄 Building palette from Display Mode slot {target_slot}")
                    
                    # Save current slot
                    original_slot = dialog.current_slot
                    
                    # Switch to target slot to read its settings
                    if dialog.current_slot != target_slot:
                        dialog.current_slot = target_slot
                        dialog._load_slot_checkboxes(target_slot)
                    
                    # Build palette from current table state
                    view_palette = {}
                    table = dialog.table
                    
                    for row in range(table.rowCount()):
                        code = int(table.item(row, 1).text())
                        chk = table.cellWidget(row, 0)
                        
                        if chk and chk.isChecked():  # Only include checked classes
                            desc = table.item(row, 2).text()
                            color = table.item(row, 5).background().color().getRgb()[:3]
                            weight_item = table.item(row, 6)
                            weight = float(weight_item.text()) if weight_item else 1.0
                            
                            view_palette[code] = {
                                "show": True,
                                "description": desc,
                                "color": color,
                                "weight": weight
                            }
                    
                    # Restore original slot
                    if original_slot != target_slot:
                        dialog.current_slot = original_slot
                        dialog._load_slot_checkboxes(original_slot)
                    
                    print(f"   ✅ Built palette with {len(view_palette)} visible classes")
            
            # Fallback: keep slot isolation if no Display Mode palette exists
            if not view_palette:
                print(f"   ⚠️ No Display Mode palette for slot {target_slot} - seeding isolated slot defaults")
                view_palette = self._get_cross_section_palette(
                    target_slot,
                    allow_default_seed=True,
                    persist_seed=False,
                )
                if not view_palette:
                    print("   ❌ No palette available - cannot refresh")
                    print(f"{'='*60}\n")
                    return
            
            # Get visible classes from THIS VIEW's palette
            visible = [c for c, v in view_palette.items() if v.get("show", False)]
            
            if not visible:
                print(f"   ⚠️ No visible classes in view {active_view}'s palette")
                vtk_widget.clear()
                vtk_widget.render()
                print(f"{'='*60}\n")
                return
            
            print(f"   📋 Visible classes: {visible}")
            
            # Get stored section data for this specific view
            pts = getattr(app, f"section_{active_view}_core_points", None)
            buf = getattr(app, f"section_{active_view}_buffer_points", None)
            core_mask = getattr(app, f"section_{active_view}_core_mask", None)
            buffer_mask = getattr(app, f"section_{active_view}_buffer_mask", None)
            
            if pts is None or core_mask is None:
                print(f"   ⚠️ No section data for view {active_view}")
                print(f"{'='*60}\n")
                return
            
            # Get CURRENT classifications from main dataset
            if not hasattr(app.data, '__getitem__'):
                current_classes = app.data.get("classification")
            else:
                current_classes = app.data["classification"]
            
            if current_classes is None:
                print("   ⚠️ No classification data")
                print(f"{'='*60}\n")
                return
            
            # Combine core + buffer points
            import numpy as np
            if buf is not None and buffer_mask is not None:
                all_pts = np.vstack([pts, buf])
                all_cls = np.concatenate([
                    current_classes[core_mask],
                    current_classes[buffer_mask & ~core_mask]
                ])
            else:
                all_pts = pts
                all_cls = current_classes[core_mask]
            
            # ✅ CRITICAL: Filter by THIS VIEW's visible classes ONLY
            mask = np.isin(all_cls, visible)
            filtered_pts = all_pts[mask]
            filtered_cls = all_cls[mask]
            
            print(f"   📊 Total points: {len(all_pts)}")
            print(f"   📊 Filtered points: {len(filtered_pts)} (visible classes only)")
            
            if len(filtered_pts) == 0:
                print("   ⚠️ No points after filtering")
                vtk_widget.clear()
                vtk_widget.render()
                print(f"{'='*60}\n")
                return
            
            # ✅ Build color array using THIS VIEW's palette ONLY
            colors = np.zeros((len(filtered_pts), 3), dtype=np.uint8)
            for i, cls in enumerate(filtered_cls):
                entry = view_palette.get(int(cls), {"color": (128, 128, 128)})
                colors[i] = entry["color"]
            
            # Debug: Show unique classes being rendered
            unique_classes = np.unique(filtered_cls)
            print(f"   🎨 Rendering classes: {unique_classes}")
            for cls in unique_classes:
                count = np.sum(filtered_cls == cls)
                color = view_palette.get(int(cls), {}).get("color", (128, 128, 128))
                print(f"      Class {cls}: {count} points, color={color}")
            
            # Save camera position
            cam_pos = vtk_widget.camera_position
            
            # Clear and re-render
            vtk_widget.clear()
            
            import pyvista as pv
            cloud = pv.PolyData(filtered_pts)
            cloud["RGB"] = colors
            
            vtk_widget.add_points(
                cloud,
                scalars="RGB",
                rgb=True,
                point_size=3,
                render_points_as_spheres=True
            )
            
            # Restore camera
            vtk_widget.camera_position = cam_pos
            vtk_widget.render()
            
            print(f"   ✅ View {active_view} refreshed with {len(filtered_pts)} points")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"   ❌ Isolated refresh failed: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")
        # ------------------ Classification Tools ------------------
        
    def set_classify_tool(self, tool_name):
            """
            Activate a classification tool.
            Works for: cut/cross sections and main view (fallback).
            ✅ UPDATED: ClassPicker never steals focus
            """
            if tool_name != "cut_section":
                self._deactivate_pending_cut_section_tool("switching to classification")

            # ✅ NEW: If cross-section was active (shortcut switch), deactivate it properly
            if getattr(self, "cross_section_active", False):
                print("🛑 Deactivating cross-section (switching to classification)")
                if hasattr(self, "_cancel_cross_section_tool_only"):
                    self._cancel_cross_section_tool_only()
                   
            if tool_name is None:
                # ✅ Temp Fence tool lifecycle: returning to "no tool" (pan/idle)
                # must stand down vertex capture too, or its left-click observer
                # keeps consuming every click and blocks panning. Same stand-down
                # already done below for switching to a different named tool.
                try:
                    _tft = getattr(self, "temp_fence_tool", None)
                    if _tft is not None:
                        _tft.deactivate()
                except Exception as _e:
                    print(f"⚠️ Temp fence stand-down failed: {_e}")

                self.active_classify_tool = None
                if hasattr(self, 'skip_main_view_refresh'):
                    self.skip_main_view_refresh = False
                self._hide_class_picker_safely()
                # Keep instance alive to preserve selections

                if hasattr(self, 'digitizer'):
                    self.digitizer.enabled = True
                    print("✅ Digitizer re-enabled")
                return

            # ✅ Temp Fence tool lifecycle: switching to any other tool stands
            # down the vertex capture. A finalized fence + popup intentionally
            # stay alive until replaced / used / Esc'd (handled inside the tool).
            if tool_name != "temp_fence":
                try:
                    _tft = getattr(self, "temp_fence_tool", None)
                    if _tft is not None:
                        _tft.deactivate()
                except Exception as _e:
                    print(f"⚠️ Temp fence stand-down failed: {_e}")
   
            # Brush size dialog — skip during right-click reactivation to avoid blocking
            if tool_name == "brush" and not getattr(self, "_right_click_reactivating", False):
                from gui.brush_size_dialog import activate_brush_tool_with_dialog
                show_settings = bool(getattr(self, "_brush_settings_requested_from_ui", False))
                self._brush_settings_requested_from_ui = False
                if not activate_brush_tool_with_dialog(self, show_settings=show_settings):
                    return

            if tool_name == "parallel_line" and not getattr(self, "_right_click_reactivating", False):
                from gui.parallel_line_settings_dialog import activate_parallel_line_tool_with_dialog
                show_settings = bool(
                    getattr(self, "_parallel_line_settings_requested_from_ui", False)
                )
                self._parallel_line_settings_requested_from_ui = False
                if not activate_parallel_line_tool_with_dialog(
                    self, show_settings=show_settings
                ):
                    return
               
              # ✅ LiDAR algorithm dialogs — By Class ribbon buttons
            if tool_name.startswith("algo_") or tool_name.startswith("byclass_"):
                try:
                    from gui.lidar_classification_tools import (
                        open_classify_low_points, open_classify_isolated_points,
                        open_classify_ground, open_classify_surface_points,
                        open_classify_below_surface,
                    )
                    {
                        "algo_low_points":        open_classify_low_points,
                        "algo_isolated":          open_classify_isolated_points,
                        "algo_ground":            open_classify_ground,
                        "algo_surface_points":    open_classify_surface_points,
                        "algo_below_surface":     open_classify_below_surface,
                        "byclass_low_points":     open_classify_low_points,
                        "byclass_isolated":       open_classify_isolated_points,
                        "byclass_ground":         open_classify_ground,
                        "byclass_surface_points": open_classify_surface_points,
                        "byclass_below_surface":  open_classify_below_surface,
                    }.get(tool_name, lambda a: None)(self)
                except Exception as e:
                    print(f"⚠️ Algorithm dialog failed ({tool_name}): {e}")
                return  ###

            # ✅ NEW: Temporary Fence tool — draw a throwaway polygon fence on
            # the main view, then pick a By Class tool from the popup that
            # appears (the fence is pre-applied in that dialog). Fully
            # standalone: never touches the digitizer or classification
            # interactors.
            if tool_name == "temp_fence":
                if self.data is None:
                    QMessageBox.warning(self, "No Data", "Please load a LiDAR file first.")
                    return
                # Stand down any active classification tool first so its
                # interactor doesn't fight the fence drawing.
                try:
                    if getattr(self, "active_classify_tool", None):
                        self.set_classify_tool(None)
                except Exception as _e:
                    print(f"⚠️ Temp Fence: failed to stand down classify tool: {_e}")
                try:
                    if hasattr(self, 'digitizer'):
                        self.digitizer.enabled = False
                    from gui.temp_fence_tool import TempFenceTool
                    tft = getattr(self, "temp_fence_tool", None)
                    if tft is None:
                        tft = TempFenceTool(self)
                        self.temp_fence_tool = tft
                    tft.activate()
                    self.active_classify_tool = "temp_fence"
                except Exception as e:
                    print(f"⚠️ Temp Fence activation failed: {e}")
                    import traceback; traceback.print_exc()
                return
           
            # Stand down conflicting tools so their VTK observers don't fight the
            # classification interactor — same direction the element-select tool
            # uses to stand down classification on activation. Each call is
            # wrapped so one failure can't abort classification setup.
            try:
                if hasattr(self, 'digitizer'):
                    self.digitizer.deactivate_element_select_tool()
            except Exception as e:
                print(f"⚠️ Failed to deactivate element select before classification: {e}")

            # Defensive: ribbon_manager.ribbons may be missing during startup/teardown.
            identify_tool = getattr(self, "identification_tool", None)
            if identify_tool is not None and getattr(identify_tool, "active", False):
                try:
                    rm = getattr(self, "ribbon_manager", None)
                    ribbons = getattr(rm, "ribbons", None) if rm is not None else None
                    identify_ribbon = ribbons.get("identify") if isinstance(ribbons, dict) else None
                    if identify_ribbon is not None and hasattr(identify_ribbon, "deactivate_all_tools"):
                        identify_ribbon.deactivate_all_tools()
                    else:
                        identify_tool.deactivate()
                    print("🛑 Identification tool deactivated for classification")
                except Exception as e:
                    print(f"⚠️ Failed to deactivate identification before classification: {e}")

            measurement_tool = getattr(self, "measurement_tool", None)
            if measurement_tool is not None and getattr(measurement_tool, "active", False):
                try:
                    if hasattr(measurement_tool, "deactivate_completely"):
                        measurement_tool.deactivate_completely()
                    else:
                        measurement_tool.deactivate()
                    print("🛑 Measurement tool deactivated for classification")
                except Exception as e:
                    print(f"⚠️ Failed to deactivate measurement before classification: {e}")

            if hasattr(self, 'digitizer'):
                self.digitizer.enabled = False
                print("🚫 Digitizer disabled (classification active)")

            has_cross_section = (hasattr(self, "section_vtks") and len(self.section_vtks) > 0)
            has_cut_section = (
                hasattr(self, "cut_section_controller")
                and self.cut_section_controller.cut_points is not None
            )
   
            # Tools that ONLY make sense in cross-section views
            section_only_tools = {"above_line", "below_line", "parallel_line"}
   
            # Tool that ONLY targets cut-section view
            cut_only_tools = {"cut_section"}
   
            # Decide what to attach (can be multiple)
            do_cut = has_cut_section and tool_name in cut_only_tools
            do_sections = has_cross_section and tool_name not in cut_only_tools
            do_main = (tool_name not in cut_only_tools) and (tool_name not in section_only_tools)
   
            if tool_name in section_only_tools and not has_cross_section:
                self.active_classify_tool = None
                QMessageBox.warning(self, "Cross Section Required", "Open a Cross Section view to use this tool.")
                return
   
            if self.data is None:
                QMessageBox.warning(self, "No Data", "Please load a LiDAR file first.")
                return
   
            self.skip_main_view_refresh = False
            self.active_classify_tool = tool_name

            # Track the last drawable classification tool so right-click in
            # cross/cut section windows can reactivate it after deactivation.
            _DRAWABLE_TOOLS = {
                "above_line", "below_line", "parallel_line",
                "rectangle", "circle", "freehand", "polygon", "brush", "point",
            }
            if tool_name in _DRAWABLE_TOOLS:
                self.last_classify_tool = tool_name
                self.last_classify_from_classes = getattr(self, "from_classes", None)
                self.last_classify_to_class = getattr(self, "to_class", None)

            self.from_classes = getattr(self, "from_classes", None)
            self.to_class = getattr(self, "to_class", None)
   
            # ✅ FIXED: Ensure class picker is ALWAYS visible (even if minimized)
            from gui.class_picker import ClassPicker
            class_picker = self._get_live_class_picker()
            if class_picker is None:
                # Create new ClassPicker
                print("📋 Creating new ClassPicker...")
                self.class_picker = ClassPicker(self, parent=self)
                class_picker = self.class_picker
            
                # ✅ Configure for non-focus mode
                class_picker.configure_for_background_mode()
            
                class_picker.show()
                print("✅ ClassPicker created and shown")
            else:
                # ✅ CRITICAL FIX: Restore from minimized state
                print("📋 ClassPicker exists - ensuring visibility...")
            
                # First sync the data
                class_picker.sync_with_app()
            
                # ✅ NEW: This line fixes the minimized window issue!
                class_picker.ensure_visible()
            
                print("✅ ClassPicker is now visible and active")
   
            # ✅ CRITICAL: Keep focus on main view
            if hasattr(self, 'vtk_widget'):
                self.vtk_widget.setFocus()
            # Attach interactors
            try:
                if do_cut:
                    self.cut_section_controller.activate_classification_mode()
                    # don't return — fall through to also attach sections + main
 
                if do_sections:
                    self.attach_classification_to_all_section_views()
 
                if do_main:
                    self.attach_classification_to_main()
                    # PRE-BUILD SPATIAL INDEX in background so the UI never freezes.
                    # The build runs off the main thread; on_left_press joins the
                    # thread if still alive (nearly instant tail-wait).
                    if tool_name == "brush" and hasattr(self, "classify_interactor"):
                        import threading
                        ci = self.classify_interactor
                        def _safe_prebuild():
                            try:
                                ci._build_spatial_index_for_brush()
                            except Exception as _e:
                                print(f"⚠️ Brush index pre-warm failed: {_e}")
                        t = threading.Thread(
                            target=_safe_prebuild,
                            daemon=True,
                            name="brush-index-prebuild",
                        )
                        t.start()
                        ci._prebuild_thread = t
 
                # ✅ Always re-attach to cut section if cut data exists
                if has_cut_section:
                    self.attach_classification_to_cut_section()
   
            except Exception as e:
                print(f"⚠️ Failed to attach classification interactor: {e}")
                import traceback; traceback.print_exc()
            
    def enable_digitizer_mode(self):
        """Enable digitizer (for drawing tools)."""
        if hasattr(self, 'digitizer'):
            self.digitizer.enabled = True
            print("✅ Digitizer enabled - Ctrl+Z/Y will affect drawings")
            self.statusBar().showMessage("✏️ Drawing mode - Undo/Redo affects drawings", 2000)

    def disable_digitizer_mode(self):
        """Disable digitizer (for classification tools)."""
        if hasattr(self, 'digitizer'):
            self.digitizer.enabled = False
            print("🚫 Digitizer disabled - Ctrl+Z/Y will affect classification")
            self.statusBar().showMessage("🎨 Classification mode - Undo/Redo affects classification", 2000)
                
            
    def attach_classification_to_all_section_views(self):
        """
        Attach a ClassificationInteractor to every open cross-section view (1..4).
        Replaces the view's interactor style so classification works from any dock.
        
        ✅ ISSUE 1 FIX: Attachment ONLY installs event observers — never touches RGB buffer.
        """
        try:
            from gui.cross_section.interactor_classify import ClassificationInteractor
        except Exception as e:
            print(f"⚠️ Cannot import ClassificationInteractor: {e}")
            return

        if not hasattr(self, "section_vtks") or not self.section_vtks:
            print("ℹ️ No cross-section views are open")
            return

        if not hasattr(self, "classify_interactors"):
            self.classify_interactors = {}

        attached = 0
        for view_idx, vtk_widget in self.section_vtks.items():
            try:
                old = self.classify_interactors.get(view_idx)
                if old and hasattr(old, 'cleanup'):
                    old.cleanup()
                wrapper = ClassificationInteractor(self, vtk_widget.interactor)
                vtk_widget.interactor.SetInteractorStyle(wrapper.style)
                self.classify_interactors[view_idx] = wrapper
                attached += 1
            except Exception as e:
                print(f"⚠️ Attach classify interactor failed for View {view_idx + 1}: {e}")

        print(f"✅ Classification interactor attached to {attached} cross-section view(s)")
        # ✅ ISSUE 1 FIX: No fast_cross_section_update / _refresh_single_view / sync_palette_to_gpu calls here

    
    def attach_classification_to_cut_section(self):
        """Re-attach ClassificationInteractor to cut_vtk after any tool activation."""
        try:
            from gui.cross_section.interactor_classify import ClassificationInteractor
        except Exception as e:
            print(f"⚠️ Cannot import ClassificationInteractor: {e}")
            return
 
        cut_ctrl = getattr(self, 'cut_section_controller', None)
        if cut_ctrl is None:
            return
 
        cut_vtk = getattr(cut_ctrl, 'cut_vtk', None)
        if cut_vtk is None:
            return
 
        # Cleanup any old cut-specific interactor (stored separately)
        old = getattr(self, 'cut_classify_interactor', None)
        if old:
            try: old.cleanup()
            except Exception: pass
 
        wrapper = ClassificationInteractor(self, cut_vtk.interactor, mode="2d")
        wrapper.vtk_widget = cut_vtk
        wrapper.is_cut_section = True
        cut_vtk.interactor.SetInteractorStyle(wrapper.style)
 
        # Store in SEPARATE attribute — not classify_interactor (which is for main view)
        self.cut_classify_interactor = wrapper
 
        print("✅ ClassificationInteractor re-attached to Cut Section")    
 
    def preserve_dxf_actors(self):
        """Forces DXF/SNT actors to stay visible and ON TOP during all zooms."""
        if not hasattr(self, 'dxf_actors') or not self.dxf_actors: return
        try:
            renderer = self.vtk_widget.renderer
            for dxf_data in self.dxf_actors:
                for actor in dxf_data.get('actors', []):
                    if renderer.HasViewProp(actor): renderer.RemoveActor(actor)
                    renderer.AddActor(actor)
                   
                    # 🛡️ THE LOD SHIELD: Force 100% render time allocation
                    # This prevents the LOD manager from hiding the lines at zoom-out
                    actor.SetAllocatedRenderTime(1000.0, renderer)
                   
                    prop = actor.GetProperty()
                    prop.SetLineWidth(5.0)     # Thick enough for UTM scale
                    prop.SetOpacity(0.999)     # Translucent Pass hack
                    prop.SetLighting(False)    # Glow effect
                   
                    mapper = actor.GetMapper()
                    if mapper:
                        mapper.SetResolveCoincidentTopologyToPolygonOffset()
                        # Massive offset to punch through 20M points
                        mapper.SetRelativeCoincidentTopologyLineOffsetParameters(-50000.0, -50000.0)
           
            renderer.ResetCameraClippingRange()
            self.vtk_widget.render()
            print("✅ SNT/DXF Shielded and Prioritized.")
        except Exception as e: print(f"⚠️ Sync failed: {e}")
 
    def deactivate_classification_tool(self, preserve_cross_section=False):
 
        """
        Properly deactivate classification and unlock all views.
        Call this when user closes class picker or cancels classification.
        """
        print(f"\n{'='*60}")
        print(f"🛑 DEACTIVATING CLASSIFICATION TOOL")
        print(f"{'='*60}")
 
        try:
            from . import session_manager
            if hasattr(session_manager, "APP_BUSY"):
                session_manager.APP_BUSY = False
        except Exception:
            pass
       
        # Clear tool
        self.active_classify_tool = None
       
        # ✅ CRITICAL: Unlock main view
        self.skip_main_view_refresh = False
        print("   🔓 Main view refresh UNLOCKED")
       
        # Close class picker
        if self._get_live_class_picker() is not None:
            self._hide_class_picker_safely()
            print("   ✅ Class picker closed")
       
        # Restore section view interactors
        if hasattr(self, "section_controller"):
            try:
                self.section_controller.unlock_after_classification()
                print("   ✅ Section controller unlocked")
            except Exception:
                pass
       
        if hasattr(self, "cut_section_controller"):
            try:
                self.cut_section_controller.unlock_after_classification()
                print("   ✅ Cut section controller unlocked")
            except Exception:
                pass
 
        # section_controller.unlock_after_classification() already restored interactors
        # and added right-click observers. Just clean up the wrappers here.
        try:
            if hasattr(self, "classify_interactors"):
                for ci in self.classify_interactors.values():
                    try: ci.cleanup()
                    except Exception: pass
                self.classify_interactors.clear()
        except Exception as e:
            print(f"⚠️ Failed to clear classification interactors: {e}")

        # Restore the main view interactor and clear any main/cut classification wrappers.
        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            if hasattr(self, "classify_interactor") and self.classify_interactor:
                try:
                    if hasattr(self.classify_interactor, "cleanup"):
                        self.classify_interactor.cleanup()
                except Exception:
                    pass
                self.classify_interactor = None
                print("   ✅ Main classification interactor cleared")

            if hasattr(self, "cut_classify_interactor") and self.cut_classify_interactor:
                try:
                    if hasattr(self.cut_classify_interactor, "cleanup"):
                        self.cut_classify_interactor.cleanup()
                except Exception:
                    pass
                self.cut_classify_interactor = None
                print("   ✅ Cut classification interactor cleared")

            if hasattr(self, "vtk_widget") and self.vtk_widget and hasattr(self.vtk_widget, "interactor"):
                self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleImage())
                print("   ✅ Main view interactor restored")
        except Exception as e:
            print(f"⚠️ Failed to restore main interactor after classification: {e}")
       
      
        # ✅ BETTER: Check if cross-section button is checked, not if interactor exists
        if (not preserve_cross_section) and hasattr(self, 'cross_action') and self.cross_action is not None and self.cross_action.isChecked():
            print("   🛑 Also deactivating cross-section mode")
           
            try:
                # Clean up cross-section visuals
                if hasattr(self, 'section_controller'):
                    self.section_controller.clear()
               
                # Uncheck cross-section button (this triggers its deactivation handler)
                self.cross_action.setChecked(False)
               
                # Clear any lingering references
                if hasattr(self, 'cross_interactor'):
                    self.cross_interactor = None
                if hasattr(self, 'cross_section_active'):
                    self.cross_section_active = False
               
                # Restore main view interactor
                from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
                self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleImage())
               
                self.vtk_widget.render()
                print("   ✅ Cross-section mode deactivated")
            except Exception as e:
                print(f"   ⚠️ Error deactivating cross-section: {e}")
 
        elif preserve_cross_section and hasattr(self, 'cross_action') and self.cross_action is not None and self.cross_action.isChecked():
            print("   ℹ️ Preserving cross-section mode while deactivating classification")
       
        print(f"{'='*60}")
        print(f"✅ Classification tool fully deactivated - all views unlocked")
        print(f"{'='*60}\n")

        # Make sure no stale refresh-suppression state survives tool teardown.
        self.skip_main_view_refresh = False
        try:
            if hasattr(self, "_gpu_sync_done"):
                self._gpu_sync_done = False
            if hasattr(self, "_section_visibility_refresh_required"):
                self._section_visibility_refresh_required = False
        except Exception:
            pass

        # A final flush keeps the main view from remaining visually stale after
        # a long classification session or shortcut-driven tool swap.
        try:
            if hasattr(self, "vtk_widget") and self.vtk_widget is not None:
                self.vtk_widget.render()
        except Exception:
            pass
       
        self.statusBar().showMessage("✅ Classification tool deactivated", 2000)
                    
    def _refresh_view(self, include_overlays=False, fast_color_only=False):
        """
        🚀 SENIOR REFACTOR: Smart Redraw.
        If fast_color_only is True, we skip the 'clear()' and 'add_mesh' pipeline entirely.
        """
        # --- NEW FAST PATH ---
        if fast_color_only:
            # We don't clear. We don't rebuild. 
            # We just tell the existing actor to update its colors.
            from .classification_tools import update_main_view_scalars_direct
            update_main_view_scalars_direct(self, np.arange(len(self.data["classification"])))
            return

        # --- SLOW PATH (Only for initial load or toggle overlays) ---
        print("⚠️ Performing full view refresh (Slow Path)")
        self.vtk_widget.clear()

        # main dataset
        if self.data and "xyz" in self.data and self.data["xyz"] is not None:
            # Ensure update_pointcloud names the actor 'main_points_cloud'
            update_pointcloud(self, self.display_mode)

        if include_overlays and self.data and "overlays" in self.data:
            # ... [Your existing DXF logic] ...
            pass

        self.vtk_widget.render()

    # --- Status Bar ---
    def _classified_point_count(self):
        """Return a cached count of points outside LAS class 0 (Unclassified)."""
        data = getattr(self, "data", None)
        classification = data.get("classification") if isinstance(data, dict) else None
        if classification is None:
            self._classified_count_cache = 0
            self._classified_count_data_id = None
            return 0

        data_id = id(classification)
        if getattr(self, "_classified_count_data_id", None) != data_id:
            self._classified_count_cache = int(np.count_nonzero(np.asarray(classification) != 0))
            self._classified_count_data_id = data_id
        return int(getattr(self, "_classified_count_cache", 0))

    def _apply_classified_point_count_delta(self, changed_mask, old_classes=None):
        """Update the cached header count using only points changed by an edit."""
        data = getattr(self, "data", None)
        classification = data.get("classification") if isinstance(data, dict) else None
        if classification is None or changed_mask is None:
            return False
        self._classified_point_count()
        undo_stack = getattr(self, "undo_stack", None) or []
        step = undo_stack[-1] if undo_stack else {}
        changed_indices = step.get("indices")
        changed = np.asarray(changed_mask)
        if changed_indices is not None:
            changed_indices = np.asarray(changed_indices, dtype=np.int64).ravel()
            new_classes = np.asarray(classification)[changed_indices]
        elif changed.dtype == bool:
            new_classes = np.asarray(classification)[changed]
        else:
            new_classes = np.asarray(classification)[changed.astype(np.int64, copy=False).ravel()]
        if new_classes.size == 0:
            return False
        if old_classes is None:
            old_classes = step.get("old_classes")
            if old_classes is None:
                old_classes = step.get("oldclasses")
        if old_classes is None:
            return False
        old_classes = np.asarray(old_classes).ravel()
        new_classes = np.asarray(new_classes).ravel()
        if old_classes.size != new_classes.size:
            return False
        delta = int(np.count_nonzero(new_classes)) - int(np.count_nonzero(old_classes))
        total = len(classification)
        self._classified_count_cache = max(0, min(total, self._classified_point_count() + delta))
        return True

    def _update_classify_count_display(self, count):
        """Show the exact count of points being classified in the top bar center.

        The label appears at the exact middle of the tab bar while a
        classification operation runs, then auto-hides after 3 seconds.
        """
        label = getattr(self, "_classify_count_label", None)
        if label is None:
            return
        try:
            count = int(count)
        except (TypeError, ValueError):
            count = 0
        if count > 0:
            label.setText(f"Classifying: {count:,} points")
            label.show()
            timer = getattr(self, "_classify_count_timer", None)
            if timer is not None:
                timer.start()  # restart the 3s auto-hide countdown
        else:
            label.hide()

    def _refresh_window_title_classification_count(self):
        """Refresh the title from the cached count without scanning the dataset."""
        filename = getattr(self, "_window_title_filename", None)
        if filename:
            self._update_window_title(filename, getattr(self, "_window_title_crs_epsg", None))
    def _update_window_title(self, filename, crs_epsg):
        """Update the title with file, total/classified point counts, and CRS."""
        self.update_epsg_display()
        base_title = "NakshaAI-Lidar"

        if not filename:
            self._window_title_filename = None
            self._window_title_crs_epsg = None
            self.setWindowTitle(base_title)
            return

        self._window_title_filename = filename
        self._window_title_crs_epsg = crs_epsg
        file_label = os.path.basename(filename)
        data = getattr(self, "data", None)
        xyz = data.get("xyz") if isinstance(data, dict) else None
        total_points = len(xyz) if xyz is not None else 0
        classified_points = self._classified_point_count()
        if total_points and " pts)" not in file_label:
            file_label = f"{file_label} ({total_points:,} pts)"
        if total_points:
            file_label = f"{file_label} | Classified: {classified_points:,}"

        if crs_epsg:
            try:
                crs = CRS.from_epsg(crs_epsg)
                crs_name = crs.to_authority() or crs.to_string()
                self.setWindowTitle(
                    f"{base_title} - {file_label} | EPSG:{crs_epsg} ({crs_name})"
                )
            except Exception:
                self.setWindowTitle(
                    f"{base_title} - {file_label} | EPSG:{crs_epsg}"
                )
        else:
            self.setWindowTitle(
                f"{base_title} - {file_label} | CRS: Unknown"
            )
    def _force_main_view_refresh_after_undo(self, changed_mask):
        """
        ✅ NEW METHOD: Guaranteed main view refresh after undo/redo
        
        This method ensures the main view ALWAYS updates, regardless of 
        cross-section state or other factors.
        """
        print(f"\n{'='*60}")
        print(f"🔄 FORCING MAIN VIEW REFRESH AFTER UNDO/REDO")
        print(f"{'='*60}")
        
        try:
            import numpy as np
            
            # Save camera position
            saved_camera = None
            if hasattr(self, 'vtk_widget') and self.vtk_widget and self.vtk_widget.renderer:
                try:
                    camera = self.vtk_widget.renderer.GetActiveCamera()
                    if camera:
                        saved_camera = {
                            'position': tuple(camera.GetPosition()),
                            'focal_point': tuple(camera.GetFocalPoint()),
                            'view_up': tuple(camera.GetViewUp()),
                            'parallel_scale': camera.GetParallelScale(),
                            'parallel_projection': camera.GetParallelProjection(),
                        }
                        print(f"   📷 Camera saved")
                except Exception as e:
                    print(f"   ⚠️ Camera save failed: {e}")
            
            # Get affected classes
            affected_classes = set()
            
            if changed_mask is not None:
                classes = self.data["classification"]
                affected_classes = set(np.unique(classes[changed_mask]).tolist())
            
            # Also check undo stack for old classes
            if hasattr(self, 'undo_stack') and self.undo_stack:
                last_undo = self.undo_stack[-1]
                if 'old_classes' in last_undo:
                    affected_classes.update(np.unique(last_undo['old_classes']).tolist())
                if 'new_classes' in last_undo:
                    affected_classes.update(np.unique(last_undo['new_classes']).tolist())
            
            print(f"   📊 Affected classes: {sorted(affected_classes)}")
            
            if len(affected_classes) == 0:
                print(f"   ⚠️ No affected classes detected")
                print(f"{'='*60}\n")
                return
            
            # ✅ STRATEGY: Remove and re-add ONLY affected class actors
            if self.display_mode == "class":
                print(f"   🎨 Class mode - updating {len(affected_classes)} classes")
                
                try:
                    from gui.class_display import update_class_mode
                    
                    # Simple approach: Full refresh (most reliable)
                    update_class_mode(self)
                    print(f"   ✅ Full refresh complete")
                    
                except Exception as e:
                    print(f"   ❌ Refresh failed: {e}")
                    import traceback
                    traceback.print_exc()
            
            elif self.display_mode == "shaded_class":
                print(f"   🌗 Shaded mode - full rebuild required")
                
                try:
                    from gui.shading_display import update_shaded_class
                    
                    azimuth = getattr(self, "last_shade_azimuth", 45.0)
                    angle = getattr(self, "shading_sharpness_angle", 45.0)
                    ambient = getattr(self, "shade_ambient", 0.2)
                    
                    update_shaded_class(self, azimuth, angle, ambient)
                    print(f"   ✅ Shaded mode refreshed")
                    
                except Exception as e:
                    print(f"   ❌ Shaded refresh failed: {e}")
            
            else:
                print(f"   📊 {self.display_mode} mode - standard refresh")
                
                try:
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(self, self.display_mode)
                    print(f"   ✅ Refreshed")
                    
                except Exception as e:
                    print(f"   ❌ Refresh failed: {e}")
            
            # Restore camera
            if saved_camera and hasattr(self, 'vtk_widget') and self.vtk_widget.renderer:
                try:
                    camera = self.vtk_widget.renderer.GetActiveCamera()
                    camera.SetPosition(saved_camera['position'])
                    camera.SetFocalPoint(saved_camera['focal_point'])
                    camera.SetViewUp(saved_camera['view_up'])
                    camera.SetParallelScale(saved_camera['parallel_scale'])
                    
                    if saved_camera['parallel_projection']:
                        camera.ParallelProjectionOn()
                    
                    self.vtk_widget.renderer.ResetCameraClippingRange()
                    self.vtk_widget.render()
                    
                    print(f"   📷 Camera restored")
                except Exception as e:
                    print(f"   ⚠️ Camera restore failed: {e}")
            
            # Force immediate render
            try:
                if hasattr(self, 'vtk_widget'):
                    self.vtk_widget.GetRenderWindow().Render()
                    print(f"   🎨 Forced render complete")
            except Exception as e:
                print(f"   ⚠️ Render warning: {e}")
            
            # Also refresh cross-sections if they exist
            if hasattr(self, 'section_vtks') and self.section_vtks:
                print(f"\n   🔄 Refreshing cross-sections...")
                for view_idx in sorted(self.section_vtks.keys()):
                    try:
                        # Get section data
                        pts = getattr(self, f'section_{view_idx}_core_points', None)
                        if pts is not None:
                            # Trigger refresh
                            if hasattr(self, 'section_controller'):
                                self.section_controller.active_view = view_idx
                                # Simple color refresh
                                vtk_widget = self.section_vtks[view_idx]
                                if vtk_widget:
                                    vtk_widget.render()
                            print(f"      ✅ View {view_idx + 1} refreshed")
                    except Exception as e:
                        print(f"      ⚠️ View {view_idx + 1} error: {e}")
            
            print(f"\n{'='*60}")
            print(f"✅ MAIN VIEW REFRESH COMPLETE")
            print(f"{'='*60}\n")
            
        except Exception as e:
            print(f"\n❌ CRITICAL ERROR IN MAIN VIEW REFRESH: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")
            
    def _smart_refresh_after_undo_redo(self, changed_mask, target_classes, operation="Update", num_points=0):
                """
                ✅ SMART REFRESH: Update ONLY affected classes without blinking
                ✅ FIXED: Border shader preserved - only classification data reverted
                
                Strategy:
                1. Identify which classes were affected (old + new)
                2. Remove ONLY class actors (preserve DXF, border state, and other overlays)
                3. Re-add ALL visible classes with updated points
                4. Apply border shader ONLY if border was previously enabled
                5. Camera stays locked - no rebuild - NO BLINK
                """
                print(f"\n{'='*60}")
                print(f"{operation} - Processing {num_points:,} points (SMART REFRESH)")
                print(f"{'='*60}")
                
                try:
                    import numpy as np
                    import pyvista as pv
                    
                    # ========================================================================
                    # STEP 1: Identify affected classes (both old and new)
                    # ========================================================================
                    affected_classes = set()
                    
                    # Add target classes (what points became)
                    if isinstance(target_classes, np.ndarray):
                        affected_classes.update(np.unique(target_classes).tolist())
                    elif isinstance(target_classes, (int, np.integer)):
                        affected_classes.add(int(target_classes))
                    
                    # Add old classes from undo stack (what points were before)
                    if hasattr(self, 'undo_stack') and self.undo_stack:
                        last_step = self.undo_stack[-1]
                        old_cls = last_step.get('old_classes')
                        if isinstance(old_cls, np.ndarray):
                            affected_classes.update(np.unique(old_cls).tolist())
                    
                    # Add current classes in the changed area (belt and suspenders)
                    current_classes = self.data["classification"][changed_mask]
                    affected_classes.update(np.unique(current_classes).tolist())
                    
                    affected_classes = sorted(list(affected_classes))
                    print(f"   📊 Affected classes: {affected_classes}")
                    
                    if len(affected_classes) == 0:
                        print(f"   ⚠️ No affected classes detected")
                        return
                    
                    # ========================================================================
                    # STEP 2: Save camera position and border state
                    # ========================================================================
                    saved_camera = None
                    border_percent = getattr(self, "point_border_percent", 0)
                    border_enabled = border_percent > 0.0
                    
                    if hasattr(self, 'vtk_widget') and self.vtk_widget and self.vtk_widget.renderer:
                        try:
                            camera = self.vtk_widget.renderer.GetActiveCamera()
                            if camera:
                                saved_camera = {
                                    'position': tuple(camera.GetPosition()),
                                    'focal_point': tuple(camera.GetFocalPoint()),
                                    'view_up': tuple(camera.GetViewUp()),
                                    'parallel_scale': camera.GetParallelScale(),
                                    'parallel_projection': camera.GetParallelProjection(),
                                    'view_angle': camera.GetViewAngle(),
                                    'clipping_range': camera.GetClippingRange()
                                }
                                print(f"   📷 Camera saved")
                                print(f"   🔳 Border state: {'ENABLED' if border_enabled else 'DISABLED'} ({border_percent}%)")
                        except Exception as e:
                            print(f"   ⚠️ Camera save failed: {e}")
                    
                    # ========================================================================
                    # STEP 3: Remove ONLY class actors (preserve border shader state)
                    # ========================================================================
                    if not hasattr(self, 'vtk_widget'):
                        return

                    plotter = self.vtk_widget
                    actors_removed = 0

                    # Remove ONLY class actors, preserve everything else
                    if hasattr(plotter, 'actors'):
                        all_actor_names = list(plotter.actors.keys())
                        
                        for actor_name in all_actor_names:
                            actor_str = str(actor_name)
                            if actor_str.startswith("class_"):
                                try:
                                    plotter.remove_actor(actor_name, render=False)
                                    actors_removed += 1
                                except Exception:
                                    pass

                    print(f"   🗑️ Removed {actors_removed} class actors (preserved border shaders and DXF)")
                
                    # ========================================================================
                    # STEP 4: Re-add ALL visible classes with border shader if enabled
                    # ========================================================================
                    palette = getattr(self, "class_palette", {}) or {}
                    xyz = self.data["xyz"]
                    classes = self.data["classification"]

                    # Get all visible classes
                    visible_classes = [c for c, info in palette.items() if info.get("show", True)]
                    visible_classes.sort()

                    actors_added = 0

                    # Calculate ring for shader if border is enabled
                    ring = 0.0
                    if border_enabled:
                        ring = min(0.45, max(0.0, border_percent / 60.0))

                    # Helper function to apply border shader
                    def _apply_border_shader_to_actor(actor):
                        if not border_enabled:
                            return
                        try:
                            sp = actor.GetShaderProperty()
                            code = f"""
    //VTK::Color::Impl
    // ---- Naksha Round Point + Border Ring ----
    vec2 naksha_uv = gl_PointCoord.xy - vec2(0.5);
    float naksha_radius_sq = dot(naksha_uv, naksha_uv);
    if (naksha_radius_sq > 0.25) {{
        discard;
    }}

    float ring = {ring:.6f};
    if (ring > 0.0) {{
        float edge_radius = 0.5 * (1.0 - ring);
        if (naksha_radius_sq >= edge_radius * edge_radius) {{
            diffuseColor = vec3(0.0, 0.0, 0.0);
            ambientColor = vec3(0.0, 0.0, 0.0);
            opacity = 1.0;
        }}
    }}
    // ---- End Naksha Round Point + Border Ring ----
    """
                            sp.AddFragmentShaderReplacement(
                                "//VTK::Color::Impl",
                                True,
                                code,
                                False
                            )
                            actor.Modified()
                        except Exception as e:
                            print(f"   ⚠️ Border shader attach failed: {e}")

                    for class_code in visible_classes:
                        try:
                            entry = palette.get(int(class_code), {})
                            
                            # Get ALL points for this class
                            class_mask = (classes == class_code)
                            class_pts = xyz[class_mask]
                            
                            if len(class_pts) == 0:
                                continue
                            
                            # Get visual properties
                            weight = float(entry.get("weight", 1.0))
                            weight = max(0.1, min(weight, 12.0))
                            point_size = max(1.5, weight * 3.0)
                            point_size = min(point_size, 30.0)
                            
                            color = np.array(entry.get("color", (128, 128, 128)), dtype=np.uint8)
                            colors = np.tile(color, (len(class_pts), 1))
                            
                            # Create point cloud
                            cloud = pv.PolyData(class_pts)
                            cloud["RGB"] = colors
                            
                            # Add main actor
                            actor = plotter.add_points(
                                cloud,
                                scalars="RGB",
                                rgb=True,
                                point_size=point_size,
                                render_points_as_spheres=True,
                                name=f"class_{class_code}",
                                reset_camera=False,
                                render=False
                            )
                            actors_added += 1
                            
                            # Apply border shader if enabled
                            _apply_border_shader_to_actor(actor)
                            
                            # Only print affected classes
                            if class_code in affected_classes:
                                print(f"   ✅ Class {class_code}: {len(class_pts):,} points (weight={weight:.2f})")
                            
                        except Exception as e:
                            print(f"   ❌ Class {class_code} failed: {e}")
                            continue

                    print(f"   🎯 Added {actors_added} class actors for {len(visible_classes)} visible classes")
                
                    # ========================================================================
                    # STEP 5: Restore camera and render ONCE
                    # ========================================================================
                    if saved_camera:
                        try:
                            camera = self.vtk_widget.renderer.GetActiveCamera()
                            camera.SetPosition(saved_camera['position'])
                            camera.SetFocalPoint(saved_camera['focal_point'])
                            camera.SetViewUp(saved_camera['view_up'])
                            camera.SetParallelScale(saved_camera['parallel_scale'])
                            camera.SetViewAngle(saved_camera['view_angle'])
                            camera.SetClippingRange(saved_camera['clipping_range'])
                            
                            if saved_camera['parallel_projection']:
                                camera.ParallelProjectionOn()
                            else:
                                camera.ParallelProjectionOff()
                            
                            self.vtk_widget.renderer.ResetCameraClippingRange()
                            print(f"   📷 Camera restored")
                        except Exception as e:
                            print(f"   ⚠️ Camera restore warning: {e}")
                    
                    # Single render call (no flicker!)
                    self.vtk_widget.render()
                    print(f"   🎨 Single render complete (NO BLINK)")
                    
                    # ========================================================================
                    # STEP 6: Update cross-sections if active
                    # ========================================================================
                    if hasattr(self, 'section_vtks') and self.section_vtks:
                        print(f"\n   🔄 Refreshing {len(self.section_vtks)} cross-section view(s)...")
                        
                        # Check if we have the classification interactor
                        if hasattr(self, 'classify_interactors'):
                            for view_idx in sorted(self.section_vtks.keys()):
                                try:
                                    # Check if interactor exists for this view
                                    if view_idx in self.classify_interactors:
                                        interactor = self.classify_interactors[view_idx]
                                        
                                        # Use the interactor's refresh method
                                        if hasattr(interactor, '_refresh_single_view'):
                                            interactor._refresh_single_view(view_idx)
                                            print(f"      ✅ View {view_idx + 1}: Refreshed")
                                        else:
                                            print(f"      ⚠️ View {view_idx + 1}: No refresh method")
                                    else:
                                        print(f"      ⏭️ View {view_idx + 1}: No interactor")
                                except Exception as e:
                                    print(f"      ⚠️ View {view_idx + 1} refresh failed: {e}")
                        else:
                            print(f"      ⚠️ No classify_interactors found")
                    
                    # ========================================================================
                    # STEP 7: Update statistics
                    # ========================================================================
                    if hasattr(self, 'point_count_widget') and self.point_count_widget:
                        from gui.point_count_widget import refresh_point_statistics
                        refresh_point_statistics(self)
                    
                    # Status feedback
                    if hasattr(self, 'statusBar'):
                        self.statusBar().showMessage(f"{operation}: {num_points:,} points", 2000)
                    
                    print(f"{'='*60}")
                    print(f"✅ {operation} COMPLETE - NO BLINK")
                    print(f"{'='*60}\n")
                    
                except Exception as e:
                    print(f"\n❌ SMART REFRESH ERROR: {e}")
                    import traceback
                    traceback.print_exc()
                    print(f"{'='*60}\n")

    def _undo_refresh_main_view_fast(self, affected_classes, changed_mask):  #####
        """
        Fast undo/redo refresh: rebuild ONLY affected class actors.
        Skips untouched classes entirely.
        """
        import numpy as np
        import pyvista as pv

        palette = getattr(self, 'class_palette', {})
        classifications = self.data['classification']
        plotter = self.vtk_widget

        # Save camera
        cam_state = None
        try:
            cam = plotter.renderer.GetActiveCamera()
            cam_state = {
                "pos": cam.GetPosition(), "fp": cam.GetFocalPoint(),
                "up": cam.GetViewUp(), "ps": cam.GetParallelScale(),
                "pp": cam.GetParallelProjection(), "va": cam.GetViewAngle(),
            }
        except Exception:
            pass

        for cls_id in affected_classes:
            actor_name = f'class_{cls_id}'
            
            # Remove old actor for this class
            if actor_name in plotter.actors:
                plotter.remove_actor(actor_name, render=False)

            # Get current points for this class
            mask = (classifications == cls_id)
            pts = self.data['xyz'][mask]
            
            if len(pts) == 0:
                continue  # Class now empty — actor removed, done

            info = palette.get(cls_id, {})
            color = info.get('color', (128, 128, 128))
            weight = float(info.get('weight', 1.0))
            point_size = max(1.5, min(2.5 * weight, 30.0))

            cloud = pv.PolyData(pts)
            cloud["RGB"] = np.tile(np.array(color, dtype=np.uint8), (len(pts), 1))

            actor = plotter.add_points(
                cloud, scalars="RGB", rgb=True,
                point_size=point_size,
                render_points_as_spheres=True,
                name=actor_name,
                reset_camera=False,
                render=False
            )
            actor.GetProperty().LightingOff()

            # Re-apply border shader if active
            border_percent = float(getattr(self, 'point_border_percent', 0) or 0.0)
            if border_percent > 0:
                try:
                    from gui.class_display import apply_border_shader_ring
                    apply_border_shader_ring(actor, border_percent)
                except Exception:
                    pass

        # Restore camera
        if cam_state:
            try:
                cam = plotter.renderer.GetActiveCamera()
                cam.SetPosition(*cam_state["pos"])
                cam.SetFocalPoint(*cam_state["fp"])
                cam.SetViewUp(*cam_state["up"])
                if cam_state["pp"]:
                    cam.ParallelProjectionOn()
                    cam.SetParallelScale(cam_state["ps"])
                else:
                    cam.ParallelProjectionOff()
                    cam.SetViewAngle(cam_state["va"])
                plotter.renderer.ResetCameraClippingRange()
            except Exception:
                pass

        plotter.render()



    def undo_classification(self):
        if not self.undo_stack: return
        step = self.undo_stack.pop()
        mask = step.get('mask')
        if mask is None:
            indices = step.get('indices')
            if indices is not None and hasattr(self, "data") and "classification" in self.data:
                import numpy as np
                mask = np.zeros(len(self.data["classification"]), dtype=bool)
                idx = np.asarray(indices, dtype=np.int64).ravel()
                if idx.size > 0:
                    idx = idx[(idx >= 0) & (idx < mask.shape[0])]
                    if idx.size > 0:
                        mask[idx] = True
        if mask is None or not mask.any(): return

        old_cls = step.get('old_classes')
        if old_cls is None: return

        # 1. Revert CPU RAM
        classes_before_undo = self.data["classification"][mask].copy()
        self.data["classification"][mask] = old_cls
        if self._apply_classified_point_count_delta(mask, classes_before_undo):
            self._refresh_window_title_classification_count()
        self.redo_stack.append(step)
        
        try:
            from gui.memory_manager import current_recommended_undo_steps
            max_steps = int(current_recommended_undo_steps(self))
        except Exception:
            max_steps = int(getattr(self, '_max_undo_steps', 30))
        while len(self.redo_stack) > max_steps:
            from gui.memory_manager import _free_undo_entry
            _free_undo_entry(self.redo_stack.pop(0))

        # 2. Main View refresh
        current_display_mode = str(getattr(self, "display_mode", "") or "").lower()

        if current_display_mode == "surface":
            try:
                self.classification_revision = int(getattr(self, "classification_revision", 0) or 0) + 1
                self.refresh_surface_after_classification(reason="undo", changed_mask=mask)
            except Exception as _surface_undo_err:
                print(f"Surface undo refresh failed: {_surface_undo_err}")
        else:
            from gui.unified_actor_manager import fast_undo_update
            try:
                fast_undo_update(self, changed_mask=mask)
            except Exception as _undo_refresh_err:
                # CPU classification is already reverted and the redo entry is
                # installed. A stale/L0D view buffer must not invalidate undo.
                print(f"⚠️ Undo main-view refresh skipped: {_undo_refresh_err}")

        # 3. ⚡ CRITICAL FIX: Invalidate ALL section view mirrors
        #    so they are rebuilt from fresh self.data["classification"]
        #    instead of stale pre-undo GPU cache
        if hasattr(self, 'section_vtks') and self.section_vtks:
            for view_idx, vtk_widget in self.section_vtks.items():
                try:
                    # Wipe the stale mirror so fast_cross_section_update
                    # is forced to rebuild from self.data["classification"]
                    actor = getattr(vtk_widget, '_naksha_unified_actor', None)
                    if actor is not None:
                        mirror = getattr(actor, '_naksha_section_class', None)
                        if mirror is not None:
                            # Re-read from ground truth classification for THIS view's points
                            section_indices = getattr(self, f'_section_{view_idx}_global_indices', None)
                            if section_indices is not None and len(section_indices) > 0:
                                import numpy as np
                                actor._naksha_section_class = (
                                    self.data["classification"][section_indices].copy()
                                )
                            else:
                                # No index map yet → mark mirror as invalid
                                actor._naksha_section_class = None
                except Exception as e:
                    print(f"⚠️ Mirror invalidation for view {view_idx}: {e}")

            # Now do the fast GPU poke with clean mirrors
            from gui.unified_actor_manager import fast_cross_section_update, build_section_unified_actor
            for view_idx in self.section_vtks.keys():
                slot_idx = view_idx + 1
                palette = self._get_cross_section_palette(slot_idx)
                ok = fast_cross_section_update(
                    self,
                    view_idx,
                    mask,
                    palette=palette,
                    force_visibility_refresh=False,
                )
                if not ok:
                    border = float(getattr(self, "view_borders", {}).get(slot_idx, 0) or 0.0)
                    view_mode = getattr(self, "cross_view_mode", "front")
                    rebuilt = build_section_unified_actor(
                        self,
                        view_idx,
                        palette=palette,
                        border_percent=border,
                        view=view_mode,
                    )
                    if rebuilt is not None:
                        fast_cross_section_update(
                            self,
                            view_idx,
                            mask,
                            palette=palette,
                            force_visibility_refresh=False,
                        )

        # 4. ⚡ CRITICAL FIX: Refresh Cut Section (Slot 5) if active
        if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
            ctrl = self.cut_section_controller
            if getattr(ctrl, 'is_cut_view_active', False):
                try:
                    print("   🔪 Refreshing Cut Section (Slot 5) after Undo")
                    ctrl._refresh_cut_colors_fast()
                except Exception as e:
                    print(f"   ⚠️ Cut section refresh failed: {e}")

        # 5. Refresh shaded mesh if in shaded mode
        if getattr(self, "display_mode", None) == "shaded_class":
            try:
                from gui.shading_display import refresh_shaded_after_history_fast
                refreshed = refresh_shaded_after_history_fast(
                    self,
                    mask,
                    classes_before_undo,
                    old_cls,
                    "undo",
                )
                if not refreshed:
                    raise RuntimeError("undo shading fast path declined")
            except Exception as _se:
                print(f"SHADING_HISTORY_FAST status=failed operation=undo error={_se}")
                try:
                    from gui.shading_display import refresh_shaded_after_undo_fast
                    if not refresh_shaded_after_undo_fast(self, mask):
                        raise RuntimeError("legacy undo shading path declined")
                except Exception:
                    from gui.shading_display import update_shaded_class, clear_shading_cache
                    clear_shading_cache("undo topology fallback")
                    update_shaded_class(self, force_rebuild=True)

        self._last_changed_mask = None
        self._last_changed_indices = None
        self._gpu_sync_done = False
        # Surface refresh owns its own presentation.  A metadata-only Surface
        # undo needs no 12M/50M-face redraw, while a true rebuild already rendered.
        if str(getattr(self, "display_mode", "") or "").lower() != "surface":
            self.vtk_widget.render()

    def redo_classification(self):
        """🚀 MICROSTATION REDO: Instant GPU Forward-Patch"""
        if not self.redo_stack: return
        step = self.redo_stack.pop()
        mask = step.get('mask')
        if mask is None:
            indices = step.get('indices')
            if indices is not None and hasattr(self, "data") and "classification" in self.data:
                import numpy as np
                mask = np.zeros(len(self.data["classification"]), dtype=bool)
                idx = np.asarray(indices, dtype=np.int64).ravel()
                if idx.size > 0:
                    idx = idx[(idx >= 0) & (idx < mask.shape[0])]
                    if idx.size > 0:
                        mask[idx] = True
        if mask is None or not mask.any(): return

        new_cls = step.get('new_classes')
        if new_cls is None: return

        # 1. Update RAM
        classes_before_redo = self.data["classification"][mask].copy()
        self.data["classification"][mask] = new_cls
        self.undo_stack.append(step)
        try:
            from gui.memory_manager import current_recommended_undo_steps
            max_steps = int(current_recommended_undo_steps(self))
        except Exception:
            max_steps = int(getattr(self, '_max_undo_steps', 30))
        while len(self.undo_stack) > max_steps:
            from gui.memory_manager import _free_undo_entry
            _free_undo_entry(self.undo_stack.pop(0))

        # 2. Main View refresh
        current_display_mode = str(getattr(self, "display_mode", "") or "").lower()

        if current_display_mode == "surface":
            try:
                self.classification_revision = int(getattr(self, "classification_revision", 0) or 0) + 1
                self.refresh_surface_after_classification(reason="redo", changed_mask=mask)
            except Exception as _surface_redo_err:
                print(f"Surface redo refresh failed: {_surface_redo_err}")
        else:
            from gui.unified_actor_manager import fast_undo_update
            fast_undo_update(self, changed_mask=mask)

        # 3. CRITICAL FIX: Invalidate section mirrors before cross-section update
        if hasattr(self, 'section_vtks') and self.section_vtks:
            for view_idx in self.section_vtks.keys():
                self._sync_section_mirror_from_data(view_idx)

        # 4. ⚡ CRITICAL FIX: Refresh Cut Section (Slot 5) if active
        if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
            ctrl = self.cut_section_controller
            if getattr(ctrl, 'is_cut_view_active', False):
                try:
                    print("   🔪 Refreshing Cut Section (Slot 5) after Redo")
                    ctrl._refresh_cut_colors_fast()
                except Exception as e:
                    print(f"   ⚠️ Cut section refresh failed: {e}")

        # 5. Refresh shaded mesh if needed
        if getattr(self, "display_mode", None) == "shaded_class":
            try:
                from gui.shading_display import refresh_shaded_after_history_fast
                refreshed = refresh_shaded_after_history_fast(
                    self,
                    mask,
                    classes_before_redo,
                    new_cls,
                    "redo",
                )
                if not refreshed:
                    raise RuntimeError("redo shading fast path declined")
            except Exception as _se:
                print(f"SHADING_HISTORY_FAST status=failed operation=redo error={_se}")
                try:
                    from gui.shading_display import refresh_shaded_after_undo_fast
                    if not refresh_shaded_after_undo_fast(self, mask):
                        raise RuntimeError("legacy redo shading path declined")
                except Exception:
                    from gui.shading_display import update_shaded_class, clear_shading_cache
                    clear_shading_cache("redo topology fallback")
                    update_shaded_class(self, force_rebuild=True)

        # 5. Emit signal (now mirrors are clean before _on_classification_finished runs)
        self.classification_finished.emit(mask)
        self._last_changed_mask = None
        self._last_changed_indices = None
        self._gpu_sync_done = False

        # Surface refresh owns presentation for both no-op and exact topology paths.
        if str(getattr(self, "display_mode", "") or "").lower() != "surface":
            self.vtk_widget.render()


    def _refresh_main_view_after_undo(self, affected_classes, changed_mask):
        """
        ✅ CRITICAL FIX: Rebuild main view actors for affected classes.
        
        This prevents void points by ensuring:
        1. Classes that LOST points have their actors removed if empty
        2. Classes that GAINED points have their actors rebuilt with new points
        """
        import pyvista as pv
        import numpy as np
        
        plotter = getattr(self, 'vtk_widget', None)
        if plotter is None:
            print(f"      ⚠️ No VTK widget found")
            return
        
        xyz = self.data["xyz"]
        classes = self.data["classification"]
        
        # Get Main View palette (Slot 0)
        palette = self._get_main_view_palette()
        
        print(f"      🔧 Rebuilding {len(affected_classes)} class actors...")
        
        # Save camera position
        saved_camera = None
        try:
            if hasattr(plotter, 'camera_position') and plotter.camera_position is not None:
                saved_camera = plotter.camera_position
        except Exception:
            pass
        
        # ════════════════════════════════════════════════════════════════════
        # Rebuild each affected class
        # ════════════════════════════════════════════════════════════════════
        for class_code in affected_classes:
            class_code = int(class_code)
            actor_name = f"class_{class_code}"
            
            # Check visibility in Main View palette
            info = palette.get(class_code, {})
            if not info.get("show", True):
                # Hidden class - remove actor if exists
                if actor_name in plotter.actors:
                    plotter.remove_actor(actor_name, render=False)
                    print(f"         🚫 Removed hidden class {class_code}")
                continue
            
            # Get all points for this class
            class_mask = (classes == class_code)
            class_pts = xyz[class_mask]
            
            # Remove old actor
            if actor_name in plotter.actors:
                plotter.remove_actor(actor_name, render=False)
            
            if len(class_pts) == 0:
                print(f"         ✂️ Class {class_code}: No points (removed)")
                continue
            
            # Get styling
            color = np.array(info.get("color", (128, 128, 128)), dtype=np.uint8)
            weight = float(info.get("weight", 1.0))
            point_size = max(1.0, min(weight * 2.5, 30.0))
            
            # Create new actor
            colors = np.tile(color, (len(class_pts), 1))
            cloud = pv.PolyData(class_pts)
            cloud["RGB"] = colors
            
            actor = plotter.add_points(
                cloud,
                scalars="RGB",
                rgb=True,
                point_size=point_size,
                render_points_as_spheres=True,
                name=actor_name,
                reset_camera=False,
                render=False
            )
            
            # Apply border shader if enabled
            border_percent = getattr(self, "point_border_percent", 0)
            if border_percent > 0:
                try:
                    from gui.class_display import apply_border_shader_ring
                    apply_border_shader_ring(actor, border_percent)
                except Exception:
                    pass
            
            print(f"         ✅ Rebuilt class {class_code}: {len(class_pts):,} points")
        
        # Restore camera
        if saved_camera is not None:
            try:
                plotter.camera_position = saved_camera
            except Exception:
                pass
        
        # Single render at the end
        plotter.render()
        print(f"      ✅ Main view refreshed")


    def _manual_refresh_cross_section(self, view_idx, affected_classes):
        """
        ✅ Manual fallback refresh for cross-section after undo.
        """
        import pyvista as pv
        import numpy as np
        
        if not hasattr(self, 'section_vtks') or view_idx not in self.section_vtks:
            print(f"      ⚠️ View {view_idx}: VTK widget not found")
            return
        
        vtk_widget = self.section_vtks[view_idx]
        slot_idx = view_idx + 1
        
        # Get section data
        core_pts = getattr(self, f'section_{view_idx}_core_points', None)
        core_mask = getattr(self, f'section_{view_idx}_core_mask', None)
        buffer_pts = getattr(self, f'section_{view_idx}_buffer_points', None)
        buffer_mask = getattr(self, f'section_{view_idx}_buffer_mask', None)
        
        if core_pts is None or core_mask is None:
            print(f"      ⏭️ View {view_idx}: No section data stored")
            return
        
        # Get palette for this view
        palette = self._get_cross_section_palette(slot_idx)
        visible = [c for c, v in palette.items() if v.get("show", False)]
        
        # ✅ CRITICAL: Get FRESH classification data from main array
        current_classes = self.data["classification"]
        
        # Verify we got the updated data
        if not isinstance(current_classes, np.ndarray):
            print(f"      ❌ View {view_idx}: Invalid classification data type")
            return
        
        # Extract section classifications using the stored mask
        try:
            section_classes = current_classes[core_mask]
        except Exception as e:
            print(f"      ❌ View {view_idx}: Failed to extract section classes: {e}")
            return
        
        print(f"      🔄 Manual refresh View {slot_idx}...")
        print(f"         Core points: {len(core_pts):,}")
        print(f"         Affected classes: {sorted(affected_classes)}")
        print(f"         Visible classes: {sorted(visible)}")
        
        # Save camera position
        saved_camera = None
        try:
            if hasattr(vtk_widget, 'camera_position'):
                saved_camera = vtk_widget.camera_position
        except Exception:
            pass
        
        # Disable interactor during update
        try:
            vtk_widget.iren.interactor.Disable()
        except Exception:
            pass
        
        try:
            # ════════════════════════════════════════════════════════════════
            # Rebuild CORE points for affected classes
            # ════════════════════════════════════════════════════════════════
            for cls_val in affected_classes:
                cls_val = int(cls_val)
                actor_name = f"class_{cls_val}"
                
                # Check visibility
                if cls_val not in visible:
                    if actor_name in vtk_widget.actors:
                        vtk_widget.remove_actor(actor_name, render=False)
                        print(f"         🚫 Removed hidden class {cls_val}")
                    continue
                
                # Get points for this class
                cls_mask = (section_classes == cls_val)
                cls_pts = core_pts[cls_mask]
                
                # Remove old actor
                if actor_name in vtk_widget.actors:
                    vtk_widget.remove_actor(actor_name, render=False)
                
                if len(cls_pts) == 0:
                    print(f"         ✂️ Class {cls_val}: 0 points (removed)")
                    continue
                
                # Get styling
                entry = palette.get(cls_val, {})
                color = tuple(entry.get("color", (128, 128, 128)))
                weight = float(entry.get("weight", 1.0))
                point_size = max(1.0, min(10.0, 5.0 * weight))
                
                # Create new actor
                colors = np.array([color] * len(cls_pts), dtype=np.uint8)
                cloud = pv.PolyData(cls_pts)
                cloud["RGB"] = colors
                
                vtk_widget.add_points(
                    cloud,
                    scalars="RGB",
                    rgb=True,
                    point_size=point_size,
                    render_points_as_spheres=True,
                    reset_camera=False,
                    name=actor_name,
                    render=False
                )
                
                print(f"         ✅ Class {cls_val}: {len(cls_pts):,} points rebuilt")
            
            # ════════════════════════════════════════════════════════════════
            # Rebuild BUFFER points for affected classes
            # ════════════════════════════════════════════════════════════════
            if buffer_pts is not None and buffer_mask is not None and len(buffer_pts) > 0:
                try:
                    buffer_classes = current_classes[buffer_mask]
                    
                    for cls_val in affected_classes:
                        cls_val = int(cls_val)
                        buffer_name = f"buffer_{cls_val}"
                        
                        # Check visibility
                        if cls_val not in visible:
                            if buffer_name in vtk_widget.actors:
                                vtk_widget.remove_actor(buffer_name, render=False)
                            continue
                        
                        # Get buffer points for this class
                        cls_mask = (buffer_classes == cls_val)
                        cls_pts = buffer_pts[cls_mask]
                        
                        # Remove old buffer actor
                        if buffer_name in vtk_widget.actors:
                            vtk_widget.remove_actor(buffer_name, render=False)
                        
                        if len(cls_pts) == 0:
                            continue
                        
                        # Get styling (buffer uses smaller points)
                        entry = palette.get(cls_val, {})
                        color = tuple(entry.get("color", (128, 128, 128)))
                        point_size = 3.0  # Fixed smaller size for buffer
                        
                        # Create buffer actor
                        colors = np.array([color] * len(cls_pts), dtype=np.uint8)
                        cloud = pv.PolyData(cls_pts)
                        cloud["RGB"] = colors
                        
                        vtk_widget.add_points(
                            cloud,
                            scalars="RGB",
                            rgb=True,
                            point_size=point_size,
                            render_points_as_spheres=True,
                            reset_camera=False,
                            name=buffer_name,
                            render=False
                        )
                        
                        print(f"         🔹 Buffer {cls_val}: {len(cls_pts):,} points")
                except Exception as e:
                    print(f"         ⚠️ Buffer refresh failed: {e}")
            
        finally:
            # Re-enable interactor
            try:
                vtk_widget.iren.interactor.Enable()
            except Exception:
                pass
            
            # Restore camera
            if saved_camera is not None:
                try:
                    vtk_widget.camera_position = saved_camera
                except Exception:
                    pass
        
        # Render the view
        vtk_widget.render()
        print(f"      ✅ View {slot_idx} refreshed and rendered")


    def _get_main_view_palette(self):
        """Get Main View (Slot 0) palette."""
        # Try display mode dialog first
        if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
            if hasattr(self.display_mode_dialog, 'view_palettes'):
                if 0 in self.display_mode_dialog.view_palettes:
                    return self.display_mode_dialog.view_palettes[0]
        
        # Try app-level storage
        if hasattr(self, 'view_palettes') and 0 in self.view_palettes:
            return self.view_palettes[0]
        
        # Fallback to global palette
        return getattr(self, 'class_palette', {})


    def _build_slot_palette_from_main_defaults(self, slot_idx: int):
        """
        Build a SAFE default palette for non-main slots from slot-0 color/weight.
        Important: visibility is intentionally defaulted to True to avoid leaking
        Main View hide/show decisions into cross/cut views.
        """
        try:
            slot_i = int(slot_idx)
        except Exception:
            slot_i = slot_idx

        master = self._get_main_view_palette() or getattr(self, "class_palette", {}) or {}
        if not isinstance(master, dict) or not master:
            return {}

        seeded = {}
        for code, info in master.items():
            if not isinstance(info, dict):
                continue
            try:
                code_i = int(code)
            except Exception:
                continue
            seeded[code_i] = {
                "show": True,  # do not inherit slot-0 visibility into section/cut
                "description": str(info.get("description", "")),
                "color": tuple(info.get("color", (128, 128, 128))),
                "weight": float(info.get("weight", 1.0)),
                "draw": info.get("draw", ""),
                "lvl": str(info.get("lvl", "")),
            }

        if seeded:
            print(
                f"   ℹ️ Seeded slot {slot_i} default palette from Main colors/weights "
                f"({len(seeded)} classes, visibility independent)"
            )
        return seeded

    def _get_cross_section_palette(self, slot_idx, allow_default_seed: bool = True, persist_seed: bool = False):
        """Get isolated palette for cross/cut slots without leaking slot-0 visibility."""
        try:
            slot_i = int(slot_idx)
        except Exception:
            slot_i = slot_idx

        # Main View always uses canonical slot 0 palette.
        if slot_i == 0:
            return self._get_main_view_palette()

        # 1) dialog slot palette
        if hasattr(self, "display_mode_dialog") and self.display_mode_dialog:
            dlg = self.display_mode_dialog
            vp = getattr(dlg, "view_palettes", None)
            if isinstance(vp, dict):
                pal = vp.get(slot_i)
                if isinstance(pal, dict) and pal:
                    return pal

        # 2) app slot palette
        vp_app = getattr(self, "view_palettes", None)
        if isinstance(vp_app, dict):
            pal = vp_app.get(slot_i)
            if isinstance(pal, dict) and pal:
                return pal

        # 3) slot not configured: return isolated seeded default (optional).
        if not allow_default_seed:
            return {}

        seeded = self._build_slot_palette_from_main_defaults(slot_i)
        if seeded and persist_seed:
            if not hasattr(self, "view_palettes") or not isinstance(self.view_palettes, dict):
                self.view_palettes = {}
            self.view_palettes[slot_i] = {int(c): dict(v) for c, v in seeded.items()}

            dlg = getattr(self, "display_mode_dialog", None)
            if dlg is not None:
                if not hasattr(dlg, "view_palettes") or not isinstance(dlg.view_palettes, dict):
                    dlg.view_palettes = {}
                dlg.view_palettes[slot_i] = {int(c): dict(v) for c, v in seeded.items()}

            print(f"   ✅ Persisted seeded slot palette for slot {slot_i}")

        return seeded

    def _reset_section_palette_locks(self, view_indices, reason=""):
        """Unlock section palette locks for specified 0-based view indices."""
        if not hasattr(self, "_view_palette_locks") or not isinstance(self._view_palette_locks, dict):
            self._view_palette_locks = {}

        changed = []
        for view_idx in view_indices or []:
            if view_idx is None:
                continue
            try:
                v = int(view_idx)
            except Exception:
                continue
            self._view_palette_locks[v] = False
            changed.append(v + 1)

        if changed:
            why = f" reason={reason}" if reason else ""
            print(f"[PALETTE-LOCK] unlocked section views={sorted(set(changed))}{why}")

    def _mark_section_palette_and_class_dirty(self, slot_indices, reason=""):
        """Mark section palette/class caches dirty for specified 1-based slots."""
        if not hasattr(self, "section_palette_dirty") or not isinstance(self.section_palette_dirty, dict):
            self.section_palette_dirty = {}
        if not hasattr(self, "section_class_dirty") or not isinstance(self.section_class_dirty, dict):
            self.section_class_dirty = {}

        dirty_slots = []
        for slot_idx in slot_indices or []:
            try:
                slot = int(slot_idx)
            except Exception:
                continue
            if slot <= 0:
                continue
            self.section_palette_dirty[slot] = True
            self.section_class_dirty[slot] = True
            dirty_slots.append(slot)

        if dirty_slots:
            try:
                from gui.unified_actor_manager import invalidate_palette_cache
                for slot in sorted(set(dirty_slots)):
                    invalidate_palette_cache(slot)
            except Exception:
                pass
            why = f" reason={reason}" if reason else ""
            print(f"[SECTION-DIRTY] palette/class dirty slots={sorted(set(dirty_slots))}{why}")

    def _ensure_class_registered_for_section_views(self, class_code, slot_indices=(1, 2)):
        """
        Ensure class exists in master + per-view palettes and normalize color lookup.
        For existing entries, only color is overwritten from master to prevent stale RGB.
        """
        try:
            code = int(class_code)
        except Exception:
            return

        if not hasattr(self, "class_palette") or not isinstance(self.class_palette, dict):
            self.class_palette = {}

        master = self.class_palette
        existing = master.get(code) if isinstance(master.get(code), dict) else None

        # Resolve a canonical entry from best available source.
        canonical = dict(existing or {})
        if not canonical:
            slot0 = None
            dialog = getattr(self, "display_mode_dialog", None)
            if dialog is not None and hasattr(dialog, "view_palettes"):
                slot0 = (dialog.view_palettes or {}).get(0)
            if not slot0 and hasattr(self, "view_palettes"):
                slot0 = (self.view_palettes or {}).get(0)
            if slot0 and isinstance(slot0.get(code), dict):
                canonical = dict(slot0[code])

        color = canonical.get("color", (128, 128, 128))
        try:
            color = tuple(int(v) for v in color[:3])
        except Exception:
            color = (128, 128, 128)
        canonical["color"] = color
        canonical.setdefault("show", True)
        canonical.setdefault("weight", 1.0)
        canonical.setdefault("description", f"Class {code}")

        if code not in master or not isinstance(master.get(code), dict):
            master[code] = dict(canonical)
        else:
            master_entry = master[code]
            master_entry["color"] = color
            master_entry.setdefault("show", canonical["show"])
            master_entry.setdefault("weight", canonical["weight"])
            master_entry.setdefault("description", canonical["description"])

        if not hasattr(self, "view_palettes") or not isinstance(self.view_palettes, dict):
            self.view_palettes = {}

        dialog = getattr(self, "display_mode_dialog", None)
        dialog_palettes = None
        if dialog is not None:
            if not hasattr(dialog, "view_palettes") or not isinstance(dialog.view_palettes, dict):
                dialog.view_palettes = {}
            dialog_palettes = dialog.view_palettes
            if not hasattr(dialog, "slot_shows") or not isinstance(dialog.slot_shows, dict):
                dialog.slot_shows = {}

        touched_slots = {0}
        for slot in slot_indices or []:
            try:
                s = int(slot)
            except Exception:
                continue
            if s < 0:
                continue
            touched_slots.add(s)

            slot_palette = self.view_palettes.setdefault(s, {})
            slot_entry = slot_palette.get(code)
            if not isinstance(slot_entry, dict):
                slot_palette[code] = dict(canonical)
            else:
                slot_entry["color"] = color
                slot_entry.setdefault("show", canonical["show"])
                slot_entry.setdefault("weight", canonical["weight"])
                slot_entry.setdefault("description", canonical["description"])

            if dialog_palettes is not None:
                dpal = dialog_palettes.setdefault(s, {})
                dent = dpal.get(code)
                if not isinstance(dent, dict):
                    dpal[code] = dict(canonical)
                else:
                    dent["color"] = color
                    dent.setdefault("show", canonical["show"])
                    dent.setdefault("weight", canonical["weight"])
                    dent.setdefault("description", canonical["description"])

                dialog.slot_shows.setdefault(s, {})
                dialog.slot_shows[s][code] = bool(dpal[code].get("show", True))

        try:
            from gui.unified_actor_manager import invalidate_palette_cache
            for slot in sorted(touched_slots):
                invalidate_palette_cache(slot)
        except Exception:
            pass

    def _debug_log_cross_section_palette_state(self, context, target_class=None, gpu_slot=None):
        """Log palette/color/visibility/lock state for section views 1 and 2."""
        try:
            p_master = getattr(self, "class_palette", {}) or {}
            p1 = self._get_cross_section_palette(1) or {}
            p2 = self._get_cross_section_palette(2) or {}
            locks = getattr(self, "_view_palette_locks", {}) or {}

            v1_visible = sorted(int(c) for c, info in p1.items() if info.get("show", True))
            v2_visible = sorted(int(c) for c, info in p2.items() if info.get("show", True))

            print("[PALETTE-AUDIT]")
            print(f"  context={context}")
            if target_class is not None:
                cls = int(target_class)
                m_rgb = (p_master.get(cls) or {}).get("color")
                v1_rgb = (p1.get(cls) or {}).get("color")
                v2_rgb = (p2.get(cls) or {}).get("color")
                print(f"  target_class={cls}")
                print(f"  rgb.master={m_rgb}")
                print(f"  rgb.view1={v1_rgb}")
                print(f"  rgb.view2={v2_rgb}")

            print(f"  visible.view1={v1_visible}")
            print(f"  visible.view2={v2_visible}")
            print(f"  lock.view1={bool(locks.get(0, False))}")
            print(f"  lock.view2={bool(locks.get(1, False))}")
            if gpu_slot is not None:
                print(f"  gpu.slot={int(gpu_slot)}")
        except Exception as e:
            print(f"   ⚠️ palette audit log failed: {e}")

    def _sync_palette_between_section_views(self, source_idx: int, target_idx: int):
        """
        Sync palette metadata for a synced section WITHOUT clobbering target view
        visibility/weight preferences.

        Rule:
        - keep target slot's existing show/weight decisions (independent view behavior)
        - ensure missing classes are added
        - refresh colors from authoritative palette sources
        """
        import copy

        source_slot = int(source_idx) + 1
        target_slot = int(target_idx) + 1

        source_palette = self._get_cross_section_palette(
            source_slot,
            allow_default_seed=True,
            persist_seed=False,
        )

        if not isinstance(source_palette, dict) or not source_palette:
            return {}

        target_existing = self._get_cross_section_palette(target_slot) or {}
        master_palette = getattr(self, "class_palette", {}) or {}

        # Start from target (preserve independent visibility/weight); if target
        # is empty, seed from source.
        if isinstance(target_existing, dict) and target_existing:
            synced = copy.deepcopy(target_existing)
        else:
            synced = copy.deepcopy(source_palette)

        # Detect whether the target slot currently uses an explicit visibility
        # filter. If yes, newly introduced classes must default to hidden so a
        # section sync cannot silently repopulate View 1 visibility.
        target_filter_active = False
        if isinstance(target_existing, dict) and target_existing:
            try:
                _visible_count = sum(
                    1 for _info in target_existing.values()
                    if isinstance(_info, dict) and _info.get("show", True)
                )
                target_filter_active = 0 < _visible_count < len(target_existing)
            except Exception:
                target_filter_active = False

        # Merge in classes/colors from source + master while preserving target
        # show/weight whenever the class already exists in target.
        merge_codes = set()
        merge_codes.update(int(c) for c in source_palette.keys())
        merge_codes.update(int(c) for c in master_palette.keys())

        for code in sorted(merge_codes):
            src_info = source_palette.get(code, {})
            master_info = master_palette.get(code, {})
            existing = synced.get(code)

            if not isinstance(existing, dict):
                seed = {}
                if isinstance(src_info, dict):
                    seed.update(src_info)
                if isinstance(master_info, dict):
                    for k, v in master_info.items():
                        seed.setdefault(k, v)
                if target_filter_active:
                    # Preserve user filtering intent in target view.
                    seed["show"] = False
                else:
                    seed.setdefault("show", True)
                seed.setdefault("weight", 1.0)
                seed.setdefault("description", f"Class {code}")
                seed.setdefault("color", (128, 128, 128))
                synced[code] = dict(seed)
                existing = synced[code]

            # Refresh color only; preserve existing show/weight/description.
            color = None
            if isinstance(master_info, dict) and "color" in master_info:
                color = master_info.get("color")
            elif isinstance(src_info, dict) and "color" in src_info:
                color = src_info.get("color")
            if color is not None:
                try:
                    existing["color"] = tuple(int(v) for v in color[:3])
                except Exception:
                    pass

        if not hasattr(self, "view_palettes") or not isinstance(self.view_palettes, dict):
            self.view_palettes = {}
        self.view_palettes[target_slot] = copy.deepcopy(synced)

        dialog = getattr(self, "display_mode_dialog", None)
        if dialog is not None:
            if not hasattr(dialog, "view_palettes") or not isinstance(dialog.view_palettes, dict):
                dialog.view_palettes = {}
            dialog.view_palettes[target_slot] = copy.deepcopy(synced)
            if not hasattr(dialog, "slot_shows") or not isinstance(dialog.slot_shows, dict):
                dialog.slot_shows = {}
            dialog.slot_shows[target_slot] = {
                int(code): bool(info.get("show", True))
                for code, info in synced.items()
            }

        target_class = getattr(self, "_last_classified_to_class", None)
        if target_class is not None:
            self._ensure_class_registered_for_section_views(
                target_class, slot_indices=(source_slot, target_slot, 0)
            )

        self._reset_section_palette_locks(
            [source_idx, target_idx],
            reason=f"sync v{target_idx + 1}<-v{source_idx + 1}",
        )
        self._mark_section_palette_and_class_dirty(
            [source_slot, target_slot],
            reason=f"sync v{target_idx + 1}<-v{source_idx + 1}",
        )

        return synced
    
    def refresh_after_classification(self, to_class, changed_mask=None): ################
        """
        ⚡ MICROSTATION STYLE REFRESH
        This replaces the slow rebuild logic during active classification.
        """
        if changed_mask is None:
            changed_mask = getattr(self, '_last_changed_mask', None)
        
        if changed_mask is None:
            return self.refresh_all_views() # Fallback to slow path

        # 1. Attempt Instant Main View Update
        from gui.unified_actor_manager import fast_classify_update, is_unified_actor_ready
        
        success = False
        if is_unified_actor_ready(self):
            success = fast_classify_update(
                self, 
                changed_mask=changed_mask, 
                to_class=to_class, 
                palette=self.class_palette,
                border_percent=self.point_border_percent
            )

        # 2. Update Cross-Sections (These usually need a partial rebuild because they are small)
        if hasattr(self, 'section_vtks') and self.section_vtks:
            for view_idx in self.section_vtks.keys():
                self._refresh_single_section_view(view_idx)

        # 3. If fast path failed (e.g. first load), do the full rebuild once
        if not success:
            from gui.class_display import update_class_mode
            update_class_mode(self, force_refresh=True)

        # 4. Update stats in background
        QTimer.singleShot(10, lambda: refresh_point_statistics(self))  ##################



    def _refresh_all_views_after_change(self, num_points, operation="Update"):
        """
        ✅ INTERNAL: Complete rebuild of main view + all cross-sections after classification change
        
        Args:
            num_points: Number of points affected
            operation: Operation name for logging (e.g., "⏪ Undo", "🔜 Redo")
        """
        print(f"\n{'='*60}")
        print(f"{operation} - Processing {num_points:,} points")
        print(f"{'='*60}")
        
        # 1. Clear refresh guard flags
        if hasattr(self, '_last_changed_mask'):
            delattr(self, '_last_changed_mask')
        if hasattr(self, 'lastchangedmask'):
            self.lastchangedmask = None
        self._doing_partial_refresh = False
        
        # 2. Save camera state
        saved_camera = self._save_camera_state()
        
        # 3. Rebuild main view
        self._rebuild_main_view()
        
        # 4. Restore camera
        if saved_camera:
            self._restore_camera_state(saved_camera)
        
        # 5. Force render main view
        self._force_render_main_view()
        
        # 6. Refresh all cross-section views
        self._refresh_cross_sections()
        
        # 7. Process Qt events
        self._process_qt_events()
        
        # 8. Update statistics
        self._update_statistics()
        
        # 9. Update status bar
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage(f"{operation}: {num_points:,} points", 3000)
        
        print(f"{'='*60}")
        print(f"✅ {operation} COMPLETE - All views updated")
        print(f"{'='*60}\n")


    def _save_camera_state(self):
        """Save current camera state for restoration after rebuild"""
        if not (hasattr(self, 'vtk_widget') and self.vtk_widget and 
                hasattr(self.vtk_widget, 'renderer') and self.vtk_widget.renderer):
            return None
        
        try:
            camera = self.vtk_widget.renderer.GetActiveCamera()
            if camera:
                return {
                    'position': tuple(camera.GetPosition()),
                    'focal_point': tuple(camera.GetFocalPoint()),
                    'view_up': tuple(camera.GetViewUp()),
                    'parallel_scale': camera.GetParallelScale(),
                    'parallel_projection': camera.GetParallelProjection()
                }
        except Exception as e:
            print(f"⚠️ Camera save failed: {e}")
        return None

    def _restore_camera_state(self, saved_camera):
        """Restore previously saved camera state"""
        if not (hasattr(self, 'vtk_widget') and self.vtk_widget and 
                hasattr(self.vtk_widget, 'renderer') and self.vtk_widget.renderer):
            return
        
        try:
            camera = self.vtk_widget.renderer.GetActiveCamera()
            camera.SetPosition(saved_camera['position'])
            camera.SetFocalPoint(saved_camera['focal_point'])
            camera.SetViewUp(saved_camera['view_up'])
            camera.SetParallelScale(saved_camera['parallel_scale'])
            if saved_camera['parallel_projection']:
                camera.ParallelProjectionOn()
            self.vtk_widget.renderer.ResetCameraClippingRange()
        except Exception as e:
            print(f"⚠️ Camera restore failed: {e}")

    def _rebuild_main_view(self):
        """Complete rebuild of main VTK view"""
        print("🔄 Rebuilding Main View...")
        self._preserve_view = True
        
        try:
            # Clear all VTK actors
            if hasattr(self, 'vtk_widget') and self.vtk_widget:
                self.vtk_widget.renderer.RemoveAllViewProps()
                if hasattr(self.vtk_widget, 'actors'):
                    self.vtk_widget.actors.clear()
                if hasattr(self.vtk_widget, '_actors'):
                    self.vtk_widget._actors.clear()
            
            # Rebuild based on display mode
            if self.display_mode == "class":
                from gui.class_display import update_class_mode
                update_class_mode(self)
                print("✅ Main view rebuilt (class)")
                
            elif self.display_mode in ("shaded_class", "shadedclass"):
                from gui.shading_display import update_shaded_class
                update_shaded_class(
                    self,
                    getattr(self, "last_shade_azimuth", 45.0),
                    getattr(self, "shading_sharpness_angle", 45.0),
                    getattr(self, "shade_ambient", 0.2),
                )
                print("✅ Main view rebuilt (shaded)")
                
            else:
                from gui.pointcloud_display import updatepointcloud
                updatepointcloud(self, self.display_mode)
                print(f"✅ Main view rebuilt ({self.display_mode})")
                
        except Exception as e:
            print(f"❌ Main view rebuild failed: {e}")
            import traceback
            traceback.print_exc()

    def _force_render_main_view(self):
        """Force immediate VTK render of main view"""
        if hasattr(self, 'vtk_widget') and self.vtk_widget:
            try:
                self.vtk_widget.GetRenderWindow().Render()
            except Exception as e:
                print(f"⚠️ Main render failed: {e}")
                
    def _refresh_cross_sections_async(self):
        """
        ✅ Schedule cross-section refresh for next event loop iteration.
        Prevents UI micro-stutters during classification.
        """
        try:
            from PySide6.QtCore import QTimer
            # Reduced to 5ms for even faster response
            QTimer.singleShot(5, self._refresh_cross_sections)
        except Exception as e:
            print(f"⚠️ Async scheduling failed: {e}")
            self._refresh_cross_sections()

    def _refresh_cross_sections(self):
        """
        🚀 REFRESH ALL CROSS-SECTIONS
        Coordinates between the SectionController and the GPU.
        """
        if not (hasattr(self, 'section_vtks') and self.section_vtks):
            return
        
        if not (hasattr(self, 'section_controller') and self.section_controller):
            print(" ❌ ERROR: section_controller not found!")
            return

        # Use the stored view-specific palettes if they exist
        palettes = getattr(self, 'view_palettes', {})

        for view_idx in sorted(self.section_vtks.keys()):
            try:
                # 1. Update the controller's state
                old_active = getattr(self.section_controller, 'active_view', None)
                self.section_controller.active_view = view_idx
                
                # 2. Assign the specific palette for THIS view (very important!)
                # Never fall back to slot-0 visibility rules.
                slot_idx = view_idx + 1
                view_palette = palettes.get(slot_idx)
                if not view_palette:
                    view_palette = self._get_cross_section_palette(
                        slot_idx,
                        allow_default_seed=True,
                        persist_seed=False,
                    )
                
                # 3. TRIGGER REFRESH
                # We prioritize the specialized view-specific refresh
                if hasattr(self.section_controller, 'refresh_colors_for_view'):
                    # ✅ The controller method should now handle the GPU Buffer Swap
                    self.section_controller.refresh_colors_for_view(view_idx, palette=view_palette)
                elif hasattr(self.section_controller, 'refresh_colors'):
                    self.section_controller.refresh_colors()
                
                if old_active is not None:
                    self.section_controller.active_view = old_active
                    
            except Exception as e:
                print(f" ❌ Cross-section View {view_idx + 1} failed: {e}")
        
        # Process events so the screen actually updates
        try:
            from PySide6.QtCore import QCoreApplication
            QCoreApplication.processEvents()
        except Exception: pass

    def _get_section_dock_widget(self, view_idx):
        """
        Get the QDockWidget parent of a cross-section view
        """
        try:
            vtk_widget = self.section_vtks[view_idx]
            from PySide6.QtWidgets import QDockWidget
            parent = vtk_widget.parent()
            
            # Walk up widget hierarchy to find QDockWidget
            while parent:
                if isinstance(parent, QDockWidget):
                    return parent
                parent = parent.parent()
                
        except Exception as e:
            print(f"      ⚠️ Could not find dock widget: {e}")
        
        return None

    def _force_section_data_update(self, view_idx):
        """
        ✅ Force complete data pipeline update for cross-section view
        This is a fallback when section_controller methods aren't available
        """
        vtk_widget = self.section_vtks[view_idx]
        
        if not (hasattr(vtk_widget, 'renderer') and vtk_widget.renderer):
            return
        
        # 1. Get all actors and update their data
        actors = vtk_widget.renderer.GetActors()
        actors.InitTraversal()
        
        updated_count = 0
        for i in range(actors.GetNumberOfItems()):
            actor = actors.GetNextActor()
            if not actor:
                continue
                
            mapper = actor.GetMapper()
            if not mapper:
                continue
            
            # Get the polydata
            polydata = mapper.GetInput()
            if not polydata:
                continue
            
            # ✅ CRITICAL: Update colors from current classification
            if hasattr(polydata, 'GetPointData'):
                point_data = polydata.GetPointData()
                if point_data and point_data.GetScalars():
                    # Mark scalars as modified
                    point_data.GetScalars().Modified()
            
            # Mark all pipeline components as modified
            polydata.Modified()
            mapper.Modified()
            mapper.Update()
            actor.Modified()
            
            updated_count += 1
        
        print(f"      🔄 Updated {updated_count} actor pipeline(s)")
        
        # 2. Force render
        if hasattr(vtk_widget, 'GetRenderWindow'):
            vtk_widget.GetRenderWindow().Render()
        elif hasattr(vtk_widget, 'render'):
            vtk_widget.render()

    def _render_section_view(self, view_idx):
        """
        ✅ FIXED: Render a single cross-section view with pipeline update
        """
        vtk_widget = self.section_vtks[view_idx]
        
        # 1. ✅ CRITICAL: Mark all data sources as modified
        if hasattr(vtk_widget, 'renderer') and vtk_widget.renderer:
            actors = vtk_widget.renderer.GetActors()
            actors.InitTraversal()
            
            for i in range(actors.GetNumberOfItems()):
                actor = actors.GetNextActor()
                if actor:
                    mapper = actor.GetMapper()
                    if mapper:
                        # Force pipeline update
                        mapper.Modified()
                        mapper.Update()
                        
                        # Mark input data as modified
                        input_data = mapper.GetInput()
                        if input_data:
                            input_data.Modified()
        
        # 2. Force render
        if hasattr(vtk_widget, 'GetRenderWindow'):
            vtk_widget.GetRenderWindow().Render()
        elif hasattr(vtk_widget, 'render'):
            vtk_widget.render()

    def _process_qt_events(self):
        """Process pending Qt events to ensure UI updates"""
        try:
            from PySide6.QtCore import QCoreApplication
            QCoreApplication.processEvents()
            print("✅ Qt events processed")
        except Exception as e:
            pass

    def _update_statistics(self):
        """Update point count statistics widget"""
        if hasattr(self, 'pointcountwidget') and self.pointcountwidget:
            try:
                from gui.point_count_widget import refreshpointstatistics
                refreshpointstatistics(self)
                print("✅ Statistics updated")
            except Exception as e:
                pass

    def _refresh_changed_points_after_undo_redo(self, changed_mask):
        """
        ✅ FIXED: Always refreshes main view, regardless of cross-section state
        
        Args:
            changed_mask: Boolean mask of points that were changed by undo/redo
        """
        print(f"\n{'='*60}")
        print(f"🔄 PARTIAL REFRESH AFTER UNDO/REDO")
        print(f"   Changed points: {changed_mask.sum():,}")
        print(f"   Display mode: {getattr(self, 'display_mode', 'unknown')}")
        print(f"{'='*60}")
        
        try:
            import numpy as np
            
            # ========================================================================
            # STEP 1: ALWAYS Refresh Main View First (most important!)
            # ========================================================================
            current_mode = getattr(self, 'display_mode', 'class')
            print(f"\n🔄 Refreshing Main View...")
            
            try:
                if current_mode == "class":
                    # ✅ CRITICAL: Always use partial refresh for undo/redo
                    print(f"   ⚡ Using PARTIAL refresh (undo/redo)")
                    self._refresh_main_view_partial_with_mask(changed_mask)
                    print(f"   ✅ Main View PARTIAL refresh complete")
                    
                elif current_mode == "shaded_class":
                    print(f"   🌗 Shaded mode - FULL refresh required")
                    from gui.shading_display import update_shaded_class
                    update_shaded_class(
                        self,
                        getattr(self, "last_shade_azimuth", 45.0),
                        getattr(self, "shading_sharpness_angle", 45.0),
                        getattr(self, "shade_ambient", 0.2)
                    )
                    print(f"   ✅ Main View refreshed (shaded_class mode)")
                    
                elif current_mode in ["rgb", "intensity", "elevation", "depth"]:
                    print(f"   📊 {current_mode} mode - FULL refresh")
                    from gui.pointcloud_display import update_pointcloud
                    update_pointcloud(self, current_mode)
                    print(f"   ✅ Main View refreshed ({current_mode} mode)")
                    
                else:
                    print(f"   ⚠️ Unknown mode '{current_mode}' - using class mode")
                    from gui.class_display import update_class_mode
                    update_class_mode(self)
                    
            except Exception as e:
                print(f"   ❌ Main View refresh failed: {e}")
                import traceback
                traceback.print_exc()
            
            # ========================================================================
            # STEP 2: Refresh cross-section views (if any exist)
            # ========================================================================
            if hasattr(self, 'section_vtks') and self.section_vtks:
                if hasattr(self, 'section_controller'):
                    self.section_controller._last_changed_mask = changed_mask
                
                num_views = len(self.section_vtks)
                print(f"\n🔄 Refreshing {num_views} cross-section view(s)...")
                
                for view_idx in sorted(self.section_vtks.keys()):
                    print(f"   📋 View {view_idx + 1}:")
                    try:
                        # Use the single view refresh
                        self._refresh_single_view_partial(view_idx, changed_mask)
                    except Exception as e:
                        print(f"      ⚠️ View {view_idx + 1} refresh failed: {e}")
            
            # ========================================================================
            # STEP 3: Refresh Cut Section (if active)
            # ========================================================================
            if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
                if getattr(self.cut_section_controller, 'is_cut_view_active', False):
                    print(f"\n🔄 Refreshing Cut Section view...")
                    try:
                        if hasattr(self.cut_section_controller, 'onclassificationchanged'):
                            self.cut_section_controller.onclassificationchanged()
                            print(f"   ✅ Cut Section refreshed")
                    except Exception as e:
                        print(f"   ⚠️ Cut Section refresh failed: {e}")
            
            # ========================================================================
            # STEP 4: Update Statistics
            # ========================================================================
            if hasattr(self, 'point_count_widget') and self.point_count_widget:
                print(f"\n📊 Updating Point Statistics...")
                try:
                    from gui.point_count_widget import refresh_point_statistics
                    refresh_point_statistics(self)
                    print(f"   ✅ Point Statistics updated")
                except Exception as e:
                    print(f"   ⚠️ Statistics update failed: {e}")

            # ========================================================================
            # STEP 5: Force Final Render
            # ========================================================================
            try:
                from PySide6.QtCore import QCoreApplication
                
                # Process any pending events
                QCoreApplication.processEvents()
                
                # Force immediate render
                if hasattr(self, 'vtk_widget') and self.vtk_widget:
                    self.vtk_widget.GetRenderWindow().Render()
                    print(f"✅ Final render forced")
                
            except Exception as e:
                print(f"⚠️ Final render warning: {e}")

            print(f"\n{'='*60}")
            print(f"✅ REFRESH COMPLETE")
            print(f"{'='*60}\n")

        except Exception as e:  # ← This is the OUTER try/except from the function start
                print(f"\n⚠️ PARTIAL REFRESH ERROR: {e}")
                import traceback
                traceback.print_exc()
                    
    def _refresh_single_view_partial(self, view_idx, changed_mask):
        """
        Helper: Only refresh a specific cross-section view if it contains changed points.
        """
        import numpy as np
        
        # 1. Get the view's point indices (core + buffer)
        # We need to know WHICH points are in this view to see if they overlap with changed_mask
        if not hasattr(self.app, 'section_controller'):
            return
        
        # Robust Fallback: If we can't quickly check intersection, just refresh.
        # But let's try to use the stored section masks if available.
        view_indices = None
        
        # Example: Accessing stored masks from section controller if they exist
        # This part depends on your data structure. Assuming standard 'section_indices'
        if hasattr(self.app.section_controller, "views_data"):
            vdata = self.app.section_controller.views_data.get(view_idx)
            if vdata:
                view_indices = vdata.get("indices")
        
        intersect = True
        if view_indices is not None:
            # Fast check: Do any of the view's points exist in the changed_mask?
            # changed_mask is boolean full array, view_indices is list of ints
            if not np.any(changed_mask[view_indices]):
                intersect = False
        
        if intersect:
            print(f"      ⚠️ Changed points detected in View {view_idx + 1} - Refreshing")
            # Call the standard single-view refresh
            self._refresh_single_view(view_idx)
        else:
            print(f"      ⏭️ View {view_idx + 1} unaffected - Skipping")

    def _refresh_single_view_partial(self, view_index, changed_mask):
        """
        ✅ Refresh only changed points in a single cross-section view
        
        Args:
            view_index: View index (0-3)
            changed_mask: Boolean mask of changed points
        """
        import numpy as np
        import pyvista as pv
        
        slot = view_index + 1
        
        print(f"      Checking for changed points in view data...")
        
        # Check if view exists
        if not hasattr(self, 'section_vtks') or view_index not in self.section_vtks:
            print(f"         ⏭️ View not found")
            return
        
        vtk_widget = self.section_vtks[view_index]
        
        # Check if view has section data
        pts = getattr(self, f'section_{view_index}_core_points', None)
        buf = getattr(self, f'section_{view_index}_buffer_points', None)
        core_mask = getattr(self, f'section_{view_index}_core_mask', None)
        buffer_mask = getattr(self, f'section_{view_index}_buffer_mask', None)
        
        if pts is None or core_mask is None:
            print(f"         ⏭️ No section data")
            return
        
        # ✅ Check if ANY changed points are in this view
        view_has_changes = np.any(changed_mask & core_mask)
        if buf is not None and buffer_mask is not None:
            view_has_changes |= np.any(changed_mask & buffer_mask)
        
        if not view_has_changes:
            print(f"         ⏭️ No changed points in this view - skipping")
            return
        
        print(f"         ✅ Changed points found - refreshing...")
        
        # ✅ Get THIS VIEW's palette from Display Mode
        view_palette = None
        
        if hasattr(self, 'display_mode_dialog'):
            dialog = self.display_mode_dialog
            
            if hasattr(dialog, 'view_palettes') and slot in dialog.view_palettes:
                view_palette = dialog.view_palettes[slot]
        if not view_palette:
            view_palette = self._get_cross_section_palette(
                slot,
                allow_default_seed=True,
                persist_seed=False,
            )
        
        if not view_palette:
            print(f"         ❌ No palette available")
            return
        
        # Get visible classes
        visible = [c for c, v in view_palette.items() if v.get("show", False)]
        
        if not visible:
            print(f"         ⏭️ No visible classes")
            vtk_widget.clear()
            vtk_widget.render()
            return
        
        # Get CURRENT classifications
        current_classes = self.data["classification"]
        
        # Combine core + buffer points
        if buf is not None and buffer_mask is not None:
            all_pts = np.vstack([pts, buf])
            all_cls = np.concatenate([
                current_classes[core_mask],
                current_classes[buffer_mask & ~core_mask]
            ])
        else:
            all_pts = pts
            all_cls = current_classes[core_mask]
        
        # Filter by visible classes
        mask = np.isin(all_cls, visible)
        filtered_pts = all_pts[mask]
        filtered_cls = all_cls[mask]
        
        if len(filtered_pts) == 0:
            vtk_widget.clear()
            vtk_widget.render()
            return
        
        # Build colors
        colors = np.zeros((len(filtered_pts), 3), dtype=np.uint8)
        for i, cls in enumerate(filtered_cls):
            entry = view_palette.get(int(cls), {"color": (128, 128, 128)})
            colors[i] = entry["color"]
        
        # Save camera
        cam_pos = vtk_widget.camera_position
        
        # Clear and re-render
        vtk_widget.clear()
        
        cloud = pv.PolyData(filtered_pts)
        cloud["RGB"] = colors
        
        vtk_widget.add_points(
            cloud,
            scalars="RGB",
            rgb=True,
            point_size=3,
            render_points_as_spheres=True
        )
        
        # Restore camera
        vtk_widget.camera_position = cam_pos
        vtk_widget.render()
        
        print(f"         ✅ Refreshed {len(filtered_pts):,} points")
        
    def _refresh_main_view_partial_with_mask(self, changed_mask):
        """
        ✅ FIXED: Proper weight-based rendering order & Filter Isolation.
        - Calculates correct point size (psize) even during targeted updates.
        - Synchronizes border settings with global app state.
        - Handles Undo/Redo class transitions without leaving 'ghost' points.
        """
        import numpy as np
        import pyvista as pv
 
        self._doing_partial_refresh = True
        print("\n   🔄 Main View Targeted Refresh (Syncing Weights & Borders)...")
 
        # --- Local Shader Helpers ---
        def _border_ring_fraction(border_percent: float) -> float:
            bp = max(0.0, float(border_percent or 0.0))
            return min(0.45, bp / 60.0)
 
        def _apply_black_ring_shader(actor, border_percent: float) -> None:
            ring = _border_ring_fraction(border_percent)
            if ring <= 0.0: return
            try:
                sp = actor.GetShaderProperty()
                code = f"""
        //VTK::Color::Impl
        vec2 naksha_uv = gl_PointCoord.xy - vec2(0.5);
        float naksha_radius_sq = dot(naksha_uv, naksha_uv);
        if (naksha_radius_sq > 0.25) {{ discard; }}
        opacity = 1.0;
        float ring = {ring:.6f};
        if (ring > 0.0) {{
            float edge_radius = 0.5 * (1.0 - ring);
            if (naksha_radius_sq >= edge_radius * edge_radius) {{
                diffuseColor = vec3(0.0, 0.0, 0.0);
                ambientColor = vec3(0.0, 0.0, 0.0);
                opacity = 1.0;
            }}
        }}
        """
                sp.AddFragmentShaderReplacement("//VTK::Color::Impl", True, code, False)
                actor.Modified()
            except Exception: pass
 
        try:
            if self.display_mode != "class":
                return
            if self.data is None or "xyz" not in self.data or "classification" not in self.data:
                return
 
            n = len(self.data["classification"])
            plotter = self.vtk_widget
 
            # 1. Normalize mask (Handles both boolean arrays and index arrays)
            undo_mask = None
            undo_old_classes = None
            try:
                if hasattr(self, "undostack") and self.undostack:
                    last = self.undostack[-1]
                    m = last.get("mask", None)
                    if isinstance(m, np.ndarray) and m.dtype == bool and m.shape[0] == n:
                        undo_mask = m
                        undo_old_classes = last.get("old_classes") or last.get("oldclasses")
            except Exception: pass
 
            if isinstance(changed_mask, np.ndarray) and changed_mask.dtype == bool and changed_mask.shape[0] == n:
                mask_bool = changed_mask
            else:
                mask_bool = np.zeros(n, dtype=bool)
                try:
                    idx = np.asarray(changed_mask, dtype=np.int64)
                    mask_bool[idx[(idx >= 0) & (idx < n)]] = True
                except Exception: pass
 
            if undo_mask is not None and undo_mask.any():
                if (not mask_bool.any()) or (undo_mask.sum() <= mask_bool.sum()):
                    mask_bool = undo_mask
 
            if not np.any(mask_bool): return
 
            # 2. Save Camera State
            saved_camera = None
            try:
                cam = plotter.renderer.GetActiveCamera()
                saved_camera = {
                    "pos": cam.GetPosition(), "foc": cam.GetFocalPoint(), 
                    "up": cam.GetViewUp(), "scale": cam.GetParallelScale(),
                    "proj": cam.GetParallelProjection()
                }
            except Exception: pass
 
            # 3. Retrieve Palette & Border Settings (Sync with Main View)
            palette0 = None
            dlg = getattr(self, "display_mode_dialog", None)
            if dlg and hasattr(dlg, "view_palettes") and 0 in dlg.view_palettes:
                palette0 = dlg.view_palettes[0] 
            if palette0 is None:
                palette0 = getattr(self, "class_palette", {}) or {}
 
            # ✅ SYNC: Use global border settings to match the classification tools
            border_val = float(getattr(self, "point_border_percent", 0) or 0.0)
 
            xyz, classes = self.data["xyz"], self.data["classification"]
            visible_classes = [c for c, v in palette0.items() if v.get("show", True)]
            has_custom_weights = any(v.get("weight", 1.0) != 1.0 for v in palette0.values())
            actor_keys = list(getattr(plotter, "actors", {}).keys())
            has_per_class_actors = any(str(k).startswith("class_") for k in actor_keys)
 
            # ===================================================================
            # PATH A: Optimized/LOD pipeline (Single Actor)
            # ===================================================================
            if not has_per_class_actors:
                from gui.performance_optimizations import fast_update_colors_optimized
                try:
                    fast_update_colors_optimized(self, mask_bool)
                except Exception:
                    from gui.pointcloud_display import fast_update_colors
                    fast_update_colors(self, mask_bool)
            # ===================================================================
            # PATH B: Per-class actors (Handles Weights & Filters)
            # ===================================================================
            else:
                # ✅ Path B1: Full Rebuild for Custom Weights (Prevents Z-fighting)
                if has_custom_weights:
                    recent_class = getattr(self, "_last_classified_to_class", None)
                    for name in list(plotter.actors.keys()):
                        if str(name).startswith(("class_", "border_")):
                            plotter.remove_actor(name, render=False)
 
                    # Heaviest at bottom, Recent on top
                    class_weights = sorted([(c, palette0[c].get("weight", 1.0)) for c in visible_classes if c != recent_class], 
                                           key=lambda x: x[1], reverse=True)
                    render_order = [cw[0] for cw in class_weights]
                    if recent_class in visible_classes: render_order.append(recent_class)
 
                    for code in render_order:
                        pts = xyz[classes == code]
                        if pts.size == 0: continue
                        entry = palette0.get(int(code), {})
                        # Weight calculation
                        w = max(0.1, min(float(entry.get("weight", 1.0)), 12.0))
                        psize = max(1.5, min(2.5 * w, 30.0))
                        cloud = pv.PolyData(pts)
                        cloud["RGB"] = np.tile(np.array(entry.get("color", (128,128,128)), dtype=np.uint8), (len(pts), 1))
                        act = plotter.add_points(cloud, scalars="RGB", rgb=True, point_size=psize, 
                                               render_points_as_spheres=True, name=f"class_{code}", render=False)
                        _apply_black_ring_shader(act, border_val)
 
                # ✅ Path B2: Targeted Update (Flicker-Free, Handles specific point size)
                else:
                    changed_idx = np.flatnonzero(mask_bool)
                    affected = set(np.unique(classes[changed_idx]).tolist())
                    # Ensure the "from" classes are also refreshed to remove moving points
                    if isinstance(undo_old_classes, np.ndarray):
                        affected.update(np.unique(undo_old_classes).tolist())
 
                    for c in affected:
                        plotter.remove_actor(f"class_{c}", render=False)
                        if c in visible_classes:
                            pts = xyz[classes == c]
                            if pts.size > 0:
                                entry = palette0.get(int(c), {})
                                # ✅ FIX: Calculate size from weight instead of hardcoding 2.5
                                w = max(0.1, min(float(entry.get("weight", 1.0)), 12.0))
                                psize = max(1.5, min(2.5 * w, 30.0))
                                cloud = pv.PolyData(pts)
                                cloud["RGB"] = np.tile(np.array(entry.get("color", (128,128,128)), dtype=np.uint8), (len(pts), 1))
                                act = plotter.add_points(cloud, scalars="RGB", rgb=True, point_size=psize, 
                                                       render_points_as_spheres=True, name=f"class_{c}", render=False)
                                _apply_black_ring_shader(act, border_val)
 
            # 4. Restore Camera
            if saved_camera:
                cam = plotter.renderer.GetActiveCamera()
                cam.SetPosition(saved_camera["pos"])
                cam.SetFocalPoint(saved_camera["foc"])
                cam.SetViewUp(saved_camera["up"])
                cam.SetParallelScale(saved_camera["scale"])
                if saved_camera["proj"]: cam.ParallelProjectionOn()
                plotter.renderer.ResetCameraClippingRange()
 
            plotter.render()
            if hasattr(self, "_last_classified_to_class"):
                delattr(self, "_last_classified_to_class")
 

 
        except Exception as e:
            print(f"❌ Partial Refresh Error: {e}")
        finally:
            self._doing_partial_refresh = False

    # ═══════════════════════════════════════════════════════════════════
    #  REPLACE your existing _load_backup_settings method with this one.
    #  It's currently called at the end of __init__ and should also be
    #  called after a file is loaded to start the timer.
    # ═══════════════════════════════════════════════════════════════════
    def _load_backup_settings(self):
        """Load backup interval from QSettings and start/stop the timer."""
        try:
            enabled = self.settings.value("backup_enabled", True, type=bool)
            interval_minutes = self.settings.value("backup_interval_minutes", 5, type=int)
            interval_minutes = max(1, min(60, interval_minutes))
            interval_ms = interval_minutes * 60 * 1000

            if enabled:
                self.auto_backup_timer.setInterval(interval_ms)
                # Only start if data is already loaded (e.g. settings dialog
                # changed while working). If no data yet, timer runs but
                # _auto_backup will silently return.
                if not self.auto_backup_timer.isActive():
                    self.auto_backup_timer.start()
                    print(f"⏱️ Auto-backup timer started: every {interval_minutes} min")
            else:
                if self.auto_backup_timer.isActive():
                    self.auto_backup_timer.stop()
                    print("⏱️ Auto-backup timer stopped (disabled)")
        except Exception as e:
            print(f"⚠️ Failed to load backup settings: {e}")

    # ═══════════════════════════════════════════════════════════════════
    #  REPLACE your existing _auto_backup method with this one.
    #  This is the critical fix: snapshot on main thread (<1ms),
    #  then write in background QThread.
    # ═══════════════════════════════════════════════════════════════════
    def _auto_backup(self):
        """Trigger a background auto-backup without blocking the UI."""
        # Guard: prevent overlapping backups
        if self._backup_worker_running:
            print("⏭️ Auto-backup skipped — previous still writing")
            return

        # Guard: no data
        data = getattr(self, "data", None)
        if data is None or "xyz" not in data or data["xyz"] is None:
            return

        # Compute backup path
        backup_path = self._compute_backup_path()
        if not backup_path:
            return

        try:
            from .save_pointcloud import _import_options_reduce_points
            if _import_options_reduce_points(data.get("import_options")):
                print(
                    "⏭️ Auto-backup skipped — current dataset is a filtered/sampled subset "
                    "and preserving the source file layout is not safe."
                )
                return
        except Exception:
            pass

        # Determine LAS version
        # Prefer the most recent explicit save version so backup tracks Save / Save As.
        las_version = getattr(self, "last_save_version", None)
        try:
            import laspy
            v = data.get("input_format_version", None)
            if las_version not in ("1.2", "1.4") and isinstance(v, tuple) and len(v) >= 2:
                las_version = f"{int(v[0])}.{int(v[1])}"
            if las_version not in ("1.2", "1.4"):
                probe = getattr(self, "loaded_file", None)
                if probe and os.path.exists(probe):
                    try:
                        with laspy.open(probe) as reader:
                            hv = reader.header.version
                            las_version = f"{hv.major}.{hv.minor}"
                    except Exception:
                        pass
                if las_version not in ("1.2", "1.4"):
                    las_version = "1.4"
        except Exception:
            pass

        # ---- FAST SNAPSHOT on main thread (< 1ms) ----
        try:
            xyz = np.asarray(data["xyz"])
            n = xyz.shape[0]

            classes = data.get("classification", np.zeros(n, dtype=np.uint8))
            classes_u8 = np.asarray(classes).astype(np.uint8, copy=False)
            if classes_u8.shape[0] != n:
                classes_u8 = np.zeros(n, dtype=np.uint8)

            rgb16 = self._rgb_to_las16_fast(data.get("rgb"))
            intensity16 = self._intensity_to_uint16_fast(data.get("intensity"), n)
            point_format = self._choose_point_format_fast(las_version, rgb16 is not None)

            # Pre-serialize drawings on main thread (safe access to digitizer)
            from .save_pointcloud import _serialize_drawings
            drawing_data = _serialize_drawings(self)

            crs_wkt = getattr(self, "project_crs_wkt", None)
            crs_epsg = getattr(self, "project_crs_epsg", None)

            snapshot = {
                "xyz": xyz,
                "classes": classes_u8,
                "rgb": rgb16,
                "intensity": intensity16,
            }
        except Exception as e:
            print(f"⚠️ Backup snapshot failed: {e}")
            return

        # ---- Launch background worker ----
        self._backup_worker_running = True
        worker = _BackupWorker(
            snapshot=snapshot,
            path=backup_path,
            las_version=las_version,
            point_format=point_format,
            crs_wkt=crs_wkt,
            crs_epsg=crs_epsg,
            drawing_data=drawing_data,
            source_path=getattr(self, "loaded_file", None),
            import_options=data.get("import_options"),
        )
        worker.finished_ok.connect(self._on_backup_finished)
        worker.failed.connect(self._on_backup_failed)
        worker.finished.connect(self._on_backup_worker_done)

        # Keep reference to prevent garbage collection
        self._active_backup_worker = worker
        worker.start()

    # ═══════════════════════════════════════════════════════════════════
    #  ADD these 5 new methods to NakshaApp (after _auto_backup)
    # ═══════════════════════════════════════════════════════════════════

    def _compute_backup_path(self) -> str:
        """Compute the backup file path from saved settings."""
        try:
            import time as _time

            loaded = getattr(self, "loaded_file", None)
            if not loaded:
                return None

            base_name = os.path.splitext(os.path.basename(loaded))[0]
            ext = os.path.splitext(loaded)[1].lower()
            if ext not in (".las", ".laz"):
                ext = ".laz"

            naming_format = self.settings.value("backup_naming_format", 0, type=int)

            if naming_format == 1:
                timestamp = _time.strftime("%Y%m%d_%H%M%S")
                filename = f"{base_name}_{timestamp}{ext}"
            elif naming_format == 2:
                filename = f"{base_name}_autosave{ext}"
            else:
                filename = f"{base_name}_backup{ext}"

            use_custom = self.settings.value("backup_use_custom_path", False, type=bool)
            if use_custom:
                folder = self.settings.value("backup_custom_path", "", type=str)
                if not folder:
                    folder = os.path.dirname(loaded)
            else:
                folder = os.path.join(os.path.dirname(loaded), "backup")

            os.makedirs(folder, exist_ok=True)
            return os.path.join(folder, filename)

        except Exception as e:
            print(f"⚠️ Failed to compute backup path: {e}")
            return None

    def _on_backup_finished(self, path: str):
        """Signal handler: backup succeeded (runs on main thread)."""
        print(f"💾 Auto-backup saved → {path}")

    def _on_backup_failed(self, error: str):
        """Signal handler: backup failed (runs on main thread)."""
        print(f"⚠️ Auto-backup failed: {error}")

    def _on_backup_worker_done(self):
        """Signal handler: worker thread finished (success or failure)."""
        self._backup_worker_running = False
        self._active_backup_worker = None

    @staticmethod
    def _rgb_to_las16_fast(rgb_arr):
        if rgb_arr is None:
            return None
        rgb = np.asarray(rgb_arr)
        if rgb.ndim != 2 or rgb.shape[1] != 3:
            return None
        if np.issubdtype(rgb.dtype, np.floating):
            rgb = np.clip(rgb, 0.0, 1.0)
            return (rgb * 65535.0).round().astype(np.uint16)
        mx = int(rgb.max()) if rgb.size else 0
        if mx <= 255:
            return (rgb.astype(np.uint16) * 256)
        return np.clip(rgb, 0, 65535).astype(np.uint16)

    @staticmethod
    def _intensity_to_uint16_fast(intensity_arr, n_points: int):
        if intensity_arr is None:
            return None
        inten = np.asarray(intensity_arr)
        if inten.shape[0] != n_points:
            return None
        if np.issubdtype(inten.dtype, np.floating):
            mx = float(np.nanmax(inten)) if inten.size else 0.0
            if mx <= 1.0:
                inten = np.clip(inten, 0.0, 1.0) * 65535.0
            inten = np.clip(inten, 0.0, 65535.0)
            return inten.round().astype(np.uint16)
        return np.clip(inten, 0, 65535).astype(np.uint16)

    @staticmethod
    def _choose_point_format_fast(las_version: str, has_rgb: bool) -> int:
        if las_version == "1.4":
            return 7 if has_rgb else 6
        return 2 if has_rgb else 0

    def _load_display_settings(self):
        """
        Load saved elevation and intensity display settings from QSettings.
        Called during app initialization to restore user preferences.
        """
        try:
            settings = QSettings("NakshaAI", "LidarApp")
            
            # ══════════════════════════════════════════════════════════
            # ELEVATION SETTINGS
            # ══════════════════════════════════════════════════════════
            # Load custom color ramp if saved
            saved_ramp = settings.value("elevation_color_ramp", None)
            if saved_ramp:
                try:
                    # Convert back from serializable format
                    self.elevation_color_ramp = [
                        (float(pos), tuple(color)) 
                        for pos, color in saved_ramp
                    ]
                    print(f"✅ Loaded saved elevation ramp: {len(self.elevation_color_ramp)} stops")
                except Exception as e:
                    print(f"⚠️ Failed to load elevation ramp: {e}")
                    self.elevation_color_ramp = None
            self.shading_quality = str(settings.value("global_shading_quality", "normal") or "normal").lower()
            if self.shading_quality not in ("fast", "normal", "slow"):
                self.shading_quality = "normal"
            self.surface_quality = str(settings.value("global_surface_quality", "normal") or "normal").lower()
            if self.surface_quality not in ("fast", "normal", "slow"):
                self.surface_quality = "normal"

            saved_surface_ramp = settings.value("surface_color_ramp", None)
            if saved_surface_ramp:
                try:
                    self.surface_color_ramp = [
                        (float(pos), tuple(color))
                        for pos, color in saved_surface_ramp
                    ]
                    print(f"✅ Loaded saved surface ramp: {len(self.surface_color_ramp)} stops")
                except Exception as e:
                    print(f"⚠️ Failed to load surface ramp: {e}")
                    self.surface_color_ramp = None
            
            # Load clip percentiles
            self.elevation_clip_low = settings.value("elevation_clip_low", 1.0, type=float)
            self.elevation_clip_high = settings.value("elevation_clip_high", 99.0, type=float)
            
            # ══════════════════════════════════════════════════════════
            # INTENSITY SETTINGS
            # ══════════════════════════════════════════════════════════
            self.intensity_gamma = settings.value("intensity_gamma", 1.65, type=float)
            self.intensity_clip_low = settings.value("intensity_clip_low", 0.5, type=float)
            self.intensity_clip_high = settings.value("intensity_clip_high", 99.8, type=float)
            
            print(f"✅ Display settings loaded:")
            print(f"   Elevation: {self.elevation_clip_low}%-{self.elevation_clip_high}%")
            print(f"   Intensity: gamma={self.intensity_gamma:.2f}, {self.intensity_clip_low}%-{self.intensity_clip_high}%")
            
        except Exception as e:
            print(f"⚠️ Failed to load display settings: {e}")
            # Keep defaults if load fails

    def toggle_gis_layers_panel(self):
        """Show/hide the Overlay Control Center for imported GIS layers (Alt+C)."""
        try:
            from gui.gis.gis_layers import toggle_gis_layers_panel
            result = toggle_gis_layers_panel(self)
            self._sync_activity_bar()
            return result
        except Exception as exc:
            print(f"⚠️ Overlay Control Center failed to open: {exc}")
            import traceback
            traceback.print_exc()
            return None

    def toggle_gdb_panel(self):
        """Show/hide the GDB import panel for ESRI File Geodatabases."""
        try:
            from gui.gis.gdb import toggle_gdb_panel
            return toggle_gdb_panel(self)
        except Exception as exc:
            print(f"⚠️ GDB panel failed to open: {exc}")
            import traceback
            traceback.print_exc()
            return None

    # ── Drag-and-drop import of GIS overlays ────────────────────────────────
    # Supported extensions that we will accept on a drop. Anything else is
    # ignored so we don't interfere with other drop targets.
    _GIS_DROP_EXTS = (".shp", ".tif", ".tiff", ".geojson", ".json", ".gdb")

    def _gis_paths_from_event(self, event):
        """Return the list of droppable GIS file paths from a drag/drop event."""
        md = event.mimeData()
        if not md or not md.hasUrls():
            return []
        paths = []
        for url in md.urls():
            if not url.isLocalFile():
                continue
            p = url.toLocalFile()
            if p.lower().endswith(self._GIS_DROP_EXTS):
                paths.append(p)
        return paths

    def dragEnterEvent(self, event):
        """Accept the drag only if it carries at least one supported GIS file."""
        try:
            if self._gis_paths_from_event(event):
                event.acceptProposedAction()
                return
        except Exception as exc:
            print(f"⚠️ dragEnterEvent: {exc}")
        event.ignore()

    def dragMoveEvent(self, event):
        try:
            if self._gis_paths_from_event(event):
                event.acceptProposedAction()
                return
        except Exception:
            pass
        event.ignore()

    def dropEvent(self, event):
        """Import every supported GIS file that was dropped onto the window."""
        try:
            paths = self._gis_paths_from_event(event)
            if not paths:
                event.ignore()
                return
            event.acceptProposedAction()

            from gui.gis.gis_layers import (_import_one, show_gis_layers_panel,
                                            sort_imports_vectors_first)
            ok = 0
            for p in sort_imports_vectors_first(paths):
                try:
                    if _import_one(self, p):
                        ok += 1
                except Exception as exc:
                    print(f"   ❌ Drop import error for {p}: {exc}")
                    import traceback
                    traceback.print_exc()
            if ok:
                show_gis_layers_panel(self)
            print(f"📥 Drag-drop import: {ok}/{len(paths)} file(s) loaded")
        except Exception as exc:
            print(f"⚠️ dropEvent: {exc}")
            event.ignore()

    def open_backup_settings(self):
        """Open the backup settings dialog."""
        from gui.backup_settings_dialog import BackupSettingsDialog
        
        dialog = BackupSettingsDialog(self)
        dialog.settings_changed.connect(self._load_backup_settings)
        dialog.exec()
        return dialog

    def _start_soak_telemetry_on_startup(self):
        """Start soak telemetry automatically in background at app launch."""
        try:
            if getattr(self, "_soak_telemetry", None) is None:
                from gui.soak_telemetry import SoakTelemetryController

                self._soak_telemetry = SoakTelemetryController(self)
            print("✅ Soak telemetry started automatically (5s sampling)")
            return True
        except Exception as e:
            print(f"⚠️ Failed to auto-start soak telemetry: {e}")
            return False

    def open_soak_telemetry(self):
        """Open the long-session telemetry panel (sampling starts at app launch)."""
        try:
            if getattr(self, "_soak_telemetry", None) is None:
                from gui.soak_telemetry import SoakTelemetryController

                self._soak_telemetry = SoakTelemetryController(self)

            self._soak_telemetry.show()
            if hasattr(self, "statusBar") and self.statusBar():
                self.statusBar().showMessage("Soak telemetry panel opened (5s sampling running)", 3000)
            return self._soak_telemetry
        except Exception as e:
            print(f"⚠️ Failed to open soak telemetry: {e}")
            if hasattr(self, "statusBar") and self.statusBar():
                self.statusBar().showMessage(f"Soak telemetry failed to open: {e}", 5000)
            QMessageBox.warning(
                self,
                "Soak Telemetry",
                f"Failed to open the resource monitoring panel.\n\n{e}",
            )
            return None

    def _get_backup_path(self, original_path):
        """
        Get the backup file path based on user settings.
        
        Args:
            original_path: Path to the original file
        
        Returns:
            Full path for the backup file
        """
        import os
        from datetime import datetime
        
        settings = QSettings("NakshaAI", "LidarApp")
        
        # Get folder
        use_custom = settings.value("backup_use_custom_path", False, type=bool)
        
        if use_custom:
            backup_folder = settings.value("backup_custom_path", "", type=str)
            if not backup_folder or not os.path.exists(backup_folder):
                # Fallback to original folder
                backup_folder = os.path.dirname(original_path)
        else:
            backup_folder = os.path.dirname(original_path)
        
        # Get filename format
        filename = os.path.basename(original_path)
        name, ext = os.path.splitext(filename)
        
        naming_format = settings.value("backup_naming_format", 0, type=int)
        
        if naming_format == 0:
            # filename_backup.laz
            backup_name = f"{name}_backup{ext}"
        elif naming_format == 1:
            # filename_YYYYMMDD_HHMMSS.laz
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"{name}_{timestamp}{ext}"
        else:
            # filename_autosave.laz
            backup_name = f"{name}_autosave{ext}"
        
        return os.path.join(backup_folder, backup_name)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            was_minimized = getattr(self, "_was_minimized", False)
            is_minimized = self.isMinimized()
            if is_minimized != was_minimized:
                self._was_minimized = is_minimized
                if is_minimized:
                    self._handle_app_minimized()
                else:
                    self._handle_app_restored()
        super().changeEvent(event)

    def _handle_app_minimized(self):
        """
        Minimize/hide all child dialogs, floating windows (like ClassPicker),
        and any associated minimized chips when the main application is minimized.
        """
        self._restorable_child_widgets = []
        from PySide6.QtWidgets import QWidget, QDialog
        from PySide6.QtWidgets import QApplication as _QApp

        def _is_app_owned_window(widget) -> bool:
            """True if a top-level widget belongs to this AppWindow instance."""
            try:
                if widget is None or widget is self or not _qt_object_is_valid(widget):
                    return False
            except Exception:
                return False

            # Parent chain ownership (for normally-parented dialogs)
            try:
                parent = widget.parentWidget()
                guard = 0
                while parent is not None and guard < 64:
                    if parent is self:
                        return True
                    parent = parent.parentWidget()
                    guard += 1
            except Exception:
                pass

            # Common ownership attributes used across dialogs in this codebase.
            owner_attrs = ("app", "app_window", "main_window", "ribbon_parent")
            for attr in owner_attrs:
                try:
                    owner = getattr(widget, attr, None)
                    if owner is self:
                        return True
                    if owner is not None and getattr(owner, "app", None) is self:
                        return True
                    if owner is not None and getattr(owner, "app_window", None) is self:
                        return True
                except Exception:
                    continue

            # Some dialogs keep an indirection via parent_item.
            try:
                parent_item = getattr(widget, "parent_item", None)
                if parent_item is not None:
                    if getattr(parent_item, "app", None) is self:
                        return True
                    if getattr(parent_item, "app_window", None) is self:
                        return True
            except Exception:
                pass

            return False

        def _safe_add(candidates_set, widget):
            try:
                if widget is None or widget is self or not _qt_object_is_valid(widget):
                    return
                candidates_set.add(widget)
            except Exception:
                pass

        candidates = set()

        # 1) Owned child windows discovered via QObject tree
        for widget in self.findChildren(QWidget):
            try:
                if widget.isWindow() and widget != self:
                    _safe_add(candidates, widget)
            except (RuntimeError, ReferenceError):
                pass

        # 2) Explicit app dialog references (many are intentionally parentless)
        explicit_attrs = [
            "class_picker",
            "display_mode_dialog",
            "display_dialog",
            "displaymodedialog",
            "_classify_low_points_dlg",
            "_classify_isolated_points_dlg",
            "_classify_ground_dlg",
            "_classify_surface_points_dlg",
            "_classify_below_surface_dlg",
            "snt_dialog",
            "dxf_dialog",
            "_dxf_dialog",
            "dwg_dialog",
            "block_identifier_dialog",
            "block_creation_settings_dialog",
            "_view_selector_dialog",
            "cross_settings_dialog",
            "_sync_views_dialog",
            "_global_settings_dialog",
            "_element_selection_dialog",
            "active_tool_dialog",
        ]
        for attr in explicit_attrs:
            _safe_add(candidates, getattr(self, attr, None))

        # 3) Ribbon-hosted dialogs
        if hasattr(self, "ribbon_manager") and hasattr(self.ribbon_manager, "ribbons"):
            by_class_ribbon = self.ribbon_manager.ribbons.get("by_class")
            if by_class_ribbon:
                for attr in (
                    "inside_fence_dialog",
                    "by_class_dialog",
                    "closed_by_class_dialog",
                    "height_convert_dialog",
                    "low_points_dialog",
                    "isolated_dialog",
                    "ground_dialog",
                    "surface_points_dialog",
                    "below_surface_dialog",
                ):
                    _safe_add(candidates, getattr(by_class_ribbon, attr, None))

        # 4) Global top-level sweep for app-owned parentless dialogs
        app_instance = _QApp.instance()
        if app_instance is not None:
            for tlw in app_instance.topLevelWidgets():
                if _is_app_owned_window(tlw):
                    _safe_add(candidates, tlw)

        # Process all found candidates
        restorable_seen = set()
        for widget in candidates:
            try:
                is_target = (
                    isinstance(widget, QDialog) or
                    type(widget).__name__ in ("ClassPicker", "ClassSelectorDialog") or
                    (widget.isWindow() and widget != self and type(widget).__name__ not in ("QDockWidget", "QMenu", "QToolTip"))
                )
                if is_target:
                    if widget.isVisible():
                        widget_id = id(widget)
                        if widget_id not in restorable_seen:
                            self._restorable_child_widgets.append(widget)
                            restorable_seen.add(widget_id)
                        widget.hide()
                    
                    if hasattr(widget, "_chip") and widget._chip is not None:
                        chip = widget._chip
                        if chip.isVisible():
                            chip_id = id(chip)
                            if chip_id not in restorable_seen:
                                self._restorable_child_widgets.append(chip)
                                restorable_seen.add(chip_id)
                            chip.hide()
            except (RuntimeError, ReferenceError):
                pass

    def _handle_app_restored(self):
        """
        Restore any child dialogs, floating windows, and minimized chips
        that were hidden when the main application was minimized.
        """
        if not hasattr(self, "_restorable_child_widgets"):
            return
        
        for widget in self._restorable_child_widgets:
            try:
                if type(widget).__name__ == "_MinimizedChip":
                    widget.show()
                else:
                    widget.show()
                    if widget.isWindow():
                        widget.raise_()
            except (RuntimeError, ReferenceError):
                pass
        self._restorable_child_widgets = []

    def _on_alt_f4_blocked(self):
        """Alt+F4 is disabled to prevent VTK/OpenGL crash during shutdown."""
        pass  # Handled by AltF4Blocker in main.py

    def nativeEvent(self, eventType, message):
        """
        Intercept Windows native messages to block Alt+F4.
        WM_SYSKEYDOWN with VK_F4 = Alt+F4 key combo.
        WM_SYSCOMMAND with SC_CLOSE = resulting close command.
        """
        if eventType == b"windows_generic_MSG" or eventType == b"windows_dispatcher_MSG":
            import ctypes
            import ctypes.wintypes

            msg = ctypes.wintypes.MSG.from_address(int(message))

            WM_SYSKEYDOWN = 0x0104
            WM_SYSCOMMAND = 0x0112
            VK_F4 = 0x73
            SC_CLOSE = 0xF060

            # if msg.message == WM_SYSKEYDOWN and msg.wParam == VK_F4:
            #     self._alt_f4_native_blocked = True
            #     return True, 0

            # if msg.message == WM_SYSCOMMAND and (msg.wParam & 0xFFF0) == SC_CLOSE:
            #     if getattr(self, "_alt_f4_native_blocked", False):
            #         self._alt_f4_native_blocked = False
            #         return True, 0
            
            if msg.message == WM_SYSKEYDOWN and msg.wParam == VK_F4:
                shortcuts = getattr(self, "shortcuts", {})
                if ("alt", "F4") in shortcuts:
                    # Alt+F4 is a user-mapped shortcut. Synthesize a normal Qt
                    # KeyPress so the GlobalShortcutFilter can dispatch it.
                    # We must consume the native message here to prevent the OS
                    # from turning it into WM_SYSCOMMAND/SC_CLOSE.
                    from PySide6.QtGui import QKeyEvent
                    from PySide6.QtCore import QCoreApplication
                    synth = QKeyEvent(
                        QEvent.KeyPress,
                        Qt.Key_F4,
                        Qt.AltModifier,
                    )
                    QCoreApplication.postEvent(self, synth)
                    return True, 0
                else:
                    self._alt_f4_native_blocked = True
                    return True, 0

            if msg.message == WM_SYSCOMMAND and (msg.wParam & 0xFFF0) == SC_CLOSE:
                if getattr(self, "_alt_f4_native_blocked", False):
                    self._alt_f4_native_blocked = False
                    return True, 0


        return super().nativeEvent(eventType, message)

    def closeEvent(self, event):
        """
        Graceful, production-grade cleanup for a graphics app.
        ✅ ENHANCED: Closes ALL dialogs and popups without leaving anything behind
        ✅ FIX: Proper cut section cleanup BEFORE VTK widget destruction
        """
        if getattr(self, "_shutdown_in_progress", False):
            event.accept()
            return

        reply = QMessageBox.question(
            self, "Close Naksha Lidar",
            "Are you sure you want to exit the application?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.No:
            event.ignore()
            print("User cancelled closing the main window")
            return

        self._shutdown_in_progress = True
        print("🧹 Main window closing - cleaning up child dialogs")

        # ✅ Temp Fence tool: full teardown (actors + popup + timer)
        try:
            _tft = getattr(self, "temp_fence_tool", None)
            if _tft is not None:
                _tft.shutdown()
        except Exception as _e:
            print(f"⚠️ Temp fence shutdown failed: {_e}")

        # Classifiers cooperatively mutate the shared class array in QThreads.
        # Join them before deleting dialogs, point data, or VTK resources.
        classification_workers = list(
            getattr(self, "_lidar_classification_workers", [])
        )
        for worker in classification_workers:
            try:
                if worker is not None and worker.isRunning():
                    worker.abort()
            except Exception:
                pass
        unfinished_classifiers = []
        for worker in classification_workers:
            try:
                if (
                    worker is not None
                    and worker.isRunning()
                    and not worker.wait(15000)
                ):
                    unfinished_classifiers.append(worker)
            except Exception:
                unfinished_classifiers.append(worker)
        if unfinished_classifiers:
            self._shutdown_in_progress = False
            event.ignore()
            QMessageBox.warning(
                self,
                "Classification Still Cancelling",
                "A LiDAR classification is still reaching a safe stop point. "
                "The application has not closed. Please try again shortly.",
            )
            return
        active_classification_dialog = getattr(
            self,
            "_lidar_classification_active_dialog",
            None,
        )
        if active_classification_dialog is not None:
            try:
                active_classification_dialog._rollback_classification_snapshot(
                    getattr(
                        active_classification_dialog,
                        "_active_old_cls",
                        None,
                    )
                )
                active_classification_dialog._active_old_cls = None
            except Exception:
                pass

        try:
            self._shutdown_canvas_cursor_system()
        except Exception:
            pass

        # Detach grid-label callbacks early to avoid mouse/key events racing VTK teardown.
        try:
            glm = getattr(self, "grid_label_manager", None)
            if glm is not None and hasattr(glm, "remove_interactor_observers"):
                glm.remove_interactor_observers()
        except Exception:
            pass

        # List of dialog attributes to clean up
        dialog_attrs = [
            'inside_fence_dialog',
            'by_class_dialog',
            'closed_by_class_dialog',
            'height_convert_dialog',
            'display_mode_dialog',
            'display_dialog',
            # ── attachment / tool dialogs stored directly on app ──────────
            'snt_dialog',
            'dxf_dialog',
            'dwg_dialog',
            'block_identifier_dialog',
            'block_creation_settings_dialog',
            '_view_selector_dialog',
            'cross_settings_dialog',
            '_sync_views_dialog',
            '_global_settings_dialog',
            '_element_selection_dialog',
        ]
        for attr in dialog_attrs:
            if hasattr(self, attr):
                dialog = getattr(self, attr)
                if dialog:
                    try:
                        dialog.close()
                        print(f"   ✅ Closed {attr}")
                    except Exception:
                        pass
       
        # Close ribbon dialogs
        if hasattr(self, 'ribbon_manager') and hasattr(self.ribbon_manager, 'ribbons'):
            by_class_ribbon = self.ribbon_manager.ribbons.get('by_class')
            if by_class_ribbon:
                for attr in ['inside_fence_dialog', 'by_class_dialog', 'closed_by_class_dialog', 'height_convert_dialog']:
                    if hasattr(by_class_ribbon, attr):
                        dialog = getattr(by_class_ribbon, attr)
                        if dialog:
                            try:
                                dialog.close()
                            except Exception:
                                pass
       
        print("✅ Main window cleanup complete")

        print("=" * 60)
        print("Main Window Closing - Graceful Cleanup")
        print("=" * 60)

        try:
            # Stop all cross-section sync pathways before VTK teardown.
            try:
                self.cleanup_camera_sync_on_shutdown()
            except Exception:
                pass
            try:
                self.disable_all_camera_sync()
            except Exception:
                pass

            # Stop memory guard early so its timers do not race with teardown.
            mem_guard = getattr(self, "_mem_guard", None)
            if mem_guard is not None:
                try:
                    mem_guard.stop()
                except Exception:
                    pass

            section_pick_obs_id = getattr(self, "_section_pick_click_observer_id", None)
            if section_pick_obs_id is not None:
                try:
                    sec_vtk = getattr(self, "sec_vtk", None)
                    if sec_vtk is not None and getattr(sec_vtk, "interactor", None) is not None:
                        sec_vtk.interactor.RemoveObserver(section_pick_obs_id)
                except Exception:
                    pass
                self._section_pick_click_observer_id = None

            gpu_mgr = getattr(self, "gpu_render_manager", None)
            if gpu_mgr is not None:
                try:
                    gpu_mgr.cleanup()
                except Exception:
                    pass

            soak = getattr(self, "_soak_telemetry", None)
            if soak is not None:
                try:
                    soak.shutdown()
                except Exception:
                    pass
                self._soak_telemetry = None

            try:
                from .memory_manager import ObserverRegistry

                ObserverRegistry.release_all()
            except Exception:
                pass

            # ============================================================
            # SAVE WINDOW STATE AND INDIVIDUAL DOCK GEOMETRIES
            # ============================================================
            try:
                # Save main window state
                self.settings.setValue("geometry", self.saveGeometry())
                self.settings.setValue("windowState", self.saveState())

                # ✅ NEW: Save each cross-section dock geometry individually
                if hasattr(self, 'section_docks') and self.section_docks:
                    for view_index, dock in self.section_docks.items():
                        dock_geo_key = f"CrossSectionDock_{view_index}_geometry"
                        self.settings.setValue(dock_geo_key, dock.saveGeometry())
                        print(f"   ✅ Saved dock {view_index + 1} geometry")
                
                # ✅ NEW: Save cut section dock geometry
                if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
                    if hasattr(self.cut_section_controller, 'cut_dock') and self.cut_section_controller.cut_dock:
                        try:
                            self.settings.setValue(
                                "CutSectionDock_geometry",
                                self.cut_section_controller.cut_dock.saveGeometry()
                            )
                            print(f"   ✅ Saved cut section dock geometry")
                        except Exception as e:
                            print(f"   ⚠️ Cut dock geometry save failed: {e}")

                print("✓ Window state and dock positions saved")
            except Exception as e:
                print(f"Warning: Could not save window state: {e}")

            # ============================================================
            # ✅ CRITICAL FIX: CLEAN CUT SECTION CONTROLLER FIRST
            # Must happen BEFORE any VTK widget cleanup
            # ============================================================
            print("\n🔧 Cleaning cut section controller...")
            # Suppress expected VTK teardown warnings while dock widgets are being finalized.
            try:
                import vtk
                vtk.vtkObject.SetGlobalWarningDisplay(0)
            except Exception:
                pass
            if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
                try:
                    if hasattr(self.cut_section_controller, 'cut_dock') and self.cut_section_controller.cut_dock:
                        self.cut_section_controller._force_close_requested = True
                        try:
                            dock = self.cut_section_controller.cut_dock
                            dock.hide()                        # ✅ destroy HWND first
                            if hasattr(self, 'removeDockWidget'):
                                self.removeDockWidget(dock)    # ✅ detach from main window
                            dock.close()
                        except Exception:
                            pass

                    self.cut_section_controller.clear()
                    self.cut_section_controller = None
                    print("   ✅ Cut section controller cleaned")
                except Exception as e:
                    print(f"   ⚠️ Cut section cleanup error: {e}")
                    import traceback
                    traceback.print_exc()

            # ============================================================
            # CLOSE ALL DIALOGS AND POPUPS (COMPREHENSIVE)
            # ============================================================
            print("\n🔒 Closing all dialogs and popups...")

            # ✅ 1. Close the "Select Cross-Section View" dialog
            if hasattr(self, '_view_selector_dialog') and self._view_selector_dialog:
                try:
                    print("   🔒 Closing 'Select Cross-Section View' dialog")
                    if self._view_selector_dialog.isVisible():
                        self._view_selector_dialog.close()
                    self._view_selector_dialog.deleteLater()
                    self._view_selector_dialog = None
                    print("   ✅ 'Select Cross-Section View' dialog closed")
                except Exception as e:
                    print(f"   ⚠️ Failed to close view selector dialog: {e}")

            # ✅ 2. Close Display Mode dialog
            if hasattr(self, "display_dialog") and self.display_dialog:
                try:
                    print("   🔒 Closing Display Mode dialog")
                    self.display_dialog.close()
                    self.display_dialog.deleteLater()
                    self.display_dialog = None
                    print("   ✅ Display Mode dialog closed")
                except Exception as e:
                    print(f"   ⚠️ Failed to close display dialog: {e}")

            # ✅ 3. Close legacy displaymodedialog (if it exists separately)
            if hasattr(self, "displaymodedialog") and self.displaymodedialog:
                try:
                    print("   🔒 Closing legacy Display Mode dialog")
                    self.displaymodedialog.close()
                    self.displaymodedialog.deleteLater()
                    self.displaymodedialog = None
                    print("   ✅ Legacy Display Mode dialog closed")
                except Exception as e:
                    print(f"   ⚠️ Failed to close legacy display dialog: {e}")

            # ✅ 4. Close Class Picker
            if hasattr(self, "class_picker") and self.class_picker:
                try:
                    print("   🔒 Closing Class Picker")
                    self.class_picker.close()
                    self.class_picker.deleteLater()
                    print("   ✅ Class Picker closed")
                except Exception as e:
                    print(f"   ⚠️ Failed to close class picker: {e}")
                finally:
                    self.class_picker = None

            # ✅ 5. Close ShortcutManager (Config Tools)
            try:
                from gui.shortcut_manager import ShortcutManager
                if ShortcutManager.instance is not None:
                    print("   🔒 Closing Shortcut Manager (Config Tools)")
                    inst = ShortcutManager.instance
                    ShortcutManager.instance = None
                    try:
                        inst._do_real_close()
                    except Exception:
                        pass
                    try:
                        inst.deleteLater()
                    except Exception:
                        pass
                    print("   ✅ Shortcut Manager closed")
            except Exception as e:
                print(f"   ⚠️ Failed to close ShortcutManager: {e}")

            # ✅ 6. Close any active tool dialog
            if hasattr(self, "active_tool_dialog") and self.active_tool_dialog:
                try:
                    if self.active_tool_dialog.isVisible():
                        print("   🔒 Closing active tool dialog")
                        self.active_tool_dialog.close()
                        self.active_tool_dialog.deleteLater()
                        self.active_tool_dialog = None
                        print("   ✅ Active tool dialog closed")
                except Exception as e:
                    print(f"   ⚠️ Failed to close active tool dialog: {e}")

            # ✅ 7. Close ALL remaining QDialog instances (catch-all)
            try:
                from PySide6.QtWidgets import QDialog
                all_dialogs = self.findChildren(QDialog)
                if all_dialogs:
                    print(f"   🔍 Found {len(all_dialogs)} additional dialog(s)")
                    for dialog in all_dialogs:
                        try:
                            if dialog.isVisible():
                                dialog_name = dialog.windowTitle() or type(dialog).__name__
                                print(f"   🔒 Closing: {dialog_name}")
                                dialog.close()
                                dialog.deleteLater()
                        except Exception as e:
                            print(f"   ⚠️ Failed to close dialog: {e}")
                    print(f"   ✅ All {len(all_dialogs)} dialog(s) closed")
            except Exception as e:
                print(f"   ⚠️ Failed to close remaining dialogs: {e}")

# ── INSERT BEFORE (new step 7b) ───────────────────────────────────────────
            # ✅ 7b. Close lidar classification + block-creation dialogs
            #         stored on ribbon panel objects (not on self).
            try:
                ribbon_dialog_attrs = [
                    'low_points_dialog',
                    'isolated_dialog',
                    'ground_dialog',
                    'surface_points_dialog',
                    'below_surface_dialog',
                ]
                ribbon_panel = None
                if hasattr(self, 'ribbon_manager') and hasattr(self.ribbon_manager, 'ribbons'):
                    ribbon_panel = self.ribbon_manager.ribbons.get('by_class')

                if ribbon_panel:
                    for attr in ribbon_dialog_attrs:
                        dlg = getattr(ribbon_panel, attr, None)
                        if dlg is not None:
                            try:
                                dlg_name = type(dlg).__name__
                                dlg.hide()
                                dlg.close()
                                dlg.deleteLater()
                                setattr(ribbon_panel, attr, None)
                                print(f"   ✅ Closed ribbon dialog: {dlg_name}")
                            except Exception as e:
                                print(f"   ⚠️ Could not close {attr}: {e}")
            except Exception as e:
                print(f"   ⚠️ Failed to close ribbon panel dialogs: {e}")

            # ✅ 8. Close ALL QWidget-based popups (if any)
            try:
                from PySide6.QtWidgets import QWidget
                from PySide6.QtCore import Qt
                all_widgets = self.findChildren(QWidget)
                popup_widgets = [w for w in all_widgets if w.windowFlags() & Qt.Tool or w.windowFlags() & Qt.Popup]
                if popup_widgets:
                    print(f"   🔍 Found {len(popup_widgets)} popup widget(s)")
                    for widget in popup_widgets:
                        try:
                            if widget.isVisible() and widget != self:
                                widget_name = widget.objectName() or type(widget).__name__
                                print(f"   🔒 Closing popup: {widget_name}")
                                widget.close()
                                widget.deleteLater()
                        except Exception as e:
                            print(f"   ⚠️ Failed to close popup widget: {e}")
                    print(f"   ✅ All {len(popup_widgets)} popup(s) closed")
            except Exception as e:
                print(f"   ⚠️ Failed to close popup widgets: {e}")

            # ✅ 9. Kill all floating minimize-chips (Qt.Tool windows – NOT
            #        children of self, so findChildren() cannot see them).
            try:
                close_all_chips()
                print("   ✅ All minimized chips removed from taskbar")
            except Exception as e:
                print(f"   ⚠️ Failed to close minimize chips: {e}")

            # ✅ 10. Force-close every OS-level top-level window that still
            #         belongs to this application (catches dialogs that called
            #         showMinimized() and are therefore top-level, plus any
            #         other parentless QWidget left over).
            try:
                from PySide6.QtWidgets import QApplication as _QApp   # local alias avoids shadowing
                app_instance = _QApp.instance()
                if app_instance:
                    for tlw in app_instance.topLevelWidgets():
                        try:
                            if tlw is self or not _qt_object_is_valid(tlw):
                                continue
                            if tlw.isVisible() or tlw.windowState() & Qt.WindowMinimized:
                                tlw_name = tlw.windowTitle() or type(tlw).__name__
                                print(f"   🔒 Closing top-level window: {tlw_name}")
                                tlw.hide()
                                tlw.close()
                                tlw.deleteLater()
                        except Exception as e:
                            print(f"   ⚠️ Could not close top-level window: {e}")
                    print("   ✅ All top-level windows closed")
            except Exception as e:
                print(f"   ⚠️ Failed to sweep top-level windows: {e}")

            print("✅ All dialogs and popups closed\n")

            # ============================================================
            # STOP BACKGROUND THREADS AND TIMERS
            # ============================================================
            import time
            import gc
            from PySide6.QtCore import QTimer, QCoreApplication, QThread

            def shutdown_thread(thread, label, timeout_ms=5000):
                if not isinstance(thread, QThread):
                    return True

                try:
                    if thread.isRunning():
                        print(f"Waiting for {label} thread to finish...")
                        try:
                            thread.requestInterruption()
                        except Exception:
                            pass
                        thread.quit()
                        waited = thread.wait(timeout_ms)
                        if not waited:
                            print(f"⚠️ {label} thread still running after timeout; leaving it to finish safely")
                            if thread not in _SHUTDOWN_THREAD_GUARD:
                                _SHUTDOWN_THREAD_GUARD.append(thread)
                                thread.finished.connect(
                                    lambda thr=thread: _SHUTDOWN_THREAD_GUARD.remove(thr)
                                    if thr in _SHUTDOWN_THREAD_GUARD else None
                                )
                            return False
                    else:
                        thread.wait(100)
                except Exception as exc:
                    print(f"   ⚠️ {label} thread shutdown error: {exc}")
                    return False
                return True

            # 1. Gracefully stop background threads (wait up to 8 seconds)
            if hasattr(self, "backupthread"):
                t = self.backupthread
                if t and t.isRunning():
                    print("Waiting for backup thread to finish...")
                    try:
                        t.requestInterruption()
                    except Exception:
                        pass
                    t.quit()
                    waited = t.wait(9000)
                    if not waited:
                        print("⚠️ Backup thread still running after timeout; leaving it to finish safely")
                        if t not in _SHUTDOWN_THREAD_GUARD:
                            _SHUTDOWN_THREAD_GUARD.append(t)
                            t.finished.connect(
                                lambda thr=t: _SHUTDOWN_THREAD_GUARD.remove(thr)
                                if thr in _SHUTDOWN_THREAD_GUARD else None
                            )
                    else:
                        self.backupthread = None
                    print("Backup thread closed")

            color_thread = getattr(self, "color_thread", None)
            if color_thread is not None:
                for worker in list(getattr(self, "color_workers", [])):
                    try:
                        if hasattr(worker, "cancel"):
                            worker.cancel()
                        elif hasattr(worker, "stop"):
                            worker.stop()
                    except Exception as exc:
                        print(f"   ⚠️ Color worker shutdown error: {exc}")
                color_stopped = shutdown_thread(color_thread, "color")
                if color_stopped:
                    try:
                        color_thread.deleteLater()
                    except Exception:
                        pass
                    self.color_thread = None
                self.color_workers = []
                print("Color thread closed")

            # DTM/DSM raster builders are QThreads owned by the main window.
            # Cancel and join them before point arrays and VTK resources are
            # released, otherwise Qt can destroy a running worker at shutdown.
            elevation_jobs = getattr(self, "_elevation_model_jobs", None)
            if isinstance(elevation_jobs, dict):
                for surface_name, job in list(elevation_jobs.items()):
                    worker = job.get("worker") if isinstance(job, dict) else None
                    try:
                        if worker is not None and hasattr(worker, "cancel"):
                            worker.cancel()
                    except Exception:
                        pass
                    shutdown_thread(worker, f"{surface_name} elevation-model", 10000)
                elevation_jobs.clear()

            # 2. Stop ALL timers, waiting for events to flush
            if hasattr(self, "backuptimer") and self.backuptimer:
                try:
                    self.backuptimer.stop()
                    self.backuptimer.deleteLater()
                    self.backuptimer = None
                except Exception:
                    pass

            for timer in self.findChildren(QTimer):
                try:
                    if timer.isActive():
                        timer.stop()
                    timer.deleteLater()
                except Exception:
                    pass
            print("All timers stopped")

            # ============================================================
            # REMOVE MANUAL INTERACTOR OBSERVERS (BEFORE VTK CLEANUP)
            # ============================================================
            try:
                glm = getattr(self, "grid_label_manager", None)
                if glm is not None and hasattr(glm, "remove_interactor_observers"):
                    glm.remove_interactor_observers()
            except Exception:
                pass

            # ============================================================
            # CLEAN VTK WIDGETS
            # ============================================================
            # Suppress VTK warning spam during teardown of destroyed Win32 handles.
            try:
                import vtk
                output = vtk.vtkOutputWindow()
                output.GlobalWarningDisplayOff()
            except Exception:
                pass

            def cleanvtkwidget(vtkwidget, label):
                """Stop PyVista render timers safely before render-window finalization."""
                try:
                    setattr(vtkwidget, "_naksha_skip_render", True)
                except Exception:
                    pass
                for timer_attr in ("render_timer", "rendertimer"):
                    if not hasattr(vtkwidget, timer_attr):
                        continue
                    try:
                        timer_obj = getattr(vtkwidget, timer_attr)
                        if timer_obj is not None:
                            try:
                                timer_obj.stop()
                                time.sleep(0.05)
                                timer_obj.deleteLater()
                            except RuntimeError:
                                # Timer already deleted by Qt - this is OK
                                pass
                        setattr(vtkwidget, timer_attr, None)
                    except Exception:
                        try:
                            setattr(vtkwidget, timer_attr, None)
                        except Exception:
                            pass
                
                # ✅ FIX: Safe render window finalization (VTK version compatibility)
                try:
                    if hasattr(vtkwidget, "GetRenderWindow"):
                        rw = vtkwidget.GetRenderWindow()
                        if rw:
                            # ✅ Check if SetMapped exists before calling (not all VTK versions have it)
                            if hasattr(rw, 'SetMapped'):
                                rw.SetMapped(False)
                            rw.Finalize()
                            del rw
                except Exception as e:
                    print(f"   ⚠️ RenderWindow finalize warning: {e}")
                
                if hasattr(vtkwidget, "SetRenderWindow"):
                    vtkwidget.SetRenderWindow(None)
                if hasattr(vtkwidget, "ren") and vtkwidget.ren:
                    vtkwidget.ren.RemoveAllViewProps()
                
                vtkwidget.deleteLater()
                print(f"{label} VTK widget cleaned")


            # Clean cross-section VTK widgets
            if hasattr(self, "section_vtks") and self.section_vtks:
                for viewidx, vtkwidget in list(self.section_vtks.items()):
                    try:
                        cleanvtkwidget(vtkwidget, f"Cross-section View {viewidx+1}")
                    except Exception as e:
                        print(f"Cross-section VTK error: {e}")
                self.section_vtks.clear()
                self.section_vtks = None

            # Alternative attribute name check
            if hasattr(self, "sectionvtks") and self.sectionvtks:
                for viewidx, vtkwidget in list(self.sectionvtks.items()):
                    try:
                        cleanvtkwidget(vtkwidget, f"Cross-section View {viewidx+1}")
                    except Exception as e:
                        print(f"Cross-section VTK error: {e}")
                self.sectionvtks.clear()
                self.sectionvtks = None

            # ✅ FIX: Cut section VTK cleanup (already handled above, skip duplicate)
            # Cut section controller was already cleaned at the start

            # Clean main VTK widget
            if hasattr(self, "vtk_widget") and self.vtk_widget:
                try:
                    cleanvtkwidget(self.vtk_widget, "Main")
                    self.vtk_widget = None
                except Exception as e:
                    print(f"Main VTK error: {e}")

            # Alternative attribute name check
            if hasattr(self, "vtkwidget") and self.vtkwidget:
                try:
                    cleanvtkwidget(self.vtkwidget, "Main")
                    self.vtkwidget = None
                except Exception as e:
                    print(f"Main VTK error: {e}")

            # ============================================================
            # FINAL CLEANUP
            # ============================================================
            # Clear data objects
            if hasattr(self, "data"):
                self.data = None
                print("Data references cleared")

            # Suppress VTK warnings (optional)
            try:
                import vtk
                output = vtk.vtkOutputWindow()
                output.GlobalWarningDisplayOff()
            except Exception:
                pass

            # Wait for OS/resource cleanup
            print("Waiting for OS/resource cleanup (0.5s)...")
            time.sleep(0.5)
            gc.collect()

            print("=" * 60)
            print("Cleanup Complete - Window Closing")
            print("=" * 60)
            event.accept()

        except Exception as e:
            print(f"Critical error in graceful shutdown: {e}")
            import traceback
            traceback.print_exc()
            event.accept()
        
        super().closeEvent(event)

        # Keep shutdown flag true while windows are actually closing.
        # Reset if close was aborted or the main window stayed visible.
        if (not event.isAccepted()) or self.isVisible():
            self._shutdown_in_progress = False

    def deactivate_all_tools(self):
        """Disable all interactor observers and reset to default."""
        if hasattr(self, "sec_vtk") and self.sec_vtk is not None:
            iren = self.sec_vtk.interactor
            iren.RemoveObservers("LeftButtonPressEvent")
            iren.RemoveObservers("MouseMoveEvent")
            iren.RemoveObservers("LeftButtonReleaseEvent")
            iren.SetInteractorStyle(None)
        self.active_tool = None
        print("🔵 All tools deactivated.")

    def enable_2d_lock(self):
        """
        Lock the VTK interactor to 2D mode (no 3D rotation).
        Allows only pan and zoom — ideal for Plan View.
        """
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
        interactor = self.vtk_widget.interactor

        # Force orthographic projection
        cam = self.vtk_widget.renderer.GetActiveCamera()
        cam.ParallelProjectionOn()
        cam.SetViewUp(0, 1, 0)
        cam.SetPosition(0, 0, 1)
        cam.SetFocalPoint(0, 0, 0)
        self.vtk_widget.renderer.ResetCamera()
        self.vtk_widget.render()

        style = vtkInteractorStyleImage()  # pan/zoom only, no rotation
        interactor.SetInteractorStyle(style)
        print("🔒 Plan View locked to 2D (rotation disabled).")

    def enable_3d_orbit(self):
        """
        Restore normal 3D orbit controls (trackball).
        """
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
        interactor = self.vtk_widget.interactor
        style = vtkInteractorStyleTrackballCamera()
        interactor.SetInteractorStyle(style)

        cam = self.vtk_widget.renderer.GetActiveCamera()
        cam.ParallelProjectionOff()
        self.vtk_widget.render()
        print("🌀 3D orbit mode re-enabled.")



    def save_file_with_drawings(self, filepath=None):
        """
        Save point cloud AND drawings together.
        Wrapper that calls both save functions.
        """
        from .save_pointcloud import save_pointcloud
        
        # Save the point cloud
        result = save_pointcloud(self, filepath)
        
        if result and hasattr(self, 'digitizer') and self.digitizer:
            # Use the actual saved filepath
            actual_path = getattr(self, 'last_save_path', filepath)
            if actual_path:
                self.digitizer.auto_save_drawings(actual_path)
        
        return result



    def _setup_sidebar_layout(self):
        """Setup horizontal ribbon layout below top bar"""
        if not hasattr(self, "ribbon_container"):
            # Create ribbon container widget
            self.ribbon_container = QWidget()
            self.ribbon_container.setObjectName("RibbonContainer")
        
        # Clear existing layout
        if self.ribbon_container.layout():
            QWidget().setLayout(self.ribbon_container.layout())
        
        ribbon_layout = QHBoxLayout(self.ribbon_container)
        ribbon_layout.setContentsMargins(8, 4, 8, 4)
        ribbon_layout.setSpacing(0)
        
        # Add all ribbons
        for name, ribbon in self.ribbon_manager.ribbons.items():
            ribbon.setParent(self.ribbon_container)
            ribbon_layout.addWidget(ribbon)
            ribbon.hide()
        
        ribbon_layout.addStretch()
        self.ribbon_container.setFixedHeight(0)  # hidden initially
        # ------------------------
                    
    def _connect_sidebar_actions(self):
        """Connect ribbon signals to main window actions once."""
        if getattr(self, "_sidebar_actions_connected", False):
            return

        # =========================
        # File ribbon
        # =========================
        file_ribbon = self.ribbon_manager.ribbons.get("file") if hasattr(self, "ribbon_manager") else None
        if file_ribbon:
            file_ribbon.open_file.connect(self.open_file)

            try:
                from .save_pointcloud import save_pointcloud
                file_ribbon.save_file.connect(lambda: save_pointcloud(self, path=None, show_dialog=True))
            except Exception as e:
                print(f"⚠️ Failed to connect Save As: {e}")

            if hasattr(file_ribbon, "save_quick"):
                file_ribbon.save_quick.connect(self._save_quick_no_dialog)

        # =========================
        # Edit ribbon
        # =========================
        edit_ribbon = self.ribbon_manager.ribbons["edit"]
        edit_ribbon.edit_action.connect(self.handle_edit_action)

        # =========================
        # View ribbon
        # =========================
        view_ribbon = self.ribbon_manager.ribbons["view"]
        view_ribbon.view_changed.connect(self.handle_view_change)
        view_ribbon.display_changed.connect(self._handle_display_mode_change)
        view_ribbon.shadow_toggled.connect(self.toggle_shadow_in_view)
        view_ribbon.depth_toggled.connect(self.toggle_depth_in_view)
        view_ribbon.saturation_changed.connect(self.on_saturation_changed)
        # sharpness_changed intentionally not connected (amplifier disabled)

        # =========================
        # Tools ribbon
        # =========================
        tools_ribbon = self.ribbon_manager.ribbons["tools"]
        tools_ribbon.tool_activated.connect(self.handle_tool_activation)

        # =========================
        # Classify ribbon
        # =========================
        classify_ribbon = self.ribbon_manager.ribbons["classify"]
        classify_ribbon.classify_tool_selected.connect(self.set_classify_tool)

        # =========================
        # Display ribbon
        # =========================
        display_ribbon = self.ribbon_manager.ribbons["display"]
        display_ribbon.display_mode_clicked.connect(self.open_display_mode)
        display_ribbon.fields_clicked.connect(self.open_fields_panel)
        display_ribbon.border_width_changed.connect(self.on_border_changed)
        print("✅ Border slider connected to on_border_changed")

        # =========================
        # Draw ribbon
        # =========================
        if hasattr(self, "digitizer"):
            draw_ribbon = self.ribbon_manager.ribbons["draw"]

            def on_draw_tool_selected(tool_name):
                self._draw_curve_context_active = False
                self._deactivate_pending_cut_section_tool("switching to drawing")

                # ✅ MUTUAL EXCLUSION: every draw tool (including the Ortho
                # polygon tool and Hatch Area) reacts to left-click on the
                # main view, which collides with Identify / Point Sync / SNT
                # pick. Disable the point-pick tools whenever a draw tool is
                # selected so they can never both be active at once.
                self._deactivate_point_pick_tools()

                # Deactivate curve tool when switching to a draw tool
                if hasattr(self, "curve_tool") and self.curve_tool:
                    if getattr(self.curve_tool, "active", False):
                        if hasattr(self.curve_tool, "suspend"):
                            self.curve_tool.suspend()
                        else:
                            self.curve_tool.deactivate()
                    if getattr(self.curve_tool, "_select_mode", False):
                        self.curve_tool.deactivate_select_mode()

                # Close the Parallel tool dialog when switching to any draw tool
                _parallel_dlg = getattr(self, "_parallel_tool_dialog", None)
                if _parallel_dlg is not None:
                    try:
                        _parallel_dlg.close()
                    except Exception:
                        pass

                _centerline_dlg = getattr(self, "_centerline_tool_dialog", None)
                if _centerline_dlg is not None:
                    try:
                        _centerline_dlg.close()
                    except Exception:
                        pass

                if hasattr(self, "digitizer"):
                    self.digitizer.enabled = True
                    print("✅ Digitizer enabled for drawing")

                    # Default behavior: normal Move Vertex remains free 360 movement.
                    self.digitizer.vertex_move_constraint_mode = "free"
                    self.digitizer.vertex_move_constraint_label = "Free 360 Move"

                    # Shift + Move Vertex opens controlled movement popup.
                    normalized_tool = str(tool_name or "").lower().replace(" ", "").replace("_", "")

                    if normalized_tool == "movevertex":
                        try:
                            shift_pressed = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
                        except Exception:
                            shift_pressed = False

                        if shift_pressed:
                            options = [
                                "Free 360 Move",
                                "Move Along Previous Edge Direction",
                                "Move Along Next Edge Direction",
                            ]

                            choice, ok = QInputDialog.getItem(
                                self,
                                "Move Vertex Mode",
                                "Choose how the selected vertex should move:",
                                options,
                                0,
                                False,
                            )

                            if not ok:
                                return

                            mode_map = {
                                "Free 360 Move": "free",
                                "Move Along Previous Edge Direction": "previous",
                                "Move Along Next Edge Direction": "next",
                            }

                            self.digitizer.vertex_move_constraint_mode = mode_map.get(choice, "free")
                            self.digitizer.vertex_move_constraint_label = choice

                self.digitizer.set_tool(tool_name)

            draw_ribbon.draw_tool_selected.connect(on_draw_tool_selected)
            draw_ribbon.clear_requested.connect(
                lambda: self.digitizer.clear_drawings(record_undo=True, preserve_committed=True)
            )
            if hasattr(draw_ribbon, "curve_tool_selected"):
                draw_ribbon.curve_tool_selected.connect(self.on_curve_button_clicked)

        # =========================
        # Measure ribbon
        # =========================
        measure_ribbon = self.ribbon_manager.ribbons["measure"]
        measure_ribbon.measure_tool_selected.connect(self.activate_measurement_tool)
        measure_ribbon.clear_measurements.connect(self.clear_all_measurements)

        # =========================
        # Curve ribbon
        # =========================
        curve_ribbon = self.ribbon_manager.ribbons.get("curve")
        if curve_ribbon:
            curve_ribbon.curve_tool_selected.connect(self.on_curve_button_clicked)
            curve_ribbon.clear_curves.connect(
                lambda: self.curve_tool.clear_all_curves() if hasattr(self, "curve_tool") else None
            )
            print("✅ Curve ribbon connected")

        self._sidebar_actions_connected = True
                
    def on_saturation_changed(self, value):
        """Handle saturation slider changes from View Ribbon."""
        # Convert percentage (0-200) to multiplier (0.0-2.0)
        self.current_saturation = value / 100.0
        print(f"🎨 Saturation set to {value}% (multiplier: {self.current_saturation:.2f}x)")
        
        # Refresh display if in depth or intensity mode
        if self.display_mode in ["depth", "intensity"]:
            from gui.pointcloud_display import update_pointcloud
            update_pointcloud(self, self.display_mode)
            self.statusBar().showMessage(f"🎨 Saturation: {value}%", 2000)

    def on_sharpness_changed(self, value):
        """
        Amplifier removed — this handler is intentionally a no-op.
        current_sharpness is permanently 1.0 to prevent depth/intensity values
        from corrupting the class color buffer when switching display modes.
        The signal is also disconnected at ribbon setup, so this should not fire.
        """
        # DO NOT update self.current_sharpness here.
        # DO NOT call update_pointcloud here.
        print(f"⚠️ on_sharpness_changed called (amplifier disabled) — ignoring value={value}")

            # In app_window.py - add to Tools ribbon
    def debug_class_0(self):
        """Show Class 0 diagnostic info"""
        if self.data is None:
            print("No data loaded")
            return
        
        unique_classes = np.unique(self.data["classification"])
        class_0_count = np.sum(self.data["classification"] == 0)
        
        info = f"""
        ═══════════════════════════════════
        CLASS 0 DIAGNOSTIC
        ═══════════════════════════════════
        Total points: {len(self.data['classification']):,}
        Class 0 points: {class_0_count:,} ({100*class_0_count/len(self.data['classification']):.1f}%)
        Unique classes: {unique_classes}
        
        Class 0 in palette: {0 in self.class_palette}
        Class 0 visible: {self.class_palette.get(0, {}).get('show', False)}
        Class 0 color: {self.class_palette.get(0, {}).get('color', 'NOT SET')}
        ═══════════════════════════════════
        """
        
        print(info)
        QMessageBox.information(self, "Class 0 Diagnostic", info)

        
    def resizeEvent(self, event):
        """Reposition sidebars on window resize"""
        super().resizeEvent(event)
        if hasattr(self, 'sidebar_manager'):
            for sidebar in self.sidebar_manager.sidebars.values():
                sidebar.move(self.width() - 280, 0)
                sidebar.resize(280, self.height())
        self._refresh_canvas_axis_guides_if_needed()


    def handle_view_change(self, mode):
        """Handle view mode changes from visual panel or sidebar"""
        
        # ✅ NEW: Store DXF visibility state
        dxf_visibility = {}
        if hasattr(self, 'dxf_actors'):
            for i, dxf_data in enumerate(self.dxf_actors):
                dxf_visibility[i] = []
                for actor in dxf_data['actors']:
                    dxf_visibility[i].append(actor.GetVisibility())
        
        # ✅ CRITICAL: Temporarily disable cross-section interactor BEFORE view change
        interactor_was_active = False
        if hasattr(self, 'cross_interactor') and self.cross_interactor:
            interactor_was_active = True
            cross_interactor_ref = self.cross_interactor
            
            from vtk import vtkInteractorStyleTrackballCamera
            self.vtk_widget.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
        
        # Switch interaction mode first, then apply the explicit target view.
        if mode == '3d':
            self.toggle_view_mode('3d')
        else:
            self.toggle_view_mode('2d')

        allow_3d_switch = mode == '3d'
        previous_allow_3d = getattr(self, "_allow_3d_switch", False)
        try:
            if allow_3d_switch:
                self._allow_3d_switch = True
            set_view(self, mode)
        finally:
            self._allow_3d_switch = previous_allow_3d
        
        # ✅ NEW: Restore DXF visibility after view change
        if hasattr(self, 'dxf_actors') and dxf_visibility:
            for i, dxf_data in enumerate(self.dxf_actors):
                if i in dxf_visibility:
                    for j, actor in enumerate(dxf_data['actors']):
                        if j < len(dxf_visibility[i]):
                            actor.SetVisibility(dxf_visibility[i][j])
        
        # ✅ CRITICAL: Re-attach interactor AFTER view change with fresh state
        if interactor_was_active:
            cross_interactor_ref.P1 = None
            cross_interactor_ref.P2 = None
            cross_interactor_ref.slice_state = 0
            
            self.vtk_widget.interactor.SetInteractorStyle(cross_interactor_ref)
            print(f"✅ Cross-section interactor reattached for {mode} view")
            
            if hasattr(self, 'section_controller') and self.section_controller:
                try:
                    self.section_controller.clear()
                except Exception as e:
                    print(f"⚠️ Clear failed: {e}")
        
        # Keep existing cross-section windows visible
        if hasattr(self, 'section_vtks') and self.section_vtks:
            for view_idx, vtk_widget in self.section_vtks.items():
                if vtk_widget:
                    try:
                        vtk_widget.render()
                    except Exception as e:
                        print(f"⚠️ View {view_idx + 1} render failed: {e}")
        
        # ✅ NEW: Final render to show DXF
        self.vtk_widget.GetRenderWindow().Render()

        if hasattr(self, "_schedule_main_view_history_commit"):
            self._schedule_main_view_history_commit(f"view_mode_{mode}", delay_ms=120)


    def _deactivate_selection_tools(self, reason="switching tools"):
        """Deactivate element/rectangle selection tools and close their panels."""
        dialog = getattr(self, "_element_selection_dialog", None)
        if dialog is not None:
            try:
                if getattr(dialog, "_is_minimized_to_chip", False):
                    dialog.restore_from_chip()
                if dialog.isVisible():
                    print(f"🛑 Closing element selection dialog ({reason})")
                    dialog.close()
            except Exception as e:
                print(f"⚠️ Failed to close element selection dialog: {e}")

        digitizer = getattr(self, "digitizer", None)
        if digitizer is not None:
            try:
                digitizer.deactivate_element_select_tool()
            except Exception as e:
                print(f"⚠️ Failed to deactivate element selection tool: {e}")

        select_rect = getattr(self, "select_rectangle_tool", None)
        if select_rect is not None:
            try:
                if getattr(select_rect, "active", False):
                    print(f"🛑 Deactivating rectangle selection tool ({reason})")
                    select_rect.deactivate()
            except Exception as e:
                print(f"⚠️ Failed to deactivate rectangle selection tool: {e}")

    def _deactivate_digitize_tool(self):
        """Deactivate any active digitize/selection tool (for mutual exclusion with section tools)."""
        self._deactivate_selection_tools("section tool activation")

        if hasattr(self, 'digitizer') and self.digitizer and getattr(self.digitizer, 'active_tool', None):
            try:
                print("🛑 Deactivating digitize tool before section tool activation")
                # Preserve in-progress draw state so the user can resume the
                # exact unfinished shape after leaving cross-section mode.
                resumable_tools = {
                    "smartline",
                    "line",
                    "polyline",
                    "freehand",
                    "rectangle",
                    "circle",
                    "hatcharea",
                }
                current_tool = str(getattr(self.digitizer, "active_tool", "") or "").lower()
                if current_tool in resumable_tools:
                    self.digitizer.set_tool(None, suspend_only=True)
                else:
                    self.digitizer.set_tool(None)
            except Exception as e:
                print(f"⚠️ Failed to deactivate digitize tool: {e}")

    def _deactivate_pending_cut_section_tool(self, reason="switching tools"):
        """
        Deactivate an unfinished cut-section placement without closing an
        already-finalized cut dock.
        """
        ctrl = getattr(self, "cut_section_controller", None)
        if not ctrl or not hasattr(ctrl, "deactivate_if_waiting"):
            return

        try:
            state = getattr(ctrl, "_state", 0)
            has_preview = any(
                getattr(ctrl, attr, None) is not None
                for attr in (
                    "line_actor",
                    "buffer_actor_upper",
                    "buffer_actor_lower",
                    "cut_preview_upper",
                    "cut_preview_lower",
                )
            )
            has_observers = bool(getattr(ctrl, "_view_observer_ids", {}))
            pending = state not in (0, 3) or has_preview or has_observers

            if pending:
                print(f"🛑 Deactivating pending cut-section tool ({reason})")

            force_cleanup = getattr(ctrl, "_force_deactivate_pending_state", None)
            if pending and force_cleanup:
                force_cleanup()
            else:
                ctrl.deactivate_if_waiting()

            if pending:
                self.cut_section_mode_on = False
                try:
                    self.set_cross_cursor_active(False, "cut_section")
                except TypeError:
                    self.set_cross_cursor_active(False)
        except Exception as e:
            print(f"⚠️ Failed to deactivate pending cut-section tool: {e}")

    def handle_tool_activation(self, tool_name):
        """Handle tool activation from sidebar"""
        if tool_name != "cut_section":
            self._deactivate_pending_cut_section_tool(f"switching to {tool_name}")

        if tool_name == "cross_section":
            self.toggle_cross_section_mode(True)
            self.set_cross_cursor_active(True)
        elif tool_name == "cut_section":
            self.cut_section_controller.activate()
            self.set_cross_cursor_active(True)

        elif tool_name == "cross_settings":
            self.open_cross_section_settings()
        elif tool_name == "element_selection":
            self.open_selection_mode()
            # self.set_cross_cursor_active(True)
        elif tool_name == "configure_shortcuts":
            self.open_shortcut_manager()
        elif tool_name == "backup_settings":  # ← ADD THIS
            self.open_backup_settings()
        elif tool_name == "soak_telemetry":
            self.open_soak_telemetry()
        elif tool_name == "prj_block_identifier":  # ← ADD THIS
            from gui.prj_block_identifier import show_block_identifier_dialog
            self.block_identifier_dialog = show_block_identifier_dialog(self)
        elif tool_name == "sync_views":
            # This will eventually open the Synchronize Views dialog
            self.open_sync_views_dialog()

        elif tool_name == "load_by_fence":
            glm = getattr(self, "grid_label_manager", None)
            if glm and hasattr(glm, "activate_load_by_fence_tool"):
                glm.activate_load_by_fence_tool()
            else:
                QMessageBox.warning(self, "Load by Fence", "Grid label manager is not available.")
        elif tool_name == "clear_fence_load":
            glm = getattr(self, "grid_label_manager", None)
            if glm and hasattr(glm, "clear_fenced_load"):
                glm.clear_fenced_load()
            else:
                QMessageBox.information(self, "Load by Fence", "No fenced load is active.")
        else:
            print(f"⚠️ Unknown tool_name from ToolsRibbon: {tool_name}")



    def open_cross_section_settings(self):
        """Open the cross-section line settings dialog"""
        from gui.cross_section_settings_dialog import CrossSectionSettingsDialog
       
        # Create or show existing dialog
        if not hasattr(self, 'cross_settings_dialog') or self.cross_settings_dialog is None:
            self.cross_settings_dialog = CrossSectionSettingsDialog(self)
       
        self.cross_settings_dialog.show()
        self.cross_settings_dialog.raise_()
        self.cross_settings_dialog.activateWindow()
        return self.cross_settings_dialog
        
    def open_sync_views_dialog(self):
        """Open the Synchronize Views dialog."""
        from gui.menu_sidebar_system import SyncViewsDialog

        if not hasattr(self, "_sync_views_dialog") or self._sync_views_dialog is None:
            self._sync_views_dialog = SyncViewsDialog(self, self)

        self._sync_views_dialog._load_from_app_state()  # refresh from current mapping
        self._sync_views_dialog.show()
        self._sync_views_dialog.raise_()
        self._sync_views_dialog.activateWindow()
        return self._sync_views_dialog
       
   
    def _load_cross_section_settings(self):
        """Load saved cross-section line preferences"""
        from PySide6.QtCore import QSettings
        from PySide6.QtGui import QColor
       
        settings = QSettings("NakshaAI", "LidarApp")
       
        # Load color
        color_name = settings.value("cross_line_color", "#FF00FF")  # Magenta hex
        qcolor = QColor(color_name)
        self.cross_line_color = (qcolor.redF(), qcolor.greenF(), qcolor.blueF())
       
        # Load width
        self.cross_line_width = settings.value("cross_line_width", 3, type=int)
       
        # Load style
        self.cross_line_style = settings.value("cross_line_style", "solid", type=str)
       
        print(f"✅ Loaded cross-section settings: {color_name}, {self.cross_line_width}px, {self.cross_line_style}")
           


    def handle_edit_action(self, action: str):
        """Handle edit actions from the Edit Sidebar."""
        if action == "undo":
            self.undo_classification()
        elif action == "redo":
            self.redo_classification()
        elif action == "cut":
            print("✂️ Cut action — not implemented yet")
        elif action == "copy":
            print("📋 Copy action — not implemented yet")
        elif action == "paste":
            print("📎 Paste action — not implemented yet")
        elif action == "delete":
            print("🗑️ Delete action — not implemented yet")
        elif action == "select_all":
            print("🔘 Select All — not implemented yet")
        elif action == "deselect":
            print("⭕ Deselect All — not implemented yet")
        else:
            print(f"⚠️ Unknown edit action: {action}")



    # ------------------ Shadow---------------------
    def toggle_shadow_in_view(self, enabled: bool):
        """Enable or disable simulated shadow effect in shaded view."""
        if not hasattr(self, "data") or self.data is None:
            return
        self.shadow_enabled = enabled
        print(f"🌗 Shadow {'enabled' if enabled else 'disabled'}")

        # Adjust ambient term: less ambient = stronger shadow
        ambient = 0.1 if enabled else 0.4
        
        # Store ambient for future use
        self.shade_ambient = ambient
        
        from .shading_display import update_shaded_class
        
        # ✅ Pass all parameters including quality and speed
        update_shaded_class(
            self,
            azimuth=getattr(self, "last_shade_azimuth", 45.0),
            angle=getattr(self, "shading_sharpness_angle", 45.0),
            ambient=ambient,
            percentile_filter=getattr(self, "shade_quality", 99.0),
            downsample=getattr(self, "shade_speed", 1)
        )


    def toggle_depth_in_view(self, enabled: bool):
        """Enable or disable depth-based shading (fake Z falloff)."""
        if not hasattr(self, "data") or self.data is None:
            return
        
        self.depth_enabled = enabled
        print(f"🧱 Depth shading {'enabled' if enabled else 'disabled'}")

        if self.display_mode != "shaded_class":
            print("⚠️ Depth shading only applies to shaded_class mode.")
            return

        # ✅ Store depth state
        if enabled:
            print("✅ Depth shading will be applied during triangulation")
        else:
            print("✅ Depth shading disabled")
        
        # ✅ Rebuild with depth consideration
        from .shading_display import update_shaded_class
        update_shaded_class(
            self,
            azimuth=getattr(self, "last_shade_azimuth", 45.0),
            angle=getattr(self, "shading_sharpness_angle", 45.0),
            ambient=getattr(self, "shade_ambient", 0.2),
            percentile_filter=getattr(self, "shade_quality", 99.0),
            downsample=getattr(self, "shade_speed", 1)
        )

# ----NEwly added function------
    # Add this method to your NakshaApp class in app_window.py

    def restore_main_interactor(self):
        """
        Restore the default 2D interactor on the main viewer.
        Call this before activating cut section or other tools.
        """
        try:
            if hasattr(self, 'cross_interactor'):
                self.cross_interactor = None

            if self.ensure_main_view_2d_interaction(
                preserve_camera=True,
                reason="restore_main_interactor",
            ):
                print("🔄 Main interactor restored to 2D mode")

        except Exception as e:
            print(f"⚠️ Failed to restore interactor: {e}")
        self.set_cross_cursor_active(False)


    #     """Instant update without rebuilding the point cloud."""
    #     self.point_border_percent = border_percent
        
    #     # 🚀 PRODUCTION FIX: Poke GPU directly, no rebuild.
        
    #     # Update Main View (Slot 0)
        
    #     # Update any active Cross-Sections (Slots 1-4)

    #     self.statusBar().showMessage(f"🔳 Border: {border_percent}% (GPU Updated)", 1000)

    def on_border_changed(self, border_percent):
        """Instant update without rebuilding the point cloud."""
        self.point_border_percent = border_percent
        if not hasattr(self, 'view_borders'):
            self.view_borders = {i: 0 for i in range(6)}
        self.view_borders[0] = border_percent          # ← keep view_borders in sync

        CLASS_MODES = {"class", "shaded_class"}
        current_mode = getattr(self, "display_mode", "class")

        if current_mode not in CLASS_MODES:
            # ✅ In non-class modes: push the border uniform directly without
            # calling sync_palette_to_gpu (which would overwrite the color buffer
            try:
                from gui.unified_actor_manager import (
                    _get_unified_actor, _apply_border_once, _push_uniforms_direct
                )
                _actor = _get_unified_actor(self)
                if _actor is not None:
                    _apply_border_once(_actor, border_percent)
                    _ctx = getattr(_actor, "_naksha_shader_ctx", None)
                    if _ctx is not None:
                        _push_uniforms_direct(_actor, _ctx)
                    try:
                        self.vtk_widget.render()
                    except Exception:
                        pass
                    print(f"🔳 Border {border_percent}% pushed in {current_mode} mode (uniform only)")
            except Exception as _be:
                print(f"⚠️ Border uniform push failed: {_be}")
            self.statusBar().showMessage(f"🔳 Border: {border_percent}%", 1000)
            return

        from gui.unified_actor_manager import sync_palette_to_gpu

        sync_palette_to_gpu(self, slot_idx=0, border=border_percent)

        if hasattr(self, 'section_vtks'):
            for view_idx in self.section_vtks.keys():
                b = float(self.view_borders.get(view_idx + 1, 0))
                sync_palette_to_gpu(self, slot_idx=view_idx + 1, border=b)

        self.statusBar().showMessage(f"🔳 Border: {border_percent}% (GPU Updated)", 1000)


    def apply_classification_signal(self, changed_mask, to_class):
        """🚀 ZERO-LAG SYNC: Main View + All Sub-views"""
        import numpy as np

        # 1. Update Core CPU Memory
        # Preserve the exact transition for Surface before mutating ground truth.
        # This is O(K) and lets Surface decide whether geometry actually changed.
        try:
            _surface_changed_idx = np.flatnonzero(changed_mask).astype(np.int64, copy=False)
            _surface_old = self.data["classification"][_surface_changed_idx].copy()
            _surface_new = np.full(
                _surface_changed_idx.size, to_class, dtype=self.data["classification"].dtype
            )
            self._pending_surface_delta = {
                "indices": _surface_changed_idx.copy(),
                "old_classes": _surface_old,
                "new_classes": _surface_new,
            }
        except Exception:
            self._pending_surface_delta = None

        self.data["classification"][changed_mask] = to_class
        self.classification_revision = int(getattr(self, "classification_revision", 0) or 0) + 1

        # *** CRITICAL FIX: Clear redo stack — new classification invalidates redo history ***
        self.redo_stack.clear()

        # 2. ⚡ POKE MAIN VIEW GPU / SURFACE MESH
        if str(getattr(self, "display_mode", "class") or "class").lower() == "surface":
            try:
                self.refresh_surface_after_classification(reason="classification", changed_mask=changed_mask)
            except Exception as _surface_err:
                print(f"⚠️ Surface refresh after classification failed: {_surface_err}")
        else:
            from gui.unified_actor_manager import fast_classify_update
            fast_classify_update(self, changed_mask, to_class)
        # 3. Invalidate section mirrors before signal fires
        if hasattr(self, 'section_vtks'):
            for view_idx in self.section_vtks.keys():
                self._sync_section_mirror_from_data(view_idx)

        # 4. POKE ALL OTHER VIEWPORTS (Signal Bus)
        self.classification_finished.emit(changed_mask)

        # 5. Final Render
        if str(getattr(self, "display_mode", "class") or "class").lower() != "surface":
            self.vtk_widget.render()

    def activate_dock_by_view(self, view_index):
        """
        Activate the corresponding dock based on Display Mode view selection.
        ✅ FIXED: Proper error handling and view activation
        """
        try:
            print(f"\n{'='*60}")
            print(f"🎯 ACTIVATING VIEW {view_index}")
            print(f"{'='*60}")
            
            if view_index == 0:
                # Main View - focus on main VTK widget
                if hasattr(self, 'vtk_widget'):
                    try:
                        self.vtk_widget.interactor.GetRenderWindow().GetInteractor().GetRenderWindow().Render()
                        self.vtk_widget.setFocus()
                        # ✅ BUG FIX: Prevent stuck panning. When returning to the main view, 
                        # force reset the mouse interaction state to clear any missed release events.
                        style = self.vtk_widget.interactor.GetInteractorStyle()
                        if style:
                            try:
                                if hasattr(style, "OnMiddleButtonUp"):
                                    style.OnMiddleButtonUp()
                                if hasattr(style, "OnLeftButtonUp"):
                                    style.OnLeftButtonUp()
                                if hasattr(style, "OnRightButtonUp"):
                                    style.OnRightButtonUp()
                            except Exception:
                                pass
                        print(f"✅ Activated Main View")
                    except Exception as e:
                        print(f"⚠️ Error activating main view: {e}")
                else:
                    print(f"⚠️ vtk_widget not found")
                    
            elif view_index >= 1 and view_index <= 4:
                # Cross Section Views 1-4
                section_index = view_index - 1  # Convert to 0-based index
                
                print(f"   Looking for section_index: {section_index}")
                print(f"   Available section_docks: {list(getattr(self, 'section_docks', {}).keys())}")
                
                if hasattr(self, 'section_docks') and section_index in self.section_docks:
                    dock = self.section_docks[section_index]
                    
                    # Activate the dock window
                    dock.raise_()
                    dock.activateWindow()
                    dock.setFocus()
                    
                    # Set as active view in section controller
                    if hasattr(self, 'section_controller'):
                        self.section_controller.active_view = section_index
                        print(f"   Set section_controller.active_view = {section_index}")
                    
                    print(f"✅ Activated Cross Section View {view_index}")
                else:
                    print(f"⚠️ Cross Section View {view_index} not found")
                    print(f"   section_docks exists: {hasattr(self, 'section_docks')}")
                    if hasattr(self, 'section_docks'):
                        print(f"   Available views: {[i+1 for i in self.section_docks.keys()]}")
                    
                    # QMessageBox.information(
                    #     self,
                    #     "View Not Open",
                    #     f"Cross Section View {view_index} is not open.\n"
                    #     f"Please create it first using Tools → Cross Section."
                    # )
            else:
                print(f"⚠️ Invalid view index: {view_index}")
            
            print(f"{'='*60}\n")
                
        except Exception as e:
            print(f"⚠️ Error activating dock for view {view_index}: {e}")
            import traceback
            traceback.print_exc()
            print(f"{'='*60}\n")

    def refresh_all_views(self):
        """
        Master refresh function - Context Aware
        ✅ Fixes Issue 1: Preserves Main View filters/weights by preventing state overwrite.
        ✅ Fixes Issue 2: Prevents Cross-View settings from leaking into the Main View.
        ✅ OPTIMIZED: Synchronizes Cut-Section using the high-speed GPU buffer path (No 3-4s lag).
        """
        try:
            print("\n" + "="*60)
            print("🔄 GLOBAL REFRESH - PRESERVING VIEW CONTEXTS")
            print("="*60)
            
            # ✅ 1. Refresh Main View with context protection
            print(f"📺 Refreshing Main View (mode: {self.display_mode})")
            
            if self.display_mode == "class":
                from gui.class_display import update_class_mode
                try:
                    update_class_mode(self, view_index=-1)
                except TypeError:
                    # Fallback if update_class_mode doesn't support view_index yet
                    update_class_mode(self)
            
            elif self.display_mode == "shaded_class":
                from .shading_display import update_shaded_class
                update_shaded_class(self, getattr(self, "last_shade_azimuth", 45.0),
                                    getattr(self, "shading_sharpness_angle", 45.0),
                                    getattr(self, "shade_ambient", 0.2))
            else:
                from .pointcloud_display import update_pointcloud
                update_pointcloud(self, self.display_mode)
            
            print("✅ Main View updated (Filters preserved)")
            
            # ✅ 2. Refresh ALL cross-section views independently
            if hasattr(self, 'section_vtks') and hasattr(self, 'section_controller'):
                if hasattr(self.section_controller, 'refresh_colors'):
                    self.section_controller.refresh_colors()
                    
                    # Force a render on each section vtk to ensure the scalar update is visible
                    for view_idx, vtk_widget in self.section_vtks.items():
                        if vtk_widget and hasattr(vtk_widget, 'render'):
                            vtk_widget.render()
                    print("✅ Cross-section views updated (Local filters maintained)")
            
            # ✅ 3. OPTIMIZED: Refresh cut-section view using fast GPU path
            if hasattr(self, 'cut_section_controller'):
                ctrl = self.cut_section_controller
                # Check if the cut view is active and has data
                if getattr(ctrl, 'is_cut_view_active', False) and ctrl.cut_points is not None:
                    print("✂️ [FAST-SYNC] Refreshing Cut Section...")
                    
                    # Direct call to the optimized millisecond-refresh method
                    if hasattr(ctrl, '_refresh_cut_colors_fast'):
                        ctrl._refresh_cut_colors_fast()
                    else:
                        # Fallback to standard refresh if fast method is missing
                        ctrl.refresh_colors()
                        
                    print("✅ Cut Section updated (Sync Complete)")
            
            # ✅ 4. Sync Display Mode dialog WITHOUT triggering a re-apply
            if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                    self.display_mode_dialog.sync_with_app_state()
                print("✅ Display Mode dialog synced")
            
            # ✅ 5. UPDATE STATISTICS
            if hasattr(self, 'point_count_widget') and self.point_count_widget:
                refresh_point_statistics(self)
                print("📊 Statistics updated")
            
            # Final Render of Main View
            if hasattr(self, 'vtk_widget'):
                self.vtk_widget.render()

            print("="*60)
            print("✅ GLOBAL REFRESH COMPLETE - NO LEAKAGE")
            print("="*60 + "\n")
            
            if hasattr(self, 'statusBar') and self.statusBar():
                self.statusBar().showMessage("✅ All views synchronized", 2000)
            
        except Exception as e:
            print(f"⚠️ refresh_all_views failed: {e}")
            import traceback
            traceback.print_exc()

# New 
    def refresh_all_classification_views(self):
        """
        ✅ OPTIMIZED: Update ALL views after classification changes.
        Uses debouncing and vectorized operations for speed.
        """
        from gui.display_mode import clone_palette
        print(f"\n{'='*60}")
        print(f"🔄 REFRESHING ALL CLASSIFICATION VIEWS (optimized)")
        print(f"{'='*60}")
        
        # ✅ CRITICAL FIX: Initialize view_palettes if missing
        if not hasattr(self, 'view_palettes'):
            self.view_palettes = {}
            print("⚠️ view_palettes was missing - created it")
        
        # ✅ EMERGENCY FALLBACK: If view_palettes is empty, seed slot defaults.
        if not self.view_palettes or all(not v for v in self.view_palettes.values()):
            print("⚠️ view_palettes is empty - seeding slot palettes")
            if hasattr(self, 'class_palette') and self.class_palette:
                # Slot 0 remains canonical main view behavior.
                self.view_palettes[0] = clone_palette(self.class_palette)
                # Slots 1..5 are isolated from slot-0 visibility.
                for i in range(1, 6):
                    seeded = self._build_slot_palette_from_main_defaults(i)
                    if seeded:
                        self.view_palettes[i] = seeded
                print(f"   ✅ Initialized slot palettes from main defaults ({len(self.class_palette)} classes)")
        
        # ✅ Track which views need updating
        views_to_update = set()
        
        if self.display_mode == "class":
            views_to_update.add(0)  # Main view
        
        if hasattr(self, 'section_vtks'):
            for view_idx in self.section_vtks.keys():
                views_to_update.add(view_idx + 1)  # Cross-section views
        
        if not views_to_update:
            print("⏭️ No views need updating")
            print(f"{'='*60}\n")
            return
        
        print(f"📊 Updating {len(views_to_update)} views: {sorted(views_to_update)}")
        
        try:
            # --------------------------------------------------------
            # 1. Refresh MAIN VIEW (View 0) - if needed
            # --------------------------------------------------------
            if 0 in views_to_update:
                print("   🎨 Refreshing Main View (0)...")
                
                # Use Main View's palette if it exists
                if 0 in self.view_palettes and self.view_palettes[0]:
                    old_palette = clone_palette(self.class_palette) if hasattr(self, 'class_palette') else {}
                    self.class_palette = clone_palette(self.view_palettes[0])
                    
                    # ✅ Use fast color update if possible
                    if hasattr(self, '_last_changed_mask') and self._last_changed_mask is not None:
                        from gui.pointcloud_display import fast_update_colors
                        fast_update_colors(self, self._last_changed_mask)
                    else:
                        from gui.class_display import update_class_mode
                        update_class_mode(self)
                    
                    if old_palette:
                        self.class_palette = clone_palette(old_palette)
                    
                    visible = [c for c, v in self.view_palettes[0].items() if v.get("show")]
                    print(f"   ✅ Main View updated ({len(visible)} visible classes)")
                else:
                    print("   ⏭️ Main View using global palette")
                    from gui.class_display import update_class_mode
                    update_class_mode(self)
            
            # --------------------------------------------------------
            # 2. Refresh CROSS-SECTION VIEWS (Views 1-4) - if needed
            # --------------------------------------------------------
            section_views = [v for v in views_to_update if 1 <= v <= 4]
            
            if section_views:
                import numpy as np
                import pyvista as pv
                
                for target_view in section_views:
                    view_idx = target_view - 1  # Convert to 0-based
                    
                    if view_idx not in self.section_vtks:
                        continue
                    
                    # Use the optimized single-view refresh
                    self._refresh_single_section_view(view_idx)
            
            # --------------------------------------------------------
            # 3. Refresh CUT SECTION if active
            # --------------------------------------------------------
            if hasattr(self, 'cut_section_controller') and self.cut_section_controller.cut_points is not None:
                print("   🔄 Refreshing Cut Section...")
                self.cut_section_controller.refresh_colors()
                print("   ✅ Cut Section updated")
            
            print(f"{'='*60}")
            print(f"✅ ALL VIEWS REFRESHED SUCCESSFULLY")
            print(f"{'='*60}\n")
            
            self.statusBar().showMessage("✅ All views updated", 2000)
            
        except Exception as e:
            print(f"⚠️ Multi-view refresh error: {e}")
            import traceback
            traceback.print_exc()

    def _apply_plain_section_2d_style(self, interactor, vtk_widget=None):
        """
        Install the default cross-section style in true Image2D mode so
        section docks cannot drift into 3D, while preserving wheel zoom.
        """
        from PySide6.QtCore import QTimer
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

        if interactor is None:
            return None

        if vtk_widget is None:
            for candidate in (getattr(self, "section_vtks", None) or {}).values():
                if getattr(candidate, "interactor", None) is interactor:
                    vtk_widget = candidate
                    break

        style = vtkInteractorStyleImage()
        try:
            style.SetInteractionModeToImage2D()
        except Exception:
            pass

        def _wheel_zoom(obj, event, _vtk_widget=vtk_widget):
            try:
                renderer = getattr(_vtk_widget, "renderer", None) if _vtk_widget is not None else None
                if renderer is None:
                    return

                camera = renderer.GetActiveCamera()
                if camera is None:
                    return

                factor = 1.1 if event == "MouseWheelForwardEvent" else (1.0 / 1.1)
                camera.ParallelProjectionOn()
                camera.SetParallelScale(max(camera.GetParallelScale() / factor, 1e-6))
                renderer.ResetCameraClippingRange()

                if _vtk_widget is None:
                    return
                if getattr(_vtk_widget, "_section_wheel_render_pending", False):
                    return
                _vtk_widget._section_wheel_render_pending = True

                def _render():
                    try:
                        _vtk_widget._section_wheel_render_pending = False
                    except Exception:
                        pass
                    try:
                        if hasattr(_vtk_widget, "isVisible") and not _vtk_widget.isVisible():
                            return
                        _vtk_widget.render()
                    except Exception:
                        pass

                QTimer.singleShot(0, _render)
            except Exception:
                pass

        style._section_wheel_zoom_callback = _wheel_zoom
        style.AddObserver("MouseWheelForwardEvent", _wheel_zoom)
        style.AddObserver("MouseWheelBackwardEvent", _wheel_zoom)
        interactor.SetInteractorStyle(style)

        # Enforce camera 2D lock for the plain 2D interaction style
        try:
            from gui.cross_section.interactor_classify import install_camera_2d_lock
            install_camera_2d_lock(vtk_widget, self)
        except Exception as e:
            print(f"⚠️ Lock install in _apply_plain_section_2d_style failed: {e}")

        # The Qt-level filter survives interactor-style swaps and consumes a
        # wheel event only after applying a valid camera update.  The observers
        # above intentionally remain installed as a fallback.
        self._install_section_wheel_zoom(vtk_widget)

        return style

    def _restore_section_interactors(self):
        """
        Fallback method to restore section interactors when controller methods are missing.
        """
        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            if not hasattr(self, '_section_right_click_observers'):
                self._section_right_click_observers = {}

            def _make_right_click_handler(app):
                def _handler(obj, event):
                    print(f"🖱️ Right-click detected in cross-section view (fallback)")
                    active_tool = getattr(app, "active_classify_tool", None)
                    section_has_classifier = bool(getattr(app, "classify_interactors", None))
                    if active_tool is None or not section_has_classifier:
                        last_tool = getattr(app, "last_classify_tool", None)
                        if last_tool and hasattr(app, "set_classify_tool"):
                            try:
                                app.from_classes = getattr(app, "last_classify_from_classes", None)
                                app.to_class = getattr(app, "last_classify_to_class", None)
                                app._right_click_reactivating = True
                                try:
                                    app.set_classify_tool(last_tool)
                                finally:
                                    app._right_click_reactivating = False
                            except Exception as e:
                                print(f"   ⚠️ Right-click reactivate failed: {e}")
                return _handler

            if hasattr(self, 'section_vtks') and self.section_vtks:
                for view_idx, vtk_widget in self.section_vtks.items():
                    try:
                        if vtk_widget and hasattr(vtk_widget, 'interactor'):
                            interactor = vtk_widget.interactor
                            # Remove old observer
                            old_tag = self._section_right_click_observers.get(view_idx)
                            if old_tag is not None:
                                try:
                                    interactor.RemoveObserver(old_tag)
                                except Exception:
                                    pass
                            self._apply_plain_section_2d_style(interactor, vtk_widget)
                            tag = interactor.AddObserver(
                                "RightButtonPressEvent", _make_right_click_handler(self), 1.0
                            )
                            self._section_right_click_observers[view_idx] = tag
                            print(f"   ✅ View {view_idx + 1}: Interactor restored (fallback)")
                    except Exception as e:
                        print(f"   ⚠️ View {view_idx + 1}: Fallback restore failed - {e}")

            print("   ✅ Section interactors restored (fallback method)")

        except Exception as e:
            print(f"   ⚠️ Fallback restore failed: {e}")

    def _reinstall_section_right_click_observer(self, view_idx):
        """
        Reinstall the right-click reactivation observer on a single section view interactor.
        Called when a dock is tabified into the main window (topLevelChanged signal)
        or when the interactor style is reset.
        """
        if not hasattr(self, '_section_right_click_observers'):
            self._section_right_click_observers = {}

        if not hasattr(self, 'section_vtks') or view_idx not in self.section_vtks:
            return

        vtk_widget = self.section_vtks.get(view_idx)
        if vtk_widget is None or not hasattr(vtk_widget, 'interactor'):
            return

        interactor = vtk_widget.interactor

        # Remove old observer if present
        old_tag = self._section_right_click_observers.get(view_idx)
        if old_tag is not None:
            try:
                interactor.RemoveObserver(old_tag)
            except Exception:
                pass

        # Only install observer if no classification tool is currently active
        if getattr(self, "active_classify_tool", None) is not None:
            print(f"   ℹ️ View {view_idx + 1}: Classification active, skipping observer reinstall")
            return

        def _make_handler(app):
            def _handler(obj, event):
                print(f"🖱️ Right-click detected in cross-section view (tabified)")
                active_tool = getattr(app, "active_classify_tool", None)
                section_has_classifier = bool(getattr(app, "classify_interactors", None))
                if active_tool is None or not section_has_classifier:
                    last_tool = getattr(app, "last_classify_tool", None)
                    if last_tool and hasattr(app, "set_classify_tool"):
                        try:
                            app.from_classes = getattr(app, "last_classify_from_classes", None)
                            app.to_class = getattr(app, "last_classify_to_class", None)
                            app._right_click_reactivating = True
                            try:
                                app.set_classify_tool(last_tool)
                            finally:
                                app._right_click_reactivating = False
                        except Exception as e:
                            print(f"   ⚠️ Right-click reactivate failed: {e}")
            return _handler

        # Ensure plain 2D style is set
        self._apply_plain_section_2d_style(interactor, vtk_widget)

        tag = interactor.AddObserver("RightButtonPressEvent", _make_handler(self), 1.0)
        self._section_right_click_observers[view_idx] = tag
        print(f"   ✅ View {view_idx + 1}: Right-click observer reinstalled (tabified)")

# In class_picker.py    
    def deactivate_classification(self):
        """
        Deactivate classification mode and restore normal interaction.
        """
        print(f"\n{'='*60}")
        print(f"🛑 DEACTIVATING CLASSIFICATION TOOL (deactivate_classification)")
        print(f"{'='*60}")
        
        # Clear tool
        self.active_classify_tool = None
        
        # ✅ CRITICAL: Unlock main view
        self.skip_main_view_refresh = False
        print("   🔓 Main view refresh UNLOCKED")
        
        # Close class picker
        if self._get_live_class_picker() is not None:
            self._hide_class_picker_safely()
            print("   ✅ Class picker closed")
        
        # Restore section view interactors (with safety check)
        if hasattr(self, "section_controller"):
            try:
                if hasattr(self.section_controller, 'unlock_after_classification'):
                    self.section_controller.unlock_after_classification()
                    print("   ✅ Section controller unlocked")
                else:
                    print("   ⚠️ section_controller missing unlock_after_classification method")
                    # Fallback: manually restore interactors
                    self._restore_section_interactors()
            except Exception as e:
                print(f"   ⚠️ Section controller unlock failed: {e}")
                # Fallback: manually restore interactors
                self._restore_section_interactors()
        
        # Restore cut section interactors (with safety check)
        if hasattr(self, "cut_section_controller"):
            try:
                if hasattr(self.cut_section_controller, 'unlock_after_classification'):
                    self.cut_section_controller.unlock_after_classification()
                    print("   ✅ Cut section controller unlocked")
                else:
                    print("   ⚠️ cut_section_controller missing unlock_after_classification method")
            except Exception as e:
                print(f"   ⚠️ Cut section controller unlock failed: {e}")

        # Restore default 2D interactor on all cross-section views
        try:
            # section_controller.unlock_after_classification() already restored
            # interactors AND added right-click observers - just clear wrappers
            if hasattr(self, "classify_interactors"):
                self.classify_interactors.clear()
                print("   ✅ Classification interactors cleared")
        except Exception as e:
            print(f"⚠️ Failed to clear classification interactors: {e}")
        
        print(f"{'='*60}")
        print(f"✅ Classification tool fully deactivated - all views unlocked")
        print(f"{'='*60}\n")
        
        self.statusBar().showMessage("✅ Classification tool deactivated", 2000)
    #     """
    #     Deactivate classification mode and restore normal interaction.
    #     """
    #     self.active_classify_tool = None
    #     self.skip_main_view_refresh = False
        
    #     # Close class picker
    #         self.class_picker.close()
    #         self.class_picker = None
        
    #     # Restore section view interactor
    #         self.section_controller.unlock_after_classification()
        
    #         self.cut_section_controller.unlock_after_classification()
        
    #     self.statusBar().showMessage("✅ Classification tool deactivated", 2000)


    def refresh_all_section_views(self):
            """
            Refresh ALL cross-section views after main window classification.
            This ensures all section views show the latest classification colors.
            """
            if not hasattr(self, 'section_vtks') or not self.section_vtks:
                return
            
            print(f"\n{'='*60}")
            print(f"🔄 REFRESHING ALL CROSS-SECTION VIEWS")
            print(f"{'='*60}")
            
            import numpy as np
            import pyvista as pv
            
            for view_idx in self.section_vtks.keys():
                try:
                    # Get stored section data for this view
                    pts = getattr(self, f"section_{view_idx}_core_points", None)
                    buf = getattr(self, f"section_{view_idx}_buffer_points", None)
                    core_mask = getattr(self, f"section_{view_idx}_core_mask", None)
                    buffer_mask = getattr(self, f"section_{view_idx}_buffer_mask", None)
                    
                    if pts is None or core_mask is None:
                        print(f"   ⏭️ View {view_idx + 1}: No data")
                        continue
                    
                    # Combine core + buffer
                    if buf is not None and buffer_mask is not None:
                        all_pts = np.vstack([pts, buf])
                        all_cls = np.concatenate([
                            self.data["classification"][core_mask],
                            self.data["classification"][buffer_mask & ~core_mask]
                        ])
                    else:
                        all_pts = pts
                        all_cls = self.data["classification"][core_mask]
                    
                    # Filter by visible classes
                    visible = [c for c, v in self.class_palette.items() if v.get("show")]
                    if not visible:
                        print(f"   ⏭️ View {view_idx + 1}: No visible classes")
                        continue
                    
                    mask = np.isin(all_cls, visible)
                    filtered_pts = all_pts[mask]
                    filtered_cls = all_cls[mask]
                    
                    # Build color array using LATEST classifications
                    colors = np.zeros((len(filtered_pts), 3), dtype=np.uint8)
                    for i, cls in enumerate(filtered_cls):
                        entry = self.class_palette.get(int(cls), {"color": (128, 128, 128)})
                        colors[i] = entry["color"]
                    
                    # Update VTK widget
                    vtk_widget = self.section_vtks[view_idx]
                    
                    # Save camera
                    cam_pos = vtk_widget.camera_position
                    
                    # Clear and re-render
                    vtk_widget.clear()
                    
                    cloud = pv.PolyData(filtered_pts)
                    cloud["RGB"] = colors
                    
                    vtk_widget.add_points(
                        cloud,
                        scalars="RGB",
                        rgb=True,
                        point_size=3,
                        render_points_as_spheres=True
                    )
                    
                    # Restore camera
                    vtk_widget.camera_position = cam_pos
                    vtk_widget.render()
                    
                    print(f"   ✅ View {view_idx + 1} refreshed ({len(filtered_pts)} points)")
                    
                except Exception as e:
                    print(f"   ⚠️ View {view_idx + 1} refresh failed: {e}")
            
            print(f"{'='*60}\n") 

    def schedule_view_update(self, view_indices):
        """
        Debounced update - prevents 100 updates/second during brush strokes.
        Batches rapid updates into single render call.
        """
        if not isinstance(view_indices, (list, set)):
            view_indices = {view_indices}
        
        self._pending_view_updates.update(view_indices)
        
        # Reuse a single timer (parented to the app) to avoid timer churn.
        if self._update_debounce_timer.isActive():
            self._update_debounce_timer.stop()
        self._update_debounce_timer.start(50)

        if getattr(self, "_debug_perf", False):
            print(f"📅 Scheduled update for views: {view_indices}")
    
    def _execute_pending_updates(self):
        """Execute batched updates (called after debounce delay)."""
        if not self._pending_view_updates:
            return
        
        if getattr(self, "_debug_perf", False):
            print(f"🔄 Batch updating views: {self._pending_view_updates}")
        
        for view_idx in sorted(self._pending_view_updates):
            if view_idx == 0:
                # Main view - fast color update only
                from gui.pointcloud_display import fast_update_colors
                fast_update_colors(self, self._last_changed_mask)
            else:
                # Cross-section view - targeted refresh
                self._refresh_single_section_view(view_idx - 1)
        
        self._pending_view_updates.clear()
        if getattr(self, "_debug_perf", False):
            print("✅ Batch update complete")

    def _refresh_single_section_view(self, view_idx, border_percent=0):
        """
        Optimized single cross-section view refresh.
        ✅ UNIFIED ACTOR PATH: Builds a single _section_X_unified actor per view.
        This enables sync_palette_to_gpu, undo/redo, and per-view weights to work
        through the same GPU uniform system as the Main View.
        """
        if not hasattr(self, 'section_vtks') or view_idx not in self.section_vtks:
            print(f"⏭️ View {view_idx} not found")
            return

        target_view = view_idx + 1  # Convert to 1-based slot (1-4)
        print(f"   🔄 Refreshing Cross Section View {target_view} (unified actor)...")

        # Guard against stale section masks from a previously loaded file.
        # If mask length does not match current classification length, clear the
        # per-view cached section data and skip this refresh safely.
        try:
            if hasattr(self, "data") and isinstance(self.data, dict):
                cls = self.data.get("classification")
                if cls is not None:
                    n_class = len(cls)
                    core_mask = getattr(self, f"section_{view_idx}_core_mask", None)
                    if core_mask is not None:
                        import numpy as _np
                        core_mask_arr = _np.asarray(core_mask, dtype=bool).ravel()
                        if core_mask_arr.size != n_class:
                            print(
                                f"   ⚠️ View {target_view}: stale section cache "
                                f"(mask={core_mask_arr.size}, data={n_class}) - clearing"
                            )
                            for _name in (
                                f"section_{view_idx}_core_points",
                                f"section_{view_idx}_buffer_points",
                                f"section_{view_idx}_core_mask",
                                f"section_{view_idx}_buffer_mask",
                                f"section_{view_idx}_core_indices",
                                f"section_{view_idx}_indices",
                                f"_section_{view_idx}_global_indices",
                            ):
                                if hasattr(self, _name):
                                    try:
                                        delattr(self, _name)
                                    except Exception:
                                        pass
                            return
        except Exception:
            pass

        # ── Read border value if not provided ─────────────────────────────
        if border_percent == 0 and hasattr(self, 'view_borders'):
            border_percent = self.view_borders.get(target_view, 0)

        # ── Get view-specific palette ─────────────────────────────────────
        if not hasattr(self, 'view_palettes'):
            self.view_palettes = {}

        palette = None
        if target_view in self.view_palettes and self.view_palettes[target_view]:
            palette = self.view_palettes[target_view]
        else:
            palette = self._get_cross_section_palette(
                target_view,
                allow_default_seed=True,
                persist_seed=True,
            )
            if not palette:
                print(f"   ⏭️ No palette available for View {target_view}")
                return
            if not hasattr(self, 'view_palettes') or not isinstance(self.view_palettes, dict):
                self.view_palettes = {}
            if target_view not in self.view_palettes or not self.view_palettes[target_view]:
                self.view_palettes[target_view] = {
                    int(code): dict(info) for code, info in palette.items()
                }

        point_size = float(getattr(self, 'point_size', 3.0))

        # ── Build unified actor (same pattern as Main View) ───────────────
        try:
            from gui.unified_actor_manager import (
                build_section_legacy_border_actors,
                build_section_unified_actor,
                section_requires_legacy_border_render,
            )

            if section_requires_legacy_border_render(
                self,
                view_idx,
                palette=palette,
                border_percent=float(border_percent),
            ):
                built = build_section_legacy_border_actors(
                    self,
                    view_idx,
                    palette=palette,
                    border_percent=float(border_percent),
                    point_size=point_size,
                )
                if built:
                    print(f"   View {target_view} legacy border renderer ready")
                    # Clear so next class-mode switch uses the fast GPU-sync path
                    # instead of forcing another full rebuild every time.
                    self._last_classified_to_class = None
                    return
                print(f"   View {target_view}: legacy border renderer failed, trying unified actor")

            actor = build_section_unified_actor(
                self,
                view_idx,
                palette=palette,
                border_percent=float(border_percent),
                point_size=point_size,
            )
            if actor is not None:
                print(f"   ✅ View {target_view} unified actor ready")
            else:
                print(f"   ⚠️ View {target_view}: build_section_unified_actor returned None")
        except Exception as e:
            print(f"   ⚠️ Unified actor build failed for View {target_view}: {e}")
            import traceback
            traceback.print_exc()



    def reset_class_weights(self):
        """
        Reset all class weights to 1.0 (normal size).
        Use this to fix the "giant bubble" issue from extreme weight values.
        """
        from PySide6.QtCore import QSettings
        
        print(f"\n{'='*60}")
        print(f"🔄 RESETTING ALL CLASS WEIGHTS TO 1.0")
        
        # 1. Reset in-memory palette
        if hasattr(self, 'class_palette') and self.class_palette:
            for code in self.class_palette:
                old_weight = self.class_palette[code].get('weight', 1.0)
                self.class_palette[code]['weight'] = 1.0
                if old_weight != 1.0:
                    print(f"   ✅ Class {code}: {old_weight:.1f}x → 1.0x")
        
        # 2. Reset in view_palettes
        if hasattr(self, 'view_palettes') and self.view_palettes:
            for view_idx, palette in self.view_palettes.items():
                if palette:
                    for code in palette:
                        palette[code]['weight'] = 1.0
        
        # 3. Clear saved weights in QSettings
        settings = QSettings("NakshaAI", "LidarApp")
        settings.remove("class_weight_states")
        settings.sync()
        print(f"   ✅ Cleared saved weights from settings")
        
        # 4. Re-render if data is loaded
        if hasattr(self, 'data') and self.data is not None:
            if self.display_mode == "class":
                from gui.class_display import update_class_mode
                update_class_mode(self)
                print(f"   ✅ Re-rendered with normal weights")
        
        print(f"{'='*60}\n")
        
        if hasattr(self, 'statusBar'):
            self.statusBar().showMessage("✅ All class weights reset to 1.0x (normal)", 3000)


    def sanitize_class_weights(self):
        """
        ✅ AUTO-FIX: Clamp any extreme weights to reasonable range (0.5-3.0).
        Call this right after loading class palette.
        """
        fixed_count = 0
        
        # Fix class_palette
        if hasattr(self, 'class_palette') and self.class_palette:
            for code, info in self.class_palette.items():
                weight = info.get('weight', 1.0)
                
                # Clamp to reasonable range
                clamped = max(0.5, min(weight, 3.0))
                
                if weight != clamped:
                    info['weight'] = clamped
                    fixed_count += 1
                    print(f"⚠️ Fixed Class {code}: {weight:.1f}x → {clamped:.1f}x")
        
        # Fix view_palettes
        if hasattr(self, 'view_palettes') and self.view_palettes:
            for view_idx, palette in self.view_palettes.items():
                if palette:
                    for code, info in palette.items():
                        weight = info.get('weight', 1.0)
                        clamped = max(0.5, min(weight, 3.0))
                        if weight != clamped:
                            info['weight'] = clamped
        
        if fixed_count > 0:
            print(f"✅ Auto-fixed {fixed_count} extreme weights")
            
            # Update QSettings to prevent this on next load
            from PySide6.QtCore import QSettings
            settings = QSettings("NakshaAI", "LidarApp")
            weight_states = settings.value("class_weight_states", {})
            if isinstance(weight_states, dict):
                for code_str, weight in weight_states.items():
                    if isinstance(weight, (int, float)):
                        weight_states[code_str] = max(0.5, min(float(weight), 3.0))
                settings.setValue("class_weight_states", weight_states)
                settings.sync()
                print(f"✅ Updated saved weights in settings")


    def test_display_mode_setup(self):
        '''Quick test to verify Display Mode signal exists'''
        if hasattr(self, 'display_dialog') and self.display_dialog:
            print("="*60)
            print("🧪 TESTING DISPLAY MODE SETUP")
            
            # Test 1: Signal exists
            has_signal = hasattr(self.display_dialog, 'classes_loaded')
            print(f"   Signal exists: {'✅ YES' if has_signal else '❌ NO'}")
            
            # Test 2: Signal is correct type
            if has_signal:
                from PySide6.QtCore import Signal
                is_signal = isinstance(type(self.display_dialog).classes_loaded, type(Signal()))
                print(f"   Is valid Signal: {'✅ YES' if is_signal else '❌ NO'}")
            
            # Test 3: Can connect to it
            if has_signal:
                try:
                    test_slot = lambda: print("   🎉 Test signal received!")
                    self.display_dialog.classes_loaded.connect(test_slot)
                    print(f"   Can connect: ✅ YES")
                    
                    # Clean up test connection
                    self.display_dialog.classes_loaded.disconnect(test_slot)
                except Exception as e:
                    print(f"   Can connect: ❌ NO - {e}")
            
            print("="*60)
        else:
            print("⚠️ Display Mode dialog not created yet")


    def normalize_all_class_weights(self):
        """
        Reset all class weights to 1.0 on file load.
        Prevents saved/corrupted weight values from causing size issues.
        """
        print("\n" + "="*60)
        print("🔧 NORMALIZING CLASS WEIGHTS ON LOAD")
        print("="*60)
        
        reset_count = 0
        
        # Fix class_palette
        if hasattr(self, 'class_palette') and self.class_palette:
            for code in self.class_palette:
                old_weight = self.class_palette[code].get("weight", 1.0)
                if old_weight != 1.0:
                    self.class_palette[code]["weight"] = 1.0
                    reset_count += 1
                    print(f"   ✅ Class {code}: {old_weight:.2f}x → 1.0x")
        
        # Fix view_palettes
        if hasattr(self, 'view_palettes') and self.view_palettes:
            for view_idx, palette in self.view_palettes.items():
                for code in palette:
                    old_weight = palette[code].get("weight", 1.0)
                    if old_weight != 1.0:
                        palette[code]["weight"] = 1.0
                        reset_count += 1
        
        # Clear saved weights from QSettings
        from PySide6.QtCore import QSettings
        settings = QSettings("NakshaAI", "LidarApp")
        if settings.contains("class_weight_states"):
            settings.remove("class_weight_states")
            settings.sync()
            print("   ✅ Cleared saved weights from settings")
        
        if reset_count > 0:
            print(f"\n   ✅ Reset {reset_count} classes to normal weight")
        else:
            print("   ℹ️  All weights already normal")        
        print("="*60 + "\n")
        
    def activate_measurement_tool(self, tool_name):
        """
        Activate measurement tool with proper coordination.
        ✅ FIXED: Proper cleanup and priority handling
        """
        self._deactivate_pending_cut_section_tool("switching to measurement")
        if not hasattr(self, 'measurement_tool'):
            from gui.measurement_tools import MeasurementTool
            self.measurement_tool = MeasurementTool(self.digitizer)

        # ✅ Deactivate element selection tool — it has its own VTK observers at
        # priority 2.0 that will compete with measurement tool observers.
        try:
            if hasattr(self, "digitizer") and self.digitizer:
                self.digitizer.deactivate_element_select_tool()
        except Exception as e:
            print(f"⚠️ Failed to deactivate element select for measurement: {e}")
        
        # ✅ CRITICAL: Deactivate cross-section COMPLETELY
        # Cancel cross-section and restore original interactor style before activating measurement
        if getattr(self, 'cross_section_active', False) or (hasattr(self, 'cross_interactor') and self.cross_interactor):
            print("🔄 Deactivating cross-section for measurement")
            self._cancel_cross_section_tool_only()

        
        # ✅ Deactivate section controller
        if hasattr(self, 'section_controller'):
            self.section_controller.deactivate_for_measurement()
        
        # ✅ Deactivate digitizer
        if hasattr(self, 'digitizer'):
            self.digitizer.active_tool = None
        
        # ✅ Now activate measurement (it will add its own observers)
        self.measurement_tool.activate(mode=tool_name)
        
        # Show instructions
        if tool_name == "measure_line":
            self.statusBar().showMessage("📏 Click 2 points to measure", 5000)
        elif tool_name == "measure_path":
            self.statusBar().showMessage("📏 Click points, right-click to finish", 5000)
        elif tool_name == "measure_polygon":
            self.statusBar().showMessage("📏 Click points, right-click to close", 5000)
            
        elif tool_name == "measure_block_area":
            self.statusBar().showMessage("📏 Click inside a block to measure its area", 5000)

        status_message = {
            "measure_line": "Click 2 points to measure",
            "measure_path": "Click points, right-click to finish",
            "measure_polygon": "Click points, right-click to close",
            "measure_block_area": "Click inside a block to measure its area",
            "measure_grid_area": "Click inside a grid to measure its area",
        }.get(tool_name)
        if status_message:
            self.statusBar().showMessage(status_message, 5000)

    def clear_all_measurements(self):
        """Clear all measurement lines and labels."""
        if hasattr(self, 'measurement_tool'):
            self.measurement_tool.clear_all_measurements()
            self.statusBar().showMessage("🗑️ Measurements cleared", 2000)
    def export_measurements(self):
        """Export measurement report to file."""
        if not hasattr(self, 'measurement_tool'):
            return
        
        report = self.measurement_tool.export_measurements()
        
        if report:
            from PySide6.QtWidgets import QFileDialog
            filepath, _ = QFileDialog.getSaveFileName(
                self,
                "Export Measurements",
                "",
                "Text Files (*.txt);;All Files (*)"
            )
            
            if filepath:
                try:
                    with open(filepath, 'w') as f:
                        f.write(report)
                    print(f"✅ Measurements exported to: {filepath}")
                    self.statusBar().showMessage(f"✅ Exported to {filepath}", 3000)
                except Exception as e:
                    print(f"⚠️ Export failed: {e}")


    def ensure_display_mode_dialog(self):
        """
        Ensure display mode dialog exists.
        Creates it if needed, returns True if it exists/was created.
        """
        dialog = getattr(self, 'display_mode_dialog', None)
        if dialog is not None:
            try:
                if _qt_object_is_valid(dialog):
                    return True
            except RuntimeError:
                pass
            self.display_mode_dialog = None

        if not hasattr(self, 'display_mode_dialog') or self.display_mode_dialog is None:
            try:
                from gui.display_mode import DisplayModeDialog
                self.display_mode_dialog = DisplayModeDialog(self)
                self.display_mode_dialog.applied.connect(self.apply_class_map)
                self.display_mode_dialog.view_switched.connect(self.activate_dock_by_view)
                
                # Phase 3: Connect palette_changed → GPU uniform sync
                self.display_mode_dialog.palette_changed.connect(self._on_palette_changed)
                
                # Connect to other systems
                class_picker = self._get_live_class_picker()
                if class_picker is not None:
                    self.display_mode_dialog.classes_loaded.connect(class_picker.on_classes_changed)
                
                if hasattr(self, 'point_count_widget') and self.point_count_widget:
                    self.point_count_widget.connect_to_display_mode(self.display_mode_dialog)

                self.display_mode_dialog.destroyed.connect(
                    lambda *_: setattr(self, 'display_mode_dialog', None)
                )
                
                print("✅ Display Mode dialog created")
                return True
            except Exception as e:
                print(f"⚠️ Failed to create display mode dialog: {e}")
                return False
        
        return True

    def _show_display_mode_front_message(
        self,
        icon,
        title,
        text,
        buttons=QMessageBox.Ok,
        default_button=QMessageBox.Ok,
        parent=None,
    ):
        """
        Show a popup above Display Mode when that dialog is topmost.
        """
        dialog = getattr(self, "display_mode_dialog", None)
        restore_dialog_enabled = True
        restore_dialog_topmost = False
        target_dialog = None

        try:
            if dialog is not None and _qt_object_is_valid(dialog) and dialog.isVisible():
                target_dialog = dialog
        except Exception:
            target_dialog = None

        if target_dialog is not None:
            try:
                restore_dialog_enabled = bool(target_dialog.isEnabled())
                restore_dialog_topmost = bool(target_dialog.windowFlags() & Qt.WindowStaysOnTopHint)
                target_dialog.setEnabled(False)
                target_dialog.setWindowFlag(Qt.WindowStaysOnTopHint, False)
                target_dialog.show()
                target_dialog.lower()
            except Exception:
                restore_dialog_enabled = True
                restore_dialog_topmost = False

        owner = parent if parent is not None else (None if target_dialog is not None else self)
        msg_box = QMessageBox(owner)
        try:
            msg_box.setIcon(icon)
            msg_box.setWindowTitle(title)
            msg_box.setText(text)
            msg_box.setStandardButtons(buttons)

            if default_button is not None:
                try:
                    msg_box.setDefaultButton(default_button)
                except Exception:
                    pass

            if target_dialog is not None:
                msg_box.setWindowModality(Qt.ApplicationModal)
                msg_box.setWindowFlag(Qt.WindowStaysOnTopHint, True)

            msg_box.show()
            msg_box.raise_()
            msg_box.activateWindow()
            return msg_box.exec()
        finally:
            if target_dialog is not None:
                try:
                    target_dialog.setEnabled(restore_dialog_enabled)
                    target_dialog.setWindowFlag(Qt.WindowStaysOnTopHint, restore_dialog_topmost)
                    target_dialog.show()
                    target_dialog.raise_()
                    target_dialog.activateWindow()
                except Exception:
                    pass

    def open_display_mode(self):
            """
            Open Display Mode as a normal non-modal NakshaAI utility window.
            Explicit open raises it once; subsequent focus follows native OS z-order.
            """
            from PySide6.QtCore import Qt

            if self._accudraw_canvas_right_click_in_progress():
                print("AccuDraw right-click finish - Display settings blocked")
                return None

            if not self.ensure_display_mode_dialog():
                return None

            dialog = self.display_mode_dialog
            dialog.setModal(False)
            if hasattr(dialog, 'sync_with_app_state'):
                try:
                    dialog.sync_with_app_state()
                except Exception as e:
                    print(f"⚠️ Dialog sync failed on open: {e}")
            # Display Mode is intentionally NOT globally always-on-top.
            # show_safely() raises it only for this explicit user request;
            # afterwards native Windows z-order is respected.
            dialog.setWindowFlag(Qt.WindowStaysOnTopHint, False)
            if hasattr(dialog, 'show_safely'):
                dialog.show_safely()
            else:
                if dialog.windowState() & Qt.WindowMinimized:
                    dialog.showNormal()
                else:
                    dialog.show()
                dialog.raise_()
                dialog.activateWindow()
                try:
                    dialog.setFocus(Qt.ActiveWindowFocusReason)
                except Exception:
                    pass

            print("Display Mode dialog visible")
            return dialog

            # Create dialog if needed
            if not hasattr(self, 'display_mode_dialog') or self.display_mode_dialog is None:
                from gui.display_mode import DisplayModeDialog
                self.display_mode_dialog = DisplayModeDialog(self)
                self.display_mode_dialog.applied.connect(self.apply_class_map)
                self.display_mode_dialog.view_switched.connect(self.activate_dock_by_view)
            
                # Phase 3: Connect palette_changed → GPU uniform sync
                self.display_mode_dialog.palette_changed.connect(self._on_palette_changed)
            
                class_picker = self._get_live_class_picker()
                if class_picker is not None:
                    self.display_mode_dialog.classes_loaded.connect(class_picker.on_classes_changed)
            
                if hasattr(self, 'point_count_widget') and self.point_count_widget:
                    self.point_count_widget.connect_to_display_mode(self.display_mode_dialog)
        
            # ✅ NUCLEAR OPTION: Force visibility no matter what state
            from PySide6.QtCore import Qt
        
            # Clear any window state flags that might hide it
            if self.display_mode_dialog.windowState() & Qt.WindowMinimized:
                self.display_mode_dialog.setWindowState(
                    self.display_mode_dialog.windowState() & ~Qt.WindowMinimized
                )
        
            # Ensure it's visible
            self.display_mode_dialog.setHidden(False)
            self.display_mode_dialog.setVisible(True)
            self.display_mode_dialog.show()
        
            # Force to foreground
            self.display_mode_dialog.setModal(False)
            self.display_mode_dialog.raise_()
            self.display_mode_dialog.activateWindow()
        
            # Belt and suspenders: also try native activation
            try:
                self.display_mode_dialog.setWindowState(Qt.WindowActive)
            except Exception:
                pass
        
            print("✅ Display Mode dialog FORCED visible")

    def open_fields_panel(self):
        """Open the MicroStation-style point data table for the loaded cloud."""
        from PySide6.QtWidgets import QMessageBox
        from gui.dialogs.view_fields_table import ViewFieldsTableDialog

        if not getattr(self, "loaded_file", None):
            QMessageBox.information(
                self,
                "View Fields",
                "Load a point cloud file first to view its fields.",
            )
            return

        if not self.data or "xyz" not in self.data:
            QMessageBox.information(
                self,
                "View Fields",
                "No point data loaded.",
            )
            return

        # Only one table window at a time — if one is already open, just
        # bring it to the front instead of creating a duplicate.
        existing = getattr(self, "_view_fields_table_dialog", None)
        if existing is not None and existing.isVisible():
            existing.refresh_data(self.loaded_file, self.data)
            existing.setWindowState(
                existing.windowState() & ~Qt.WindowMinimized
                | Qt.WindowActive)
            existing.raise_()
            existing.activateWindow()
            return

        dialog = ViewFieldsTableDialog(
            filename=self.loaded_file,
            app_data=self.data,
            parent=self,
        )
        # Keep a reference so the Identification tool can highlight rows here.
        self._view_fields_table_dialog = dialog
        dialog.finished.connect(
            lambda _r: setattr(self, "_view_fields_table_dialog", None))
        dialog.show()  # non-modal so the user can still interact with the 3D view

    def load_las_for_grid(self, grid_name):
        """
        Load LAZ/LAS file matching the clicked grid name - AUTO-DETECT folder
        ✅ FIXED: Now actually loads the file using same path as menu bar
        """
        try:
            from pathlib import Path
            from PySide6.QtWidgets import QFileDialog, QMessageBox
            from PySide6.QtCore import QSettings, QCoreApplication
            from gui.progress_dialog import LoadingProgressDialog
            import os
            import time
            import numpy as np
            
            print(f"\n{'='*60}")
            print(f"📂 LOADING LAZ/LAS FOR GRID: {grid_name}")
            print(f"{'='*60}")
            
            settings = QSettings("NakshaAI", "LidarApp")
            
            # ============================================================================
            # STEP 1: FIND THE LAZ FOLDER (your existing logic)
            # ============================================================================
            las_folder = None
            
            # Strategy 1: Check DXF file locations
            print(f"\n📋 STRATEGY 1: Check DXF locations")
            if hasattr(self, 'dxf_actors') and self.dxf_actors:
                for dxf_data in self.dxf_actors:
                    filename = dxf_data.get('filename', '') or dxf_data.get('full_path', '')
                    
                    if filename:
                        dxf_path = Path(filename)
                        
                        if dxf_path.exists():
                            dxf_folder = dxf_path.parent
                            
                            # Check same folder
                            las_files = list(dxf_folder.glob("*.laz")) + list(dxf_folder.glob("*.las"))
                            if las_files:
                                las_folder = dxf_folder
                                print(f"   ✅ FOUND in DXF folder: {las_folder}")
                                break
                            
                            # Check common subfolder names first
                            for subfolder_name in ['lazz', 'LAZZ', 'laz', 'LAZ', 'las', 'LAS']:
                                potential_folder = dxf_folder / subfolder_name
                                if potential_folder.exists():
                                    las_files = list(potential_folder.glob("*.laz")) + list(potential_folder.glob("*.las"))
                                    if las_files:
                                        las_folder = potential_folder
                                        print(f"   ✅ FOUND in subfolder: {las_folder}")
                                        break
                            
                            # Also check ALL immediate subfolders
                            if not las_folder:
                                try:
                                    for entry in dxf_folder.iterdir():
                                        if entry.is_dir():
                                            las_files = list(entry.glob("*.laz")) + list(entry.glob("*.las"))
                                            if las_files:
                                                las_folder = entry
                                                print(f"   ✅ FOUND in subfolder: {las_folder}")
                                                break
                                except Exception:
                                    pass
                            
                            if las_folder:
                                break
            
            # Strategy 2: Check saved settings
            if not las_folder:
                print(f"\n📋 STRATEGY 2: Check saved settings")
                last_las_path = settings.value("last_las_folder", "")
                if last_las_path:
                    path_obj = Path(last_las_path)
                    if path_obj.exists():
                        las_folder = path_obj
                        print(f"   ✅ FOUND: {las_folder}")
            
            # Strategy 3: Ask user
            if not las_folder:
                print(f"\n📋 STRATEGY 3: Ask user for folder")
                reply = QMessageBox.question(
                    self,
                    "Select LAZ/LAS Folder",
                    f"Could not auto-detect LAZ/LAS folder for grid '{grid_name}'.\n\n"
                    f"Would you like to select the folder manually?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes
                )
                
                if reply == QMessageBox.Yes:
                    folder = QFileDialog.getExistingDirectory(
                        self,
                        "Select LAZ/LAS Folder",
                        "",
                        QFileDialog.ShowDirsOnly
                    )
                    if folder:
                        las_folder = Path(folder)
                        settings.setValue("last_las_folder", str(las_folder))
                        settings.sync()
            
            if not las_folder:
                QMessageBox.warning(
                    self,
                    "Folder Not Found",
                    f"Could not locate LAZ/LAS folder for grid '{grid_name}'."
                )
                return
            
            # ============================================================================
            # STEP 2: FIND MATCHING FILE
            # ============================================================================
            print(f"\n🔍 Searching for file matching: {grid_name}")
            
            all_files = list(las_folder.glob("*.laz")) + list(las_folder.glob("*.las"))
            print(f"   Total files in folder: {len(all_files)}")
            
            if not all_files:
                QMessageBox.warning(
                    self,
                    "No Files Found",
                    f"No LAZ/LAS files found in:\n{las_folder}"
                )
                return
            
            from gui.lidar_file_matcher import (
                find_matching_lidar_file,
                strip_lidar_extension,
            )

            grid_name_clean = strip_lidar_extension(grid_name)
            las_file = find_matching_lidar_file(all_files, grid_name_clean)
            if las_file is not None:
                print(f"   ✅ EXACT GRID MATCH FOUND: {las_file.name}")
            
            if not las_file:
                print(f"   ❌ No automatic match found for '{grid_name_clean}'")
                print(f"   📄 Available files:")
                for fp in all_files[:10]:
                    print(f"      - {fp.name}")
                
                file_path, _ = QFileDialog.getOpenFileName(
                    self,
                    f"Select LAZ/LAS file for grid {grid_name}",
                    str(las_folder),
                    "LiDAR Files (*.laz *.las);;All Files (*.*)"
                )
                
                if not file_path:
                    return
                
                las_file = Path(file_path)
            
            import_options = self._prompt_lidar_import_options_for_files([str(las_file)])
            if import_options is None:
                return

            # ============================================================================
            # STEP 3: AUTO-SAVE CURRENT FILE (if exists) - SAME AS MENU BAR
            # ============================================================================
            if hasattr(self, 'data') and self.data is not None:
                save_path = getattr(self, 'last_save_path', None) or getattr(self, 'loaded_file', None)
                # Never overwrite the source file when the current load is class-filtered.
                _class_filtered = getattr(self, '_loaded_with_class_filter', False)
                if save_path and not _class_filtered:
                    try:
                        print(f"\n💾 AUTO-SAVING CURRENT FILE")
                        from gui.save_pointcloud import save_pointcloud_quick
                        result = save_pointcloud_quick(self, save_path)
                        if result:
                            print(f"   ✅ Saved successfully")
                    except Exception as e:
                        print(f"   ⚠️ Save failed: {e}")
            
            # ============================================================================
            # STEP 4: CLEAR CURRENT PROJECT - SAME AS MENU BAR
            # ============================================================================
            print(f"\n🧹 CLEARING CURRENT PROJECT")

            # Persist current file-specific display/PTC state before data clear.
            try:
                from gui.clear_project import _save_display_settings_before_clear
                _save_display_settings_before_clear(self)
                print("[RUNTIME-CHECK] pre-clear-save caller=grid-load step=main-window")
            except Exception as e:
                print(f"⚠️ Display settings pre-save skipped: {e}")

            # Stop queued debounced refresh callbacks from previous dataset.
            try:
                timer = getattr(self, "_update_debounce_timer", None)
                if timer is not None and timer.isActive():
                    timer.stop()
                pending = getattr(self, "_pending_view_updates", None)
                if hasattr(pending, "clear"):
                    pending.clear()
                if hasattr(self, "_last_changed_mask"):
                    self._last_changed_mask = None
                if hasattr(self, "_last_changed_indices"):
                    self._last_changed_indices = None
            except Exception:
                pass

            # Align grid-switch cleanup with open-file cleanup hooks.
            try:
                from gui.memory_manager import ObserverRegistry, release_data_arrays
                from gui.unified_actor_manager import reset_uam
                release_data_arrays(self)
                ObserverRegistry.release_all()
                reset_uam(self)
                mem_guard = getattr(self, "_mem_guard", None)
                if mem_guard is not None:
                    mem_guard.force_gc()
            except Exception as mem_exc:
                print(f"⚠️ Memory manager clear hook skipped: {mem_exc}")
            
            # Backup DXF actors
            dxf_backup = []
            if hasattr(self, 'dxf_actors') and self.dxf_actors:
                for dxf_data in self.dxf_actors:
                    for actor in dxf_data.get('actors', []):
                        dxf_backup.append(actor)
                
                if dxf_backup:
                    renderer = self.vtk_widget.renderer
                    for actor in dxf_backup:
                        renderer.RemoveActor(actor)
                    print(f"   💾 Backed up {len(dxf_backup)} DXF actors")
            
            # Backup SNT actors (same pattern as DXF)
            snt_backup = []
            if hasattr(self, 'snt_actors') and self.snt_actors:
                renderer = self.vtk_widget.renderer
                for snt_data in self.snt_actors:
                    for actor in snt_data.get('actors', []):
                        snt_backup.append(actor)
                        try:
                            renderer.RemoveActor(actor)
                        except Exception:
                            pass
                if snt_backup:
                    print(f"   💾 Backed up {len(snt_backup)} SNT actors")
            
            # Clear VTK
            if hasattr(self, "vtk_widget") and self.vtk_widget:
                renderer = self.vtk_widget.renderer
                renderer.RemoveAllViewProps()
                
                if hasattr(self.vtk_widget, 'actors'):
                    self.vtk_widget.actors.clear()
                if hasattr(self.vtk_widget, '_actors'):
                    self.vtk_widget._actors.clear()
                
                self.vtk_widget.render()
            
            # Clear cross-sections
            if hasattr(self, 'section_vtks') and self.section_vtks:
                for view_idx, vtk_widget in self.section_vtks.items():
                    try:
                        vtk_widget.renderer.RemoveAllViewProps()
                        if hasattr(vtk_widget, 'actors'):
                            vtk_widget.actors.clear()
                        vtk_widget.render()
                    except Exception:
                        pass

            # Clear cut section state/view to prevent stale cut data after grid switch
            if hasattr(self, 'cut_section_controller') and self.cut_section_controller:
                try:
                    self.cut_section_controller.clear()
                    print("   ✅ Cut section cleared")
                except Exception as e:
                    print(f"   ⚠️ Cut section clear failed: {e}")
                    try:
                        ctrl = self.cut_section_controller
                        ctrl.cut_points = None
                        ctrl._cut_index_map = None
                        ctrl.is_cut_view_active = False
                        print("   ✅ Applied fallback cut-state reset")
                    except Exception:
                        pass
            
            # Clear internal state
            self.data = None
            self.loaded_file = None
            self.last_save_path = None
            self.class_palette = {}

            # Clear layers so previous file arrays are not retained across grid switches.
            if hasattr(self, "layers") and isinstance(self.layers, list):
                self.layers.clear()
            if hasattr(self, "layers_dock") and self.layers_dock:
                try:
                    if hasattr(self.layers_dock, "clear_layers"):
                        self.layers_dock.clear_layers()
                except Exception:
                    pass

            # Drop stale section caches/masks bound to previous dataset.
            try:
                import re
                stale_section_attrs = [
                    name for name in list(vars(self).keys())
                    if re.match(r"^section_\d+_", name) or re.match(r"^_section_\d+_", name)
                ]
                for name in stale_section_attrs:
                    try:
                        delattr(self, name)
                    except Exception:
                        pass
            except Exception:
                pass
             
            if hasattr(self, "view_palettes"):
                self.view_palettes.clear()
            if hasattr(self, 'undo_stack'):
                self.undo_stack.clear()
            if hasattr(self, 'redo_stack'):
                self.redo_stack.clear()
            if hasattr(self, 'spatial_index'):
                self.spatial_index = None
            
            # Clear grid tracking
            if hasattr(self, 'grid_label_manager'):
                self.grid_label_manager.loaded_grids.clear()
            
            QCoreApplication.processEvents()
            
            # Restore DXF actors
            if dxf_backup:
                renderer = self.vtk_widget.renderer
                for actor in dxf_backup:
                    renderer.AddActor(actor)
                self.vtk_widget.render()
                QCoreApplication.processEvents()
                print(f"   ✅ Restored {len(dxf_backup)} DXF actors")
            
            # Restore SNT actors
            if snt_backup:
                renderer = self.vtk_widget.renderer
                for actor in snt_backup:
                    renderer.AddActor(actor)
                self.vtk_widget.render()
                print(f"   ✅ Restored {len(snt_backup)} SNT actors")
            
            print(f"   ✅ Clear complete")
            
            # ============================================================================
            # STEP 5: LOAD FILE - SAME AS MENU BAR
            # ============================================================================
            print(f"\n📂 LOADING: {las_file.name}")
            
            progress = LoadingProgressDialog(self, show_cancel=False)
            progress.set_filename(las_file.name)
            progress.show()
            
            def update_progress(percent, status):
                progress.set_progress(percent)
                progress.set_status(status)
                QCoreApplication.processEvents()
            
            load_start = time.time()
            
            try:
                update_progress(10, "Loading file...")
                
                from gui.data_loader import load_lidar_file
                tile_data = load_lidar_file(
                    str(las_file),
                    parent=self,
                    import_options=import_options,
                    prompt_user=False,
                )
                
                if not tile_data:
                    progress.finish_error("Load cancelled or failed")
                    return
                
                total_points = len(tile_data.get('xyz', []))
                print(f"   ✅ Loaded {total_points:,} points")
                
                # Set data
                update_progress(50, "Setting data...")
                
                self.data = {
                    "xyz": tile_data["xyz"],
                    "classification": tile_data["classification"]
                }
                self.data_bounds = None  # invalidate stale SNT z-offset cache for new dataset
                
                # Track whether this load was class-filtered so auto-save skips the file.
                _io = tile_data.get("import_options") or {}
                self._loaded_with_class_filter = bool(
                    _io.get("only_class") and _io.get("class_codes")
                )

                if tile_data.get("rgb") is not None:
                    self.data["rgb"] = tile_data["rgb"]
                if tile_data.get("intensity") is not None:
                    self.data["intensity"] = tile_data["intensity"]
                
                # Set CRS
                if tile_data.get("crs_epsg"):
                    self.project_crs_epsg = tile_data["crs_epsg"]
                    self.project_crs_wkt = tile_data.get("crs_wkt")
                    try:
                        from pyproj import CRS
                        self.crs = CRS.from_epsg(tile_data["crs_epsg"])
                    except Exception:
                        pass
                
                # Store as layer
                layer = {
                    "type": "laz_tile",
                    "filename": str(las_file),
                    "xyz": tile_data["xyz"],
                    "classification": tile_data.get("classification"),
                    "rgb": tile_data.get("rgb"),
                    "intensity": tile_data.get("intensity"),
                    "crs_epsg": tile_data.get("crs_epsg"),
                    "visible": True,
                }
                
                if hasattr(self, 'layers'):
                    self.layers.append(layer)
                if hasattr(self, 'layers_dock') and self.layers_dock:
                    self.layers_dock.add_layer(layer)
                
                self.loaded_file = str(las_file)
                self.last_save_path = str(las_file)
                
                # Build DEM
                try:
                    from gui.shading_display import build_base_dem_mesh
                    build_base_dem_mesh(self, percentile_filter=99.9, downsample=2)
                except Exception:
                    pass
                
                # Build spatial index
                if total_points > 50_000:
                    try:
                        update_progress(70, "Building spatial index...")
                        from gui.performance_optimizations import SpatialIndex
                        self.spatial_index = SpatialIndex(self.data["xyz"])
                    except Exception:
                        self.spatial_index = None
                
                # Restore settings
                update_progress(75, "Restoring settings...")
                try:
                    from gui.display_mode import restore_display_settings_for_file
                    self._prefer_session_display_restore = True
                    try:
                        restore_display_settings_for_file(self, str(las_file))
                    finally:
                        self._prefer_session_display_restore = False
                except Exception:
                    self._prefer_session_display_restore = False
                    pass

                from gui.unified_actor_manager import reset_border_logic_to_structured
                reset_border_logic_to_structured(self)
                
                # Set display mode
                self.display_mode = "class"
                if hasattr(self, 'display_mode_dialog') and self.display_mode_dialog:
                    if hasattr(self.display_mode_dialog, 'sync_with_app_state'):
                        try:
                            self.display_mode_dialog.sync_with_app_state()
                        except Exception:
                            pass
                
                # Get palette
                update_progress(60, "Loading classification palette...")
                palette_to_apply = None
                if hasattr(self, '_get_palette_for_file'):
                    palette_to_apply = self._get_palette_for_file(str(las_file))
                
                # Apply palette - SAME AS MENU BAR
                if palette_to_apply:
                    if isinstance(import_options, dict) and import_options.get("only_class"):
                        from gui.display_mode import clone_palette
                        _sel = set(int(c) for c in (import_options.get("class_codes") or []))
                        palette_to_apply = clone_palette(palette_to_apply)
                        for _code, _entry in palette_to_apply.items():
                            _entry["show"] = (_code in _sel)
                        print(f"   👁 Initial visibility: showing classes {sorted(_sel)}")
                    visible_count = len([c for c, v in palette_to_apply.items() if v.get("show")])
                    update_progress(85, f"Rendering {visible_count} classes...")
                    
                    self.apply_class_map({
                        "classes": palette_to_apply,
                        "slot": 0,
                        "color_mode": 0,
                        "target_view": 0
                    })
                else:
                    # Build palette from classification
                    try:
                        from gui.class_display import build_class_palette
                        self.class_palette = build_class_palette(tile_data['classification'])
                        
                        self.apply_class_map({
                            "classes": self.class_palette,
                            "slot": 0,
                            "color_mode": 0,
                            "target_view": 0
                        })
                    except Exception:
                        from gui.pointcloud_display import update_pointcloud
                        update_pointcloud(self, "class")
                
                # Finalize drawings
                try:
                    from gui.save_pointcloud import finalize_drawing_render
                    finalize_drawing_render(self)
                except Exception:
                    pass
                
                # Finalize
                update_progress(95, "Finalizing...")
                
                try:
                    from gui.pointcloud_display import force_interactor_ready
                    force_interactor_ready(self, delay_ms=300)
                except Exception:
                    pass
                
                if hasattr(self, 'toggle_view_mode'):
                    self.toggle_view_mode("2d")
                
                # Update title
                self._update_window_title(
                    f"{grid_name} ({total_points:,} pts)",
                    getattr(self, 'project_crs_epsg', None)
                )
                
                # Auto-load drawings
                if hasattr(self, "digitizer") and self.digitizer:
                    try:
                        self.digitizer.auto_load_drawings(str(las_file))
                    except Exception:
                        pass
                
                # Update statistics
                if hasattr(self, 'point_count_widget') and self.point_count_widget:
                    try:
                        from gui.point_count_widget import refresh_point_statistics
                        refresh_point_statistics(self)
                    except Exception:
                        pass
                
                # Track grid
                if hasattr(self, 'grid_label_manager'):
                    grid_indices = np.arange(total_points)
                    self.grid_label_manager.loaded_grids[grid_name] = grid_indices
                    
                    if not hasattr(self, 'original_file_paths'):
                        self.original_file_paths = {}
                    self.original_file_paths[grid_name] = str(las_file)
                
                total_time = time.time() - load_start
                
                print(f"\n{'='*60}")
                print(f"✅ GRID LOAD COMPLETE")
                print(f"   Grid: {grid_name}")
                print(f"   Points: {total_points:,}")
                print(f"   Time: {total_time:.1f}s")
                print(f"{'='*60}\n")

                if hasattr(self, 'block_identifier_dialog') and self.block_identifier_dialog:
                            try:
                                self.block_identifier_dialog.reapply_hide_state()
                            except Exception:
                                pass

                if hasattr(self, 'block_identifier_dialog') and self.block_identifier_dialog:
                    try:
                        prj_dlg = self.block_identifier_dialog
                        prj_path = getattr(prj_dlg, "current_prj_path", None)
                        prj_data = getattr(prj_dlg, "prj_data", None)
                        if (not prj_data) and prj_path and os.path.exists(prj_path):
                            if hasattr(prj_dlg, "parse_prj_file"):
                                print(f"🔁 Restoring PRJ dialog data: {os.path.basename(prj_path)}")
                                prj_dlg.parse_prj_file(prj_path)
                        prj_dlg.reapply_hide_state()
                    except Exception:
                        pass    
                                        
                # ✅ BULLETPROOF: Re-ensure all overlay actors are in renderer
                self._ensure_overlay_actors()
                
                progress.finish_success(f"Loaded {total_points:,} points")
                
                QMessageBox.information(
                    self,
                    "Grid Loaded",
                    f"✅ Loaded: {grid_name}\n\n"
                    f"File: {las_file.name}\n"
                    f"Points: {total_points:,}"
                )
                
            except Exception as e:
                print(f"❌ Load failed: {e}")
                import traceback
                traceback.print_exc()
                progress.finish_error(f"Load failed: {e}")
                QMessageBox.critical(self, "Load Error", f"Failed to load: {e}")
                
        except Exception as e:
            print(f"⚠️ Failed to load LAZ/LAS for {grid_name}: {e}")
            import traceback
            traceback.print_exc()
            
            QMessageBox.critical(
                self,
                "Load Error",
                f"Failed to load LAZ/LAS for grid '{grid_name}':\n{str(e)}"
            )
        
    def rebuild_shortcuts(self):
        """
        Rebuild the shortcuts dictionary from current configuration.
        Call this whenever classification settings are changed.
        """
        self.shortcuts = {}
        
        # Load shortcuts from your configuration
        # This depends on where you store your shortcut configuration
        # Example assuming you have a config object:
        
        if hasattr(self, 'config') and 'shortcuts' in self.config:
            shortcut_config = self.config['shortcuts']
            
            for shortcut_def in shortcut_config:
                # Parse the shortcut definition
                # Example format: {"key": "F1", "modifier": "ctrl", "tool": "classify", "from": "A", "to": "B"}
                
                mod = shortcut_def.get('modifier', 'none').lower()
                key = shortcut_def.get('key', '').upper()
                tool = shortcut_def.get('tool')
                from_cls = shortcut_def.get('from')
                to_cls = shortcut_def.get('to')
                
                combo = (mod, key)
                self.shortcuts[combo] = {
                    'tool': tool,
                    'from': from_cls,
                    'to': to_cls
                }
        
        print(f"🔄 Shortcuts rebuilt: {len(self.shortcuts)} shortcuts loaded")
        print(f"📋 Current shortcuts: {self.shortcuts}")

    # Also add this method to be called whenever settings are saved:
    def save_classification_settings(self):
        """
        Call this after user modifies classification settings in the UI.
        """
        # Your existing save logic here
        # ...
        
        # ✅ ADD THIS LINE: Rebuild shortcuts after settings change
        self.rebuild_shortcuts()
        
        print("✅ Classification settings saved and shortcuts reloaded")

    def _consume_vtk_event(self, obj):
        """Prevent the default interactor style from processing the same wheel event."""
        if obj is None:
            return

        try:
            if hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
            elif hasattr(obj, "SetAbortFlag"):
                try:
                    obj.SetAbortFlag(1)
                except TypeError:
                    obj.SetAbortFlag(True)
        except Exception:
            pass

    def _update_zoom_level_display(self, factor):
        if not hasattr(self, "_current_zoom_level"):
            self._current_zoom_level = 100.0

        self._current_zoom_level *= factor
        self._current_zoom_level = max(10.0, min(self._current_zoom_level, 5000.0))

        if hasattr(self, "magnifier_combo") and self.magnifier_combo:
            zoom_int = int(round(self._current_zoom_level))
            self.magnifier_combo.blockSignals(True)
            self.magnifier_combo.setCurrentText(f"{zoom_int}%")
            self.magnifier_combo.blockSignals(False)

    def _display_to_world_on_focal_plane(self, renderer, display_x, display_y):
        camera = renderer.GetActiveCamera()
        if camera is None:
            return None

        focal_point = camera.GetFocalPoint()
        renderer.SetWorldPoint(focal_point[0], focal_point[1], focal_point[2], 1.0)
        renderer.WorldToDisplay()
        _, _, focal_depth = renderer.GetDisplayPoint()

        renderer.SetDisplayPoint(float(display_x), float(display_y), float(focal_depth))
        renderer.DisplayToWorld()
        world_point = renderer.GetWorldPoint()
        if world_point is None or len(world_point) < 4:
            return None

        w = world_point[3]
        if abs(w) < 1e-9:
            return None

        return (
            world_point[0] / w,
            world_point[1] / w,
            world_point[2] / w,
        )

    def _display_to_world(self, renderer, display_x, display_y, display_z):
        if renderer is None:
            return None

        renderer.SetDisplayPoint(float(display_x), float(display_y), float(display_z))
        renderer.DisplayToWorld()
        world_point = renderer.GetWorldPoint()
        if world_point is None or len(world_point) < 4:
            return None

        w = world_point[3]
        if abs(w) < 1e-9:
            return None

        return (
            world_point[0] / w,
            world_point[1] / w,
            world_point[2] / w,
        )

    def _world_to_display(self, renderer, world_point):
        if renderer is None or world_point is None:
            return None

        renderer.SetWorldPoint(world_point[0], world_point[1], world_point[2], 1.0)
        renderer.WorldToDisplay()
        display_point = renderer.GetDisplayPoint()
        if display_point is None or len(display_point) < 3:
            return None
        return (
            float(display_point[0]),
            float(display_point[1]),
            float(display_point[2]),
        )

    def _pick_world_point(self, vtk_widget, interactor=None, display_x=None, display_y=None):
        if vtk_widget is None:
            return None

        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return None

        if interactor is None:
            interactor = getattr(vtk_widget, "interactor", None)
        if interactor is None:
            return None

        if display_x is None or display_y is None:
            display_x, display_y = interactor.GetEventPosition()

        pickers = []
        try:
            point_picker = vtk.vtkPointPicker()
            point_picker.SetTolerance(0.01)
            pickers.append((point_picker, lambda p: p.GetPointId() >= 0))
        except Exception:
            pass

        try:
            cell_picker = vtk.vtkCellPicker()
            cell_picker.SetTolerance(0.001)
            pickers.append((cell_picker, lambda p: p.GetCellId() >= 0))
        except Exception:
            pass

        try:
            prop_picker = vtk.vtkPropPicker()
            pickers.append((prop_picker, lambda p: p.GetViewProp() is not None))
        except Exception:
            pass

        for picker, is_valid in pickers:
            try:
                if picker.Pick(float(display_x), float(display_y), 0.0, renderer) and is_valid(picker):
                    picked = picker.GetPickPosition()
                    if picked is not None and len(picked) >= 3:
                        return (float(picked[0]), float(picked[1]), float(picked[2]))
            except Exception:
                continue

        return None
    
    def _main_view_pick_hits_actor(self, actor, display_x=None, display_y=None, tolerance=0.0005):
        """
        True when the current main-view click lands on the given VTK actor.

        Uses pick-lists so overlay props, labels, and other scene actors do not
        accidentally count as a hit for Surface/Shading settings.
        """
        if actor is None:
            return False

        vtk_widget = getattr(self, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        interactor = getattr(vtk_widget, "interactor", None) if vtk_widget is not None else None
        if renderer is None or interactor is None:
            return False

        if display_x is None or display_y is None:
            try:
                display_x, display_y = interactor.GetEventPosition()
            except Exception:
                return False

        # Hardware prop selection is effectively constant-time even for the
        # 69M-face Slow shading mesh. A vtkCellPicker performs a CPU cell
        # intersection and can block the right-click popup for seconds, so it
        # is strictly a compatibility fallback when hardware picking errors.
        try:
            prop_picker = vtk.vtkPropPicker()
            if hasattr(prop_picker, "PickFromListOn"):
                prop_picker.PickFromListOn()
            if hasattr(prop_picker, "AddPickList"):
                prop_picker.AddPickList(actor)
            picked = bool(prop_picker.Pick(
                float(display_x), float(display_y), 0.0, renderer
            ))
            if not picked:
                return False
            picked_actor = (
                prop_picker.GetActor()
                if hasattr(prop_picker, "GetActor") else None
            )
            if picked_actor is None and hasattr(prop_picker, "GetViewProp"):
                picked_actor = prop_picker.GetViewProp()
            return picked_actor is actor
        except Exception:
            pass

        try:
            cell_picker = vtk.vtkCellPicker()
            cell_picker.SetTolerance(float(tolerance))
            cell_picker.PickFromListOn()
            cell_picker.AddPickList(actor)
            if not cell_picker.Pick(
                    float(display_x), float(display_y), 0.0, renderer):
                return False
            return cell_picker.GetActor() is actor
        except Exception:
            return False

    def _store_zoom_anchor(self, vtk_widget, interactor=None, display_x=None, display_y=None):
        if vtk_widget is None:
            return

        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return

        if interactor is None:
            interactor = getattr(vtk_widget, "interactor", None)
        if interactor is None:
            return

        if display_x is None or display_y is None:
            display_x, display_y = interactor.GetEventPosition()

        world_point = self._pick_world_point(
            vtk_widget,
            interactor=interactor,
            display_x=display_x,
            display_y=display_y,
        )
        if world_point is None:
            world_point = self._display_to_world_on_focal_plane(renderer, display_x, display_y)
        if world_point is None:
            return

        if not hasattr(self, "_zoom_anchor_points"):
            self._zoom_anchor_points = {}
        self._zoom_anchor_points[id(vtk_widget)] = {
            "world": world_point,
            "display": (float(display_x), float(display_y)),
        }

    def _zoom_widget_at_anchor(self, vtk_widget, factor, anchor_data, *, render=True):
        if vtk_widget is None or factor <= 0 or anchor_data is None:
            return False

        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return False

        camera = renderer.GetActiveCamera()
        if camera is None:
            return False

        if isinstance(anchor_data, dict):
            anchor_world = anchor_data.get("world")
            anchor_display_target = anchor_data.get("display")
        else:
            anchor_world = anchor_data
            anchor_display_target = None

        if anchor_world is None:
            return False

        anchor_display_before = self._world_to_display(renderer, anchor_world)
        if anchor_display_before is None:
            return False

        camera.Zoom(factor)

        anchor_display_after = self._world_to_display(renderer, anchor_world)
        if anchor_display_after is None:
            return False

        if (
            anchor_display_target is None
            or len(anchor_display_target) < 2
        ):
            target_x = anchor_display_before[0]
            target_y = anchor_display_before[1]
        else:
            target_x = float(anchor_display_target[0])
            target_y = float(anchor_display_target[1])

        world_after = self._display_to_world(
            renderer,
            target_x,
            target_y,
            anchor_display_after[2],
        )
        if world_after is not None:
            delta = (
                anchor_world[0] - world_after[0],
                anchor_world[1] - world_after[1],
                anchor_world[2] - world_after[2],
            )
            position = camera.GetPosition()
            focal_point = camera.GetFocalPoint()
            camera.SetPosition(
                position[0] + delta[0],
                position[1] + delta[1],
                position[2] + delta[2],
            )
            camera.SetFocalPoint(
                focal_point[0] + delta[0],
                focal_point[1] + delta[1],
                focal_point[2] + delta[2],
            )

        if render:
            vtk_widget.render()
        return True

    def _zoom_widget_at_cursor(
        self,
        vtk_widget,
        factor,
        interactor=None,
        *,
        display_position=None,
        render=True,
    ):
        if vtk_widget is None or factor <= 0:
            return False

        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return False

        if interactor is None:
            interactor = getattr(vtk_widget, "interactor", None)
        if interactor is None:
            return False

        camera = renderer.GetActiveCamera()
        if camera is None:
            return False

        snapshot = {
            "position": tuple(camera.GetPosition()),
            "focal_point": tuple(camera.GetFocalPoint()),
            "parallel_scale": float(camera.GetParallelScale()),
            "view_angle": float(camera.GetViewAngle()),
            "parallel_projection": bool(camera.GetParallelProjection()),
        }

        def _restore_camera():
            try:
                camera.SetPosition(snapshot["position"])
                camera.SetFocalPoint(snapshot["focal_point"])
                camera.SetParallelScale(snapshot["parallel_scale"])
                camera.SetViewAngle(snapshot["view_angle"])
                if snapshot["parallel_projection"]:
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
            except Exception:
                pass

        if display_position is None:
            display_x, display_y = interactor.GetEventPosition()
        else:
            display_x, display_y = display_position
        world_before = self._display_to_world_on_focal_plane(renderer, display_x, display_y)
        if world_before is None:
            return False

        try:
            camera.Zoom(factor)
        except Exception:
            _restore_camera()
            return False

        world_after = self._display_to_world_on_focal_plane(renderer, display_x, display_y)
        if world_after is None:
            _restore_camera()
            return False

        delta = (
            world_before[0] - world_after[0],
            world_before[1] - world_after[1],
            world_before[2] - world_after[2],
        )
        if not all(np.isfinite(value) for value in delta):
            _restore_camera()
            return False
        position = camera.GetPosition()
        focal_point = camera.GetFocalPoint()
        camera.SetPosition(
            position[0] + delta[0],
            position[1] + delta[1],
            position[2] + delta[2],
        )
        camera.SetFocalPoint(
            focal_point[0] + delta[0],
            focal_point[1] + delta[1],
            focal_point[2] + delta[2],
        )

        if render:
            vtk_widget.render()
        return True

    def _main_pan_display_position(self, position, canvas_width, canvas_height):
        """Convert a Qt mouse position to this canvas's VTK display space."""
        vtk_widget = getattr(self, "vtk_widget", None)
        render_window = (
            vtk_widget.GetRenderWindow() if vtk_widget is not None else None
        )
        render_size = render_window.GetSize() if render_window is not None else None
        if not render_size or render_size[0] <= 0 or render_size[1] <= 0:
            return None

        from gui.zoom_navigation import qt_position_to_vtk_display
        return qt_position_to_vtk_display(
            position.x(),
            position.y(),
            canvas_width,
            canvas_height,
            render_size[0],
            render_size[1],
        )

    def _set_digitizer_qt_pan_guard(self, active):
        """Keep drawing/edit tools inert while Qt owns middle-button pan."""
        digitizer = getattr(self, "digitizer", None)
        if digitizer is None:
            return

        guarded = bool(active)
        try:
            digitizer._middle_button_down = guarded
            digitizer.middle_down = guarded
            digitizer._is_panning = guarded
            digitizer._pan_start_pos = None
            digitizer._last_pos = None
            digitizer._blocked_fake_middle = False
            digitizer._pan_press_monotonic = 0.0
            digitizer._pan_button_miss_count = 0
            if not guarded:
                digitizer._move_vertex_pan_block_until = 0.0
                digitizer._ignore_move_vertex_finish_until = 0.0
        except Exception:
            pass

    def _repair_stale_main_interactor_drag(self):
        """Stop a leaked VTK drag state when no physical button is down."""
        vtk_widget = getattr(self, "vtk_widget", None)
        interactor = getattr(vtk_widget, "interactor", None)
        if interactor is None and vtk_widget is not None:
            try:
                render_window = vtk_widget.GetRenderWindow()
                interactor = render_window.GetInteractor() if render_window else None
            except Exception:
                interactor = None
        if interactor is None:
            return False

        try:
            style = interactor.GetInteractorStyle()
            state = int(style.GetState()) if style is not None else 0
        except Exception:
            return False
        if state == 0:
            return False

        digitizer = getattr(self, "digitizer", None)
        reset = getattr(digitizer, "_reset_stale_zoom_mouse_state", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass

        try:
            if int(style.GetState()) != 0 and hasattr(style, "StopState"):
                style.StopState()
            if hasattr(interactor, "ReleaseFocus"):
                interactor.ReleaseFocus()
        except Exception:
            pass

        print(f"Stale VTK interaction state {state} stopped before mouse move")
        return True

    def _handle_fast_main_pan_press(self, position, canvas_width, canvas_height):
        """Start one Qt-owned main-view pan before VTK sees the press."""
        if getattr(self, "_shutdown_in_progress", False):
            return False
        if getattr(self, "is_3d_mode", False):
            return False
        if getattr(self, "active_classify_tool", None) is not None:
            return False

        zoom_tool = getattr(self, "zoom_rectangle_tool", None)
        if (
            zoom_tool is not None
            and getattr(zoom_tool, "active", False)
            and getattr(zoom_tool, "start_pos", None) is not None
        ):
            return False

        vtk_widget = getattr(self, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        camera = renderer.GetActiveCamera() if renderer is not None else None
        if camera is None:
            return False

        try:
            display_position = self._main_pan_display_position(
                position, canvas_width, canvas_height
            )
        except (TypeError, ValueError, AttributeError, RuntimeError):
            return False
        if display_position is None:
            return False

        self._cancel_smooth_zoom_for_pan()
        self._qt_main_pan_last_display = tuple(display_position)
        self._qt_main_pan_active = True
        self._main_view_history_pan_active = True
        try:
            history_timer = getattr(self, "_view_history_timer", None)
            if history_timer is not None:
                history_timer.stop()
        except Exception:
            pass
        self._set_digitizer_qt_pan_guard(True)

        try:
            render_window = vtk_widget.GetRenderWindow()
            if render_window is not None:
                render_window.SetDesiredUpdateRate(30.0)
        except Exception:
            pass

        manager = getattr(self, "gpu_render_manager", None)
        self._qt_main_pan_manager_started = False
        if manager is not None and hasattr(manager, "begin_pan_interaction"):
            try:
                point_count = len(self.data.get("xyz", ())) if self.data else 0
            except Exception:
                point_count = 0
            try:
                self._qt_main_pan_manager_started = bool(
                    manager.begin_pan_interaction(point_count=point_count)
                )
            except Exception:
                self._qt_main_pan_manager_started = False
        return True

    def _handle_fast_main_pan_move(
        self,
        position,
        canvas_width,
        canvas_height,
        *,
        middle_down=True,
    ):
        """Apply a cursor delta once; queued moves after release are ignored."""
        if not getattr(self, "_qt_main_pan_active", False):
            return False
        if not middle_down:
            return self._handle_fast_main_pan_release()

        vtk_widget = getattr(self, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        camera = renderer.GetActiveCamera() if renderer is not None else None
        if camera is None:
            self._handle_fast_main_pan_release()
            return True

        try:
            current_display = self._main_pan_display_position(
                position, canvas_width, canvas_height
            )
            previous_display = getattr(self, "_qt_main_pan_last_display", None)
            self._qt_main_pan_last_display = tuple(current_display)
        except (TypeError, ValueError, AttributeError, RuntimeError):
            return True

        if previous_display is None or tuple(current_display) == tuple(previous_display):
            return True

        try:
            world_previous = self._display_to_world_on_focal_plane(
                renderer, previous_display[0], previous_display[1]
            )
            world_current = self._display_to_world_on_focal_plane(
                renderer, current_display[0], current_display[1]
            )
            if world_previous is None or world_current is None:
                return True

            delta = tuple(
                float(world_previous[index]) - float(world_current[index])
                for index in range(3)
            )
            if not all(np.isfinite(value) for value in delta):
                return True

            camera_position = camera.GetPosition()
            focal_point = camera.GetFocalPoint()
            camera.SetPosition(
                camera_position[0] + delta[0],
                camera_position[1] + delta[1],
                camera_position[2] + delta[2],
            )
            camera.SetFocalPoint(
                focal_point[0] + delta[0],
                focal_point[1] + delta[1],
                focal_point[2] + delta[2],
            )
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            return True

        manager = getattr(self, "gpu_render_manager", None)
        if manager is not None and hasattr(manager, "request_render"):
            try:
                manager.request_render(vtk_widget)
                return True
            except Exception:
                pass
        try:
            vtk_widget.render()
        except Exception:
            pass
        return True

    def _handle_fast_main_pan_release(self):
        """End Qt-owned pan, cancel queued repaint work, and settle once."""
        if not getattr(self, "_qt_main_pan_active", False):
            return False

        self._qt_main_pan_active = False
        self._qt_main_pan_last_display = None
        self._main_view_history_pan_active = False
        self._set_digitizer_qt_pan_guard(False)

        vtk_widget = getattr(self, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        try:
            if renderer is not None:
                renderer.ResetCameraClippingRange()
                from gui.unified_actor_manager import refresh_widget_camera_uniforms
                refresh_widget_camera_uniforms(vtk_widget)
            render_window = vtk_widget.GetRenderWindow() if vtk_widget is not None else None
            if render_window is not None:
                render_window.SetDesiredUpdateRate(0.001)
        except Exception:
            pass

        manager = getattr(self, "gpu_render_manager", None)
        settled = False
        if (
            manager is not None
            and getattr(self, "_qt_main_pan_manager_started", False)
            and hasattr(manager, "finish_pan_interaction")
        ):
            try:
                settled = bool(manager.finish_pan_interaction())
            except Exception:
                settled = False
        self._qt_main_pan_manager_started = False
        if not settled and vtk_widget is not None:
            try:
                vtk_widget.render()
            except Exception:
                pass

        self._schedule_main_view_history_commit("pan_or_view_change", delay_ms=80)

        digitizer = getattr(self, "digitizer", None)
        if (
            digitizer is not None
            and getattr(digitizer, "temp_points", None)
            and getattr(digitizer, "active_tool", None)
            and hasattr(digitizer, "_deferred_preview_update")
        ):
            try:
                QTimer.singleShot(0, digitizer._deferred_preview_update)
            except Exception:
                pass
        return True

    def _handle_fast_main_wheel(self, wheel_delta, *, display_position=None):
        """Apply one immediate, bounded 2D zoom and schedule one repaint.

        This method is called from the Qt event filter, before QtInteractor can
        translate the same physical input into a native VTK wheel event.
        """
        if getattr(self, "_shutdown_in_progress", False):
            return False
        if getattr(self, "is_3d_mode", False):
            return False

        vtk_widget = getattr(self, "vtk_widget", None)
        renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
        if renderer is None:
            return False
        camera = renderer.GetActiveCamera()
        if camera is None:
            return False

        try:
            from gui.zoom_navigation import fast_wheel_zoom_factor
            factor = fast_wheel_zoom_factor(wheel_delta)
        except (TypeError, ValueError):
            return False

        snapshot = {
            "position": tuple(camera.GetPosition()),
            "focal_point": tuple(camera.GetFocalPoint()),
            "parallel_scale": float(camera.GetParallelScale()),
            "view_angle": float(camera.GetViewAngle()),
            "parallel_projection": bool(camera.GetParallelProjection()),
        }

        def _restore_camera():
            try:
                camera.SetPosition(snapshot["position"])
                camera.SetFocalPoint(snapshot["focal_point"])
                camera.SetParallelScale(snapshot["parallel_scale"])
                camera.SetViewAngle(snapshot["view_angle"])
                if snapshot["parallel_projection"]:
                    camera.ParallelProjectionOn()
                else:
                    camera.ParallelProjectionOff()
            except Exception:
                pass

        applied = False
        try:
            # Main 2D wheel zoom is always cursor-oriented.  Do not let an old
            # saved center/picked-point preference move production users away
            # from the feature currently under the pointer.
            applied = self._zoom_widget_at_cursor(
                vtk_widget,
                factor,
                interactor=getattr(vtk_widget, "interactor", None),
                display_position=display_position,
                render=False,
            )

            if not applied:
                # Coordinate conversion can be unavailable during viewport
                # creation/teardown.  Restore first, then use exactly one safe
                # center zoom rather than leaving a half-applied anchor update.
                _restore_camera()
                camera.Zoom(factor)
                applied = True
        except (RuntimeError, AttributeError, OSError, ReferenceError):
            _restore_camera()
            return False
        except Exception:
            _restore_camera()
            return False

        if not applied:
            return False

        try:
            if camera.GetParallelProjection():
                scale = float(camera.GetParallelScale())
                if not np.isfinite(scale) or scale <= 0.0:
                    _restore_camera()
                    return False
                camera.SetParallelScale(max(1.0e-6, min(1.0e12, scale)))
            else:
                view_angle = float(camera.GetViewAngle())
                if not np.isfinite(view_angle) or view_angle <= 0.0:
                    _restore_camera()
                    return False
                camera.SetViewAngle(max(0.1, min(179.0, view_angle)))
        except Exception:
            _restore_camera()
            return False

        render_manager = getattr(self, "gpu_render_manager", None)
        if render_manager is not None:
            try:
                point_count = len(self.data.get("xyz", ())) if self.data else 0
            except Exception:
                point_count = 0
            try:
                render_manager.begin_wheel_interaction(point_count=point_count)
                render_manager.request_render(vtk_widget)
            except Exception:
                try:
                    vtk_widget.render()
                except Exception:
                    pass
        else:
            try:
                vtk_widget.render()
            except Exception:
                pass

        self._update_zoom_level_display(factor)
        self._schedule_main_view_history_commit("fast_cursor_wheel_zoom", delay_ms=180)

        # The consumed Qt event no longer reaches the digitizer's VTK wheel
        # observer, so preserve its screen-space preview refresh explicitly.
        digitizer = getattr(self, "digitizer", None)
        if digitizer is not None and hasattr(digitizer, "_on_zoom"):
            try:
                vtk_event = (
                    "MouseWheelForwardEvent"
                    if float(wheel_delta) > 0.0
                    else "MouseWheelBackwardEvent"
                )
                digitizer._on_zoom(getattr(vtk_widget, "interactor", None), vtk_event)
            except Exception:
                pass
        return True

    def _on_main_left_click_for_zoom_anchor(self, obj, evt):
        if getattr(self, "zoom_behavior", "center") != "picked_point":
            return
        self._store_zoom_anchor(self.vtk_widget, interactor=self.vtk_widget.interactor)

    def _main_view_3d_unlocked(self) -> bool:
        """True only when user explicitly unlocked main view for 3D orbit."""
        return not bool(getattr(self, "_main_view_2d_locked", True))

    def _should_enforce_main_2d_policy(self) -> bool:
        """Guard to keep main canvas in 2D unless explicitly unlocked."""
        if getattr(self, "is_3d_mode", False):
            return False
        if self._main_view_3d_unlocked():
            return False
        if bool(getattr(self, "cross_section_active", False)):
            return False
        if getattr(self, "cross_interactor", None) is not None:
            return False
        cross_action = getattr(self, "cross_action", None)
        if cross_action is not None and hasattr(cross_action, "isChecked"):
            try:
                if cross_action.isChecked():
                    return False
            except Exception:
                pass
        return True

    def _on_main_left_press_2d_guard(self, obj, evt):
        """Repair accidental trackball style leaks before normal click handlers run."""
        if not self._should_enforce_main_2d_policy():
            return

        interactor = getattr(getattr(self, "vtk_widget", None), "interactor", None)
        if interactor is None:
            return

        try:
            style = interactor.GetInteractorStyle()
            style_name = style.GetClassName() if style is not None else "None"
        except Exception:
            style_name = "None"

        if style_name == "vtkInteractorStyleTrackballCamera":
            self.ensure_main_view_2d_interaction(
                preserve_camera=True,
                reason="left_press_guard",
            )

    def _on_main_left_double_click_guard(self, obj, evt):
        """Consume VTK-level double-clicks while main view is in locked 2D mode."""
        if not self._should_enforce_main_2d_policy():
            return

        self._consume_vtk_event(obj)
        self.ensure_main_view_2d_interaction(
            preserve_camera=True,
            reason="double_click_guard",
        )

    def _on_main_mouse_wheel(self, obj, evt):
        # In 3D mode, do not run 2D cursor/picked-point zoom logic.
        # Let vtkInteractorStyleTrackballCamera handle wheel zoom normally.
        if getattr(self, "is_3d_mode", False):
            self._schedule_main_view_history_commit("3d_mouse_wheel_zoom", delay_ms=120)
            return

        zoom_behavior = getattr(self, "zoom_behavior", "center")

        # Accumulate each standard notch into the eased target multiplier.
        notch = self._zoom_notch_factor if evt == "MouseWheelForwardEvent" else (1.0 / self._zoom_notch_factor)
        self._zoom_target_factor *= notch

        # This high-priority handler consumes the VTK event, so the render
        # manager's lower-priority wheel observer will not see it.  Explicitly
        # mark the whole eased animation as an interaction: intermediate frames
        # are then coalesced and one full-quality frame is drawn when it settles.
        render_manager = getattr(self, "gpu_render_manager", None)
        if render_manager is not None and hasattr(render_manager, "begin_wheel_interaction"):
            render_manager.begin_wheel_interaction()

        # For picked_point mode, capture the anchor once per gesture.
        if zoom_behavior == "picked_point":
            self._zoom_anchor_pending = getattr(self, "_zoom_anchor_points", {}).get(id(self.vtk_widget))

        self._start_smooth_zoom()

        digitizer = getattr(self, "digitizer", None)
        if digitizer and hasattr(digitizer, "_on_zoom"):
            try:
                digitizer._on_zoom(obj, evt)
            except Exception:
                pass

        self._consume_vtk_event(obj)

    def _start_smooth_zoom(self):
        """Kick off / keep alive the eased zoom animation timer."""
        if self._zoom_anim_active:
            return
        self._zoom_anim_active = True
        from PySide6.QtCore import QTimer
        if self._zoom_anim_timer is None:
            self._zoom_anim_timer = QTimer(self)
            self._zoom_anim_timer.timeout.connect(self._smooth_zoom_tick)
        self._zoom_anim_timer.start(16)

    def _cancel_smooth_zoom_for_pan(self):
        """End pending wheel easing immediately when direct panning begins.

        The camera already contains every step applied so far, so cancellation
        only clears the pending multiplier/timer. It does not alter framing or
        trigger a competing render while the pan gesture is starting.
        """
        was_active = bool(
            getattr(self, "_zoom_anim_active", False)
            or abs(float(getattr(self, "_zoom_target_factor", 1.0)) - 1.0)
            > 1e-12
        )
        self._zoom_target_factor = 1.0
        self._zoom_anim_active = False
        timer = getattr(self, "_zoom_anim_timer", None)
        if timer is not None:
            timer.stop()
        if was_active:
            self._schedule_main_view_history_commit(
                "smooth_mouse_wheel_zoom",
                delay_ms=80,
            )
            print("Wheel zoom cancelled cleanly for middle-button pan")
        return was_active

    def _finish_smooth_zoom(self):
        """Stop easing and settle the render manager exactly once."""
        was_active = bool(self._zoom_anim_active)
        self._zoom_target_factor = 1.0
        self._zoom_anim_active = False
        if self._zoom_anim_timer is not None:
            self._zoom_anim_timer.stop()

        render_manager = getattr(self, "gpu_render_manager", None)
        if render_manager is not None and hasattr(render_manager, "finish_wheel_interaction"):
            # The manager restores the full actor, recalculates clipping from
            # its authoritative bounds, then draws the settled frame.
            render_manager.finish_wheel_interaction()
        elif was_active:
            try:
                self.vtk_widget.renderer.ResetCameraClippingRange()
                self.vtk_widget.render()
            except Exception:
                pass

        if was_active:
            # One history entry per completed wheel gesture. Intermediate
            # eased frames are implementation details, not user-visible steps.
            self._schedule_main_view_history_commit(
                "smooth_mouse_wheel_zoom",
                delay_ms=80,
            )

    def _smooth_zoom_tick(self):
        """Apply one bounded, symmetric ease-out step."""
        if getattr(self, "is_3d_mode", False):
            self._finish_smooth_zoom()
            return

        remaining = getattr(self, "_zoom_target_factor", 1.0)
        if remaining is None or abs(remaining - 1.0) < 1e-12:
            self._finish_smooth_zoom()
            return

        # Log-space easing gives zoom-in/out matching feel and settles a single
        # notch in a bounded handful of frames instead of dozens of tiny renders.
        from gui.zoom_navigation import eased_zoom_step
        try:
            step, next_remaining, _settled = eased_zoom_step(remaining)
        except (TypeError, ValueError):
            self._finish_smooth_zoom()
            return
        applied = False
        try:
            # Legacy VTK-wheel fallback follows the same mandatory cursor
            # contract as the primary Qt wheel route.
            applied = self._zoom_widget_at_cursor(
                self.vtk_widget, step, interactor=self.vtk_widget.interactor
            )
        except Exception:
            applied = False

        if not applied:
            # Could not apply (no renderer/camera) — abort gesture.
            self._finish_smooth_zoom()
            return

        self._zoom_target_factor = next_remaining
        self._update_zoom_level_display(step)

    def _zoom_widget_at_center(self, factor):
        """Plain center zoom (fallback / default behavior)."""
        vtk_widget = self.vtk_widget
        if vtk_widget is None or factor <= 0:
            return False
        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is None:
            return False
        camera = renderer.GetActiveCamera()
        if camera is None:
            return False
        camera.Zoom(factor)
        vtk_widget.render()
        return True


    def _on_magnifier_changed(self, text):
        """Handle magnifier zoom level changes."""
        try:
            val_str = text.strip().replace('%', '')
            if not val_str:
                return
                
            new_zoom = float(val_str)
            if new_zoom <= 0.01:
                return

            if not hasattr(self, "vtk_widget") or not self.vtk_widget:
                return
            
            if not hasattr(self, "_current_zoom_level"):
                self._current_zoom_level = 100.0

            ratio = new_zoom / self._current_zoom_level
            self._current_zoom_level = new_zoom

            cam = self.vtk_widget.renderer.GetActiveCamera()
            cam.Zoom(ratio)
            self.vtk_widget.render()

            self._schedule_main_view_history_commit("magnifier_zoom", delay_ms=120)
        except ValueError:
            pass # Ignore incomplete or invalid text inputs like 'a'
        except Exception as e:
            print(f"⚠️ Magnifier Error: {e}")

    def fit_view(self):
        """
        Fit main view to visible data.
        Priority:
        1. Point cloud bounds (if available)
        2. DXF/SNT/drawings bounds fallback (when no point cloud bounds)
        Camera-only operation. Maintains 2D lock if active.
        Safe to call anytime.
        """
        try:
            import numpy as np
            
            # Reset zoom level tracker when fitting view
            self._current_zoom_level = 100.0
            self._zoom_anchor_points = {}
            if hasattr(self, 'magnifier_combo'):
                self.magnifier_combo.blockSignals(True)
                self.magnifier_combo.setCurrentText("100%")
                self.magnifier_combo.blockSignals(False)
            
            print(f"\n{'='*60}")
            print(f"🧲 FIT VIEW (Point Cloud + DXF)")
            print(f"{'='*60}")
            
            # ============================================================
            # CHECK IF 2D LOCK IS ALREADY ACTIVE
            # ============================================================
            is_2d_locked = bool(getattr(self, '_main_view_2d_locked', False))
            print(f"Already 2D locked: {is_2d_locked}")
            
            if is_2d_locked:
                try:
                    short_filter = getattr(self, "short_cut_filter", None)
                    if short_filter and hasattr(short_filter, "_clear_main_camera_lock_observer"):
                        short_filter._clear_main_camera_lock_observer(self.vtk_widget.renderer.GetActiveCamera())
                        print("🧹 Cleared owned camera lock observer")
                except Exception as lock_clear_error:
                    print(f"⚠️ Could not clear main camera lock observer: {lock_clear_error}")
            
            # ============================================================
            # 1. Get point cloud bounds
            # ============================================================
            bounds_list = []
            point_cloud_bounds_added = False
            
            if hasattr(self, 'data') and self.data is not None:
                xyz = self.data.get('xyz')
                if xyz is not None and len(xyz) > 0:
                    # Get visible points only (based on class_palette)
                    if hasattr(self, 'class_palette') and self.class_palette:
                        visible_classes = [c for c, v in self.class_palette.items() if v.get('show', True)]
                        
                        if visible_classes and 'classification' in self.data:
                            classes = self.data['classification']
                            mask = np.isin(classes, visible_classes)
                            
                            if np.any(mask):
                                visible_xyz = xyz[mask]
                                bounds_list.append({
                                    'xmin': visible_xyz[:, 0].min(),
                                    'xmax': visible_xyz[:, 0].max(),
                                    'ymin': visible_xyz[:, 1].min(),
                                    'ymax': visible_xyz[:, 1].max(),
                                })
                                point_cloud_bounds_added = True
                                print(f"   ✅ Point cloud bounds added")
                    else:
                        # No palette - use all points
                        bounds_list.append({
                            'xmin': xyz[:, 0].min(),
                            'xmax': xyz[:, 0].max(),
                            'ymin': xyz[:, 1].min(),
                            'ymax': xyz[:, 1].max(),
                        })
                        point_cloud_bounds_added = True
                        print(f"   ✅ Point cloud bounds added (all points)")

            if point_cloud_bounds_added:
                print("   🎯 Point cloud bounds present — skipping DXF/SNT bounds for main fit")
            else:
                # ============================================================
                # 2. Get DXF grid bounds (fallback)
                # ============================================================
                if hasattr(self, 'dxf_actors') and self.dxf_actors:
                    print(f"   📐 Checking {len(self.dxf_actors)} DXF grids...")
                    
                    for i, dxf_data in enumerate(self.dxf_actors):
                        try:
                            # Check if DXF is visible
                            actors = dxf_data.get('actors', [])
                            if not actors:
                                continue
                            
                            # Check first actor's visibility
                            if not actors[0].GetVisibility():
                                print(f"      ⏭️ DXF {i}: Hidden - skipping")
                                continue
                            
                            # Get DXF bounds
                            dxf_bounds = dxf_data.get('bounds')
                            
                            if dxf_bounds:
                                bounds_list.append({
                                    'xmin': dxf_bounds[0],
                                    'xmax': dxf_bounds[1],
                                    'ymin': dxf_bounds[2],
                                    'ymax': dxf_bounds[3],
                                })
                                print(f"      ✅ DXF {i}: Bounds added")
                            else:
                                print(f"      ⚠️ DXF {i}: No bounds data")
                                
                        except Exception as e:
                            print(f"      ⚠️ DXF {i}: Error - {e}")

                # ============================================================
                # 3. Get SNT grid bounds (fallback)
                # ============================================================
                if hasattr(self, 'snt_actors') and self.snt_actors:
                    print(f"   🗂️ Checking {len(self.snt_actors)} SNT grids...")

                    for i, snt_data in enumerate(self.snt_actors):
                        try:
                            # Check if SNT is visible
                            actors = snt_data.get('actors', [])
                            if not actors:
                                continue

                            # Check first actor's visibility
                            if not actors[0].GetVisibility():
                                print(f"      ⏭️ SNT {i}: Hidden - skipping")
                                continue

                            # Get SNT bounds. Prefer camera-fit bounds because
                            # some SNT files contain isolated outlier geometry.
                            snt_bounds = snt_data.get('fit_bounds') or snt_data.get('bounds')
                            if snt_bounds:
                                bounds_list.append({
                                    'xmin': snt_bounds[0],
                                    'xmax': snt_bounds[1],
                                    'ymin': snt_bounds[2],
                                    'ymax': snt_bounds[3],
                                })
                                print(f"      ✅ SNT {i}: Bounds added")
                            else:
                                print(f"      ⚠️ SNT {i}: No bounds data")

                        except Exception as e:
                            print(f"      ⚠️ SNT {i}: Error - {e}")

                # ============================================================
                # 4. Get digitized drawing bounds (fallback)
                # ============================================================
                digitizer = getattr(self, 'digitizer', None)
                drawings = list(getattr(digitizer, 'drawings', []) or [])
                if drawings:
                    print(f"   ✏️ Checking {len(drawings)} drawing(s)...")

                    for i, drawing in enumerate(drawings):
                        try:
                            raw_coords = drawing.get('coords') or drawing.get('coordinates') or []
                            if hasattr(raw_coords, 'tolist'):
                                raw_coords = raw_coords.tolist()

                            coords_xy = []
                            for coord in raw_coords:
                                if hasattr(coord, 'tolist'):
                                    coord = coord.tolist()
                                if isinstance(coord, (list, tuple)) and len(coord) >= 2:
                                    coords_xy.append((float(coord[0]), float(coord[1])))

                            if not coords_xy:
                                continue

                            xs, ys = zip(*coords_xy)
                            bounds_list.append({
                                'xmin': min(xs),
                                'xmax': max(xs),
                                'ymin': min(ys),
                                'ymax': max(ys),
                            })
                        except Exception as e:
                            print(f"      ⚠️ Drawing {i}: Error - {e}")

            # ============================================================
            # 5. Calculate combined bounds
            # ============================================================
            if not bounds_list:
                print(f"   ⚠️ No visible data to fit")
                print(f"{'='*60}\n")
                return
            
            # Find overall min/max
            xmin = min(b['xmin'] for b in bounds_list)
            xmax = max(b['xmax'] for b in bounds_list)
            ymin = min(b['ymin'] for b in bounds_list)
            ymax = max(b['ymax'] for b in bounds_list)
            
            print(f"\n   📊 Combined bounds:")
            print(f"      X: {xmin:.2f} → {xmax:.2f} (width: {xmax-xmin:.2f})")
            print(f"      Y: {ymin:.2f} → {ymax:.2f} (height: {ymax-ymin:.2f})")
            
            # ============================================================
            # 6. Set camera to fit combined bounds
            # ============================================================
            if not hasattr(self, 'vtk_widget') or not self.vtk_widget:
                print(f"   ⚠️ No VTK widget")
                return
            
            renderer = self.vtk_widget.renderer
            camera = renderer.GetActiveCamera()
            
            # Calculate center and size
            center_x = (xmin + xmax) / 2.0
            center_y = (ymin + ymax) / 2.0
            width = xmax - xmin
            height = ymax - ymin

            # Add 10% margin
            margin = 1.1
            max_dim = max(width, height, 1.0) * margin
            center_z = 0.0
            if hasattr(self, 'data') and self.data is not None:
                xyz = self.data.get('xyz')
                if xyz is not None and len(xyz) > 0:
                    center_z = float(np.median(xyz[:, 2]))

            # Set camera for top view
            camera.SetPosition(center_x, center_y, center_z + 5000)
            camera.SetFocalPoint(center_x, center_y, center_z)
            camera.SetViewUp(0, 1, 0)
            camera.ParallelProjectionOn()
            camera.SetParallelScale(max_dim / 2.0)

            # Update clipping range
            renderer.ResetCameraClippingRange()
            
            # Render
            self.vtk_widget.render()
            
            print(f"   ✅ Camera fitted to combined bounds")
            
            # ============================================================
            # 7. REFRESH 2D LOCK IF ACTIVE
            # ============================================================
            if is_2d_locked:
                print(f"ℹ️ Already 2D locked — re-fitting bounds only (no view reset)")
                self._refresh_2d_lock()
                print(f"🔒 Main view 2D lock REFRESHED")
            
            print(f"{'='*60}\n")
            
            self._schedule_main_view_history_commit("fit_view", delay_ms=120)

            if hasattr(self, 'statusBar'):
                self.statusBar().showMessage("🧲 View fitted to all visible data", 2000)
            
        except Exception as e:
            print(f"⚠️ Fit view failed: {e}")
            import traceback
            traceback.print_exc()

    def fit_view_with_2d_lock(self, source: str = "fit"):
        """
        Canonical "fit + keep main view 2D" entrypoint.
        Used by ribbon Fit and can be reused by future UI triggers.
        """
        try:
            short_filter = getattr(self, "short_cut_filter", None)
            if short_filter and hasattr(short_filter, "_fit_main_view_with_2d_lock"):
                short_filter._fit_main_view_with_2d_lock()
                self._main_view_2d_locked = True

                if hasattr(self, "_schedule_main_view_history_commit"):
                    self._schedule_main_view_history_commit("fit_view_with_2d_lock", delay_ms=120)

                return True

        except Exception as e:
            print(f"⚠️ fit_view_with_2d_lock shortcut-path failed ({source}): {e}")

        try:
            self.fit_view()
            self.ensure_main_view_2d_interaction(
                preserve_camera=True,
                reason=f"{source}_fallback",
            )
            self._main_view_2d_locked = True
            return True
        except Exception as e:
            print(f"⚠️ fit_view_with_2d_lock fallback failed ({source}): {e}")
            return False

    def _refresh_2d_lock(self):
        """
        Recapture the current camera state as the 2D lock target.
        Call this after any camera adjustment that should maintain 2D lock.
        """
        try:
            if not hasattr(self, 'vtk_widget') or not self.vtk_widget:
                return
            
            renderer = self.vtk_widget.renderer
            camera = renderer.GetActiveCamera()

            if camera is None:
                return

            camera.ParallelProjectionOn()
            lock_params = {
                'position': camera.GetPosition(),
                'focal_point': camera.GetFocalPoint(),
                'view_up': camera.GetViewUp(),
                'parallel_scale': camera.GetParallelScale(),
                'view_angle': camera.GetViewAngle(),
            }
            self._main_view_locked_params = lock_params
            self._main_view_2d_locked = True
            print("📸 Captured new lock target")

            short_filter = getattr(self, "short_cut_filter", None)
            if not short_filter or not hasattr(short_filter, "_install_main_camera_lock_observer"):
                self.ensure_main_view_2d_interaction(
                    preserve_camera=True,
                    reason="refresh_2d_lock_fallback",
                )
                return

            short_filter._clear_main_camera_lock_observer(camera)

            import numpy as np
            import time

            _enforcing = [False]
            _last_enforce = [0.0]

            def enforce_camera_lock_main(obj, event):
                if _enforcing[0]:
                    return

                now = time.time()
                if now - _last_enforce[0] < 0.033:
                    return
                _last_enforce[0] = now
                _enforcing[0] = True
                try:
                    cam = renderer.GetActiveCamera()
                    cam.ParallelProjectionOn()

                    cur_pos = np.array(cam.GetPosition())
                    cur_focal = np.array(cam.GetFocalPoint())
                    cur_up = np.array(cam.GetViewUp())

                    lock_pos = np.array(lock_params['position'])
                    lock_focal = np.array(lock_params['focal_point'])
                    lock_up = np.array(lock_params['view_up'])

                    cur_dir = cur_focal - cur_pos
                    lock_dir = lock_focal - lock_pos
                    cur_norm = cur_dir / (np.linalg.norm(cur_dir) + 1e-10)
                    lock_norm = lock_dir / (np.linalg.norm(lock_dir) + 1e-10)

                    direction_dot = np.dot(cur_norm, lock_norm)
                    up_dot = np.dot(cur_up, lock_up)

                    if direction_dot < 0.9999 or up_dot < 0.9999:
                        distance = np.linalg.norm(cur_dir)
                        new_position = cur_focal - (lock_norm * distance)
                        cam.SetPosition(*new_position)
                        cam.SetViewUp(*lock_up)
                finally:
                    _enforcing[0] = False

            short_filter._install_main_camera_lock_observer(camera, enforce_camera_lock_main)
            print("🔒 Fresh camera lock observer installed")
            
        except Exception as e:
            print(f"⚠️ Failed to refresh 2D lock: {e}")
        
    def _save_quick_no_dialog(self):
        from .save_pointcloud import save_pointcloud, has_fenced_parent_writeback

        if has_fenced_parent_writeback(self):
            save_pointcloud(self, path=None, show_dialog=False)
            return

        # Save to last path if possible, otherwise fallback to Save As dialog
        path = getattr(self, "last_save_path", None)
        if not path:
            save_pointcloud(self, path=None, show_dialog=True)
            return

        save_pointcloud(
            self,
            path=path,
            file_format=getattr(self, "last_save_format", None),
            las_version=getattr(self, "last_save_version", None),
            show_dialog=False
        )


    def activate_grid_tool(self):
        """Activate grid creation tool"""
        if not hasattr(self, 'grid_tool'):
            from gui.grid_tool import GridTool
            self.grid_tool = GridTool(self)
        
        self.grid_tool.activate()
        
    def on_view_mode_toggled(self, new_mode):
        self.cross_view_mode = new_mode
        # Notify all interactors to reset their projection math
        for interactor in self.classify_interactors.values():
            if hasattr(interactor, '_invalidate_coord_cache'):
                interactor._invalidate_coord_cache()
        self.refresh_all_views() # Ensure the screen updates

    def cleanup_camera_sync_on_shutdown(self):
        """
        Cleanup camera sync resources on app close.
        Call this in your closeEvent() method.
        """
        print("🧹 Cleaning up camera sync...")
        
        # Stop all debounce timers
        if hasattr(self, '_camera_debounce_timers'):
            for timer in self._camera_debounce_timers.values():
                try:
                    timer.stop()
                except Exception:
                    pass
            self._camera_debounce_timers.clear()
        
        # Clear pending states
        if hasattr(self, '_pending_camera_states'):
            self._pending_camera_states.clear()
        
        # Set sync flag to prevent new syncs
        self._syncing_camera = True
        
        # Clear sync map
        if hasattr(self, 'view_sync_map'):
            self.view_sync_map.clear()
        
        # Clear camera states
        if hasattr(self, '_last_camera_states'):
            self._last_camera_states.clear()
        
        print("✅ Camera sync cleanup complete")       

    def on_curve_tool_selected(self, tool_name):
        """Handle curve tool selection from ribbon"""
        if tool_name == "curve_point":
            # Deactivate other tools first
            if hasattr(self, 'digitizer') and self.digitizer:
                self.digitizer.deactivate_all()
           
            # Resume an in-progress curve if we have one; otherwise start fresh.
            if hasattr(self, 'curvetool'):
                if getattr(self.curvetool, "points", None):
                    self.curvetool.resume()
                else:
                    self.curvetool.activate()
                print(f"🔮 Curve tool '{tool_name}' activated")
 
    def on_curve_button_clicked(self):
        """Activate the curve drawing tool"""
        # Deactivate other tools first (safe checks)
        for dialog_attr in ("_parallel_tool_dialog", "_centerline_tool_dialog"):
            dialog = getattr(self, dialog_attr, None)
            if dialog is not None:
                try:
                    dialog.close()
                except Exception:
                    pass

        if hasattr(self, 'measurement_tool'):
            try:
                self.measurement_tool.deactivate()
            except Exception:
                pass
       
        if hasattr(self, 'select_rectangle_tool'):
            try:
                self.select_rectangle_tool.deactivate()
            except Exception:
                pass
       
        if hasattr(self, 'zoom_rectangle_tool'):
            try:
                self.zoom_rectangle_tool.deactivate()
            except Exception:
                pass
       
        # ✅ Deactivate digitizer when starting curve tool
        if hasattr(self, 'digitizer') and self.digitizer:
            try:
                self.digitizer.deactivate_element_select_tool()
            except Exception:
                pass
            try:
                self.digitizer.set_tool(None)
            except Exception:
                pass
       
        # Activate curve tool
        self._draw_curve_context_active = True
        if getattr(self.curve_tool, "active", False):
            self.curve_tool.activate()
        else:
            if getattr(self.curve_tool, "points", None):
                self.curve_tool.resume()
            else:
                self.curve_tool.activate()
        print("🔮 Curve tool activated")

    # ═══════════════════════════════════════════════════════════════════
    # Phase 3: GPU Uniform Sync handler (palette_changed → sync_palette_to_gpu)
    # ═══════════════════════════════════════════════════════════════════
    #     """
    #     Instant GPU uniform poke — O(1) palette update.
    #     Connected to DisplayModeDialog.palette_changed signal.
    #     """

    def _on_palette_changed(self, slot_idx: int):
        """
        Fast GPU sync handler triggered by Display Mode palette_changed signal.
        Pushes updated color/visibility/weight LUTs to the GPU shader without
        rebuilding actors. Only fires when the Display Mode fast-path did NOT
        already handle the update (see display_mode.py bug-7 fix).
        """
        try:
            from gui.unified_actor_manager import sync_palette_to_gpu, is_unified_actor_ready

            if self.data is None:
                return

            # For the main view only sync in class-based modes — pushing class
            # palette uniforms in depth/rgb/intensity/elevation mode overwrites
            # the active color buffer and corrupts the display.
            if slot_idx == 0:
                current_mode = getattr(self, "display_mode", "class")
                CLASS_MODES = {"class", "shaded_class"}
                if current_mode not in CLASS_MODES:
                    print(f"⚡ _on_palette_changed slot 0 skipped (mode={current_mode})")
                    return

            if slot_idx == 0:
                border = float(getattr(self, "point_border_percent", 0.0))
                palette = getattr(self, "class_palette", None)
            else:
                border = float(getattr(self, "view_borders", {}).get(slot_idx, 0.0))
                palette = (getattr(self, "view_palettes", {}) or {}).get(slot_idx)

            if not palette:
                return

            if is_unified_actor_ready(self):
                sync_palette_to_gpu(
                    self,
                    slot_idx=slot_idx,
                    palette=palette,
                    border=border,
                    render=True,
                )

        except Exception as e:
            print(f"❌ Error in _on_palette_changed (Slot {slot_idx}): {e}")

    # Add this inside the NakshaApp class
    def _on_classification_finished(self, changed_mask):
        """
        FIXED: Never reads from _naksha_section_class mirror.
        Always derives colors from self.data["classification"] (ground truth).
        """
        try:
            from gui.unified_actor_manager import fast_cross_section_update, sync_palette_to_gpu, build_section_unified_actor

            if hasattr(self, 'section_vtks') and self.section_vtks:
                for view_idx in self.section_vtks.keys():

                    # [CS-MESH-DISPLAY] classification dispatcher
                    try:
                        from gui.cross_section.section_mesh_display import (
                            refresh_section_display_after_classification,
                        )
                        if refresh_section_display_after_classification(
                            self,
                            view_idx,
                            changed_mask,
                            operation=str(getattr(self, "_classification_refresh_operation", "classification") or "classification"),
                        ):
                            continue
                    except Exception as _cs_mesh_refresh_err:
                        print(
                            f"SECTION_MESH view={view_idx + 1} status=classification_refresh_failed "
                            f"reason={_cs_mesh_refresh_err}"
                        )
                    if (changed_mask is not None
                            and isinstance(changed_mask, np.ndarray)
                            and changed_mask.dtype == bool):
                        # *** CRITICAL FIX ***
                        # Before calling fast_cross_section_update,
                        # ensure the section mirror is in sync with
                        # self.data["classification"] for the WHOLE section,
                        # not just changed_mask. This prevents stale mirror
                        # data from being used as source of truth.
                        self._sync_section_mirror_from_data(view_idx)
                        slot_idx = view_idx + 1
                        palette = self._get_cross_section_palette(slot_idx)
                        ok = fast_cross_section_update(
                            self,
                            view_idx,
                            changed_mask,
                            palette=palette,
                            force_visibility_refresh=False,
                        )
                        if not ok:
                            border = float(getattr(self, "view_borders", {}).get(slot_idx, 0) or 0.0)
                            view_mode = getattr(self, "cross_view_mode", "front")
                            rebuilt = build_section_unified_actor(
                                self,
                                view_idx,
                                palette=palette,
                                border_percent=border,
                                view=view_mode,
                            )
                            if rebuilt is not None:
                                fast_cross_section_update(
                                    self,
                                    view_idx,
                                    changed_mask,
                                    palette=palette,
                                    force_visibility_refresh=False,
                                )
                    else:
                        sync_palette_to_gpu(self, slot_idx=view_idx + 1, render=True)

            # Refresh the main view immediately after the cross-section commit.
            # This is the actual repaint path the user expects after classification,
            # independent of any display-sync shortcut.
            try:
                from gui.unified_actor_manager import guarantee_main_view_visual_refresh

                if changed_mask is None:
                    changed_mask = getattr(self, "_last_changed_mask", None)

                main_refresh_ok = guarantee_main_view_visual_refresh(
                    self,
                    changed_mask=changed_mask,
                    to_class=getattr(self, "to_class", None),
                    reason="cross_section_classification_finished",
                )
                if not main_refresh_ok and str(getattr(self, "display_mode", "") or "").lower() == "class":
                    from gui.class_display import update_class_mode
                    update_class_mode(self, force_refresh=True)
                    if hasattr(self, "vtk_widget") and self.vtk_widget is not None:
                        self.vtk_widget.render()
                print("   ✅ Main View refreshed after cross-section classification")
            except Exception as _main_refresh_err:
                print(f"   ⚠️ Main View refresh failed after classification: {_main_refresh_err}")

            if str(getattr(self, "display_mode", "") or "").lower() == "surface":
                try:
                    if bool(getattr(self, "_surface_refresh_already_scheduled", False)):
                        self._surface_refresh_already_scheduled = False

                    elif bool(getattr(self, "_surface_skip_next_classification_finished_refresh", False)):
                        self._surface_skip_next_classification_finished_refresh = False

                    else:
                        self.refresh_surface_after_classification(
                            reason="classification finished",
                            changed_mask=changed_mask,
                        )
                except Exception as _surface_refresh_err:
                    print(f"Surface post-classification refresh failed: {_surface_refresh_err}")


            if hasattr(self, 'cut_section_controller'):
                ctrl = self.cut_section_controller
                if getattr(ctrl, 'is_cut_view_active', False):
                    b = float(getattr(self, 'view_borders', {}).get(5, 0))
                    sync_palette_to_gpu(self, slot_idx=5, border=b, render=True)

            from gui.point_count_widget import refresh_point_statistics
            refresh_point_statistics(self)

        except Exception as e:
            print(f"⚠️ Global Sync Error: {e}")
            import traceback
            traceback.print_exc()

        finally:
            # Keep header accounting off the render hot path: inspect only the
            # changed subset and reuse the edit's existing undo metadata.
            try:
                if self._apply_classified_point_count_delta(changed_mask):
                    self._refresh_window_title_classification_count()
            except Exception as title_error:
                print(f"Window title classification count refresh failed: {title_error}")
            if hasattr(self, "_last_classified_to_class"):
                delattr(self, "_last_classified_to_class")

    def refresh_surface_after_classification(self, reason="classification", changed_mask=None):
            """
            Central Surface refresh entry point after classification edits.
            Keeps the existing rebuild pipeline but ensures cache invalidation is
            tied to the current classification revision.
    
            IMPORTANT: delay_ms must NOT be hardcoded to 0 for live edits.
            surface_mode.refresh_surface_after_classification() only does a real
            Delaunay re-triangulation via its debounced QTimer path when
            delay_ms > 0 (or for undo/redo, which always rebuild regardless).
            Forcing delay_ms=0 here silently routes every live classification
            commit into the local plane-fit patch (_apply_surface_bridge_update),
            which never adds/removes triangles — so terrain-support class
            changes go stale until the next undo/redo or mode switch.
            """
            if str(getattr(self, "display_mode", "") or "").lower() != "surface":
                return False
    
            reason_lc = str(reason or "").lower()
    
            # Undo/redo always force a full rebuild inside surface_mode.py
            # regardless of delay_ms, so 0 is fine (and fastest) here.
            if reason_lc in ("undo", "redo"):
                delay_ms = 0
            else:
                # Live classification edits (brush, main-view commit, cross-section
                # commit, "classification finished") must debounce into a REAL
                # re-triangulation once the user pauses, instead of being silently
                # downgraded to a non-topology-changing patch forever.
                delay_ms = int(getattr(self, "surface_classification_rebuild_delay_ms", 300) or 300)
    
            try:
                from gui.surface_mode import refresh_surface_after_classification
                return refresh_surface_after_classification(
                    self,
                    changed_mask=changed_mask,
                    operation=reason,
                    delay_ms=delay_ms,
                )
            except Exception as e:
                print(f"⚠️ refresh_surface_after_classification failed: {e}")
            return False

    def ensure_main_view_2d_interaction(self, preserve_camera=True, reason=None):
        """
        Force the main viewer back to 2D pan/zoom without refitting or losing zoom.
        Keeps the current framing intact and removes accidental 3D orbit behavior.
        """
        try:
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage

            if not hasattr(self, 'vtk_widget') or not self.vtk_widget:
                return False

            interactor = getattr(self.vtk_widget, 'interactor', None)
            renderer = getattr(self.vtk_widget, 'renderer', None)
            if interactor is None or renderer is None:
                return False

            camera = renderer.GetActiveCamera()
            if camera is None:
                return False

            saved_camera = None
            if preserve_camera:
                saved_camera = {
                    'position': tuple(camera.GetPosition()),
                    'focal_point': tuple(camera.GetFocalPoint()),
                    'view_up': tuple(camera.GetViewUp()),
                    'parallel_scale': camera.GetParallelScale(),
                }

            style = interactor.GetInteractorStyle()
            style_name = style.GetClassName() if style is not None else "None"
            # ✅ BUG FIX: Always clear stuck interaction states (like panning) 
            if style is not None:
                try:
                    if hasattr(style, "OnMiddleButtonUp"):
                        style.OnMiddleButtonUp()
                    if hasattr(style, "OnLeftButtonUp"):
                        style.OnLeftButtonUp()
                    if hasattr(style, "OnRightButtonUp"):
                        style.OnRightButtonUp()
                except Exception:
                    pass
            if style_name != "vtkInteractorStyleImage":
                style_2d = vtkInteractorStyleImage()
                try:
                    style_2d.SetInteractionModeToImageSlicing()
                except Exception:
                    pass
                interactor.SetInteractorStyle(style_2d)

            camera.ParallelProjectionOn()
            if saved_camera:
                camera.SetPosition(saved_camera['position'])
                camera.SetFocalPoint(saved_camera['focal_point'])
                camera.SetViewUp(saved_camera['view_up'])
                camera.SetParallelScale(saved_camera['parallel_scale'])

            if renderer.VisibleActorCount() > 0:
                renderer.ResetCameraClippingRange()
            else:
                pos = np.array(camera.GetPosition())
                fp = np.array(camera.GetFocalPoint())
                dist = max(1.0, np.linalg.norm(pos - fp))
                camera.SetClippingRange(dist * 0.001, dist * 100.0)
            # ✅ FIX: After resetting clip planes, ensure all SNT/DXF overlay actors
            # are still present in the renderer. ResetCameraClippingRange() does not
            # evict actors, but any upstream classification pass or display-mode change
            # that ran before this call may have stripped the renderer. Calling
            # tool is activated (e.g. Shift+F for freehand).
            try:
                self._ensure_overlay_actors()
            except Exception:
                pass
            self.is_3d_mode = False
            self._main_view_2d_locked = True
            self.vtk_widget.render()

            suffix = f" ({reason})" if reason else ""
            print(f"🔒 Main view kept in 2D interaction mode{suffix}")
            return True

        except Exception as e:
            print(f"⚠️ Failed to enforce 2D main interaction: {e}")
            return False        


    def _sync_section_mirror_from_data(self, view_idx: int):
        """
        CRITICAL GUARD: Rebuild the _naksha_section_class mirror for a section view
        entirely from self.data["classification"] (ground truth).

        This prevents fast_cross_section_update from ever reading stale mirror
        data (e.g. left over from before an undo) and writing it back into
        self.data["classification"], which is the core of the regression bug.

        Called before every fast_cross_section_update.
        """
        if self.data is None or "classification" not in self.data:
            return

        vtk_widget = (getattr(self, 'section_vtks', {}) or {}).get(view_idx)
        if vtk_widget is None:
            return

        try:
            import numpy as np

            # --- find the actor ---
            actor = None
            for attr in ('_naksha_unified_actor', '_section_unified_actor'):
                actor = getattr(vtk_widget, attr, None)
                if actor is not None:
                    break

            if actor is None:
                return

            # --- find global indices for this section view ---
            section_global_indices = None
            for attr in (
                f'_section_{view_idx}_global_indices',
                f'section_{view_idx}_indices',
                f'section_{view_idx}_core_indices',
            ):
                section_global_indices = getattr(self, attr, None)
                if section_global_indices is not None:
                    break

            if section_global_indices is None or len(section_global_indices) == 0:
                # No index map → mark mirror invalid so downstream rebuilds fully
                if hasattr(actor, '_naksha_section_class'):
                    actor._naksha_section_class = None
                return

            # --- rebuild mirror from ground truth ---
            fresh_classes = self.data["classification"][section_global_indices]
            actor._naksha_section_class = fresh_classes.copy()

        except Exception as e:
            print(f"⚠️ _sync_section_mirror_from_data view={view_idx}: {e}")

    def _setup_interactor_swapper(
        self,
        interactor,
        *,
        preserve_physical_middle_pan=False,
    ):
        if not hasattr(self, "_vtk_event_swappers"):
            self._vtk_event_swappers = []
            
        class VTKEventSwapper:
            def __init__(self, interactor, app, preserve_middle_pan):
                self.interactor = interactor
                self.app = app
                self._in_swap = False
                self.preserve_middle_pan = bool(preserve_middle_pan)
                
                self.obs_ids = [
                    interactor.AddObserver("LeftButtonPressEvent", self.on_left_press, 10.0),
                    interactor.AddObserver("LeftButtonReleaseEvent", self.on_left_release, 10.0),
                    interactor.AddObserver("MiddleButtonPressEvent", self.on_middle_press, 10.0),
                    interactor.AddObserver("MiddleButtonReleaseEvent", self.on_middle_release, 10.0),
                ]
                
            def _is_tool_active(self):
                if getattr(self.app, "active_classify_tool", None) is not None:
                    return True
                if getattr(self.app, "cross_section_active", False):
                    return True
                if hasattr(self.app, "digitizer") and self.app.digitizer is not None:
                    if getattr(self.app.digitizer, "active_tool", None) is not None:
                        return True
                    element_select = getattr(
                        self.app.digitizer, "_element_select_tool", None
                    )
                    if element_select is not None and getattr(
                        element_select, "_active", False
                    ):
                        return True
                if hasattr(self.app, "measurement_tool") and self.app.measurement_tool is not None:
                    if getattr(self.app.measurement_tool, "is_measuring", False):
                        return True
                for tool_name in (
                    "identification_tool",
                    "point_sync_tool",
                    "snt_layer_pick_tool",
                ):
                    tool = getattr(self.app, tool_name, None)
                    if tool is not None and getattr(tool, "active", False):
                        return True
                # ✅ FIX: A pending cut-section placement (center/depth pick)
                # must never be swallowed by the left-click-pan swap, whether
                # it originated from the cut dock ('cut') or a cross-section
                # view ('cross'). Without this, left-click panning steals the
                # placement clicks before the cut tool's own observer sees them.
                cut_controller = getattr(self.app, "cut_section_controller", None)
                if cut_controller is not None:
                    from .cross_section.cut_section_controller import CutSectionState
                    if getattr(cut_controller, "_state", CutSectionState.IDLE) in (
                        CutSectionState.WAITING_CENTER,
                        CutSectionState.WAITING_DEPTH,
                    ):
                        return True
                return False

            def _safe_abort(self, obj):
                try:
                    if hasattr(obj, "AbortFlagOn"):
                        obj.AbortFlagOn()
                    elif hasattr(obj, "SetAbortFlag"):
                        obj.SetAbortFlag(1)
                except Exception:
                    pass

            def on_left_press(self, obj, event):
                if self._in_swap:
                    return
                if getattr(self.app, "_left_pan_shortcut_active", False) and not self._is_tool_active():
                    self._in_swap = True
                    try:
                        self._safe_abort(obj)
                        obj.InvokeEvent("MiddleButtonPressEvent")
                    finally:
                        self._in_swap = False

            def on_left_release(self, obj, event):
                if self._in_swap:
                    return
                if getattr(self.app, "_left_pan_shortcut_active", False) and not self._is_tool_active():
                    self._in_swap = True
                    try:
                        self._safe_abort(obj)
                        obj.InvokeEvent("MiddleButtonReleaseEvent")
                    finally:
                        self._in_swap = False

            def on_middle_press(self, obj, event):
                if self._in_swap:
                    return
                # A physical middle drag is an unconditional navigation
                # gesture in every cross/cut-section viewport.  Never convert
                # it into a left press merely because left-pan is configured.
                if self.preserve_middle_pan:
                    return
                if getattr(self.app, "panning_button", "scroll") == "left" and not self._is_tool_active():
                    self._in_swap = True
                    try:
                        self._safe_abort(obj)
                        obj.InvokeEvent("LeftButtonPressEvent")
                    finally:
                        self._in_swap = False

            def on_middle_release(self, obj, event):
                if self._in_swap:
                    return
                if self.preserve_middle_pan:
                    return
                if getattr(self.app, "panning_button", "scroll") == "left" and not self._is_tool_active():
                    self._in_swap = True
                    try:
                        self._safe_abort(obj)
                        obj.InvokeEvent("LeftButtonReleaseEvent")
                    finally:
                        self._in_swap = False
                        
        swapper = VTKEventSwapper(
            interactor,
            self,
            preserve_physical_middle_pan,
        )
        self._vtk_event_swappers.append(swapper)
