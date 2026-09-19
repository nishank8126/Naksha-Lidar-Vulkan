

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QLabel, QComboBox, QPushButton,
                               QHBoxLayout, QListWidget, QListWidgetItem)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QIcon, QColor, QPainter, QBrush, QPen, QLinearGradient
import os

from gui.theme_manager import ThemeColors, get_dialog_stylesheet

_COLOR_ICON_CACHE = {}
_USE_EXISTING_SELECTION = object()

try:
    from shiboken6 import isValid as _shiboken_is_valid
except ImportError:
    _shiboken_is_valid = None


def _qt_object_is_valid(obj):
    if obj is None:
        return False
    if _shiboken_is_valid is None:
        return True
    try:
        return bool(_shiboken_is_valid(obj))
    except TypeError:
        # Tests and lightweight integrations may expose a non-QObject dialog
        # facade; ordinary Python objects are valid for metadata reads.
        return True


def _normalize_rgb(rgb):
    if isinstance(rgb, QColor):
        return (rgb.red(), rgb.green(), rgb.blue())
    return tuple(int(c) for c in rgb[:3])


def _normalize_palette(palette):
    """Return one palette entry per numeric class code."""
    normalized = {}
    if not isinstance(palette, dict):
        return normalized

    for raw_code, raw_info in palette.items():
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            continue
        info = dict(raw_info) if isinstance(raw_info, dict) else {}
        existing = normalized.get(code)
        if existing is None:
            normalized[code] = info
            continue

        # QSettings and older presets can leave both "17" and 17 keys behind.
        # Runtime integer keys win; string-key records only fill missing fields.
        for key, value in info.items():
            current = existing.get(key)
            if isinstance(raw_code, int) or current is None or (
                isinstance(current, str) and not current.strip()
            ):
                existing[key] = value
    return normalized


def _display_table_palette(app):
    dialog = getattr(app, "display_mode_dialog", None)
    if dialog is None:
        dialog = getattr(app, "display_dialog", None)
    if dialog is not None and not _qt_object_is_valid(dialog):
        return {}
    table = getattr(dialog, "table", None) if dialog is not None else None
    if table is None:
        return {}

    palette = {}
    try:
        row_count = table.rowCount()
    except (RuntimeError, ReferenceError):
        return {}

    for row in range(row_count):
        code_item = table.item(row, 1)
        if code_item is None:
            continue
        try:
            code = int(str(code_item.text()).strip())
        except (TypeError, ValueError):
            continue

        def item_text(column):
            item = table.item(row, column)
            return item.text() if item is not None else ""

        entry = {
            "description": item_text(2),
            "draw": item_text(3),
            "lvl": item_text(4),
        }
        color_item = table.item(row, 5)
        if color_item is not None:
            try:
                brush = color_item.background()
                if brush.style() != Qt.BrushStyle.NoBrush:
                    entry["color"] = brush.color().getRgb()[:3]
            except Exception:
                pass
        palette[code] = entry
    return palette


def _is_meaningful_name(value, code):
    text = str(value or "").strip()
    return bool(text) and text.lower() not in {
        str(code).lower(), f"class {code}".lower(), f"code {code}".lower(), "-",
    }


