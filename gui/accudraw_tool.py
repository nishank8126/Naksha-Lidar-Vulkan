# gui/accudraw_tool.py
# Naksha AccuDraw — separate MicroStation-style XYZ + Angle precision polyline tool.
#
# Behavior:
#   - Separate tool only: active_tool = "accudraw"
#   - First left-click starts polyline
#   - Mouse move shows cyan preview/highlight
#   - If angle lock is ON, preview is constrained to that angle
#   - If distance lock is ON, preview is constrained to that distance
#   - Next left-click fixes the preview point, like Polyline
#   - Right-click / Finish completes the polyline
#
# Does NOT modify SmartLine / Line / Hatch / Move Vertex.

import math
import time
import numpy as np

try:
    from PySide6.QtCore import Qt, QObject
    from PySide6.QtGui import QDoubleValidator
    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
        QLineEdit, QPushButton, QFrame
    )
except Exception:
    from PyQt5.QtCore import Qt, QObject
    from PyQt5.QtGui import QDoubleValidator
    from PyQt5.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
        QLineEdit, QPushButton, QFrame
    )


class AccuDrawDialog(QDialog):
    """Small floating XYZ / Angle / Distance input palette."""

    def __init__(self, tool, parent=None):
        super().__init__(parent, Qt.Tool | Qt.WindowStaysOnTopHint)
        self.tool = tool

        self.setWindowTitle("AccuDraw")
        self.setModal(False)
        self.setFixedWidth(305)

        self._build_ui()

    def keyPressEvent(self, event):
        """
        ESC must deactivate AccuDraw entirely (tool + palette).
        QDialog's default behaviour only hides this window via reject(),
        leaving the tool armed with its priority-100 observers.
        """
        if event.key() == Qt.Key_Escape:
            tool = getattr(self, "tool", None)
            if tool is not None and getattr(tool, "active", False):
                try:
                    if hasattr(tool, "finish_for_tool_switch"):
                        tool.finish_for_tool_switch("ESC")
                    else:
                        tool.deactivate(cancel=True)
                except Exception as e:
                    print(f"⚠️ AccuDraw ESC deactivate failed: {e}")
                # deactivate() hides this palette; do NOT call reject().
                return
        super().keyPressEvent(event)

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(7)

        title = QLabel("AccuDraw")
        title.setStyleSheet("font-weight: bold;")
        outer.addWidget(title)

        validator = QDoubleValidator(self)
        try:
            validator.setNotation(QDoubleValidator.StandardNotation)
        except Exception:
            pass

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(5)

        self.x_edit = QLineEdit("0.000000")
        self.y_edit = QLineEdit("0.000000")
        self.z_edit = QLineEdit("0.000000")

        for edit in (self.x_edit, self.y_edit, self.z_edit):
            edit.setValidator(validator)
            edit.setMinimumWidth(160)

            # Enter only adds the typed XYZ.
            # Do NOT connect textChanged to preview/render.
            edit.returnPressed.connect(self.tool.add_point_from_dialog)

        grid.addWidget(QLabel("X"), 0, 0)
        grid.addWidget(self.x_edit, 0, 1)
        grid.addWidget(QLabel("Y"), 1, 0)
        grid.addWidget(self.y_edit, 1, 1)
        grid.addWidget(QLabel("Z"), 2, 0)
        grid.addWidget(self.z_edit, 2, 1)

        outer.addLayout(grid)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.HLine)
        sep1.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep1)

        angle_grid = QGridLayout()
        angle_grid.setHorizontalSpacing(6)
        angle_grid.setVerticalSpacing(5)

        self.angle_edit = QLineEdit("30.0")
        self.angle_edit.setValidator(validator)
        self.angle_edit.setMinimumWidth(95)

        self.angle_tolerance_edit = QLineEdit("2.0")
        self.angle_tolerance_edit.setValidator(validator)
        self.angle_tolerance_edit.setMinimumWidth(95)

        angle_grid.addWidget(QLabel("Target Angle"), 0, 0)
        angle_grid.addWidget(self.angle_edit, 0, 1)
        angle_grid.addWidget(QLabel("°"), 0, 2)

        angle_grid.addWidget(QLabel("Tolerance"), 1, 0)
        angle_grid.addWidget(self.angle_tolerance_edit, 1, 1)
        angle_grid.addWidget(QLabel("°"), 1, 2)

        outer.addLayout(angle_grid)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setFrameShadow(QFrame.Sunken)
        outer.addWidget(sep2)

        btn_row = QHBoxLayout()

        self.add_btn = QPushButton("Add Point")
        self.undo_btn = QPushButton("Undo")
        self.finish_btn = QPushButton("Finish")

        self.add_btn.clicked.connect(self.tool.add_point_from_dialog)
        self.undo_btn.clicked.connect(self.tool.undo_last_point)
        self.finish_btn.clicked.connect(self.tool.finish)

        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.undo_btn)
        btn_row.addWidget(self.finish_btn)
        outer.addLayout(btn_row)

        self.status_lbl = QLabel(
            "Enter target angle. When mouse direction matches, preview highlights. Left-click fixes point."
        )
        self.status_lbl.setWordWrap(True)
        self.status_lbl.setStyleSheet("font-size: 10px; color: #777;")
        outer.addWidget(self.status_lbl)

    def set_xyz(self, point):
        try:
            x, y, z = point
            for edit, value in (
                (self.x_edit, x),
                (self.y_edit, y),
                (self.z_edit, z),
            ):
                edit.blockSignals(True)
                edit.setText(f"{float(value):.6f}")
                edit.blockSignals(False)
        except Exception:
            pass

    def get_xyz(self):
        return (
            float(self.x_edit.text().strip()),
            float(self.y_edit.text().strip()),
            float(self.z_edit.text().strip()),
        )

    def target_angle_degrees(self):
        try:
            return float(self.angle_edit.text().strip())
        except Exception:
            return 0.0

    def angle_tolerance_degrees(self):
        try:
            return max(0.1, float(self.angle_tolerance_edit.text().strip()))
        except Exception:
            return 2.0

    def set_status(self, text):
        try:
            self.status_lbl.setText(str(text))
        except Exception:
            pass


