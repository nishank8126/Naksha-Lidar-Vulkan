"""
SelectionModeDialog — MicroStation-style PowerSelector panel.

UI rewrite: no fixed size, full-text buttons, and auto-positioned to
the top-right of the parent window so it doesn't block the canvas.
Drives gui.element_select_tool.ElementSelectTool plus
exposes Select-All / Deselect-All / Invert / Delete and
Rotate / Mirror on top of the centralized SelectionManager.
"""

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QButtonGroup, QFrame, QSizePolicy, QLineEdit, QComboBox, QMenu
)
from gui.minimize_chip import MinimizableDialogMixin


# --- Stylesheet ---------------------------------------------------------------
_PANEL_QSS = """
QDialog {
    background: #232629;
}
QLabel#sectionLabel {
    color: #9aa0a6;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.5px;
    padding: 2px 0 2px 2px;
}
QLabel#countLabel {
    color: #d0d4d8;
    font-size: 11px;
    padding: 4px 6px;
    background: #1b1d1f;
    border: 1px solid #2e3236;
    border-radius: 4px;
}
QLabel#searchStatus {
    color: #8a9bb0;
    font-size: 10px;
    padding: 0 2px;
}
QLineEdit#toolSearch {
    background: #1b1d1f;
    color: #e6e8eb;
    border: 1px solid #3a3e44;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 11px;
    selection-background-color: #1e5fa3;
}
QLineEdit#toolSearch:focus {
    border-color: #2a7bc7;
}
QComboBox#toolCombo {
    background: #2a2d31;
    color: #e6e8eb;
    border: 1px solid #3a3e44;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 11px;
}
QComboBox#toolCombo:hover {
    border-color: #4a5058;
}
QComboBox#toolCombo::drop-down {
    border: none;
    width: 18px;
}
QComboBox#toolCombo QAbstractItemView {
    background: #1b1d1f;
    color: #e6e8eb;
    border: 1px solid #3a3e44;
    selection-background-color: #1e5fa3;
    selection-color: white;
    outline: none;
}
QPushButton {
    background: #2a2d31;
    color: #e6e8eb;
    border: 1px solid #3a3e44;
    border-radius: 4px;
    padding: 5px 10px;
    font-size: 11px;
}
QPushButton:hover {
    background: #34383d;
    border-color: #4a5058;
}
QPushButton:checked {
    background: #1e5fa3;
    color: white;
    border-color: #2a7bc7;
    font-weight: bold;
}
QPushButton#deleteBtn {
    color: #ffb4b4;
    font-weight: bold;
}
QPushButton#deleteBtn:hover {
    background: #5a1f1f;
    color: #ffd6d6;
    border-color: #c0392b;
}
QFrame[frameShape="4"] {
    color: #2e3236;
}
QPushButton#toolFilterHeader {
    text-align: left;
    color: #5a6370;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.2px;
    padding: 3px 2px;
    border: none;
    background: transparent;
}
QPushButton#toolFilterHeader:hover {
    color: #8a9198;
    background: transparent;
    border: none;
}
QPushButton#toolRowAll {
    text-align: left;
    border-radius: 6px;
    border: 1px solid #303640;
    padding: 0px 14px;
    font-size: 13px;
    font-weight: 700;
    background: #1e2228;
    color: #6e7880;
    min-height: 34px;
}
QPushButton#toolRowAll:hover {
    background: #252c36;
    border-color: #3c4450;
    color: #a0aab4;
}
QPushButton#toolRowAll:checked {
    background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
        stop:0 #0d2040, stop:0.5 #112840, stop:1 #0d1e34);
    border: 1px solid #1e5090;
    border-left: 3px solid #3b9eff;
    color: #90c8ff;
    font-weight: 800;
    padding-left: 12px;
}
"""

# ---------------------------------------------------------------------------
# Premium tool-filter row widget  (each tool gets its own accent colour)
# ---------------------------------------------------------------------------
def _hex_to_rgba(h: str, a: int) -> str:
    """Convert #rrggbb + alpha 0-255 to rgba(r,g,b,a) CSS string."""
    h = h.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{a})"