def resolve_class_catalog(app):
    """Resolve stable PTC class metadata without changing runtime palettes.

    The live app palette owns the active class set and presentation state. The
    Display Mode table owns code/name identity and is not rewritten by shortcut
    application, so it prevents stale preset snapshots from renaming classes.
    """
    live = _normalize_palette(getattr(app, "class_palette", {}) or {})
    table = _display_table_palette(app)
    active = _normalize_palette(getattr(app, "active_ptc_schema", {}) or {})

    app_views = getattr(app, "view_palettes", {}) or {}
    app_slot_zero = _normalize_palette(
        app_views.get(0, {}) if isinstance(app_views, dict) else {}
    )
    dialog = getattr(app, "display_mode_dialog", None)
    if dialog is not None and not _qt_object_is_valid(dialog):
        dialog = None
    active_path = getattr(app, "active_ptc_path", None)
    current_path = getattr(dialog, "current_ptc_path", None) if dialog else None
    if active_path and current_path:
        try:
            if os.path.normcase(os.path.abspath(str(active_path))) != os.path.normcase(
                os.path.abspath(str(current_path))
            ):
                active = {}
        except (OSError, TypeError, ValueError):
            active = {}
    dialog_views = getattr(dialog, "view_palettes", {}) if dialog is not None else {}
    dialog_slot_zero = _normalize_palette(
        dialog_views.get(0, {}) if isinstance(dialog_views, dict) else {}
    )

    # Preserve existing behavior: class_palette defines which classes tools can
    # target. The Display table is only a startup fallback for the class set.
    class_codes = set(live or table or active or app_slot_zero or dialog_slot_zero)
    catalog = {}
    metadata_sources = (table, active, live, app_slot_zero, dialog_slot_zero)

    for code in sorted(class_codes):
        entry = dict(live.get(code, {}))
        if not entry:
            for source in metadata_sources:
                if code in source:
                    entry = dict(source[code])
                    break

        for field in ("lvl", "description"):
            value = next(
                (
                    source[code].get(field)
                    for source in metadata_sources
                    if code in source
                    and _is_meaningful_name(source[code].get(field), code)
                ),
                None,
            )
            if value is not None:
                entry[field] = str(value).strip()
            elif field == "lvl" and not _is_meaningful_name(
                entry.get(field), code
            ):
                entry[field] = ""

        draw = next(
            (
                source[code].get("draw")
                for source in metadata_sources
                if code in source and str(source[code].get("draw") or "").strip()
            ),
            None,
        )
        if draw is not None:
            entry["draw"] = str(draw)

        try:
            entry["color"] = _normalize_rgb(
                entry.get("color", (128, 128, 128))
            )
        except Exception:
            entry["color"] = (128, 128, 128)
        entry.setdefault("show", True)
        entry.setdefault("weight", 1.0)
        entry.setdefault("description", "")
        entry.setdefault("lvl", "")
        entry.setdefault("draw", "")
        catalog[code] = entry

    return catalog


def make_color_icon(rgb):
    """Creates a high-fidelity color pill icon with a subtle glow."""
    rgb = _normalize_rgb(rgb)
    cached = _COLOR_ICON_CACHE.get(rgb)
    if cached is not None:
        return cached

    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    
    color = QColor(*rgb)
    
    # Draw subtle shadow
    painter.setBrush(QBrush(QColor(0, 0, 0, 60)))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, 24, 24)
    
    # Draw main color circle with a slight gradient for depth
    grad = QLinearGradient(0, 0, 0, 32)
    grad.setColorAt(0, color.lighter(115))
    grad.setColorAt(1, color.darker(115))
    
    painter.setBrush(QBrush(grad))
    # Subtle white border to make colors pop against dark UI
    painter.setPen(QPen(QColor(255, 255, 255, 50), 1.5))
    painter.drawEllipse(2, 2, 24, 24)
    
    painter.end()
    icon = QIcon(pix)
    _COLOR_ICON_CACHE[rgb] = icon
    return icon