class AccuDrawTool(QObject):
    """
    Separate AccuDraw polyline tool.

    It owns its own VTK observers and creates normal polyline drawings:
        type   = "polyline"
        source = "accudraw"
    """

    def __init__(self, digitizer):
        super().__init__(digitizer.app if hasattr(digitizer, "app") else None)

        self.digitizer = digitizer
        self.app = getattr(digitizer, "app", None)
        self.interactor = getattr(digitizer, "interactor", None)
        self.renderer = getattr(digitizer, "renderer", None)

        self.active = False
        self.points = []
        self.drawing = None
        self.preview_actor = None
        self.observer_ids = []
        self.dialog = None

        self._undo_saved = False
        self._last_preview_time = 0.0
        self._last_preview_target = None
        self._angle_highlight_active = False

        # AccuDraw-only temporary auto snap state.
        # Restored when AccuDraw is deactivated so other tools are not affected.
        self._snap_state_saved = None

    # ------------------------------------------------------------------
    # Activation
    # ------------------------------------------------------------------
    def activate(self):
        if self.active:
            self._enable_accudraw_auto_snap()
            self._show_dialog()
            self._status("AccuDraw already active")
            return True

        self.active = True
        self.points = []
        self.drawing = None
        self.preview_actor = None
        self._undo_saved = False
        self._last_preview_time = 0.0
        self._last_preview_target = None

        try:
            self.digitizer.active_tool = "accudraw"
        except Exception:
            pass

        self._enable_accudraw_auto_snap()
        self._install_observers()
        self._show_dialog()

        self._status("AccuDraw: left-click start point, move mouse for preview, left-click fixes next point.")
        print("📐 AccuDraw activated")
        return True

    def deactivate(self, cancel=False):
        if cancel:
            self.clear_current(remove_drawing=True)

        self._remove_observers()
        self._remove_preview()
        self._restore_accudraw_snap_state()

        self.active = False

        try:
            if getattr(self.digitizer, "active_tool", None) == "accudraw":
                self.digitizer.active_tool = None
        except Exception:
            pass

        try:
            if self.dialog is not None:
                self.dialog.hide()
        except Exception:
            pass

        self._render()
        print("📐 AccuDraw deactivated")

    def finish(self):
        """
        Finish the current AccuDraw geometry but keep AccuDraw active.

        Behaviour:
        - 2 or more points: commit the line as a normal drawing.
        - 0 or 1 point: remove unfinished temporary AccuDraw state.
        - AccuDraw remains active so the user can immediately draw again.
        - AccuDraw deactivates only when another tool/ribbon is selected.
        """
        self._remove_preview()

        if len(self.points) < 2:
            self.clear_current(remove_drawing=True)
            self._status("AccuDraw: nothing to finish", 1800)
            return

        self._rebuild_or_create_drawing()

        finished_drawing = self.drawing
        if finished_drawing is not None:
            finished_drawing["type"] = "polyline"
            finished_drawing["source"] = "accudraw"
            finished_drawing["coords"] = list(self.points)
            finished_drawing["finalized"] = True
            finished_drawing["committed"] = True
            finished_drawing["layer"] = finished_drawing.get("layer", "DIGITIZER")
            if hasattr(self.digitizer, "_emit_drawing_finalized"):
                self.digitizer._emit_drawing_finalized(finished_drawing)
        finished_count = len(self.points)

        # Detach committed drawing from active AccuDraw draft.
        # The committed drawing stays in digitizer.drawings.
        # New clicks start a new AccuDraw line.
        self.points = []
        self.drawing = None
        self._undo_saved = False
        self._last_preview_target = None
        self._angle_highlight_active = False

        try:
            if hasattr(self.digitizer, "clear_coordinate_labels"):
                self.digitizer.clear_coordinate_labels()
        except Exception:
            pass

        # AccuDraw finish must remove temporary vertex/snap markers.
        # The committed polyline should remain, but the last green ballpoint
        # from snap/vertex preview must disappear immediately after right-click finish.
        try:
            if hasattr(self.digitizer, "_hide_snap_marker_now"):
                self.digitizer._hide_snap_marker_now()
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "clear_vertex_markers"):
                self.digitizer.clear_vertex_markers()
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "_clear_vertex_markers"):
                self.digitizer._clear_vertex_markers()
        except Exception:
            pass

        try:
            if self.dialog is not None:
                self.dialog.set_status(
                    "Finished. AccuDraw is still active. Click to start next line, or select another tool to exit."
                )
        except Exception:
            pass

        self._render()
        self._status("AccuDraw finished - ready for next line")
        print(f"✅ AccuDraw finalized and kept active: {finished_count} vertices")

    def finish_for_tool_switch(self, next_tool=""):
        """
        Cancel only the current unfinished AccuDraw draft when switching tools.

        Important:
        - If user already pressed Finish/right-click, finish() detached the committed
          drawing and reset self.points/self.drawing, so it will stay.
        - If user did not press Finish/right-click, any current AccuDraw line is
          unfinished and must be removed before the next tool starts.
        """
        try:
            had_unfinished = bool(
                self.points or self.drawing is not None or self.preview_actor is not None
            )

            self.clear_current(remove_drawing=True)

            try:
                if hasattr(self.digitizer, "clear_coordinate_labels"):
                    self.digitizer.clear_coordinate_labels()
            except Exception:
                pass

            try:
                if hasattr(self.digitizer, "_hide_snap_marker_now"):
                    self.digitizer._hide_snap_marker_now()
            except Exception:
                pass

            self.deactivate(cancel=False)

            if had_unfinished:
                print(f"📐 AccuDraw unfinished draft cancelled before {next_tool}")
            else:
                print(f"📐 AccuDraw deactivated before {next_tool}")

        except Exception as e:
            print(f"⚠️ AccuDraw tool-switch cleanup failed before {next_tool}: {e}")
            try:
                self.deactivate(cancel=True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _show_dialog(self):
        if self.dialog is None:
            self.dialog = AccuDrawDialog(self, parent=self.app)

        self.dialog.show()
        self.dialog.raise_()
        try:
            self.dialog.activateWindow()
        except Exception:
            pass

    def _status(self, message, timeout=3000):
        try:
            self.app.statusBar().showMessage(str(message), timeout)
        except Exception:
            print(message)

    def _enable_accudraw_auto_snap(self):
        """
        Temporarily enable snap for AccuDraw only.
        Previous snap settings are restored when AccuDraw deactivates.
        """
        digitizer = self.digitizer
        if digitizer is None:
            return

        try:
            if self._snap_state_saved is None:
                self._snap_state_saved = (
                    bool(getattr(digitizer, "snap_enabled", False)),
                    getattr(digitizer, "snap_mode", None),
                )

            if not getattr(digitizer, "snap_enabled", False):
                digitizer.snap_enabled = True
                digitizer.snap_mode = getattr(digitizer, "snap_mode", None) or "nearby"
                print(f"📐 AccuDraw temporary auto snap enabled: {digitizer.snap_mode}")
            elif not getattr(digitizer, "snap_mode", None):
                digitizer.snap_mode = "nearby"
                print("📐 AccuDraw snap mode defaulted to nearby")

        except Exception as e:
            print(f"⚠️ AccuDraw auto snap enable failed: {e}")

    def _restore_accudraw_snap_state(self):
        """Restore snap state changed only for AccuDraw."""
        digitizer = self.digitizer
        if digitizer is None or self._snap_state_saved is None:
            return

        try:
            old_enabled, old_mode = self._snap_state_saved
            digitizer.snap_enabled = bool(old_enabled)
            digitizer.snap_mode = old_mode

            if not old_enabled and hasattr(digitizer, "_hide_snap_marker_now"):
                digitizer._hide_snap_marker_now()

            print(f"📐 AccuDraw snap restored: enabled={old_enabled}, mode={old_mode}")

        except Exception as e:
            print(f"⚠️ AccuDraw snap restore failed: {e}")
        finally:
            self._snap_state_saved = None

    # ------------------------------------------------------------------
    # VTK observers
    # ------------------------------------------------------------------
    def _install_observers(self):
        self._remove_observers()

        if self.interactor is None:
            return

        self.observer_ids = [
            self.interactor.AddObserver("LeftButtonPressEvent", self._on_left_press, 100.0),
            self.interactor.AddObserver("MouseMoveEvent", self._on_mouse_move, 100.0),
            self.interactor.AddObserver("RightButtonPressEvent", self._on_right_press, 100.0),
        ]

    def _remove_observers(self):
        if self.interactor is None:
            self.observer_ids = []
            return

        for oid in list(self.observer_ids):
            try:
                self.interactor.RemoveObserver(oid)
            except Exception:
                pass

        self.observer_ids = []

    def _consume(self, obj, mark_right_click=False):
        """
        Strongly consume AccuDraw mouse events.

        This prevents AccuDraw right-click finish from also opening
        Surface / Shading / Display settings from global handlers.
        """
        if mark_right_click:
            try:
                until_time = time.monotonic() + 0.35

                try:
                    setattr(self.digitizer, "_accudraw_right_click_consumed_until", until_time)
                except Exception:
                    pass

                try:
                    if self.app is not None:
                        setattr(self.app, "_accudraw_right_click_consumed_until", until_time)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            if hasattr(self.digitizer, "_consume_vtk_event"):
                self.digitizer._consume_vtk_event(obj)
        except Exception:
            pass

        try:
            if obj is not None and hasattr(obj, "AbortFlagOn"):
                obj.AbortFlagOn()
        except Exception:
            pass

        try:
            if obj is not None and hasattr(obj, "SetAbortFlag"):
                obj.SetAbortFlag(1)
        except Exception:
            pass

    def _on_left_press(self, obj, evt):
        if not self.active:
            return

        self._consume(obj, mark_right_click=False)

        try:
            raw, snapped = self._current_mouse_world_with_snap_state()

            if not self.points:
                final_point = self._clean_point(raw)
            elif snapped:
                # IMPORTANT:
                # If AccuSnap found an endpoint/nearby point, commit that exact point.
                # Do not apply AccuDraw angle constraint on top of a snap result.
                final_point = self._clean_point(raw)
                self._angle_highlight_active = False
            else:
                final_point = self._constrained_point_from_mouse(raw)

            if self.dialog is not None:
                self.dialog.set_xyz(final_point)

            self.add_point(final_point, from_canvas=True)

        except Exception as e:
            print(f"⚠️ AccuDraw left-click failed: {e}")

    def _on_mouse_move(self, obj, evt):
        if not self.active:
            return

        # No first point yet: do not live-update text boxes/render.
        # First left-click sets the start point.
        if not self.points:
            return

        now = time.monotonic()

        # Throttle preview render to avoid UI hang.
        if now - self._last_preview_time < 0.035:
            return

        self._last_preview_time = now

        try:
            raw, snapped = self._current_mouse_world_with_snap_state()

            if snapped:
                # Preview must also go exactly to the snap point.
                # This prevents visual snap and committed point from differing after zoom.
                target = self._clean_point(raw)
                self._angle_highlight_active = False
            else:
                target = self._constrained_point_from_mouse(raw)

            if self.dialog is not None:
                self.dialog.set_xyz(target)

            self._update_preview(target)

        except Exception as e:
            print(f"⚠️ AccuDraw preview failed: {e}")

    def _on_right_press(self, obj, evt):
        if not self.active:
            return

        self._consume(obj, mark_right_click=True)
        self.finish()

    # ------------------------------------------------------------------
    # Point logic
    # ------------------------------------------------------------------
    def _current_mouse_world(self):
        """
        Backward-compatible helper.
        Returns only the point, same as before.
        """
        point, _snapped = self._current_mouse_world_with_snap_state()
        return point


    def _current_mouse_world_with_snap_state(self):
        """
        AccuDraw-only snap resolver.

        AccuDraw must auto-snap even if Nearby Snap is OFF globally.
        This only forces snap while AccuDraw is active and restores the old
        snap state when AccuDraw deactivates.
        """
        try:
            self._enable_accudraw_auto_snap()
        except Exception:
            pass

        try:
            raw = self.digitizer._get_mouse_world_no_snap()
        except Exception:
            try:
                raw = self.digitizer._get_mouse_world()
            except Exception:
                raw = (0.0, 0.0, 0.0)

        raw_np = np.asarray(raw, dtype=np.float64)
        snapped_np = raw_np.copy()
        did_snap = False

        try:
            if getattr(self.digitizer, "snap_enabled", False):
                snapped_np = np.asarray(
                    self.digitizer._snap_point(raw_np.copy()),
                    dtype=np.float64,
                )

                # Best signal: snap marker visibility OR actual snapped coordinate change.
                # This prevents AccuDraw angle constraint from moving the point away
                # from the exact snap target.
                marker = getattr(self.digitizer, "_snap_marker", None)

                marker_visible = False
                try:
                    marker_visible = bool(marker is not None and marker.GetVisibility())
                except Exception:
                    marker_visible = False

                try:
                    snap_delta = float(np.linalg.norm(snapped_np[:2] - raw_np[:2]))
                except Exception:
                    snap_delta = 0.0

                did_snap = marker_visible or snap_delta > 1e-7
            else:
                try:
                    if hasattr(self.digitizer, "_hide_snap_marker_now"):
                        self.digitizer._hide_snap_marker_now()
                except Exception:
                    pass

        except Exception:
            snapped_np = raw_np.copy()
            did_snap = False

        # Match normal Polyline/Line 2D behavior: after first AccuDraw point,
        # keep drawing on the same Z plane in Plan View.
        try:
            if (
                did_snap
                and self.points
                and not getattr(self.app, "is_3d_mode", False)
            ):
                snapped_np[2] = float(self.points[0][2])
        except Exception:
            pass

        return self._clean_point(snapped_np), did_snap

    def add_point_from_dialog(self):
        """
        Manual XYZ add.
        This is optional. Main workflow is canvas left-click.
        """
        if not self.active:
            return

        try:
            pt = self._dialog_xyz()
        except Exception:
            return

        self.add_point(pt, from_canvas=False)

    def add_point(self, point, from_canvas=False):
        if not self.active:
            return False

        pt = self._clean_point(point)

        if not self.points:
            self.points.append(pt)
            self._remove_preview()

            self._status(
                f"AccuDraw start point set: X={pt[0]:.3f}, Y={pt[1]:.3f}, Z={pt[2]:.3f}"
            )

            if self.dialog is not None:
                self.dialog.set_status("Start point fixed. Move mouse for highlighted preview, left-click fixes next point.")

            return True

        if self._same_point(self.points[-1], pt):
            self._status("AccuDraw: duplicate point ignored", 1800)
            return False

        # Save undo before every new fixed vertex/edge.
        # Ctrl+Z should go back to previous AccuDraw edge, not remove all.
        try:
            self.digitizer._save_state()
        except Exception:
            pass
        self._undo_saved = True

        self.points.append(pt)
        self._rebuild_or_create_drawing()
        self._show_accudraw_vertices()
        self._remove_preview()

        self._status(f"AccuDraw point fixed. Total points: {len(self.points)}")

        if self.dialog is not None:
            self.dialog.set_status("Point fixed. Move mouse for next preview, left-click to continue, right-click to finish.")

        return True

    def undo_last_point(self):
        if not self.points:
            self._status("AccuDraw: no point to undo", 1800)
            return

        self.points.pop()
        self._remove_preview()

        if len(self.points) < 2:
            if self.drawing is not None:
                try:
                    self.digitizer._remove_drawing(self.drawing)
                except Exception:
                    self._remove_actor_safe(self.drawing.get("actor"))
                self.drawing = None
        else:
            self._rebuild_or_create_drawing()

        self._render()
        self._status(f"AccuDraw undo. Points: {len(self.points)}")

    def handle_global_undo(self):
        """
        Handle Ctrl+Z while AccuDraw is active.

        Important:
        Normal Digitizer undo restores drawing snapshots, but AccuDraw also keeps
        live draft points in self.points. If we let normal undo run during an
        unfinished AccuDraw line, the restored drawing and self.points go out of sync.

        So while AccuDraw has live points:
        - undo only the last AccuDraw point
        - remove the matching temporary undo snapshot
        - clear preview/markers
        - keep AccuDraw active
        """
        if not self.active:
            return False

        if not self.points:
            return False

        before_count = len(self.points)

        # AccuDraw saves one normal undo snapshot before each second-and-later point.
        # Remove that snapshot so stale restored drawings do not come back later.
        try:
            if before_count >= 2 and getattr(self.digitizer, "undo_stack", None):
                self.digitizer.undo_stack.pop()
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "redo_stack"):
                self.digitizer.redo_stack.clear()
        except Exception:
            pass

        self.undo_last_point()

        try:
            if hasattr(self.digitizer, "clear_coordinate_labels"):
                self.digitizer.clear_coordinate_labels()
        except Exception:
            pass

        try:
            if self.points and hasattr(self.digitizer, "show_vertex_coordinates"):
                self.digitizer.show_vertex_coordinates(list(self.points))
        except Exception:
            pass

        try:
            if not self.points and self.dialog is not None:
                self.dialog.set_status("AccuDraw cleared. Left-click to start again.")
        except Exception:
            pass

        self._render()
        print(f"↶ AccuDraw point undo handled internally (points: {len(self.points)})")
        return True

    def clear_current(self, remove_drawing=True):
        self.points = []
        self._remove_preview()

        if remove_drawing and self.drawing is not None:
            try:
                self.digitizer._remove_drawing(self.drawing)
            except Exception:
                self._remove_actor_safe(self.drawing.get("actor"))
            self.drawing = None

        self._render()

    # ------------------------------------------------------------------
    # Angle / distance constraint
    # ------------------------------------------------------------------
    def _constrained_point_from_mouse(self, raw_point):
        """
        Auto angle-match behavior.

        User enters target angle, example 30°.
        Mouse is free.
        When mouse direction from last fixed point becomes close to 30°,
        preview becomes highlighted and the point snaps exactly to 30°.
        Left-click then fixes that highlighted point.
        """
        raw = np.asarray(self._clean_point(raw_point), dtype=np.float64)

        was_locked = self._angle_highlight_active
        self._angle_highlight_active = False

        if not self.points:
            return tuple(raw.tolist())

        base = np.asarray(self.points[-1], dtype=np.float64)
        out = raw.copy()
        out[2] = base[2]

        vec = raw[:2] - base[:2]
        raw_len = float(np.linalg.norm(vec))

        if raw_len <= 1e-12:
            return tuple(base.tolist())

        target_angle = 0.0
        tolerance = 2.0

        if self.dialog is not None:
            target_angle = self.dialog.target_angle_degrees()
            tolerance = self.dialog.angle_tolerance_degrees()

        # Actual cursor angle from last fixed point.
        actual_angle = math.degrees(math.atan2(float(vec[1]), float(vec[0]))) % 360.0
        # First segment: use user angle as absolute angle.
        # Next segments: use user angle relative to previous edge direction.
        if len(self.points) >= 2:
            prev_pt = np.asarray(self.points[-2], dtype=np.float64)
            last_pt = np.asarray(self.points[-1], dtype=np.float64)

            prev_vec = last_pt[:2] - prev_pt[:2]
            prev_len = float(np.linalg.norm(prev_vec))

            if prev_len > 1e-12:
                previous_edge_angle = math.degrees(
                    math.atan2(float(prev_vec[1]), float(prev_vec[0]))
                ) % 360.0
                target_angle = (previous_edge_angle + float(target_angle)) % 360.0
            else:
                target_angle = float(target_angle) % 360.0
        else:
            target_angle = float(target_angle) % 360.0

        # Smallest angle difference.
        diff = abs((actual_angle - target_angle + 180.0) % 360.0 - 180.0)

        # Sticky snap: entering the lock still requires the cursor within
        # the configured tolerance, but once locked, small hand jitter
        # while dragging out along that exact angle (to set the segment's
        # length) must not immediately kick it back out to the raw cursor
        # angle - that made it feel like the line wouldn't actually hold
        # the target angle. Staying locked instead uses a wider exit
        # tolerance, so the line only lets go once you deliberately steer
        # the cursor well away from the target angle. The user can still
        # move the cursor freely the whole time - only the angle is held,
        # never the distance.
        effective_tolerance = tolerance * 3.5 if was_locked else tolerance

        # If mouse comes near the user-given angle, highlight and snap.
        if diff <= effective_tolerance:
            theta = math.radians(target_angle)
            direction = np.asarray([math.cos(theta), math.sin(theta)], dtype=np.float64)

            # Keep same cursor distance, but align it exactly to target angle.
            out[0] = base[0] + direction[0] * raw_len
            out[1] = base[1] + direction[1] * raw_len

            self._angle_highlight_active = True

        return tuple(out.tolist())

    # ------------------------------------------------------------------
    # Drawing / preview
    # ------------------------------------------------------------------
    def _style(self):
        try:
            style = self.digitizer._get_draw_style("accudraw")
            return (
                tuple(style.get("color", (0.0, 1.0, 1.0))),
                float(style.get("width", 2.0)),
                str(style.get("style", "solid")),
            )
        except Exception:
            return (0.0, 1.0, 1.0), 2.0, "solid"

    def _rebuild_or_create_drawing(self):
        if len(self.points) < 2:
            return

        color, width, line_style = self._style()

        actor = self.digitizer._make_polyline_actor(
            list(self.points),
            color=color,
            width=width,
            line_style=line_style,
        )

        if actor is None:
            return

        if self.drawing is not None:
            old_actor = self.drawing.get("actor")
            if old_actor is not None:
                self._remove_actor_safe(old_actor)

            self.drawing["coords"] = list(self.points)
            self.drawing["actor"] = actor
            self.drawing["bounds"] = actor.GetBounds()
            self.drawing["original_color"] = color
            self.drawing["original_width"] = width
            self.drawing["original_style"] = line_style
        else:
            self.drawing = {
                "type": "polyline",
                "source": "accudraw",
                "coords": list(self.points),
                "actor": actor,
                "bounds": actor.GetBounds(),
                "original_color": color,
                "original_width": width,
                "original_style": line_style,
                "layer": "DIGITIZER",
            }

            self.digitizer.drawings.append(self.drawing)

        self.digitizer._add_actor_to_overlay(actor)
        self._render()

    def _update_preview(self, target):
        if not self.points:
            return

        target = self._clean_point(target)

        if self._same_point(self.points[-1], target):
            self._remove_preview()
            return

        if self._last_preview_target is not None and self._same_point(self._last_preview_target, target, tol=1e-6):
            return

        self._last_preview_target = target

        self._remove_preview()

        try:
            if getattr(self, "_angle_highlight_active", False):
                preview_color = (0.0, 1.0, 0.0)   # green highlight when angle matches
                preview_width = 4.0
                preview_style = "solid"
            else:
                preview_color = (0.0, 1.0, 1.0)   # cyan normal preview
                preview_width = 2.0
                preview_style = "dotted"

            actor = self.digitizer._make_polyline_actor(
                [self.points[-1], target],
                color=preview_color,
                width=preview_width,
                line_style=preview_style,
            )

            if actor is None:
                return

            try:
                actor.GetProperty().SetOpacity(0.95)
            except Exception:
                pass

            self.preview_actor = actor
            self.digitizer._add_actor_to_overlay(actor)
            self._render()

        except Exception as e:
            print(f"⚠️ AccuDraw preview actor failed: {e}")

    def _remove_preview(self):
        if self.preview_actor is not None:
            self._remove_actor_safe(self.preview_actor)
            self.preview_actor = None

        self._last_preview_target = None

    def _remove_actor_safe(self, actor):
        if actor is None:
            return

        for renderer in (
            getattr(self.digitizer, "overlay_renderer", None),
            getattr(self.digitizer, "renderer", None),
            getattr(self.digitizer, "text_overlay_renderer", None),
        ):
            if renderer is None:
                continue

            try:
                renderer.RemoveActor(actor)
            except Exception:
                pass

            try:
                renderer.RemoveActor2D(actor)
            except Exception:
                pass

            try:
                renderer.RemoveViewProp(actor)
            except Exception:
                pass

    def _render(self):
        try:
            if hasattr(self.digitizer, "_force_render"):
                self.digitizer._force_render()
                return
        except Exception:
            pass

        try:
            if self.app is not None and hasattr(self.app, "vtk_widget"):
                self.app.vtk_widget.render()
        except Exception:
            pass

    def _show_accudraw_vertices(self):
        """Show fixed AccuDraw vertices after every left-click for easy snapping."""
        try:
            if hasattr(self.digitizer, "clear_coordinate_labels"):
                self.digitizer.clear_coordinate_labels()

            if hasattr(self.digitizer, "show_vertex_coordinates") and self.points:
                self.digitizer.show_vertex_coordinates(list(self.points))
        except Exception as e:
            print(f"⚠️ AccuDraw vertex display failed: {e}")

    def reset_after_external_clear(self):
        """Called by Draw > Clear so AccuDraw does not keep stale actors/points."""
        self.points = []
        self.drawing = None
        self._undo_saved = False
        self._last_preview_target = None
        self._angle_highlight_active = False

        self._remove_preview()

        # Remove the green AccuSnap marker also.
        # This is only temporary snap UI cleanup; it does not change snap behavior.
        try:
            if hasattr(self.digitizer, "_hide_snap_marker_now"):
                self.digitizer._hide_snap_marker_now()
        except Exception:
            pass

        try:
            if hasattr(self.digitizer, "clear_coordinate_labels"):
                self.digitizer.clear_coordinate_labels()
        except Exception:
            pass

        self._render()
    # ------------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------------
    @staticmethod
    def _clean_point(point):
        vals = list(point)
        x = float(vals[0]) if len(vals) > 0 else 0.0
        y = float(vals[1]) if len(vals) > 1 else 0.0
        z = float(vals[2]) if len(vals) > 2 else 0.0
        return (x, y, z)

    @staticmethod
    def _same_point(a, b, tol=1e-9):
        return (
            abs(float(a[0]) - float(b[0])) <= tol
            and abs(float(a[1]) - float(b[1])) <= tol
            and abs(float(a[2]) - float(b[2])) <= tol
        )