class _ToolRow(QFrame):
    """Clickable tool-filter row.

    One accent colour for everything (#3b9eff). States:
      disabled    – 0 drawings; name still readable, dimmed
      checked     – filter ON; full accent colour + filled pill
      active_tool – user is drawing with this tool right now (outline glow)
      all_mode    – All is selected; counts visible in uniform soft blue
      hover
      normal      – name readable, count shown if > 0
    """

    toggled = Signal(int)
    _A = "#3b9eff"   # single accent colour

    def __init__(self, idx: int, name: str, parent=None):
        super().__init__(parent)
        self._idx = idx
        self._checked = False
        self._hover = False
        self._count = 0
        self._all_mode = True
        self._active_tool = False
        self.setFixedHeight(32)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 10, 0)
        h.setSpacing(0)

        self._bar = QFrame()
        self._bar.setFixedSize(3, 32)
        h.addWidget(self._bar)
        h.addSpacing(10)

        self._lbl = QLabel(name)
        h.addWidget(self._lbl, 1)

        self._badge = QLabel()
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedSize(26, 18)
        h.addWidget(self._badge)

        self._paint()

    def setChecked(self, v: bool):
        self._checked = v
        self._paint()

    def isChecked(self) -> bool:
        return self._checked

    def setCount(self, n: int):
        self._count = n
        if not n and self._checked:
            self._checked = False
        self.setEnabled(bool(n))
        self._paint()

    def setAllMode(self, v: bool):
        self._all_mode = v
        self._paint()

    def setActiveTool(self, v: bool):
        self._active_tool = v
        self._paint()

    def enterEvent(self, _e):
        if self.isEnabled() and not self._checked:
            self._hover = True
            self._paint()

    def leaveEvent(self, _e):
        self._hover = False
        self._paint()

    def mousePressEvent(self, _e):
        if self.isEnabled():
            self.toggled.emit(self._idx)

    def _paint(self):
        a = self._A
        n = self._count

        if not self.isEnabled():
            self.setStyleSheet("QFrame{background:#1c1f24;border-radius:5px;}")
            self._bar.setStyleSheet("QFrame{background:#252a30;border-radius:1px;}")
            self._lbl.setStyleSheet("color:#4a5260;font-size:11px;")
            self._badge.setStyleSheet("background:transparent;color:#3a4050;font-size:9px;border-radius:8px;")
            self._badge.setText("—")

        elif self._checked:
            self.setStyleSheet(
                f"QFrame{{background:{_hex_to_rgba(a,22)};border:1px solid {_hex_to_rgba(a,60)};"
                f"border-left:3px solid {a};border-radius:5px;}}"
            )
            self._bar.setStyleSheet(f"QFrame{{background:{a};border-radius:1px;}}")
            self._lbl.setStyleSheet(f"color:{a};font-size:11px;font-weight:700;")
            self._badge.setStyleSheet(
                f"background:{a};color:#fff;font-size:9px;font-weight:700;border-radius:8px;"
            )
            self._badge.setText(str(n))

        elif self._active_tool and n:
            self.setStyleSheet(
                f"QFrame{{background:{_hex_to_rgba(a,12)};border:1px solid {_hex_to_rgba(a,35)};"
                f"border-left:3px solid {_hex_to_rgba(a,70)};border-radius:5px;}}"
            )
            self._bar.setStyleSheet(f"QFrame{{background:{_hex_to_rgba(a,70)};border-radius:1px;}}")
            self._lbl.setStyleSheet("color:#d4dce8;font-size:11px;font-weight:600;")
            self._badge.setStyleSheet(
                f"background:{_hex_to_rgba(a,35)};color:{a};"
                f"font-size:9px;font-weight:700;border-radius:8px;"
            )
            self._badge.setText(str(n))

        elif self._all_mode and n:
            self.setStyleSheet(
                f"QFrame{{background:{_hex_to_rgba(a,14)};border:1px solid {_hex_to_rgba(a,28)};"
                f"border-left:3px solid {_hex_to_rgba(a,50)};border-radius:5px;}}"
            )
            self._bar.setStyleSheet(f"QFrame{{background:{_hex_to_rgba(a,50)};border-radius:1px;}}")
            self._lbl.setStyleSheet("color:#b0bcc8;font-size:11px;font-weight:500;")
            self._badge.setStyleSheet(
                f"background:{_hex_to_rgba(a,55)};color:#fff;"
                f"font-size:9px;font-weight:600;border-radius:8px;"
            )
            self._badge.setText(str(n))

        elif self._hover:
            self.setStyleSheet(
                f"QFrame{{background:{_hex_to_rgba(a,10)};border:1px solid {_hex_to_rgba(a,22)};"
                f"border-left:2px solid {_hex_to_rgba(a,45)};border-radius:5px;}}"
            )
            self._bar.setStyleSheet(f"QFrame{{background:{_hex_to_rgba(a,45)};border-radius:1px;}}")
            self._lbl.setStyleSheet("color:#c0cad6;font-size:11px;")
            self._badge.setStyleSheet(
                "background:#252c38;color:#8090a4;font-size:9px;font-weight:600;border-radius:8px;"
            )
            self._badge.setText(str(n) if n else "—")

        else:
            self.setStyleSheet("QFrame{background:#1e2228;border:1px solid #2a303a;border-radius:5px;}")
            self._bar.setStyleSheet("QFrame{background:#2a3040;border-radius:1px;}")
            self._lbl.setStyleSheet("color:#68737f;font-size:11px;")
            self._badge.setStyleSheet(
                "background:#242a34;color:#454e5a;font-size:9px;font-weight:600;border-radius:8px;"
            )
            self._badge.setText(str(n) if n else "—")