class WheelClassComboBox(QComboBox):
    """QComboBox with reliable mouse-wheel class selection.

    The previous protection that swallowed wheel events made the ClassPicker
    require a manual click for every "To class" change.  This implementation
    keeps normal popup behaviour, but when the combo is collapsed one wheel
    notch moves exactly one class and emits the normal currentIndexChanged
    signal, so the existing app.to_class persistence logic remains untouched.
    """

    def wheelEvent(self, event):
        # When the popup list is open, keep Qt's normal popup/list behaviour.
        # In practice the popup view receives the wheel event directly, but
        # this guard avoids forcing a selection if the combo receives it.
        try:
            if self.view() is not None and self.view().isVisible():
                super().wheelEvent(event)
                return
        except (RuntimeError, ReferenceError):
            pass

        if not self.isEnabled() or self.count() <= 1:
            super().wheelEvent(event)
            return

        delta = event.angleDelta().y()
        if delta == 0:
            # High-resolution touchpads can report pixelDelta instead.
            delta = event.pixelDelta().y()

        if delta == 0:
            event.ignore()
            return

        current = self.currentIndex()
        if current < 0:
            current = 0

        # Windows mouse wheels normally report +/-120 per notch.  Preserve
        # multi-notch events while still responding to high-resolution wheels.
        notches = max(1, abs(int(delta)) // 120)
        direction = -1 if delta > 0 else 1
        new_index = max(0, min(self.count() - 1, current + direction * notches))

        if new_index != current:
            self.setCurrentIndex(new_index)

        # Do not bubble the wheel to the parent ClassPicker/list and cause an
        # unrelated scroll.
        event.accept()


class ClassPicker(QWidget):
    """
    Persistent floating Class Picker window for classification tools.
    ✅ FIXED: "To class" selection persists across file loads
    """
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.destroyed.connect(self._on_destroyed)
        
        # ✅ CRITICAL: Save initial app state BEFORE any UI is built
        self._saved_to_class = getattr(app, 'to_class', None)
        self._saved_from_classes = getattr(app, 'from_classes', None)
        self._last_class_signature = None
        print(f"📌 ClassPicker init - saved to_class: {self._saved_to_class}, from_classes: {self._saved_from_classes}")
 
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        self.setWindowModality(Qt.NonModal)
        
        # Set window icon
        try:
            logo_path = os.path.join(os.path.dirname(__file__), "icons", "logo.png")
            if os.path.exists(logo_path):
                icon = QIcon(logo_path)
                from PySide6.QtCore import QSize
                for size in [16, 20, 24, 32, 48]:
                    icon.addFile(logo_path, QSize(size, size))
                self.setWindowIcon(icon)
                from PySide6.QtWidgets import QApplication
                QApplication.instance().setWindowIcon(icon)
        except Exception as e:
            print(f"⚠️ Failed to set window icon: {e}")

        self._update_title()

        layout = QVBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        # --- From class (multi-select)
        layout.addWidget(QLabel("From class:"))

        self.from_list = QListWidget()
        self.from_list.setSelectionMode(QListWidget.ExtendedSelection)
        self.from_list.setMinimumHeight(80)      # ← prevents collapsing too small
        # NOTE: Do NOT call setMaximumHeight here — that was the bug
        self.from_list.setUniformItemSizes(True)

        # Add "Any class" option
        any_item = QListWidgetItem("Any class")
        any_item.setData(Qt.UserRole, None)
        self.from_list.addItem(any_item)

        layout.addWidget(self.from_list, stretch=1)   # ← stretch=1 makes it expand

        # --- To class dropdown (single selection)
        layout.addWidget(QLabel("To class:"))
        # Mouse wheel must be a first-class input for classification users.
        # This only changes the selected class; PTC/catalog ownership remains
        # entirely in resolve_class_catalog().
        self.to_combo = WheelClassComboBox()
        self.to_combo.setFocusPolicy(Qt.StrongFocus)
        layout.addWidget(self.to_combo, stretch=0)    # ← stretch=0 stays fixed size

        # --- Invert button
        btn_row = QHBoxLayout()
        invert_btn = QPushButton("Invert")
        btn_row.addWidget(invert_btn)
        layout.addLayout(btn_row)

        self.setLayout(layout)

        # ✅ BLOCK signals during initial setup to prevent unwanted resets
        self.from_list.blockSignals(True)
        self.to_combo.blockSignals(True)

        # Connect to Display Mode
        display_dialog = getattr(app, 'display_mode_dialog', getattr(app, 'display_dialog', None))
        if display_dialog:
            try:
                display_dialog.classes_loaded.connect(self.on_classes_changed)
                print("✅ ClassPicker connected to display_mode_dialog.classes_loaded")
            except Exception as e:
                print(f"⚠️ Could not connect to display_dialog: {e}")
        
        # Populate dropdowns (this will preserve saved selections)
        self.populate_dropdowns()

        # ✅ UNBLOCK signals after setup
        self.from_list.blockSignals(False)
        self.to_combo.blockSignals(False)

        # Connect signals AFTER initial population
        self.from_list.itemSelectionChanged.connect(self._on_from_changed)
        self.to_combo.currentIndexChanged.connect(self._on_to_changed)
        invert_btn.clicked.connect(self._invert_classes)
        invert_btn.setObjectName("secondaryBtn")
        invert_btn.setAutoDefault(False)
        invert_btn.setDefault(False)
        invert_btn.setFocusPolicy(Qt.NoFocus)

        # Default size
        self.setGeometry(200, 200, 320, 280)
        self.setObjectName("ClassPickerRoot")
        self.setProperty("themeStyledWindow", True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.refresh_theme()

    def _on_destroyed(self, *_):
        """Drop stale app.class_picker ref when Qt destroys this widget."""
        try:
            app = getattr(self, "app", None)
            if app is None:
                return
            current = getattr(app, "class_picker", None)
            if current is self or not _qt_object_is_valid(current):
                app.class_picker = None
        except Exception:
            pass

    def refresh_theme(self):
        c = ThemeColors
        self.setStyleSheet(
            get_dialog_stylesheet()
            + f"""
            QWidget#ClassPickerRoot {{
                background-color: {c.get('bg_secondary')};
            }}
            QWidget#ClassPickerRoot QLabel {{
                color: {c.get('text_primary')};
            }}
            QWidget#ClassPickerRoot QListWidget {{
                background-color: {c.get('bg_input')};
                border: 1px solid {c.get('border_light')};
                border-radius: 10px;
                padding: 4px;
            }}
            QWidget#ClassPickerRoot QListWidget::item {{
                padding: 8px 10px;
                margin: 2px 4px;
                border-radius: 7px;
                border: 1px solid transparent;
            }}
            QWidget#ClassPickerRoot QListWidget::item:hover {{
                background-color: {c.get('bg_button_hover')};
                border-color: {c.get('border_light')};
            }}
            QWidget#ClassPickerRoot QListWidget::item:selected {{
                background-color: {c.get('dialog_selection')};
                color: {c.get('text_primary')};
                border-color: {c.get('dialog_primary_border')};
            }}
            QWidget#ClassPickerRoot QComboBox {{
                min-height: 22px;
                padding: 8px 12px;
            }}
            QWidget#ClassPickerRoot QComboBox QAbstractItemView {{
                selection-background-color: {c.get('dialog_selection')};
                selection-color: {c.get('text_primary')};
            }}
            QWidget#ClassPickerRoot QScrollBar:vertical {{
                background: {c.get('bg_secondary')};
                width: 10px;
                margin: 0px;
            }}
            QWidget#ClassPickerRoot QScrollBar::handle:vertical {{
                background: {c.get('border_light')};
                min-height: 28px;
                border-radius: 5px;
                margin: 2px;
            }}
            QWidget#ClassPickerRoot QScrollBar::handle:vertical:hover {{
                background: {c.get('dialog_primary_border')};
            }}
            QWidget#ClassPickerRoot QScrollBar::add-line:vertical,
            QWidget#ClassPickerRoot QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            """
        )

    def _create_logo_header(self, logo_path):
        """Create a header widget with logo and title."""
        if not os.path.exists(logo_path):
            return None
        
        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(5, 5, 5, 5)
        
        logo_label = QLabel()
        pixmap = QPixmap(logo_path)
        scaled_pixmap = pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        logo_label.setPixmap(scaled_pixmap)
        
        self.header_title_label = QLabel("Class Picker")
        self.header_title_label.setStyleSheet("font-weight: bold; font-size: 11pt;")
        
        header_layout.addWidget(logo_label)
        header_layout.addWidget(self.header_title_label)
        header_layout.addStretch()
        
        header.setStyleSheet("""
            QWidget {
                background-color: #2a2a2a;
                border-bottom: 1px solid #404040;
            }
        """)
        
        return header

    def showEvent(self, event):
        super().showEvent(event)
        from gui.theme_manager import ThemeManager
        ThemeManager.apply_native_window_theme(self)
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def ensure_visible(self):
        """Force the ClassPicker visible, first reconciling it with live PTC state."""
        print(f"🔄 Ensuring ClassPicker is visible...")
        self.sync_with_app()

        if self.isMinimized():
            self.showNormal()
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

    def _update_title(self):
        """Update the window title to show the current tool."""
        tool = getattr(self.app, "active_classify_tool", None)
        title = f"Tool: {tool}" if tool else "Tool: None"
        self.setWindowTitle(title)
        if hasattr(self, 'header_title_label'):
            self.header_title_label.setText(title)

    def _on_from_changed(self):
        """Called when From class selection changes."""
        selected_items = self.from_list.selectedItems()
        
        if not selected_items:
            self.app.from_classes = None
            self.app.last_classify_from_classes = None
            print(f"👉 From class cleared")
            return
        
        any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
        
        if any_selected:
            self.app.from_classes = None
            self.app.last_classify_from_classes = None
            print(f"👉 From class: Any class")
        else:
            selected_codes = [item.data(Qt.UserRole) for item in selected_items]
            self.app.from_classes = selected_codes
            self.app.last_classify_from_classes = selected_codes
            print(f"👉 From classes: {selected_codes}")

    def _on_to_changed(self, idx):
        """Called when To class dropdown changes."""
        new_to_class = self.to_combo.currentData()
        self.app.to_class = new_to_class
        self.app.last_classify_to_class = new_to_class
        
        # ✅ CRITICAL: Also update saved value for persistence
        self._saved_to_class = new_to_class
        
        print(f"👉 To class changed to {new_to_class}")

    def _invert_classes(self):
        print("🔄 Invert triggered")
 
        old_from = []
        for i in range(self.from_list.count()):
            item = self.from_list.item(i)
            if item.isSelected():
                val = item.data(Qt.UserRole)
                if val is not None:
                    old_from.append(val)
 
        old_to = self.to_combo.currentData()
 
        self.from_list.blockSignals(True)
        self.to_combo.blockSignals(True)
 
        if not old_from:
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                item.setSelected(item.data(Qt.UserRole) == old_to)
            if self.to_combo.count() > 0:
                self.to_combo.setCurrentIndex(0)
        else:
            first_from = old_from[0]
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                item.setSelected(False)
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                if item.data(Qt.UserRole) == old_to:
                    item.setSelected(True)
                    break
            idx = self.to_combo.findData(first_from)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)
 
        self.from_list.blockSignals(False)
        self.to_combo.blockSignals(False)
 
        self._on_from_changed()
        self._on_to_changed(self.to_combo.currentIndex())

    def _restore_from_selection(self, from_classes):
        """Restore From list selections from a list of class codes"""
        self.from_list.clearSelection()
        
        if from_classes is None:
            self.from_list.item(0).setSelected(True)
            return
        
        if not isinstance(from_classes, (list, tuple)):
            from_classes = [from_classes]
        
        for i in range(self.from_list.count()):
            item = self.from_list.item(i)
            code = item.data(Qt.UserRole)
            if code in from_classes:
                item.setSelected(True)
    
    def sync_with_app(self):
        """Call this whenever tool/classes change externally."""
        signature = self._current_class_signature()
        if (
            self._last_class_signature is not None
            and signature != self._last_class_signature
        ):
            # Classification shortcuts enter through sync_with_app(). Rebuild
            # here before displaying the shortcut's From/To selections.
            self.populate_dropdowns(
                to_class_override=getattr(self.app, "to_class", None),
                from_classes_override=getattr(self.app, "from_classes", None),
            )
            self._update_title()
            return

        self.from_list.blockSignals(True)
        self.to_combo.blockSignals(True)

        self._update_title()

        if hasattr(self.app, "from_classes"):
            self._restore_from_selection(self.app.from_classes)

        if getattr(self.app, "to_class", None) is not None:
            idx = self.to_combo.findData(self.app.to_class)
            if idx >= 0:
                self.to_combo.setCurrentIndex(idx)

        self.from_list.blockSignals(False)
        self.to_combo.blockSignals(False)

    def _current_class_signature(self):
        """Build a lightweight signature so unchanged palettes skip full UI rebuilds."""
        palette = resolve_class_catalog(self.app)
        signature = []
        for class_code, info in palette.items():
            signature.append((
                class_code,
                str(info.get("lvl", "")),
                str(info.get("description", "")),
                _normalize_rgb(info.get("color", (128, 128, 128))),
            ))
        return tuple(signature)

    def on_classes_changed(self):
        """
        Called when Display Mode loads new classes.
        ✅ FIXED: Properly preserves user's "To class" selection
        """
        signature = self._current_class_signature()
        if signature and signature == self._last_class_signature:
            self.sync_with_app()
            print("   Class Picker skipped rebuild (palette unchanged)")
            return

        print("\n" + "="*60)
        print("🔄 CLASS PICKER: Detected class changes from Display Mode")
        print("="*60)
        
        # ✅ CRITICAL: Save current selections BEFORE rebuild
        # Priority: 1) Current UI state, 2) Saved instance var, 3) App state
        old_to = self.to_combo.currentData()
        if old_to is None:
            old_to = self._saved_to_class
        if old_to is None:
            old_to = getattr(self.app, 'to_class', None)
        
        selected_items = self.from_list.selectedItems()
        old_from = [item.data(Qt.UserRole) for item in selected_items] if selected_items else None
        if old_from is None or len(old_from) == 0:
            old_from = self._saved_from_classes
        if old_from is None:
            old_from = getattr(self.app, 'from_classes', None)
        
        print(f"   📌 Preserving selections - From: {old_from}, To: {old_to}")
        
        # ✅ Update saved values before rebuild
        self._saved_to_class = old_to
        self._saved_from_classes = old_from
        
        # Rebuild dropdowns (will use saved values)
        self.populate_dropdowns()
        
        print(f"✅ Class Picker updated with preserved selections")
        print("="*60 + "\n")

    def _restore_selection(self, combo, old_value):
        """Try to restore a previous selection in combo box"""
        if old_value is None:
            return False
        
        for i in range(combo.count()):
            if combo.itemData(i) == old_value:
                combo.setCurrentIndex(i)
                print(f"   ✅ Restored selection: class {old_value}")
                return True
        
        print(f"   ⚠️ Could not find class {old_value}")
        return False

    def populate_dropdowns(
        self,
        *,
        to_class_override=_USE_EXISTING_SELECTION,
        from_classes_override=_USE_EXISTING_SELECTION,
    ):
        """
        Build class dropdowns with FORCEFUL defaults.
        ✅ FIXED: Preserves "To class" and "From class" selections across rebuilds
        """
        print(f"\n🔄 Populating Class Picker...")
        
        # ---------------------------------------------------------
        # ✅ CRITICAL FIX: Get saved selections BEFORE clearing
        # ---------------------------------------------------------
        # Priority order: saved instance var > current UI > app state
        
        # Get "To class" to preserve
        if to_class_override is not _USE_EXISTING_SELECTION:
            to_class_to_restore = to_class_override
        else:
            to_class_to_restore = self._saved_to_class
            if to_class_to_restore is None:
                to_class_to_restore = (
                    self.to_combo.currentData()
                    if self.to_combo.count() > 0
                    else None
                )
            if to_class_to_restore is None:
                to_class_to_restore = getattr(self.app, 'to_class', None)
        
        # Get "From classes" to preserve
        if from_classes_override is not _USE_EXISTING_SELECTION:
            from_classes_to_restore = from_classes_override
        else:
            from_classes_to_restore = self._saved_from_classes
            if from_classes_to_restore is None:
                selected_items = self.from_list.selectedItems()
                if selected_items:
                    from_classes_to_restore = [
                        item.data(Qt.UserRole)
                        for item in selected_items
                        if item.data(Qt.UserRole) is not None
                    ]
            if from_classes_to_restore is None:
                from_classes_to_restore = getattr(self.app, 'from_classes', None)
        
        print(f"   📌 Will restore - To: {to_class_to_restore}, From: {from_classes_to_restore}")
        
        # ---------------------------------------------------------
        # Define Forceful Defaults (Safety Net)
        # ---------------------------------------------------------
        STANDARD_LEVELS = {
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
        }

        # Block signals during rebuild
        self.setUpdatesEnabled(False)
        self.from_list.blockSignals(True)
        self.to_combo.blockSignals(True)

        # Clear existing items
        self.from_list.clear()
        self.to_combo.clear()
        
        class_list = []
        
        # Resolve metadata by numeric code so a shortcut cannot substitute
        # stale names from an older per-view palette.
        palette = resolve_class_catalog(self.app)
        if palette:
            for code, info in palette.items():
                color_tuple = info.get('color', (128, 128, 128))
                class_list.append({
                    'code': code,
                    'desc': info.get('description', ''),
                    'lvl': info.get('lvl', ''),
                    'color': QColor(*color_tuple)
                })

        # ---------------------------------------------------------
        # Populate UI
        # ---------------------------------------------------------
        
        # Add "Any class" to From list
        any_item = QListWidgetItem("Any class")
        any_item.setData(Qt.UserRole, None)
        self.from_list.addItem(any_item)
        
        class_list.sort(key=lambda x: x['code'])
        self._last_class_signature = tuple(
            (
                int(cls['code']),
                str(cls['lvl']),
                str(cls['desc']),
                _normalize_rgb(cls['color']),
            )
            for cls in class_list
        )
        
        for cls in class_list:
            code = cls['code']
            raw_lvl = str(cls['lvl'] or '').strip()
            desc = str(cls['desc'] or '').strip()

            # Custom PTC metadata always wins.  If an older runtime snapshot has
            # no lvl but still has description, use that description rather than
            # displaying an unrelated hard-coded LAS class name.
            lvl = raw_lvl or desc or STANDARD_LEVELS.get(code, str(code))
            
            icon = make_color_icon(cls['color'])
            label = f"{code} - {lvl}"
            if desc and desc != lvl:
                label += f" ({desc})"
            
            # Add to "From" List
            item = QListWidgetItem(icon, label)
            item.setData(Qt.UserRole, code)
            self.from_list.addItem(item)
            
            # Add to "To" Dropdown
            self.to_combo.addItem(icon, label, code)
        
        # ---------------------------------------------------------
        # ✅ CRITICAL FIX: Restore selections AFTER rebuilding
        # ---------------------------------------------------------
        
        # Restore "To class"
        restored_to = False
        if to_class_to_restore is not None:
            for i in range(self.to_combo.count()):
                if self.to_combo.itemData(i) == to_class_to_restore:
                    self.to_combo.setCurrentIndex(i)
                    restored_to = True
                    print(f"   ✅ Restored To class: {to_class_to_restore}")
                    break
        
        if not restored_to and self.to_combo.count() > 0:
            # Default to first non-zero class if available, else first item
            default_idx = 0
            for i in range(self.to_combo.count()):
                if self.to_combo.itemData(i) != 0:
                    default_idx = i
                    break
            self.to_combo.setCurrentIndex(default_idx)
            print(f"   ℹ️ No saved To class, defaulting to index {default_idx}")
        
        # Restore "From classes"
        if from_classes_to_restore is not None and len(from_classes_to_restore) > 0:
            for i in range(self.from_list.count()):
                item = self.from_list.item(i)
                code = item.data(Qt.UserRole)
                if code in from_classes_to_restore:
                    item.setSelected(True)
            print(f"   ✅ Restored From classes: {from_classes_to_restore}")
        else:
            # Default to "Any class"
            self.from_list.item(0).setSelected(True)
            print(f"   ℹ️ No saved From class, defaulting to 'Any class'")
        
        # Unblock signals
        self.from_list.blockSignals(False)
        self.to_combo.blockSignals(False)
        self.setUpdatesEnabled(True)
        
        # ✅ Update app state to match restored selections
        self.app.to_class = self.to_combo.currentData()
        self._saved_to_class = self.app.to_class
        
        selected_items = self.from_list.selectedItems()
        if any(item.data(Qt.UserRole) is None for item in selected_items):
            self.app.from_classes = None
        else:
            self.app.from_classes = [item.data(Qt.UserRole) for item in selected_items]
        self._saved_from_classes = self.app.from_classes
        
        print(f"   ✅ Final state - To: {self.app.to_class}, From: {self.app.from_classes}")
        print(f"✅ Populated Class Picker with {len(class_list)} classes\n")

    def configure_for_background_mode(self):
        """Configure picker when 'To class' is background (0)."""
        self.setEnabled(True)
        print("🎨 ClassPicker: background mode configured")
