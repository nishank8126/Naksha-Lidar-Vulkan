Replace DisplayModeDialog._open_lines_dialog in gui/display_mode.py, from its def line through the end of that method, immediately before @staticmethod / def _recover_flight_line_ids.

```python
    def _open_lines_dialog(self):
        """Open a persistent selector; changes commit only when OK is clicked."""
        existing_popup = getattr(self, "_lines_popup", None)
        if existing_popup is not None:
            try:
                if existing_popup.isVisible():
                    existing_popup.raise_()
                    existing_popup.activateWindow()
                    return
            except RuntimeError:
                self._lines_popup = None

        self._rebuild_lines_menu()
        app = self._get_app_window()
        line_ids = list(getattr(self, "_line_ids", []) or [])
        if not line_ids:
            self._show_front_message(
                QMessageBox.Information,
                "Display Lines",
                "No flight-line data is available in the active point cloud.",
            )
            return

        class LinesDialog(QDialog):
            """Movable frameless selector with resize handles on every edge."""

            RESIZE_MARGIN = 10

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setMouseTracking(True)
                self._drag_offset = None
                self._resize_edges_active = Qt.Edges()
                self._resize_start_position = None
                self._resize_start_geometry = None

            def _resize_edges(self, position):
                edges = Qt.Edges()
                margin = self.RESIZE_MARGIN
                if position.x() <= margin:
                    edges |= Qt.LeftEdge
                elif position.x() >= self.width() - margin:
                    edges |= Qt.RightEdge
                if position.y() <= margin:
                    edges |= Qt.TopEdge
                elif position.y() >= self.height() - margin:
                    edges |= Qt.BottomEdge
                return edges

            def _update_resize_cursor(self, position):
                edges = self._resize_edges(position)
                if edges in (Qt.LeftEdge | Qt.TopEdge, Qt.RightEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeFDiagCursor)
                elif edges in (Qt.RightEdge | Qt.TopEdge, Qt.LeftEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeBDiagCursor)
                elif edges & (Qt.LeftEdge | Qt.RightEdge):
                    self.setCursor(Qt.SizeHorCursor)
                elif edges & (Qt.TopEdge | Qt.BottomEdge):
                    self.setCursor(Qt.SizeVerCursor)
                else:
                    self.unsetCursor()

            def mousePressEvent(self, event):
                if event.button() == Qt.LeftButton:
                    edges = self._resize_edges(event.position().toPoint())
                    if edges:
                        handle = self.windowHandle()
                        if handle is not None and handle.startSystemResize(edges):
                            event.accept()
                            return
                        # Some window managers do not implement native resize
                        # for frameless windows, so retain a manual fallback.
                        self._resize_edges_active = edges
                        self._resize_start_position = event.globalPosition().toPoint()
                        self._resize_start_geometry = self.geometry()
                        event.accept()
                        return
                    self._drag_offset = event.globalPosition().toPoint() - self.pos()
                    handle = self.windowHandle()
                    if handle is not None and handle.startSystemMove():
                        self._drag_offset = None
                    event.accept()
                    return
                super().mousePressEvent(event)

            def mouseMoveEvent(self, event):
                if self._resize_edges_active and self._resize_start_geometry is not None:
                    delta = (
                        event.globalPosition().toPoint()
                        - self._resize_start_position
                    )
                    geometry = self._resize_start_geometry
                    left, top = geometry.left(), geometry.top()
                    right, bottom = geometry.right(), geometry.bottom()
                    minimum_width = self.minimumWidth()
                    minimum_height = self.minimumHeight()
                    edges = self._resize_edges_active
                    if edges & Qt.LeftEdge:
                        left = min(left + delta.x(), right - minimum_width + 1)
                    if edges & Qt.RightEdge:
                        right = max(right + delta.x(), left + minimum_width - 1)
                    if edges & Qt.TopEdge:
                        top = min(top + delta.y(), bottom - minimum_height + 1)
                    if edges & Qt.BottomEdge:
                        bottom = max(bottom + delta.y(), top + minimum_height - 1)
                    self.setGeometry(left, top, right - left + 1, bottom - top + 1)
                    event.accept()
                    return
                if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
                    self.move(event.globalPosition().toPoint() - self._drag_offset)
                    event.accept()
                    return
                self._update_resize_cursor(event.position().toPoint())
                super().mouseMoveEvent(event)

            def mouseReleaseEvent(self, event):
                self._drag_offset = None
                self._resize_edges_active = Qt.Edges()
                self._resize_start_position = None
                self._resize_start_geometry = None
                self._update_resize_cursor(event.position().toPoint())
                super().mouseReleaseEvent(event)

            def leaveEvent(self, event):
                if not self._resize_edges_active:
                    self.unsetCursor()
                super().leaveEvent(event)

            def resizeEvent(self, event):
                super().resizeEvent(event)
                buttons = getattr(self, "_responsive_buttons", ())
                if not buttons:
                    return
                margins = self.layout().contentsMargins()
                spacing = self._responsive_button_spacing
                usable = (
                    self.contentsRect().width()
                    - margins.left() - margins.right()
                    - spacing * (len(buttons) - 1)
                )
                equal_width = max(56, usable // len(buttons))
                for button in buttons:
                    button.setFixedWidth(equal_width)

        class VisibilityCheckBox(QCheckBox):
            """High-contrast visibility checkbox with an explicit check mark."""

            def __init__(self, parent=None):
                super().__init__(parent)
                self.setFixedSize(22, 22)
                self.setCursor(Qt.PointingHandCursor)

            def paintEvent(self, event):
                painter = QPainter(self)
                painter.setRenderHint(QPainter.Antialiasing, True)
                box = QRectF(2.0, 2.0, 18.0, 18.0)
                if self.isChecked():
                    painter.setPen(QPen(QColor("#69b8ff"), 1.5))
                    painter.setBrush(QColor("#087ff5"))
                    painter.drawRoundedRect(box, 4.0, 4.0)
                    tick = QPainterPath()
                    tick.moveTo(6.0, 11.0)
                    tick.lineTo(9.5, 14.5)
                    tick.lineTo(16.5, 7.0)
                    painter.setPen(QPen(QColor("#ffffff"), 2.2, Qt.SolidLine,
                                        Qt.RoundCap, Qt.RoundJoin))
                    painter.drawPath(tick)
                else:
                    painter.setPen(QPen(QColor("#7b8793"), 1.5))
                    painter.setBrush(QColor("#303840"))
                    painter.drawRoundedRect(box, 4.0, 4.0)
        popup = LinesDialog(self)
        popup.setWindowFlag(Qt.FramelessWindowHint, True)
        popup.setObjectName("flightLinesPopup")
        popup.setWindowTitle("Display Lines")
        popup.setModal(False)
        popup.setWindowModality(Qt.NonModal)
        popup.setAttribute(Qt.WA_DeleteOnClose, True)
        # Keep this as an owned dialog so the application's existing window-
        # state handler hides/restores it with NakshaAI. The size grip makes
        # the frameless popup resizable without adding a native title bar.
        popup.setSizeGripEnabled(True)
        popup.setMinimumSize(344, 250)
        popup.setStyleSheet("""
            QDialog#flightLinesPopup {
                background: #191c1f; border: 1px solid #30363c;
                border-radius: 8px;
            }
            QTableWidget {
                background: #141719; color: #ebebeb;
                border: 1px solid #30363c; border-radius: 5px;
                font-family: 'Segoe UI'; font-size: 14px;
            }
            QTableWidget::item { padding-left: 12px; }
            QHeaderView { background: #202428; }
            QHeaderView::section {
                background: #202428; color: #ebebeb;
                border: none; border-right: 1px solid #30363c;
                border-bottom: 1px solid #30363c;
                padding: 8px; font-family: 'Segoe UI'; font-size: 14px;
            }
            QCheckBox { background: transparent; }
            QScrollBar:vertical {
                background: #191c1f; width: 10px; margin: 2px;
            }
            QScrollBar::handle:vertical {
                background: #59636f; border-radius: 3px; min-height: 24px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
            QPushButton {
                background: #252a30; color: #ebebeb;
                border: 1px solid #363e47; border-radius: 6px;
                padding: 0px; font-family: 'Segoe UI'; font-size: 14px;
            }
            QPushButton:hover { background: #303740; }
            QPushButton:pressed { background: #1c2127; }
            QPushButton:focus { border: 1px solid #8794a2; }
            QPushButton#applyFlightLines {
                background: #0067df; border-color: #2388ff; color: white;
            }
            QPushButton#applyFlightLines:hover { background: #0877ef; }
            QPushButton#applyFlightLines:pressed { background: #0058c4; }
            QPushButton#applyFlightLines:focus { border-color: #b6d8ff; }
        """)
        root = QVBoxLayout(popup)
        root.setContentsMargins(16, 20, 16, 16)
        root.setSpacing(16)

        table = QTableWidget(len(line_ids), 2, popup)
        table.setHorizontalHeaderLabels(["Show", "Flight line / Color"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setFocusPolicy(Qt.NoFocus)
        table.setShowGrid(False)
        table.verticalHeader().setDefaultSectionSize(40)
        table.horizontalHeader().setFixedHeight(40)
        table.horizontalHeader().setMinimumSectionSize(64)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        table.setColumnWidth(0, 80)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        table.setMinimumHeight(40 + min(len(line_ids), 4) * 40 + 2)

        slot = int(getattr(self, "current_slot", 0))
        visibility = dict(
            getattr(app, "flight_line_visibility_by_slot", {}).get(slot, {}) or {}
        )
        colors = dict(getattr(app, "flight_line_colors", {}) or {})
        checks = {}
        for row, lid in enumerate(line_ids):
            check_host = QWidget()
            check_layout = QHBoxLayout(check_host)
            check_layout.setContentsMargins(0, 0, 0, 0)
            check_layout.setAlignment(Qt.AlignCenter)
            check = VisibilityCheckBox()
            check.setChecked(bool(visibility.get(lid, True)))
            check_layout.addWidget(check)
            table.setCellWidget(row, 0, check_host)
            checks[lid] = check

            rgb = tuple(colors.get(lid, self._flight_line_color(lid)))
            item = QTableWidgetItem(f"Line {lid}")
            item.setForeground(QColor(235, 235, 235))
            item.setBackground(QColor(*rgb))
            # Ensure text remains legible on bright swatches.
            if sum(rgb) > 440:
                item.setForeground(QColor(20, 20, 20))
            table.setItem(row, 1, item)
        root.addWidget(table)

        all_on = QPushButton("All on", popup)
        invert = QPushButton("Invert", popup)
        all_off = QPushButton("All off", popup)
        all_on.clicked.connect(lambda: [c.setChecked(True) for c in checks.values()])
        invert.clicked.connect(lambda: [c.setChecked(not c.isChecked()) for c in checks.values()])
        all_off.clicked.connect(lambda: [c.setChecked(False) for c in checks.values()])
        ok_btn = QPushButton("OK", popup)
        close_btn = QPushButton("Close", popup)
        ok_btn.setObjectName("applyFlightLines")
        ok_btn.setDefault(True)
        close_btn.clicked.connect(popup.reject)
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)
        buttons = [all_on, invert, all_off, ok_btn, close_btn]
        for button in buttons:
            # The dialog resize handler recalculates one shared width so all
            # five controls remain equal as the popup grows or shrinks.
            button.setMinimumWidth(56)
            button.setMinimumHeight(36)
            button.setMaximumHeight(44)
            button_layout.addWidget(button, 1)
        popup._responsive_buttons = tuple(buttons)
        popup._responsive_button_spacing = button_layout.spacing()
        root.addLayout(button_layout)

        # Choose a compact initial size from the current screen's available
        # geometry. There is deliberately no fixed width or height: users can
        # resize the popup and the table/button layouts consume the space.
        available = self.screen().availableGeometry()
        preferred_width = max(380, min(620, int(available.width() * 0.34)))
        preferred_height = 20 + 40 + min(len(line_ids), 6) * 40 + 2 + 16 + 40 + 16
        popup.resize(
            min(preferred_width, max(344, available.width() - 32)),
            min(preferred_height, max(250, available.height() - 32)),
        )

        def apply_line_visibility():
            slot_visibility = {
                int(lid): check.isChecked() for lid, check in checks.items()
            }
            by_slot = getattr(app, "flight_line_visibility_by_slot", None)
            if not isinstance(by_slot, dict):
                by_slot = {}
            by_slot[slot] = slot_visibility
            app.flight_line_visibility_by_slot = by_slot
            if slot == 0:
                app.flight_line_visibility = dict(slot_visibility)
            self._line_visibility = dict(slot_visibility)
            # Refresh only the selected view. Other slots retain their own
            # independent flight-line selection.
            try:
                if slot == 0:
                    current_mode = str(getattr(app, "display_mode", "class") or "class").lower()
                    from gui.unified_actor_manager import (
                        fast_main_flight_line_visibility_update,
                        invalidate_unified_actor,
                    )
                    fast_done = (
                        current_mode == "class"
                        and fast_main_flight_line_visibility_update(app)
                    )
                    if not fast_done:
                        invalidate_unified_actor(app)
                        if current_mode == "shaded_class":
                            from gui.shading_display import clear_shading_cache
                            clear_shading_cache("Main View flight-line visibility changed")
                        elif current_mode == "surface":
                            app._surface_visible_class_signature = None
                        if hasattr(app, "set_display_mode"):
                            app.set_display_mode(current_mode)
                elif 1 <= slot <= 4:
                    from gui.unified_actor_manager import build_section_unified_actor
                    view_idx = slot - 1
                    if view_idx in (getattr(app, "section_vtks", {}) or {}):
                        build_section_unified_actor(
                            app,
                            view_idx,
                            border_percent=float(
                                getattr(app, "view_borders", {}).get(slot, 0.0)
                            ),
                        )
            except Exception as exc:
                print(f"⚠️ Flight-line filter refresh failed: {exc}")

        # Applying is intentionally non-destructive to the popup lifecycle:
        # users can inspect the result, adjust lines, and apply again. Only
        # Close (or Escape) dismisses this selector.
        ok_btn.clicked.connect(apply_line_visibility)
        self._lines_popup = popup

        def clear_popup_reference(*_args):
            if getattr(self, "_lines_popup", None) is popup:
                self._lines_popup = None

        popup.finished.connect(clear_popup_reference)
        popup.destroyed.connect(clear_popup_reference)
        popup.show()
        popup.raise_()
        popup.activateWindow()
```