class SelectionModeDialog(MinimizableDialogMixin, QDialog):
    """Element selection control panel."""

    METHODS = [
        ("Individual", "individual", "🎯"),
        ("Block",      "block",      "▭"),
        ("Shape",      "shape",      "⬟"),
        ("Line",       "cline",      "╱"),
    ]

    TOOL_FILTERS = [
        ("Smart",    {"smartline"}),
        ("Line",     {"line_segment"}),
        ("Ortho",    {"polygon"}),
        ("Polyline", {"polyline"}),
        ("Rect",     {"rectangle"}),
        ("Circle",   {"circle"}),
        ("Free",     {"freehand"}),
        ("Text",     {"text"}),
        ("Curve",    {"curve"}),
        ("Polygon",  {"polygon"}),
    ]


    def __init__(self, app):
        super().__init__(app, Qt.Window)
        self._init_minimize_state()
        self.app = app
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowTitle("Element Selection")
        # NO fixed size — let Qt size to content, but cap a minimum so it's usable
        self.setMinimumWidth(380)
        self.setSizeGripEnabled(False)
        self.setModal(False)
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        self.setWindowModality(Qt.NonModal)
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setStyleSheet(_PANEL_QSS)

        self._method = "individual"
        self._enclose_mode = "overlap"
        self._poll_generation = 0  # incremented each time move mode is armed

        # Tool row toggle state — set of active chip indices (0 = All / no filter)
        self._active_chip_indices: set = {0}
        self._tool_chips = []
        self._last_active_tool_idx = -1  # tracks which row has active-tool glow

        # Polls digitizer.active_tool every 250 ms while the dialog is visible
        self._active_tool_timer = QTimer(self)
        self._active_tool_timer.setInterval(250)
        self._active_tool_timer.timeout.connect(self._refresh_active_tool)

        self._build_ui()
        self._install_undo_redo_shortcuts()
        self._connect_selection_signal()
        self._activate_tool()
        self._position_top_right()

    # ------------------------------------------------------------------
    # UI build
    # ------------------------------------------------------------------
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Methods grid ─────────────────────────────────────────────
        root.addWidget(self._section_label("PICK METHOD"))
        method_row = QGridLayout()
        method_row.setSpacing(4)
        self._method_group = QButtonGroup(self)
        self._method_group.setExclusive(True)
        for i, (label, key, icon) in enumerate(self.METHODS):
            btn = QPushButton(f"{icon}  {label}")
            btn.setCheckable(True)
            btn.setMinimumHeight(36)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.setProperty("method_key", key)
            if key == self._method:
                btn.setChecked(True)
            btn.setToolTip(self._tooltip_for_method(key))
            self._method_group.addButton(btn)
            method_row.addWidget(btn, i // 4, i % 4)
        self._method_group.buttonClicked.connect(self._on_method_button)
        root.addLayout(method_row)

        # ── Find by tool (collapsible toggle rows) ────────────────────
        root.addWidget(self._divider())
        self._tool_filter_toggle = QPushButton("▶  FIND BY TOOL")
        self._tool_filter_toggle.setObjectName("toolFilterHeader")
        self._tool_filter_toggle.setFlat(True)
        self._tool_filter_toggle.setMinimumHeight(22)
        self._tool_filter_toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._tool_filter_toggle.clicked.connect(self._toggle_tool_filter_bar)
        root.addWidget(self._tool_filter_toggle)

        self._tool_filter_bar = QWidget()
        chip_grid = QGridLayout(self._tool_filter_bar)
        chip_grid.setContentsMargins(0, 4, 0, 4)
        chip_grid.setSpacing(4)

        # "All" row — spans both columns
        self._all_chip = QPushButton("All")
        self._all_chip.setObjectName("toolRowAll")
        self._all_chip.setCheckable(True)
        self._all_chip.setChecked(True)
        self._all_chip.setMinimumHeight(30)
        self._all_chip.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._all_chip.setToolTip("Clear tool filter — show all drawings.")
        self._all_chip.clicked.connect(lambda: self._on_tool_chip_clicked(0))
        chip_grid.addWidget(self._all_chip, 0, 0, 1, 2)
        self._tool_chips.append(self._all_chip)

        # Tool rows — 2 per row, single accent colour
        for i, (name, _types) in enumerate(self.TOOL_FILTERS):
            row = _ToolRow(i + 1, name)
            row.setToolTip(f"Select all {name} tool drawings.")
            row.toggled.connect(self._on_tool_chip_clicked)
            chip_grid.addWidget(row, 1 + (i // 2), i % 2)
            self._tool_chips.append(row)

        self._tool_filter_bar.setVisible(False)  # collapsed by default
        root.addWidget(self._tool_filter_bar)
        self._refresh_tool_counts()

        root.addWidget(self._divider())
        root.addWidget(self._section_label("DISPLAY"))
        display_row = QHBoxLayout()
        display_row.setSpacing(4)
        self.btn_vertex_display = QPushButton("Vertex Display")
        self.btn_vertex_clear = QPushButton("Clear")
        for b in (self.btn_vertex_display, self.btn_vertex_clear):
            b.setMinimumHeight(32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            display_row.addWidget(b)
        self.btn_vertex_display.setToolTip("Show vertex markers for the selected element(s).")
        self.btn_vertex_clear.setToolTip("Hide displayed vertex markers.")
        self.btn_vertex_display.clicked.connect(self._on_vertex_display)
        self.btn_vertex_clear.clicked.connect(self._on_vertex_clear)
        root.addLayout(display_row)

        # ── Edit ─────────────────────────────────────────────────────
        root.addWidget(self._divider())
        root.addWidget(self._section_label("EDIT"))
        edit_row = QHBoxLayout()
        edit_row.setSpacing(4)
        self.btn_move = QPushButton("Move")
        self.btn_move.setCheckable(True)
        self.btn_copy = QPushButton("Copy")
        self.btn_intersect = QPushButton("Intersect")
        self.btn_extend = QPushButton("Extend")
        self.btn_intersect.setCheckable(True)
        for b in (self.btn_move, self.btn_copy, self.btn_intersect, self.btn_extend):
            b.setMinimumHeight(32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            edit_row.addWidget(b)
        self.btn_move.setToolTip(
            "Arm Move: click a selected element and drag it to the new position. "
            "Click empty space or press Esc to finish."
        )
        self.btn_copy.setToolTip(
            "Copy: ghost preview follows your cursor.\n"
            "Left-click to place a copy — click again to place another.\n"
            "Right-click or Esc to exit copy mode."
        )
        self.btn_intersect.setToolTip(
            "Intersect: finds where the selected element is crossed by another line, "
            "bisects it at that point, then lets you left-click one half and "
            "right-click to delete it."
        )
        self.btn_extend.setToolTip(
            "Select Line / SmartLine, then enter distance to extend selected segment/chain."
        )
        self.btn_move.clicked.connect(self._on_move)
        self.btn_copy.clicked.connect(self._on_copy)
        self.btn_intersect.clicked.connect(self._on_intersect)
        self.btn_extend.clicked.connect(self._on_extend)
        root.addLayout(edit_row)

        # ── By Class ──────────────────────────────────────────────
        root.addWidget(self._divider())
        root.addWidget(self._section_label("BY CLASS"))
        byclass_row = QHBoxLayout()
        byclass_row.setSpacing(4)
        self.btn_add_to_byclass = QPushButton("By Class ▾")
        self.btn_add_to_byclass.setMinimumHeight(32)
        self.btn_add_to_byclass.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_add_to_byclass.setToolTip(
            "Send selected drawing(s) as fence(s) to a By Class dialog.\n\n"
            "Options: Fence · Height · Close"
        )
        self.btn_add_to_byclass.clicked.connect(self._show_byclass_menu)
        byclass_row.addWidget(self.btn_add_to_byclass)

        self.btn_lidar_classif = QPushButton("LiDAR Classif. ▾")
        self.btn_lidar_classif.setMinimumHeight(32)
        self.btn_lidar_classif.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.btn_lidar_classif.setToolTip(
            "Send selected drawing(s) as fence(s) to a LiDAR classification\n"
            "algorithm. The fence selector in the dialog is hidden because the\n"
            "fence is pre-loaded from your current selection.\n\n"
            "Options: Low Points · Isolated · Ground · Surface"
        )
        self.btn_lidar_classif.clicked.connect(self._show_lidar_classif_menu)
        byclass_row.addWidget(self.btn_lidar_classif)

        root.addLayout(byclass_row)

        root.addWidget(self._divider())
        root.addWidget(self._section_label("TRANSFORM"))
        transform_row = QHBoxLayout()
        transform_row.setSpacing(4)
        self.btn_rotate = QPushButton("Rotate")
        self.btn_mirror = QPushButton("Mirror")
        for b in (self.btn_rotate, self.btn_mirror):
            b.setMinimumHeight(32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            transform_row.addWidget(b)
        self.btn_rotate.setToolTip(
            "Rotate the current selection. Choose angle entry or corner-drag rotate."
        )
        self.btn_mirror.setToolTip(
            "Mirror the current selection on the X axis, Y axis, or a custom angle."
        )
        self.btn_rotate.clicked.connect(self._on_rotate)
        self.btn_mirror.clicked.connect(self._on_mirror)
        root.addLayout(transform_row)

        # ── Quick actions ────────────────────────────────────────────
        root.addWidget(self._divider())
        actions_row = QHBoxLayout()
        actions_row.setSpacing(4)
        self.btn_all     = QPushButton("Select All")
        self.btn_none    = QPushButton("Deselect")
        self.btn_invert  = QPushButton("Invert")
        self.btn_delete  = QPushButton("🗑  Delete")
        self.btn_delete.setObjectName("deleteBtn")
        for b in (self.btn_all, self.btn_none, self.btn_invert, self.btn_delete):
            b.setMinimumHeight(32)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            actions_row.addWidget(b)
        self.btn_all.setToolTip("Select all drawings (classified fences excluded) — Ctrl+A")
        self.btn_none.setToolTip("Clear the current selection — Ctrl+D")
        self.btn_invert.setToolTip("Invert: select what isn't, deselect what is — Ctrl+I")
        self.btn_delete.setToolTip("Delete every selected element. One Ctrl+Z restores them all.")
        self.btn_all.clicked.connect(self._on_select_all)
        self.btn_none.clicked.connect(self._on_deselect)
        self.btn_invert.clicked.connect(self._on_invert)
        self.btn_delete.clicked.connect(self._on_delete)
        root.addLayout(actions_row)

        # ── Footer ───────────────────────────────────────────────────
        footer = QHBoxLayout()
        self.lbl_count = QLabel("Selection: 0")
        self.lbl_count.setObjectName("countLabel")
        footer.addWidget(self.lbl_count, 1)
        self.lbl_measure_total = QLabel("Measured Total: 0.000 m")
        self.lbl_measure_total.setObjectName("countLabel")
        footer.addWidget(self.lbl_measure_total, 1)
        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(28)
        close_btn.setMinimumWidth(70)
        close_btn.clicked.connect(self.close)
        footer.addWidget(close_btn)
        root.addLayout(footer)

    def _install_undo_redo_shortcuts(self):
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.setContext(Qt.WindowShortcut)
        self._undo_shortcut.activated.connect(self._on_shortcut_undo)
        self._undo_shortcut.activatedAmbiguously.connect(self._on_shortcut_undo)

        self._redo_shortcut = QShortcut(QKeySequence("Ctrl+Y"), self)
        self._redo_shortcut.setContext(Qt.WindowShortcut)
        self._redo_shortcut.activated.connect(self._on_shortcut_redo)
        self._redo_shortcut.activatedAmbiguously.connect(self._on_shortcut_redo)

        self._redo_shift_shortcut = QShortcut(QKeySequence("Ctrl+Shift+Z"), self)
        self._redo_shift_shortcut.setContext(Qt.WindowShortcut)
        self._redo_shift_shortcut.activated.connect(self._on_shortcut_redo)
        self._redo_shift_shortcut.activatedAmbiguously.connect(self._on_shortcut_redo)

    def _shortcut_should_skip_text_edit(self):
        try:
            return isinstance(self.focusWidget(), QLineEdit)
        except Exception:
            return False

    def _on_shortcut_undo(self):
        if self._shortcut_should_skip_text_edit():
            return
        tool = self._get_tool()
        digitizer = getattr(self.app, "digitizer", None)
        try:
            if tool is not None and getattr(tool, "_intersect_mode", False):
                tool._intersect_undo_last_delete()
            elif digitizer is not None:
                digitizer.undo()
        except Exception as e:
            print(f"⚠️ Element Selection undo failed: {e}")

    def _on_shortcut_redo(self):
        if self._shortcut_should_skip_text_edit():
            return
        tool = self._get_tool()
        digitizer = getattr(self.app, "digitizer", None)
        try:
            if tool is not None and getattr(tool, "_intersect_mode", False):
                tool._intersect_redo_last_delete()
            elif digitizer is not None:
                digitizer.redo()
        except Exception as e:
            print(f"⚠️ Element Selection redo failed: {e}")

    @staticmethod
    def _section_label(text):
        lbl = QLabel(text)
        lbl.setObjectName("sectionLabel")
        return lbl

    @staticmethod
    def _divider():
        f = QFrame()
        f.setFrameShape(QFrame.HLine)
        f.setFrameShadow(QFrame.Sunken)
        return f

    @staticmethod
    def _tooltip_for_method(key):
        return {
            "individual": "Click an element to select it. Hover shows a preview.",
            "block":      "Drag a rectangle to pick touched elements.",
            "shape":      "Click polygon vertices, right-click to close.",
            "cline":      "Click two points to draw a line; crossed elements pick.",
        }.get(key, "")

    # ------------------------------------------------------------------
    # Window positioning
    # ------------------------------------------------------------------
    def _position_top_right(self):
        """Anchor the panel to the top-right of the parent window so it
        doesn't sit on top of the drawing area."""
        try:
            parent = self.parent()
            if parent is None:
                return
            geo = parent.frameGeometry() if hasattr(parent, 'frameGeometry') else parent.geometry()
            pw = geo.width()
            margin = 20
            self.adjustSize()
            dw = self.width()
            x = geo.x() + pw - dw - margin
            y = geo.y() + 80
            self.move(QPoint(max(geo.x() + margin, x), y))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------
    def _connect_selection_signal(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            mgr = digitizer.selection_manager
            if mgr is not None:
                mgr.selection_changed.connect(self._on_selection_count)
                # Live count update on every draw/delete/undo that changes drawings
                mgr.selection_changed.connect(self._on_drawings_changed)
                self._on_selection_count(mgr.count())
        except Exception as e:
            print(f"⚠️ SelectionModeDialog could not bind selection_changed: {e}")

    def _on_drawings_changed(self, *_):
        """Called whenever the drawings list changes (delete, undo, add)."""
        self._refresh_tool_counts()
        self._refresh_measurement_total()

    def _on_selection_count(self, count):
        self.lbl_count.setText(f"Selection: {count}")
        self._refresh_measurement_total()

    def _refresh_measurement_total(self):
        tool = getattr(self.app, "measurement_tool", None)
        total = 0.0
        unit = "m"
        if tool is not None and hasattr(tool, "get_selected_measurement_total"):
            try:
                total = float(tool.get_selected_measurement_total())
            except Exception:
                total = 0.0
        if tool is not None:
            try:
                style = getattr(tool, "_measure_style", {}) or {}
                if isinstance(style, dict):
                    if style.get("line", {}).get("unit"):
                        unit = style["line"]["unit"]
                    elif style.get("path", {}).get("unit"):
                        unit = style["path"]["unit"]
            except Exception:
                pass
        refs = []
        if tool is not None:
            refs = list(getattr(tool, "_selected_measurement_refs", []) or [])
            if refs and total == 0.0:
                try:
                    for ref in refs:
                        m_idx = int(ref.get("measurement_index"))
                        s_idx = int(ref.get("segment_index"))
                        measurement = tool.measurements[m_idx]
                        total += float(measurement["labels"][s_idx].get("distance", 0.0))
                except Exception:
                    total = 0.0
        if hasattr(self, "lbl_measure_total"):
            if unit == "km":
                self.lbl_measure_total.setText(f"Measured Total: {total / 1000.0:.3f} km")
            else:
                self.lbl_measure_total.setText(f"Measured Total: {total:.3f} m")

    # ------------------------------------------------------------------
    # Tool plumbing
    # ------------------------------------------------------------------
    def _get_tool(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return None
        return digitizer._element_select_tool

    def _activate_tool(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            activated = digitizer.activate_element_select_tool(method=self._method, delete_on_click=False)
            live_tool = self._get_tool()
            if activated is None and (live_tool is None or not live_tool.is_active()):
                QTimer.singleShot(0, self.close)
                return
        except Exception as e:
            print(f"⚠️ activate_element_select_tool failed: {e}")
        tool = self._get_tool()
        if tool is not None and hasattr(tool, "set_enclose_mode"):
            try:
                tool.set_enclose_mode(self._enclose_mode)
            except Exception:
                pass

    def suspend_pick_interaction(self):
        """Temporarily stand down live pick observers while keeping the dialog open."""
        self._poll_generation += 1
        self.btn_move.setChecked(False)
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.deactivate_element_select_tool()
        except Exception as e:
            print(f"⚠️ suspend_pick_interaction failed: {e}")

    def restore_pick_interaction(self):
        """Re-arm element selection using the dialog's current method."""
        self._activate_tool()

    def _on_method_button(self, btn):
        key = btn.property("method_key")
        if not key:
            return

        tool = self._get_tool()

        # Preserve Intersect behavior:
        # if method changes while Intersect is active, deactivate it first.
        if tool is not None and getattr(tool, "_intersect_mode", False):
            try:
                tool.exit_intersect_mode()
            except Exception:
                pass
            self.btn_intersect.setChecked(False)
            self.lbl_count.setText(f"Selection: {self._current_selection_count()}")
            self._refresh_measurement_total()

        # Extend / scaling integration:
        # When switching between Individual / Block / Shape / Line,
        # clear old selection. Otherwise Block-selected items remain selected
        # and Extend can grow previous selections also.
        if key != self._method:
            digitizer = getattr(self.app, "digitizer", None)
            if digitizer is not None:
                try:
                    digitizer.selection_manager.clear()
                except Exception:
                    pass

                try:
                    digitizer.multi_selected = []
                    digitizer.selected_drawing = None
                    digitizer.selected = None
                except Exception:
                    pass

                try:
                    digitizer.clear_coordinate_labels()
                except Exception:
                    pass

        self._method = key

        if tool is None or not tool.is_active():
            self._activate_tool()
        else:
            tool.set_method(key)

    # ------------------------------------------------------------------
    # Find-by-tool chip toggle bar
    # ------------------------------------------------------------------
    def _toggle_tool_filter_bar(self):
        """Expand or collapse the FIND BY TOOL row panel."""
        bar = self._tool_filter_bar
        visible = not bar.isVisible()
        bar.setVisible(visible)
        btn = self._tool_filter_toggle
        arrow = "▼" if visible else "▶"
        btn.setText(f"{arrow}  FIND BY TOOL")
        if visible:
            btn.setStyleSheet(
                "QPushButton { text-align:left; color:#4a9fd4; font-size:10px;"
                "font-weight:700; letter-spacing:1px; padding:3px 2px;"
                "border:none; background:transparent; }"
                "QPushButton:hover { color:#7ab8f5; border:none; background:transparent; }"
            )
        else:
            btn.setStyleSheet("")  # revert to QSS rule (#toolFilterHeader)
        self._refresh_tool_counts()
        self.adjustSize()

    def _refresh_tool_counts(self):
        """Update All row text and each _ToolRow count badge."""
        digitizer = getattr(self.app, "digitizer", None)
        all_drawings = list(getattr(digitizer, "drawings", None) or []) if digitizer else []

        total = len(all_drawings)
        if self._tool_chips:
            self._tool_chips[0].setText(f"All  {total}")

        all_mode = (0 in self._active_chip_indices)
        for i, (_, types) in enumerate(self.TOOL_FILTERS):
            count = sum(1 for d in all_drawings if d.get("type") in types)
            chip_idx = i + 1
            if chip_idx < len(self._tool_chips):
                chip = self._tool_chips[chip_idx]
                if hasattr(chip, "setAllMode"):
                    chip.setAllMode(all_mode)
                chip.setCount(count)

    def _on_tool_chip_clicked(self, idx: int):
        """Toggle a tool row on/off. Multiple tools can be active at once."""
        if idx == 0:
            self._active_chip_indices = {0}
        else:
            self._active_chip_indices.discard(0)
            if idx in self._active_chip_indices:
                self._active_chip_indices.discard(idx)
                if not self._active_chip_indices:
                    self._active_chip_indices = {0}
            else:
                self._active_chip_indices.add(idx)

        all_mode = (0 in self._active_chip_indices)
        for i, chip in enumerate(self._tool_chips):
            if i == 0:
                chip.setChecked(all_mode)
            else:
                chip.setChecked(i in self._active_chip_indices)
                if hasattr(chip, "setAllMode"):
                    chip.setAllMode(all_mode)

        self._apply_tool_filter()

    def _apply_tool_filter(self):
        """Apply or clear the tool filter based on the active row selections."""
        digitizer = getattr(self.app, "digitizer", None)
        mgr = getattr(digitizer, "selection_manager", None) if digitizer is not None else None
        if mgr is None:
            return

        if 0 in self._active_chip_indices:
            try:
                mgr.clear()
            except Exception as e:
                print(f"⚠️ Tool filter clear failed: {e}")
            return

        # Union of types for all active rows
        active_types: set = set()
        for idx in self._active_chip_indices:
            fi = idx - 1
            if 0 <= fi < len(self.TOOL_FILTERS):
                active_types |= self.TOOL_FILTERS[fi][1]

        if not active_types:
            try:
                mgr.clear()
            except Exception:
                pass
            return

        try:
            mgr.select_all(
                predicate=lambda d, t=active_types: (d.get("type") in t)
                and not d.get("classified_fence", False)
            )
        except Exception as e:
            print(f"⚠️ Tool filter select failed: {e}")

    def _selected_vertex_points(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return []

        try:
            selected = digitizer.selection_manager.get()
        except Exception:
            selected = []

        if not selected:
            legacy = getattr(digitizer, "selected_drawing", None)
            if legacy is not None:
                selected = [legacy]

        # No selection at all — fall back to every drawing in the scene,
        # including finalized curves from curve_tool.
        if not selected:
            all_drawings = list(getattr(digitizer, "drawings", None) or [])
            # ── PATCH: include curve_tool.finalized_actors in the fallback ──
            curve_tool = getattr(self.app, "curve_tool", None)
            if curve_tool is not None:
                for cd in list(getattr(curve_tool, "finalized_actors", []) or []):
                    coords = digitizer._get_drawing_coords(cd) if hasattr(digitizer, "_get_drawing_coords") else []
                    if isinstance(cd, dict) and len(coords) > 0:
                        if not any(cd is d for d in all_drawings):
                            all_drawings.append(cd)
            # ─────────────────────────────────────────────────────────────────
            selected = [d for d in all_drawings if d is not None]

        points = []
        for drawing in selected:
            if drawing is None or drawing.get("type") == "text":
                continue
            if drawing.get("source") == "curve_tool":
                coords = drawing.get("control_points")
            else:
                coords = digitizer._get_drawing_coords(drawing) if hasattr(digitizer, "_get_drawing_coords") else drawing.get("coords")
            if coords is None:
                continue
            points.extend(list(coords))
        return points

    def _on_vertex_display(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        points = self._selected_vertex_points()
        if not points:
            try:
                digitizer.clear_coordinate_labels()
            except Exception:
                pass
            return
        # Remove the selection highlight from each selected drawing so only
        # the vertex markers are visible (selection state is preserved).
        try:
            selected = digitizer.selection_manager.get()
            for drawing in selected:
                if drawing is not None:
                    digitizer._unhighlight_line(drawing)
        except Exception:
            pass
        try:
            digitizer.show_vertex_coordinates(points)
        except Exception as e:
            print(f"Vertex display failed: {e}")

    def _on_vertex_clear(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.clear_coordinate_labels()
            vtk_widget = getattr(self.app, "vtk_widget", None)
            if vtk_widget is not None:
                vtk_widget.render()
        except Exception as e:
            print(f"Vertex display clear failed: {e}")

    # ------------------------------------------------------------------
    # Quick-action handlers
    # ------------------------------------------------------------------
    def _on_select_all(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.selection_manager.select_all(
                predicate=lambda d: not d.get("classified_fence", False)
            )
        except Exception as e:
            print(f"⚠️ Select All failed: {e}")

    def _on_deselect(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            curve_tool = getattr(self.app, "curve_tool", None)
            if curve_tool is not None and hasattr(curve_tool, "_deselect_curve"):
                try:
                    curve_tool._deselect_curve()
                except Exception:
                    pass
            digitizer.clear_coordinate_labels()
            digitizer.selection_manager.clear()
        except Exception as e:
            print(f"⚠️ Deselect failed: {e}")

    def _on_invert(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.selection_manager.invert_all()
        except Exception as e:
            print(f"⚠️ Invert failed: {e}")

    def _on_delete(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.clear_coordinate_labels()
            digitizer.delete_selection(include_classified=False)
        except Exception as e:
            print(f"⚠️ Delete selection failed: {e}")

    def _deactivate_intersect_if_needed(self):
        """Deactivate intersect mode when another edit mode is entered."""
        tool = self._get_tool()
        if tool is not None and getattr(tool, "_intersect_mode", False):
            try:
                tool.exit_intersect_mode()
            except Exception:
                pass
            self.btn_intersect.setChecked(False)
            self.lbl_count.setText(f"Selection: {self._current_selection_count()}")

    def _on_move(self):
        tool = self._get_tool()
        if tool is None or not tool.is_active():
            self.btn_move.setChecked(False)
            return
        if self.btn_move.isChecked():
            self._deactivate_intersect_if_needed()
            self._poll_generation += 1
            tool.enter_move_mode()
            self._poll_move_mode(self._poll_generation)
        else:
            tool.exit_move_mode(cancel=False)

    def _poll_move_mode(self, generation):
        """Keep the Move button in sync with the tool's move-mode state.

        Each arm operation increments _poll_generation; stale timer callbacks
        from a previous arm are dropped by the generation check, preventing a
        prior poll chain from unchecking the button during a new move session.
        """
        if generation != self._poll_generation:
            return
        tool = self._get_tool()
        if tool is None or not tool.is_active():
            self.btn_move.setChecked(False)
            return
        if not tool._move_mode:
            self.btn_move.setChecked(False)
            return
        QTimer.singleShot(150, lambda: self._poll_move_mode(generation))

    def _on_copy(self):
        self._deactivate_intersect_if_needed()
        tool = self._get_tool()
        if tool is None or not tool.is_active():
            return
        tool.enter_copy_mode()

    def _on_intersect(self):
        tool = self._get_tool()
        if tool is None or not tool.is_active():
            self.btn_intersect.setChecked(False)
            return
        try:
            if tool._intersect_mode:
                # Second press deactivates
                tool.exit_intersect_mode()
                self.btn_intersect.setChecked(False)
                self.lbl_count.setText(f"Selection: {self._current_selection_count()}")
                self._refresh_measurement_total()
            else:
                tool.enter_intersect_mode()
                # Button checked state is set by _notify_intersect_active inside the tool
                if tool._intersect_mode:
                    self.lbl_count.setText("Intersect Active")
                    self.lbl_measure_total.setText("Measured Total: -")
        except Exception as e:
            print(f"⚠️ Intersect failed: {e}")
            self.btn_intersect.setChecked(False)

    def _current_selection_count(self):
        try:
            return self.app.digitizer.selection_manager.count()
        except Exception:
            return 0

    def _on_extend(self):
        """
        Simple Extend:
        Select Line / SmartLine first, then enter distance.
        """
        self._deactivate_intersect_if_needed()
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return

        if not hasattr(digitizer, "extend_selected_element_distance_command"):
            try:
                self.app.statusBar().showMessage(
                    "Extend command is not available in digitizer",
                    2500,
                )
            except Exception:
                pass
            return

        try:
            digitizer.extend_selected_element_distance_command()
        except Exception as e:
            print(f"⚠️ Extend failed: {e}")
            try:
                self.app.statusBar().showMessage(
                    f"Extend failed: {e}",
                    2500,
                )
            except Exception:
                pass

    def _on_add_to_byclass(self):
        """Send selected drawings as fences to the Inside Fence (By Class) dialog.

        Gets the current selection from SelectionManager, converts each selected
        drawing into a fence dict compatible with InsideFenceDialog, opens the
        dialog, and pre-loads the fences.
        """
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            self.app.statusBar().showMessage("Digitizer not available", 2500)
            return

        mgr = getattr(digitizer, "selection_manager", None)
        if mgr is None:
            self.app.statusBar().showMessage("Selection manager not available", 2500)
            return

        selected = mgr.get()
        if not selected:
            try:
                self.app.statusBar().showMessage(
                    "No drawings selected — select at least one drawing first", 2500
                )
            except Exception:
                pass
            return

        fence_shapes = []
        for drawing in selected:
            if drawing is None:
                continue
            coords = None
            if hasattr(digitizer, "_get_drawing_coords"):
                coords = digitizer._get_drawing_coords(drawing)
            if coords is None:
                coords = drawing.get("coords")
            if not coords:
                continue

            shape = dict(drawing)
            shape["coords"] = coords
            fence_shapes.append(shape)

        if not fence_shapes:
            try:
                self.app.statusBar().showMessage(
                    "Selected drawings have no usable coordinates", 2500
                )
            except Exception:
                pass
            return

        ribbon = None
        try:
            rm = getattr(self.app, "ribbon_manager", None)
            if rm is not None:
                ribbon = getattr(rm, "ribbons", {}).get("by_class")
        except Exception:
            pass

        if ribbon is None:
            try:
                self.app.statusBar().showMessage(
                    "By Class ribbon not found in the application", 2500
                )
            except Exception:
                pass
            return

        try:
            ribbon.open_inside_fence_dialog()
        except Exception as e:
            print(f"⚠️ Failed to open Inside Fence dialog: {e}")
            try:
                self.app.statusBar().showMessage(
                    f"Failed to open Inside Fence dialog: {e}", 2500
                )
            except Exception:
                pass
            return

        fence_dialog = getattr(ribbon, "inside_fence_dialog", None)
        if fence_dialog is None:
            try:
                self.app.statusBar().showMessage(
                    "Inside Fence dialog could not be created", 2500
                )
            except Exception:
                pass
            return

        try:
            fence_dialog.add_fences_from_selection(fence_shapes)
            count = len(fence_shapes)
            try:
                self.app.statusBar().showMessage(
                    f"{count} drawing{'s' if count != 1 else ''} sent to By Class as fence(s)",
                    3000,
                )
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ Failed to add fences to InsideFenceDialog: {e}")
            try:
                self.app.statusBar().showMessage(
                    f"Failed to add fences: {e}", 2500
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # By Class menu (Fence / Height / Close)
    # ------------------------------------------------------------------
    def _show_byclass_menu(self):
        """Show dropdown for the 3 By Class fence tools (Convert excluded)."""
        menu = QMenu(self)
        menu.addAction("Fence",  self._on_add_to_byclass)
        menu.addAction("Height", lambda: self._on_add_to_byclass_tool("height"))
        menu.addAction("Close",  lambda: self._on_add_to_byclass_tool("close"))
        menu.exec(self.btn_add_to_byclass.mapToGlobal(
            self.btn_add_to_byclass.rect().bottomLeft()
        ))

    def _on_add_to_byclass_tool(self, tool_name: str):
        """Open Close or Height By Class dialog with selected drawings pre-loaded as fences."""
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            try:
                self.app.statusBar().showMessage("Digitizer not available", 2500)
            except Exception:
                pass
            return

        mgr = getattr(digitizer, "selection_manager", None)
        if mgr is None:
            try:
                self.app.statusBar().showMessage("Selection manager not available", 2500)
            except Exception:
                pass
            return

        selected = mgr.get()
        if not selected:
            try:
                self.app.statusBar().showMessage(
                    "No drawings selected — select at least one drawing first", 2500
                )
            except Exception:
                pass
            return

        fence_shapes = []
        for drawing in selected:
            if drawing is None:
                continue
            coords = None
            if hasattr(digitizer, "_get_drawing_coords"):
                coords = digitizer._get_drawing_coords(drawing)
            if coords is None:
                coords = drawing.get("coords")
            if not coords:
                continue
            shape = dict(drawing)
            shape["coords"] = coords
            fence_shapes.append(shape)

        if not fence_shapes:
            try:
                self.app.statusBar().showMessage(
                    "Selected drawings have no usable coordinates", 2500
                )
            except Exception:
                pass
            return

        ribbon = None
        try:
            rm = getattr(self.app, "ribbon_manager", None)
            if rm is not None:
                ribbon = getattr(rm, "ribbons", {}).get("by_class")
        except Exception:
            pass

        if ribbon is None:
            try:
                self.app.statusBar().showMessage("By Class ribbon not found", 2500)
            except Exception:
                pass
            return

        try:
            if tool_name == "height":
                ribbon.open_height_convert_dialog()
                dlg = getattr(ribbon, "height_convert_dialog", None)
                label = "Height"
            else:  # close
                ribbon.open_closed_convert_dialog()
                dlg = getattr(ribbon, "closed_by_class_dialog", None)
                label = "Close"
        except Exception as e:
            print(f"⚠️ Failed to open {tool_name} dialog: {e}")
            return

        if dlg is None:
            return

        try:
            dlg.add_fences_from_selection(fence_shapes)
            count = len(fence_shapes)
            try:
                self.app.statusBar().showMessage(
                    f"{count} drawing{'s' if count != 1 else ''} sent to {label} as fence(s)",
                    3000,
                )
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ Failed to pre-load fences into {tool_name} dialog: {e}")

    # ------------------------------------------------------------------
    # LiDAR Classification from selection
    # ------------------------------------------------------------------
    def _show_lidar_classif_menu(self):
        """Show dropdown menu for LiDAR classification tools."""
        menu = QMenu(self)
        menu.addAction("Low Points",  lambda: self._on_add_to_lidar_classif("low_points"))
        menu.addAction("Isolated",    lambda: self._on_add_to_lidar_classif("isolated"))
        menu.addAction("Ground",      lambda: self._on_add_to_lidar_classif("ground"))
        menu.addAction("Surface",     lambda: self._on_add_to_lidar_classif("surface"))
        menu.exec(self.btn_lidar_classif.mapToGlobal(
            self.btn_lidar_classif.rect().bottomLeft()
        ))

    def _on_add_to_lidar_classif(self, tool_name: str):
        """Open a LiDAR classification dialog with selected drawings pre-loaded as fences.

        The fence selector inside the dialog is hidden — the fence is determined
        by the current drawing selection, matching the InsideFenceDialog pattern.
        """
        _OPENERS = {
            "low_points": ("_classify_low_points_dlg",    "open_classify_low_points"),
            "isolated":   ("_classify_isolated_points_dlg", "open_classify_isolated_points"),
            "ground":     ("_classify_ground_dlg",          "open_classify_ground"),
            "surface":    ("_classify_below_surface_dlg",   "open_classify_below_surface"),
        }
        entry = _OPENERS.get(tool_name)
        if entry is None:
            return

        dlg_attr, opener_name = entry

        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            try:
                self.app.statusBar().showMessage("Digitizer not available", 2500)
            except Exception:
                pass
            return

        mgr = getattr(digitizer, "selection_manager", None)
        if mgr is None:
            try:
                self.app.statusBar().showMessage("Selection manager not available", 2500)
            except Exception:
                pass
            return

        selected = mgr.get()
        if not selected:
            try:
                self.app.statusBar().showMessage(
                    "No drawings selected — select at least one drawing first", 2500
                )
            except Exception:
                pass
            return

        fence_shapes = []
        for drawing in selected:
            if drawing is None:
                continue
            coords = None
            if hasattr(digitizer, "_get_drawing_coords"):
                coords = digitizer._get_drawing_coords(drawing)
            if coords is None:
                coords = drawing.get("coords")
            if not coords:
                continue
            shape = dict(drawing)
            shape["coords"] = coords
            fence_shapes.append(shape)

        if not fence_shapes:
            try:
                self.app.statusBar().showMessage(
                    "Selected drawings have no usable coordinates", 2500
                )
            except Exception:
                pass
            return

        try:
            from gui.lidar_classification_tools import (
                open_classify_low_points, open_classify_isolated_points,
                open_classify_ground, open_classify_below_surface,
            )
            openers = {
                "low_points": open_classify_low_points,
                "isolated":   open_classify_isolated_points,
                "ground":     open_classify_ground,
                "surface":    open_classify_below_surface,
            }
            openers[tool_name](self.app)
        except Exception as e:
            print(f"⚠️ Failed to open LiDAR classification dialog: {e}")
            try:
                self.app.statusBar().showMessage(
                    f"Failed to open dialog: {e}", 2500
                )
            except Exception:
                pass
            return

        dlg = getattr(self.app, dlg_attr, None)
        if dlg is None:
            return

        try:
            dlg.add_fences_from_selection(fence_shapes)
            count = len(fence_shapes)
            try:
                self.app.statusBar().showMessage(
                    f"{count} drawing{'s' if count != 1 else ''} sent to "
                    f"{tool_name.replace('_', ' ').title()} as fence(s)",
                    3000,
                )
            except Exception:
                pass
        except Exception as e:
            print(f"⚠️ Failed to pre-load fences into LiDAR dialog: {e}")

    def _on_rotate(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.show_rotate_dialog()
        except Exception as e:
            print(f"⚠️ Rotate selection failed: {e}")

    def _on_mirror(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return
        try:
            digitizer.show_mirror_menu(self.btn_mirror)
        except Exception as e:
            print(f"⚠️ Mirror selection failed: {e}")

    def _ask_offset(self, title, default_dx="1.0", default_dy="1.0"):
        """Show a small modal dialog asking for dx/dy offset values.

        Returns (dx, dy) as floats, or None if cancelled.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setModal(True)
        dlg.setStyleSheet(_PANEL_QSS)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(6)
        layout.setContentsMargins(12, 12, 12, 12)

        for row_label, attr_name, default in (
            ("Offset X (world units):", "_dx_edit", default_dx),
            ("Offset Y (world units):", "_dy_edit", default_dy),
        ):
            row = QHBoxLayout()
            lbl = QLabel(row_label)
            lbl.setObjectName("sectionLabel")
            edit = QLineEdit(default)
            edit.setMinimumWidth(90)
            row.addWidget(lbl)
            row.addWidget(edit)
            layout.addLayout(row)
            setattr(dlg, attr_name, edit)

        btn_row = QHBoxLayout()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        for b in (ok_btn, cancel_btn):
            b.setMinimumHeight(28)
            btn_row.addWidget(b)
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        layout.addLayout(btn_row)

        if dlg.exec() != QDialog.Accepted:
            return None
        try:
            dx = float(dlg._dx_edit.text())
            dy = float(dlg._dy_edit.text())
        except ValueError:
            return None
        return dx, dy

    def _refresh_active_tool(self):
        """Highlight the row matching the currently active draw tool."""
        digitizer = getattr(self.app, "digitizer", None)
        active = getattr(digitizer, "active_tool", None) or ""
        new_idx = -1
        for i, (_, types) in enumerate(self.TOOL_FILTERS):
            if active in types:
                new_idx = i + 1
                break
        if new_idx == self._last_active_tool_idx:
            return
        self._last_active_tool_idx = new_idx
        for i, chip in enumerate(self._tool_chips):
            if i == 0:
                continue
            if hasattr(chip, "setActiveTool"):
                chip.setActiveTool(i == new_idx)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_tool_counts()
        self._active_tool_timer.start()

    def closeEvent(self, event):
        self._active_tool_timer.stop()
        self._cleanup_chip()
        # Deactivate intersect before teardown
        tool = self._get_tool()
        if tool is not None and getattr(tool, "_intersect_mode", False):
            try:
                tool.exit_intersect_mode()
            except Exception:
                pass
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is not None:
            try:
                curve_tool = getattr(self.app, "curve_tool", None)
                if curve_tool is not None and hasattr(curve_tool, "_deselect_curve"):
                    try:
                        curve_tool._deselect_curve()
                    except Exception:
                        pass
                if getattr(digitizer, "selection_manager", None) is not None:
                    digitizer.selection_manager.clear()
                digitizer.clear_coordinate_labels()
                digitizer.deactivate_element_select_tool()
            except Exception:
                pass
            try:
                tool = digitizer._element_select_tool
                if tool is not None and hasattr(tool, "_original_current_mode"):
                    tool._current_mode = tool._original_current_mode
                    del tool._original_current_mode
            except Exception:
                pass
        super().closeEvent(event)
