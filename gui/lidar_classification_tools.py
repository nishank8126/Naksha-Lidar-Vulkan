import numpy as np
from scipy.spatial import cKDTree, Delaunay
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QComboBox, QDoubleSpinBox, QSpinBox, QCheckBox,
    QPushButton, QProgressDialog, QMessageBox, QApplication,
    QListWidget, QListWidgetItem, QAbstractItemView, QFrame,
    QSizePolicy, QWidget, QScrollArea, QTabWidget
)
from PySide6.QtCore import Qt, QThread, Signal, QRect, QTimer, QEvent, QSize
from PySide6.QtGui import QColor, QPixmap, QIcon, QPainter, QBrush, QPen
from gui.app_icon import load_app_icon
from gui.theme_manager import ThemeColors


def _apply_fence_picker_row_style(row_widget, checked, is_curve):
    """Update one fence-picker row without relying on checkbox parent timing."""
    if row_widget is None:
        return False

    bg = ThemeColors.get('bg_active') if checked else (
        ThemeColors.get('bg_secondary') if is_curve else ThemeColors.get('bg_button')
    )
    try:
        row_widget.setStyleSheet(f"background:{bg}; border-radius:5px;")
        return True
    except RuntimeError:
        # The dialog may be closing while a queued state change is delivered.
        return False


def _connect_fence_picker_checkbox(
    shape,
    badge,
    checkbox,
    row_widget,
    is_curve,
    on_toggle,
):
    """Wire a picker checkbox to its badge, selection actor, and stable row."""
    def _changed(state):
        checked = bool(state)
        badge.setVisible(checked)
        on_toggle(shape, checked)
        _apply_fence_picker_row_style(row_widget, checked, is_curve)

    checkbox.stateChanged.connect(_changed)
    return _changed


def _apply_dialog_icon(dialog, app=None):
    """Apply main app icon/logo to classification dialogs."""
    try:
        icon = None
        if app is not None and hasattr(app, "windowIcon"):
            app_icon = app.windowIcon()
            if app_icon is not None and not app_icon.isNull():
                icon = app_icon
        if icon is None:
            icon = load_app_icon()
        if icon is not None and not icon.isNull():
            dialog.setWindowIcon(icon)
    except Exception:
        pass


def _get_class_colors(app) -> dict:
    """
    Build {class_code: QColor} from available display/palette sources.
    Falls back to deterministic LAS default colors when runtime colors
    are not exposed yet.
    """
    colors = {}
    if not app:
        return colors

    # Source 1: table backgrounds in display dialogs
    for dlg_attr in ("display_mode_dialog", "display_dialog",
                     "class_display_dialog", "classification_dialog"):
        dlg = getattr(app, dlg_attr, None)
        table = getattr(dlg, "table", None) if dlg is not None else None
        if table is None:
            continue
        for row in range(table.rowCount()):
            code = None
            for col in (1, 0, 2):
                item = table.item(row, col)
                if item is None:
                    continue
                try:
                    code = int(item.text())
                    break
                except Exception:
                    continue
            if code is None:
                continue
            # Prefer dedicated color column background, then any background.
            color = None
            for col in (5, 4, 3, 2, 1, 0):
                item = table.item(row, col)
                if item is None:
                    continue
                bg = item.background()
                if isinstance(bg, QBrush) and bg.style() != 0:
                    c = bg.color()
                    if c.isValid() and c.alpha() > 0:
                        color = c
                        break
            if color is not None and code not in colors:
                colors[code] = color
        if colors:
            break

    # Source 2: palette dicts
    for palette_attr in ("class_palette", "classification_palette",
                         "las_class_palette", "point_classes"):
        palette = getattr(app, palette_attr, None)
        if not isinstance(palette, dict):
            continue
        for code_key, info in palette.items():
            try:
                code = int(code_key)
            except Exception:
                continue
            if code in colors or not isinstance(info, dict):
                continue
            val = (info.get("color") or info.get("colour") or info.get("rgb")
                   or info.get("swatch") or info.get("background"))
            c = None
            if isinstance(val, QColor):
                c = val
            elif isinstance(val, (tuple, list)) and len(val) >= 3:
                try:
                    r, g, b = [float(x) for x in val[:3]]
                    if all(v <= 1.0 for v in (r, g, b)):
                        c = QColor.fromRgbF(r, g, b)
                    else:
                        c = QColor(int(r), int(g), int(b))
                except Exception:
                    pass
            elif isinstance(val, str):
                qc = QColor(val)
                if qc.isValid():
                    c = qc
            if c is None:
                r = info.get("r", info.get("red"))
                g = info.get("g", info.get("green"))
                b = info.get("b", info.get("blue"))
                if r is not None and g is not None and b is not None:
                    try:
                        rf, gf, bf = float(r), float(g), float(b)
                        if all(v <= 1.0 for v in (rf, gf, bf)):
                            c = QColor.fromRgbF(rf, gf, bf)
                        else:
                            c = QColor(int(rf), int(gf), int(bf))
                    except Exception:
                        pass
            if c is not None and c.isValid():
                colors[code] = c

    # Final fallback: deterministic LAS colors for any known class codes
    fallback_map = _default_las_colors()
    for code in _get_las_classes(app).keys():
        try:
            code = int(code)
        except Exception:
            continue
        if code not in colors and code in fallback_map:
            colors[code] = fallback_map[code]

    return colors

def _debug_color_sources(app):
    """Print diagnostic info about where colors might be stored."""
    print("   === DEBUG: Searching for color data ===")
    
    # Check display dialog table structure
    for dlg_attr in ('display_mode_dialog', 'display_dialog'):
        dlg = getattr(app, dlg_attr, None)
        if dlg is None:
            print(f"   {dlg_attr}: None")
            continue
        print(f"   {dlg_attr}: EXISTS ({type(dlg).__name__})")
        table = getattr(dlg, 'table', None)
        if table is None:
            print(f"      table: None")
            # Check other widget names
            for child_name in dir(dlg):
                child = getattr(dlg, child_name, None)
                if hasattr(child, 'rowCount'):
                    print(f"      Found table-like: {child_name} ({type(child).__name__})")
            continue
        
        print(f"      table: {table.rowCount()} rows × {table.columnCount()} cols")
        
        # Inspect first row in detail
        if table.rowCount() > 0:
            for col in range(table.columnCount()):
                item = table.item(0, col)
                widget = table.cellWidget(0, col)
                
                item_info = "None"
                if item:
                    bg = item.background()
                    item_info = (f"text='{item.text()}' "
                                f"bg_style={bg.style() if isinstance(bg, QBrush) else 'N/A'} "
                                f"bg_color={bg.color().name() if isinstance(bg, QBrush) and bg.style() != 0 else 'none'}")
                
                widget_info = "None"
                if widget:
                    ss = widget.styleSheet()
                    widget_info = (f"{type(widget).__name__} "
                                  f"ss_len={len(ss)} "
                                  f"autoFill={widget.autoFillBackground()} "
                                  f"ss_preview='{ss[:80]}'" if ss else
                                  f"{type(widget).__name__} no_stylesheet")
                
                print(f"      Row0 Col{col}: item=[{item_info}]  widget=[{widget_info}]")
    
    # Check palette dicts
    for attr in ('class_palette', 'class_colors', 'classification_colors'):
        val = getattr(app, attr, None)
        if val is None:
            continue
        if isinstance(val, dict) and val:
            first_key = next(iter(val))
            first_val = val[first_key]
            print(f"   {attr}: dict with {len(val)} entries, "
                  f"first: {first_key} → {type(first_val).__name__}: {first_val}")


def _make_color_icon(color: QColor, size: int = 14) -> QIcon:
    """Create a small square color swatch icon."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor(100, 100, 100))
    painter.setBrush(color)
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 2, 2)
    painter.end()
    return QIcon(pixmap)


def _make_color_pixmap(color: QColor, w: int = 16, h: int = 16) -> QPixmap:
    """Create a small square color swatch pixmap."""
    pixmap = QPixmap(w, h)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor(100, 100, 100))
    painter.setBrush(color)
    painter.drawRoundedRect(1, 1, w - 2, h - 2, 2, 2)
    painter.end()
    return pixmap

# ═══════════════════════════════════════════════════════════════════════
# PERSISTENT CLASS SETTINGS
# ═══════════════════════════════════════════════════════════════════════
class PersistentClassSettings:
    """
    Singleton store that remembers the last-used class codes and parameter
    values for each classification dialog.

    Keys use the pattern:
        "low_points.from_class"   → [project source class]
        "low_points.to_class"     → [project low-noise class]
        "isolated.from_class"     → [project source class]
        "ground.terrain_angle"    → 88.0
        ...
    """
    _store: dict = {}
    _MAX_ENTRIES: int = 512          # hard cap — more than enough for all dialogs

    @classmethod
    def get(cls, key: str, default=None):
        return cls._store.get(key, default)

    @classmethod
    def set(cls, key: str, value):
        cls._store[key] = value
        # Evict oldest half when cap is hit
        if len(cls._store) > cls._MAX_ENTRIES:
            evict = list(cls._store.keys())[: cls._MAX_ENTRIES // 2]
            for k in evict:
                del cls._store[k]

    @classmethod
    def get_codes(cls, key: str, default_codes: list) -> list:
        val = cls._store.get(key)
        if val is not None and isinstance(val, list):
            return list(val)
        return list(default_codes)

    @classmethod
    def set_codes(cls, key: str, codes: list):
        cls._store[key] = list(codes)

    @classmethod
    def get_value(cls, key: str, default):
        val = cls._store.get(key)
        return val if val is not None else default

    @classmethod
    def set_value(cls, key: str, value):
        cls._store[key] = value


# ═══════════════════════════════════════════════════════════════════════
# DYNAMIC PTC CLASSES HELPER
# ═══════════════════════════════════════════════════════════════════════
def _get_las_classes(app):
    """
    Build {code: "code - Name"} from every possible source on app.
    Tries multiple attribute names so it works regardless of which
    dialog/palette structure the host application uses.
    """
    classes = {}

    if not app:
        return _default_las_classes()

    # ── SOURCE 1: display_mode_dialog / display_dialog table ─────────
    for dlg_attr in ('display_mode_dialog', 'display_dialog',
                     'class_display_dialog', 'classification_dialog'):
        dlg = getattr(app, dlg_attr, None)
        if dlg is None:
            continue
        # Try table widget (various column layouts)
        table = getattr(dlg, 'table', None)
        if table is not None:
            for row in range(table.rowCount()):
                try:
                    # Try columns 0,1 for code; columns 1,2,3,4,5 for name
                    code = None
                    for col in (1, 0, 2):
                        item = table.item(row, col)
                        if item:
                            try:
                                code = int(item.text())
                                break
                            except (ValueError, TypeError):
                                continue
                    if code is None:
                        continue
                    # Try columns 4,3,2,5,1 for the human-readable name
                    name = ""
                    for col in (4, 3, 2, 5, 1):
                        item = table.item(row, col)
                        if item:
                            txt = item.text().strip()
                            if txt and not txt.isdigit():
                                name = txt
                                break
                    if code not in classes:
                        classes[code] = f"{code} - {name}" if name else str(code)
                except Exception:
                    continue
        if classes:
            break

    # ── SOURCE 2: class_palette dict ─────────────────────────────────
    for palette_attr in ('class_palette', 'classification_palette',
                         'las_class_palette', 'point_classes'):
        palette = getattr(app, palette_attr, None)
        if not palette or not isinstance(palette, dict):
            continue
        for code, info in palette.items():
            try:
                code = int(code)
            except (ValueError, TypeError):
                continue
            if code in classes:
                continue
            if isinstance(info, dict):
                name = (info.get('name', '') or info.get('lvl', '') or
                        info.get('label', '') or info.get('description', '') or
                        info.get('title', '') or '')
            elif isinstance(info, str):
                name = info
            else:
                name = ''
            classes[code] = f"{code} - {name}" if name.strip() else str(code)
        if classes:
            break

    # ── SOURCE 3: class_names / class_labels dict ─────────────────────
    for names_attr in ('class_names', 'class_labels', 'las_classes',
                       'classification_names', 'point_class_names'):
        names = getattr(app, names_attr, None)
        if not names or not isinstance(names, dict):
            continue
        for code, name in names.items():
            try:
                code = int(code)
            except (ValueError, TypeError):
                continue
            if code not in classes:
                classes[code] = f"{code} - {name}" if str(name).strip() else str(code)
        if classes:
            break

    # ── SOURCE 4: scan data classification array for unique codes ─────
    if not classes:
        data = getattr(app, 'data', None)
        if data is not None:
            cls_arr = data.get('classification') if isinstance(data, dict) else getattr(data, 'classification', None)
            if cls_arr is not None:
                import numpy as np
                for code in np.unique(cls_arr):
                    try:
                        code = int(code)
                        if code not in classes:
                            classes[code] = str(code)
                    except Exception:
                        continue

    # ── FALLBACK: standard LAS class names ───────────────────────────
    if not classes:
        return _default_las_classes()

    # Fill in standard names for any codes that only have a bare number
    _std = _standard_las_names()
    for code, label in classes.items():
        if label == str(code) and code in _std:
            classes[code] = f"{code} - {_std[code]}"

    return classes


def _standard_las_names():
    """Standard ASPRS LAS point class names."""
    return {
        0:  "Never Classified",
        1:  "Unclassified",
        2:  "Ground",
        3:  "Low Vegetation",
        4:  "Medium Vegetation",
        5:  "High Vegetation",
        6:  "Building",
        7:  "Low Point (Noise)",
        8:  "Reserved",
        9:  "Water",
        10: "Rail",
        11: "Road Surface",
        12: "Reserved",
        13: "Wire – Guard",
        14: "Wire – Conductor",
        15: "Transmission Tower",
        16: "Wire – Connector",
        17: "Bridge Deck",
        18: "High Noise",
        19: "Overhead Structure",
        20: "Ignored Ground",
        21: "Snow",
        22: "Temporal Exclusion",
    }


def _default_las_classes():
    """Return standard LAS classes when no app data is available."""
    return {code: f"{code} - {name}"
            for code, name in _standard_las_names().items()}


def _resolve_ground_class_defaults(app):
    """Resolve source and ground codes from the application's active class map."""
    classes = _get_las_classes(app)

    def _name(code, label):
        text = str(label).strip()
        prefix = f"{code} -"
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
        return " ".join(text.strip().lower().replace("_", " ").split())

    normalized = {int(code): _name(code, label) for code, label in classes.items()}

    ground_code = next(
        (code for code, name in normalized.items() if name == "ground"),
        2,
    )

    source_code = None
    for preferred_name in ("unclassified", "default", "created", "never classified"):
        source_code = next(
            (
                code
                for code, name in normalized.items()
                if name == preferred_name and code != ground_code
            ),
            None,
        )
        if source_code is not None:
            break
    if source_code is None:
        source_code = 1 if ground_code != 1 else 0

    return int(source_code), int(ground_code)


def _resolve_surface_class_defaults(app):
    """Resolve sensible source/surface defaults from the active class map."""
    classes = _get_las_classes(app)

    def _normalized(code, label):
        text = str(label).strip()
        prefix = f"{code} -"
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
        return " ".join(text.strip().lower().replace("_", " ").split())

    names = {
        int(code): _normalized(code, label)
        for code, label in classes.items()
    }
    target = next(
        (
            code
            for code, name in names.items()
            if name in ("surface", "surface points", "road surface", "hard surface")
        ),
        None,
    )
    if target is None:
        target = 11 if 11 in names else next(iter(names), 11)

    source = next(
        (
            code
            for preferred in (
                "unclassified", "default", "created", "never classified"
            )
            for code, name in names.items()
            if name == preferred and code != target
        ),
        None,
    )
    if source is None:
        source = next((code for code in names if code != target), 1)
    return int(source), int(target)


def _resolve_noise_class_defaults(app, *, isolated=False):
    """Resolve safe source/noise defaults from the active project class map."""
    classes = _get_las_classes(app)

    def _normalized(code, label):
        text = str(label).strip()
        prefix = f"{code} -"
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):]
        return " ".join(text.strip().lower().replace("_", " ").split())

    names = {
        int(code): _normalized(code, label)
        for code, label in classes.items()
    }
    if isolated:
        preferred_targets = (
            "isolated point", "isolated points", "high noise", "noise",
            "low point (noise)", "low point", "low noise",
        )
        fallback_codes = (18, 7)
    else:
        preferred_targets = (
            "low point (noise)", "low point", "low points", "low noise",
            "below ground", "noise",
        )
        fallback_codes = (7, 18)

    target = next(
        (
            code
            for preferred in preferred_targets
            for code, name in names.items()
            if name == preferred
        ),
        None,
    )
    if target is None:
        target = next(
            (code for code in fallback_codes if code in names),
            next(iter(names), fallback_codes[0]),
        )

    source = next(
        (
            code
            for preferred in (
                "unclassified", "default", "created", "never classified"
            )
            for code, name in names.items()
            if name == preferred and code != target
        ),
        None,
    )
    if source is None:
        source = next((code for code in names if code != target), 1)
    return int(source), int(target)


def _default_las_colors():
    """Deterministic fallback class colors (QColor) keyed by LAS class code."""
    rgb = {
        0: (160, 160, 160), 1: (235, 235, 235), 2: (166, 124, 82),
        3: (141, 211, 99), 4: (88, 180, 66), 5: (40, 124, 46),
        6: (232, 179, 86), 7: (216, 69, 69), 8: (120, 120, 120),
        9: (65, 143, 222), 10: (234, 172, 74), 11: (120, 120, 120),
        12: (120, 120, 120), 13: (255, 220, 88), 14: (255, 201, 40),
        15: (176, 112, 255), 16: (204, 160, 255), 17: (115, 189, 255),
        18: (255, 64, 129), 19: (255, 133, 0), 20: (115, 115, 115),
        21: (245, 250, 255), 22: (255, 105, 180),
    }
    return {code: QColor(r, g, b) for code, (r, g, b) in rgb.items()}


# ═══════════════════════════════════════════════════════════════════════
# DIALOG STYLE
# ═══════════════════════════════════════════════════════════════════════
def _get_dialog_style():
    from gui.theme_manager import ThemeColors as _TC
    return f"""
QDialog {{
    background-color: {_TC.get('bg_secondary')};
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 12px;
}}
QGroupBox {{
    background-color: {_TC.get('bg_primary')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 10px;
    margin-top: 15px;
    padding-top: 18px;
    padding-bottom: 10px;
    font-weight: bold;
    color: {_TC.get('text_primary')};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    color: {_TC.get('accent')};
}}
QLabel {{ 
    color: {_TC.get('text_primary')}; 
    font-size: 11px; 
    font-weight: 500;
}}
QComboBox, QDoubleSpinBox, QSpinBox {{
    background-color: {_TC.get('bg_input')}; 
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')}; 
    border-radius: 6px;
    padding: 4px 8px; 
    min-height: 24px;
}}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover {{ 
    border: 1px solid {_TC.get('accent')}; 
    background-color: {_TC.get('bg_button_hover')};
}}
QComboBox::drop-down {{ 
    border: none; 
    width: 20px; 
}}
QCheckBox {{ 
    color: {_TC.get('text_primary')}; 
    spacing: 8px; 
    font-size: 11px;
}}
QCheckBox::indicator {{
    width: 18px; 
    height: 18px;
    border: 1px solid {_TC.get('border_light')}; 
    border-radius: 4px;
    background-color: {_TC.get('bg_input')};
}}
QCheckBox::indicator:checked {{ 
    background-color: {_TC.get('accent')}; 
    border-color: {_TC.get('accent')}; 
}}
QPushButton {{
    background-color: {_TC.get('bg_button')}; 
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')}; 
    border-radius: 6px;
    padding: 8px 16px; 
    font-weight: bold; 
    font-size: 11px;
}}
QPushButton:hover {{ 
    background-color: {_TC.get('bg_button_hover')}; 
    border-color: {_TC.get('accent')}; 
}}
QPushButton:pressed {{ 
    background-color: {_TC.get('accent')}; 
    color: {_TC.get('text_on_active')}; 
}}
QPushButton#okButton {{ 
    background-color: {_TC.get('accent')}; 
    border-color: {_TC.get('accent')}; 
    color: {_TC.get('text_on_active')}; 
}}
QPushButton#okButton:hover {{ 
    background-color: {_TC.get('accent_hover')}; 
}}
QPushButton#arrowBtn {{
    background-color: {_TC.get('bg_secondary')}; 
    border: 1px solid {_TC.get('border_light')}; 
    border-radius: 4px;
    padding: 2px; 
    min-width: 32px; 
    max-width: 32px;
    font-weight: bold; 
    font-size: 12px;
    color: {_TC.get('accent')};
}}
QPushButton#arrowBtn:hover {{ 
    border-color: {_TC.get('accent')}; 
    background-color: {_TC.get('bg_button_hover')}; 
}}
QListWidget {{
    background-color: {_TC.get('bg_input')}; 
    color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')}; 
    border-radius: 8px;
    padding: 4px;
}}
QListWidget::item {{
    padding: 6px 10px;
    border-radius: 4px;
}}
QListWidget::item:selected {{ 
    background-color: {_TC.get('accent')}; 
    color: {_TC.get('text_on_active')}; 
}}
QListWidget::item:hover {{ 
    background-color: {_TC.get('bg_button_hover')}; 
}}
QFrame[frameShape="4"] {{ 
    color: {_TC.get('border_light')}; 
    max-height: 1px;
    background: {_TC.get('border_light')};
}}
"""


def _note_text_style():
    from gui.theme_manager import ThemeColors as _TC
    return (
        f"color:{_TC.get('text_secondary')}; "
        "font-size:9px; font-style:italic; padding:2px 4px;"
    )


def _get_inline_multiselect_style():
    from gui.theme_manager import ThemeColors as _TC
    return f"""
QListWidget {{
    background-color: {_TC.get('bg_input')};
    border: 1px solid {_TC.get('border_light')};
    border-radius: 4px;
    min-height: 90px;
    max-height: 120px;
}}
QListWidget::item {{ padding: 2px 6px; }}
QListWidget::item:selected {{ background-color: {_TC.get('accent')}; color: {_TC.get('text_on_active')}; }}
QListWidget::item:hover:!selected {{ background-color: {_TC.get('bg_button_hover')}; }}
"""

def _get_fence_picker_style():
    from gui.theme_manager import ThemeColors as _TC
    return f"""
QDialog {{ background-color: {_TC.get('bg_primary')}; color: {_TC.get('text_primary')}; }}
QLabel {{ color: {_TC.get('text_primary')}; font-size: 11px; }}
QListWidget {{
    background-color: {_TC.get('bg_input')}; border: 1px solid {_TC.get('border_light')};
    border-radius: 4px; padding: 4px;
}}
QListWidget::item {{ background: transparent; border: none; padding: 2px; }}
QCheckBox {{ color: {_TC.get('text_primary')}; font-size: 11px; padding: 8px; font-weight: bold; }}
QCheckBox::indicator {{ width: 18px; height: 18px; }}
QCheckBox::indicator:unchecked {{
    background-color: {_TC.get('bg_button')}; border: 1px solid {_TC.get('border_light')}; border-radius: 3px;
}}
QCheckBox::indicator:checked {{
    background-color: {_TC.get('accent')}; border: 1px solid {_TC.get('accent')}; border-radius: 3px;
}}
QPushButton {{
    background-color: {_TC.get('bg_button')}; color: {_TC.get('text_primary')};
    border: 1px solid {_TC.get('border_light')}; border-radius: 4px;
    padding: 5px 14px; font-size: 10px; font-weight: bold;
}}
QPushButton:hover {{ background-color: {_TC.get('bg_button_hover')}; border-color: {_TC.get('accent')}; }}
"""

from gui.minimize_chip import _get_chip_style, _MinimizedChip


# ═══════════════════════════════════════════════════════════════════════
# WIDGET HELPER
# ═══════════════════════════════════════════════════════════════════════
def _w(layout):
    w = QWidget()
    w.setLayout(layout)
    return w

# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
class ClassSelectorDialog(QDialog):
    def __init__(self, current_codes, app, multi=True, extended=False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Classes")
        self.setStyleSheet(_get_dialog_style())
        _apply_dialog_icon(self, app)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.setMinimumWidth(320)
        self.setMinimumHeight(400)
        self.app = app
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        
        layout.addWidget(QLabel("Select class(es):"))
        
        self.list_widget = QListWidget()
        self.list_widget.setSpacing(2)
        
        if multi:
            # Switch back to ExtendedSelection for Ctrl+click support as requested
            self.list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        else:
            self.list_widget.setSelectionMode(QAbstractItemView.SingleSelection)
        
        las_classes = _get_las_classes(self.app)
        colors = _get_class_colors(self.app)
        
        # Add "Any Class" option for multi-select
        if multi:
            any_item = QListWidgetItem("Any Class")
            any_item.setData(Qt.UserRole, None)
            any_item.setForeground(QColor(ThemeColors.get('accent')))
            font = any_item.font()
            font.setBold(True)
            any_item.setFont(font)
            self.list_widget.addItem(any_item)
            if not current_codes or current_codes == [None]:
                any_item.setSelected(True)

        for code, name in sorted(las_classes.items()):
            color = colors.get(code)
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, code)
            # Add color swatch icon
            if color is not None:
                item.setIcon(_make_color_icon(color, 16))
            self.list_widget.addItem(item)
            if code in (current_codes or []):
                item.setSelected(True)
                
        layout.addWidget(self.list_widget)
        
        if multi:
            hint = QLabel("💡 Use Ctrl + Click to select multiple classes")
            hint.setStyleSheet(_note_text_style())
            layout.addWidget(hint)
            
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        ok = QPushButton("Apply Selection"); ok.setObjectName("okButton")
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btn_row.addStretch(); btn_row.addWidget(cancel); btn_row.addWidget(ok)
        layout.addLayout(btn_row)

    def selected_codes(self):
        selected = [it.data(Qt.UserRole) for it in self.list_widget.selectedItems()]
        if not selected:
            return []
        if None in selected:
            return [None]
        return selected


# ═══════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════
def _class_combo(app, selected=1, persist_key=None):
    cb = QComboBox(); cb.setMinimumWidth(185)
    cb.setIconSize(QSize(16, 16))
    # Always fetch latest colors from app
    colors = _get_class_colors(app)
    for code, name in sorted(_get_las_classes(app).items()):
        color = colors.get(code)
        label = name
        if color is not None:
            # Create a fresh icon for each class item
            icon = _make_color_icon(color, 16)
            cb.addItem(icon, label, code)
        else:
            cb.addItem(label, code)
    idx = cb.findData(selected)
    if idx >= 0: cb.setCurrentIndex(idx)
    
    if persist_key:
        cb.currentIndexChanged.connect(
            lambda: PersistentClassSettings.set_codes(persist_key, [cb.currentData()])
        )
    return cb

def _multi_class_label(codes):
    codes = sorted(codes)
    if not codes: return "None"
    if len(codes) > 1 and codes == list(range(codes[0], codes[-1] + 1)):
        return f"Classes {codes[0]}-{codes[-1]}"
    return ", ".join(str(c) for c in codes)


def _make_class_row(app, default_codes, multi=True, single_default=None,
                    persist_key=None, extended=False):
    """
    Build a premium class-selector row with color patches.
    """
    if persist_key:
        initial_codes = PersistentClassSettings.get_codes(persist_key, default_codes)
    else:
        initial_codes = list(default_codes)

    row = QHBoxLayout(); row.setSpacing(8); row.setContentsMargins(0, 2, 0, 2)
    
    arrow = QPushButton("⋮⋮"); arrow.setObjectName("arrowBtn")
    arrow.setToolTip("Open Full Class Selector")
    arrow.setCursor(Qt.PointingHandCursor)

    if not multi:
        sd = single_default if single_default is not None else (
            initial_codes[0] if initial_codes else 1)
        if persist_key:
            persisted = PersistentClassSettings.get_codes(persist_key, [sd])
            sd = persisted[0] if persisted else sd

        holder = [int(sd)]
        display = QComboBox()
        display.setMinimumWidth(185)
        display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        display.setFocusPolicy(Qt.NoFocus)
        display.setCursor(Qt.PointingHandCursor)
        display.setIconSize(QSize(16, 16))
        # Single-target rows should not open native dropdown lists.
        # Selection is handled only through the dedicated class selector dialog.
        display.setStyleSheet(f"""
            QComboBox {{
                background-color: {ThemeColors.get('bg_input')};
                border: 1px solid {ThemeColors.get('border_light')};
                color: {ThemeColors.get('text_primary')};
                padding-left: 5px;
            }}
            QComboBox::drop-down {{ width: 0px; border: none; }}
        """)

        def _update_display(code):
            display.clear()
            classes = _get_las_classes(app)
            colors = _get_class_colors(app)
            label = classes.get(code, str(code))
            color = colors.get(code)
            if color is not None:
                display.addItem(_make_color_icon(color, 16), label, int(code))
            else:
                display.addItem(label, int(code))

        _update_display(holder[0])

        def _open():
            dlg = ClassSelectorDialog([holder[0]], app,
                                      multi=False, parent=display.window())
            if dlg.exec():
                codes = dlg.selected_codes()
                if codes:
                    holder[0] = int(codes[0])
                    _update_display(holder[0])
                    if persist_key:
                        PersistentClassSettings.set_codes(persist_key, codes)

        # Make the full display area clickable (same UX as multi-class rows).
        display_overlay = QPushButton(display)
        display_overlay.setGeometry(display.rect())
        display_overlay.setFlat(True)
        display_overlay.setCursor(Qt.PointingHandCursor)
        display_overlay.setStyleSheet("background: transparent; border: none;")
        display_overlay.clicked.connect(_open)

        display.installEventFilter(app)
        def _resize_overlay(event):
            if event.type() == QEvent.Resize:
                display_overlay.setGeometry(0, 0, event.size().width(), event.size().height())
            return False
        display.eventFilter = lambda o, e: _resize_overlay(e)

        arrow.clicked.connect(_open)
        row.addWidget(display); row.addWidget(arrow)
        return row, lambda: [holder[0]]
    else:
        display = QComboBox(); display.setMinimumWidth(185)
        display.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        
        # Build a combined color icon for multi-class display
        colors = _get_class_colors(app)
        
        def _update_display(codes):
            display.clear()
            # Fetch fresh colors every time
            current_colors = _get_class_colors(app)
            
            if not codes or codes == [None]:
                display.addItem("Any Class")
                return
            
            icon = _make_multi_class_icon(codes, current_colors)
            label = _multi_class_label(codes)
            if icon:
                display.addItem(icon, label, list(codes))
            else:
                display.addItem(label, list(codes))
        
        _update_display(initial_codes)
        display.setFocusPolicy(Qt.NoFocus)
        display.setCursor(Qt.PointingHandCursor)
        display.setIconSize(QSize(32, 16)) # Wider for multi-icons
        # Use a style that looks enabled but isn't editable
        display.setStyleSheet(f"""
            QComboBox {{ 
                background-color: {ThemeColors.get('bg_input')}; 
                border: 1px solid {ThemeColors.get('border_light')};
                color: {ThemeColors.get('text_primary')};
                padding-left: 5px;
            }}
            QComboBox::drop-down {{ width: 0px; border: none; }}
        """)
        
        holder = [list(initial_codes)]

        def _open():
            dlg = ClassSelectorDialog(holder[0], app, multi=True, extended=extended,
                                      parent=display.window())
            if dlg.exec():
                holder[0] = dlg.selected_codes()
                _update_display(holder[0])
                if persist_key:
                    PersistentClassSettings.set_codes(persist_key, holder[0])

        # Overlay a transparent button to make the whole area clickable like a button
        display_overlay = QPushButton(display)
        display_overlay.setGeometry(display.rect())
        display_overlay.setFlat(True)
        display_overlay.setCursor(Qt.PointingHandCursor)
        display_overlay.setStyleSheet("background: transparent; border: none;")
        display_overlay.clicked.connect(_open)
        
        # Ensure overlay resizes with combo
        display.installEventFilter(app) 
        def _resize_overlay(event):
            if event.type() == QEvent.Resize:
                display_overlay.setGeometry(0, 0, event.size().width(), event.size().height())
            return False
        display.eventFilter = lambda o, e: _resize_overlay(e)
        
        arrow.clicked.connect(_open)
        row.addWidget(display); row.addWidget(arrow)
        return row, lambda: list(holder[0])


def _make_color_icon(color, size=16):
    """Fallback for single color icon."""
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QColor(80, 80, 80))
    painter.setBrush(color)
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 2, 2)
    painter.end()
    return QIcon(pixmap)


def _make_multi_class_icon(codes, colors_dict, size=16, max_swatches=4):
    """
    Create a combined icon showing up to max_swatches color patches
    side by side for a multi-class selection.
    Returns QIcon or None if no colors available.
    """
    if not codes or not colors_dict:
        return None

    # Collect available colors for the selected codes
    available = []
    for code in sorted(codes):
        if code in colors_dict:
            available.append(colors_dict[code])
    
    if not available:
        return None

    # Limit swatches shown
    show = available[:max_swatches]
    n = len(show)
    swatch_w = size
    gap = 2
    total_w = n * swatch_w + (n - 1) * gap
    
    pixmap = QPixmap(total_w, size)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    
    for i, color in enumerate(show):
        x = i * (swatch_w + gap)
        painter.setPen(QColor(80, 80, 80))
        painter.setBrush(color)
        painter.drawRoundedRect(x + 1, 1, swatch_w - 2, size - 2, 2, 2)
    
    painter.end()
    return QIcon(pixmap)
# ═══════════════════════════════════════════════════════════════════════
# FENCE SELECTOR WIDGET  (FIXED: cleans up when digitizer clears)
# ═══════════════════════════════════════════════════════════════════════
class FenceSelectorWidget(QWidget):
    """
    Self-contained fence selection component for classification dialogs.
    
    Supports both digitizer drawings AND curve tool curves as fences.
    
    FIX: Watches the digitizer's drawings list AND curve tool's finalized_actors.
    When either is cleared, this widget automatically removes its
    selection-highlight actors from the VTK renderer.
    """

    _SHAPE_ICONS = {
        'rectangle': '▭', 'circle': '○', 'polygon': '⬟',
        'polyline': '⬡', 'line': '─', 'smartline': '⚡',
        'smart_line': '⚡', 'freehand': '✏️',
        'curve': '〰️',  # ✅ NEW: Curve tool icon
    }

    def __init__(
        self,
        app,
        parent=None,
        *,
        include_curve_fences=True,
        allowed_shape_types=None,
        allow_permanent_mode=True,
        picker_as_tool_window=False,
        empty_status_text="No fence — runs on all points",
        no_shapes_message=None,
    ):
        super().__init__(parent)
        self.app                  = app
        self.selected_fences      = []
        self.permanent_fence_mode = False
        self.include_curve_fences = bool(include_curve_fences)
        self.allow_permanent_mode = bool(allow_permanent_mode)
        self.picker_as_tool_window = bool(picker_as_tool_window)
        self.empty_status_text    = str(empty_status_text or "No fence — runs on all points")

        if allowed_shape_types is None:
            allowed_shape_types = [
                'rectangle', 'circle', 'polygon', 'freehand',
                'line', 'smart_line', 'polyline', 'smartline',
            ]
        self.allowed_shape_types = {
            str(shape_type).strip().lower()
            for shape_type in (allowed_shape_types or [])
            if str(shape_type).strip()
        }

        if no_shapes_message is None:
            if self.include_curve_fences:
                no_shapes_message = (
                    "No shapes or curves found.\n\n"
                    "Draw a shape using Digitize tools or Curve tool first."
                )
            else:
                no_shapes_message = (
                    "No digitized fence found.\n\n"
                    "Draw a closed fence using Digitize tools first."
                )
        self.no_shapes_message = str(no_shapes_message)

        self._hover_actor         = [None]
        self._sel_actors          = {}       # uuid → vtkActor
        self._fence_id_to_uuid    = {}       # id(fence) → stable uuid string
        self._check_timer         = None
        self._conversion_completed = False
        self._build_ui()
        self._start_fence_watch()

    def _build_ui(self):
        main = QVBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(4)

        btn_row = QHBoxLayout(); btn_row.setSpacing(6)
        self.select_btn = QPushButton("📐 Select Fence(s)")
        self.select_btn.setObjectName("fenceBtn")
        self.select_btn.clicked.connect(self.open_fence_picker)

        self.clear_btn = QPushButton("✕ Clear")
        self.clear_btn.setObjectName("fenceClearBtn")
        self.clear_btn.clicked.connect(self.clear_fences)

        btn_row.addWidget(self.select_btn, 1)
        btn_row.addWidget(self.clear_btn)
        main.addLayout(btn_row)

        self.status_lbl = QLabel(self.empty_status_text)
        self.status_lbl.setStyleSheet("color:#888; font-size:10px; padding:2px 0;")
        self.status_lbl.setWordWrap(True)
        main.addWidget(self.status_lbl)

    # ──────────────────────────────────────────────────────────────
    # FIX: Watch for digitizer drawings AND curve tool changes
    # ──────────────────────────────────────────────────────────────
    def _start_fence_watch(self):
        """
        Start a lightweight timer that checks whether the digitizer's
        drawings list or curve tool's finalized_actors have been cleared.
        """
        self._check_timer = QTimer(self)
        self._check_timer.setInterval(500)
        self._check_timer.timeout.connect(self._check_fences_still_valid)
        self._check_timer.start()

    def _check_fences_still_valid(self):
        """
        Check if our selected fences still exist.
        Handles both digitizer drawings and curve tool curves.
        """
        if not self.selected_fences:
            return

        # Build sets of valid IDs for both sources
        digitizer = getattr(self.app, 'digitizer', None)
        drawing_ids = set()
        if digitizer:
            drawings = getattr(digitizer, 'drawings', [])
            if drawings is not None:
                drawing_ids = {id(d) for d in drawings}

        curve_tool = getattr(self.app, 'curve_tool', None)
        curve_ids = set()
        if curve_tool:
            try:
                for curve_data in getattr(curve_tool, 'finalized_actors', []):
                    if isinstance(curve_data, dict):
                        curve_ids.add(id(curve_data))
            except Exception:
                pass

        # Filter valid fences
        still_valid = []
        removed_actor_ids = set()

        for fence in self.selected_fences:
            is_valid = False

            if fence.get('source') == 'curve_tool':
                # Check if curve still exists
                curve_ref = fence.get('curve_data')
                if curve_ref and id(curve_ref) in curve_ids:
                    is_valid = True
                else:
                    # Curve was removed - get actor ID for cleanup
                    removed_actor_ids.add(id(fence))
            elif fence.get('_selection_panel_fence'):
                # Fence was injected via the Element Selection panel — it is a
                # coordinate copy, not a live digitizer object, so skip id-based
                # validation.  restore_fence_mode() clears these when the dialog
                # is reopened from the toolbar.
                is_valid = True
            elif fence.get('_temp_fence'):
                # ✅ Temp Fence tool fence — a coordinate copy, not a live
                # digitizer object, so skip id-based validation. Its lifetime
                # (replacement / removal after conversion) is managed entirely
                # by the Temp Fence tool.
                is_valid = True
            else:
                # Digitizer drawing
                if id(fence) in drawing_ids:
                    is_valid = True
                else:
                    removed_actor_ids.add(id(fence))

            if is_valid:
                still_valid.append(fence)

        # _sel_actors is now keyed by stable UUID strings — no id() reuse risk.
        # Match by iterating; only remove actors whose fence is gone.
        for rid in removed_actor_ids:
            uuid_key = self._fence_id_to_uuid.pop(rid, None)
            if uuid_key and uuid_key in self._sel_actors:
                try:
                    actor = self._sel_actors[uuid_key]
                    self.app.vtk_widget.renderer.RemoveActor(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveActor2D(actor)
                except Exception: pass
                try:
                    self.app.vtk_widget.renderer.RemoveViewProp(actor)
                except Exception: pass
                del self._sel_actors[uuid_key]


        # Update if anything changed
        if len(still_valid) < len(self.selected_fences):
            self.selected_fences = still_valid
            self._update_status()

            if not self.selected_fences:
                try:
                    self.app.vtk_widget.render()
                except Exception:
                    pass

    def get_fence_mask(self, xyz: np.ndarray):
        if not self.selected_fences:
            return None
        combined = np.zeros(len(xyz), dtype=bool)
        for fence in self.selected_fences:
            coords = self._shape_coords(fence)
            if len(coords) > 0:
                combined |= self._poly_mask(xyz, coords)
        return combined

    def _shape_coords(self, shape):
        """Read fence vertices from live and restored drawing formats."""
        if not isinstance(shape, dict):
            return []
        digitizer = getattr(self.app, "digitizer", None)
        getter = getattr(digitizer, "_get_drawing_coords", None)
        if callable(getter):
            try:
                coords = getter(shape)
                if coords is not None and len(coords) > 0:
                    return coords
            except Exception:
                pass
        for key in ("coords", "coordinates", "points", "interpolated"):
            coords = shape.get(key)
            if coords is None:
                continue
            try:
                if len(coords) > 0:
                    return coords
            except TypeError:
                continue
        return []

    def _poly_mask(self, xyz: np.ndarray, coords) -> np.ndarray:
        """
        Point-in-polygon test using ray casting algorithm.
        
        Args:
            xyz: Nx3 array of point coordinates
            coords: Polygon coordinates (list of [x,y,z] or Nx3 array)
        
        Returns:
            Boolean mask of points inside the polygon
        """
        if len(coords) < 3:
            return np.zeros(len(xyz), dtype=bool)
        
        try:
            from matplotlib.path import Path
        except ImportError:
            # Fallback to manual ray casting
            return self._ray_cast_mask(xyz, coords)
        
        # Extract XY coordinates only (ignore Z)
        if isinstance(coords, list):
            poly_xy = np.array([[c[0], c[1]] for c in coords])
        else:
            poly_xy = coords[:, :2]
        
        points_xy = xyz[:, :2]
        
        # 1. AABB Pre-filter (O(N) - Ultra Fast)
        min_x, min_y = np.min(poly_xy, axis=0)
        max_x, max_y = np.max(poly_xy, axis=0)
        
        bbox_mask = (
            (points_xy[:, 0] >= min_x) & 
            (points_xy[:, 0] <= max_x) & 
            (points_xy[:, 1] >= min_y) & 
            (points_xy[:, 1] <= max_y)
        )
        
        inside = np.zeros(len(xyz), dtype=bool)
        
        # 2. Exact Polygon Check ONLY on points inside the bounding box
        if np.any(bbox_mask):
            # Close polygon if not already closed
            if not np.allclose(poly_xy[0], poly_xy[-1]):
                poly_xy = np.vstack([poly_xy, poly_xy[0]])
            
            poly_path = Path(poly_xy)
            inside[bbox_mask] = poly_path.contains_points(points_xy[bbox_mask])
        
        return inside

    def _ray_cast_mask(self, xyz: np.ndarray, coords) -> np.ndarray:
        """
        Manual ray casting fallback for point-in-polygon test.
        Used when matplotlib is not available.
        """
        if isinstance(coords, list):
            poly_xy = np.array([[c[0], c[1]] for c in coords])
        else:
            poly_xy = coords[:, :2]
        
        # Close polygon if not already closed
        if not np.allclose(poly_xy[0], poly_xy[-1]):
            poly_xy = np.vstack([poly_xy, poly_xy[0]])
        
        n_poly = len(poly_xy)
        mask = np.zeros(len(xyz), dtype=bool)
        points_xy = xyz[:, :2]
        
        for i, pt in enumerate(points_xy):
            inside = False
            j = n_poly - 1
            for k in range(n_poly):
                xi, yi = poly_xy[k]
                xj, yj = poly_xy[j]
                if ((yi > pt[1]) != (yj > pt[1])) and \
                   (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi + 1e-10) + xi):
                    inside = not inside
                j = k
            mask[i] = inside
        
        return mask

    def cleanup_actors(self):
        """Remove all VTK actors and stop the watch timer."""
        self._remove_hover()
        self._remove_sel_actors()
        if self._check_timer is not None:
            self._check_timer.stop()

    def _remove_hover(self):
        """Remove hover highlight actor."""
        if self._hover_actor[0]:
            try:
                actor = self._hover_actor[0]
                self.app.vtk_widget.renderer.RemoveActor(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(actor)
            except Exception: pass
            self._hover_actor[0] = None

    def _remove_sel_actors(self):
        """Remove all selection highlight actors."""
        for actor in self._sel_actors.values():
            try:
                self.app.vtk_widget.renderer.RemoveActor(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(actor)
            except Exception: pass
        self._sel_actors.clear()
        self._fence_id_to_uuid.clear()   # ← release stale id→uuid mappings

    def open_fence_picker(self):
        """Open fence picker dialog for selected fence sources."""
        active_picker = getattr(self, "_active_picker_dialog", None)
        if active_picker is not None:
            try:
                if active_picker.isVisible():
                    active_picker.raise_()
                    active_picker.activateWindow()
                    return
            except RuntimeError:
                self._active_picker_dialog = None

        self._conversion_completed = False
        digitize = getattr(self.app, 'digitizer', None)
        curve_tool = getattr(self.app, 'curve_tool', None)
        
        # ── Collect digitizer drawings ────────────────────────────
        valid = []
        if digitize:
            drawings = getattr(digitize, 'drawings', [])
            for drawing in drawings:
                stype = str(drawing.get('type', '')).strip().lower()
                if self.allowed_shape_types and stype not in self.allowed_shape_types:
                    continue
                if len(self._shape_coords(drawing)) < 3:
                    continue
                valid.append(drawing)

        # ── Collect curve tool curves ✅ NEW ─────────────────────
        curve_fences = []
        if self.include_curve_fences and curve_tool and hasattr(curve_tool, 'get_curves_as_fences'):
            curve_fences = curve_tool.get_curves_as_fences()
            valid.extend(curve_fences)

        if not valid:
            QMessageBox.warning(self.window(), "No Shapes Found",
                self.no_shapes_message)
            return

        if self.picker_as_tool_window:
            # Qt.SubWindow is intended for QMdiArea children and can be clipped
            # or hidden when attached to a normal top-level QDialog on Windows.
            # An owned Tool window stays with the classifier while remaining
            # independently visible and interactive.
            picker_flags = (
                Qt.Tool
                | Qt.WindowTitleHint
                | Qt.WindowCloseButtonHint
            )
        else:
            picker_flags = Qt.SubWindow
        dlg = QDialog(self.window(), picker_flags)
        dlg.setAttribute(Qt.WA_QuitOnClose, False)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        dlg.setWindowTitle("Select Fence(s)")
        dlg.setWindowModality(Qt.NonModal)
        dlg.setStyleSheet(_get_fence_picker_style())
        dlg.resize(420, 500)
        layout = QVBoxLayout(dlg)

        info = QLabel("Select one or more fences to use for conversion")
        from gui.theme_manager import ThemeColors
        info.setStyleSheet(f"color:{ThemeColors.get('accent')}; font-weight:bold; padding:8px;")
        layout.addWidget(info)
        
        # Show source counts
        digitizer_count = len(valid) - len(curve_fences)
        curve_count = len(curve_fences)
        if self.include_curve_fences:
            source_text = f"📐 Digitizer: {digitizer_count} | 〰️ Curves: {curve_count}"
        else:
            source_text = f"📐 Digitizer: {digitizer_count}"
        source_info = QLabel(source_text)
        source_info.setStyleSheet(f"color:{ThemeColors.get('text_muted')}; font-size:10px; padding:2px 8px;")
        layout.addWidget(source_info)

        perm_chk = None
        if self.allow_permanent_mode:
            perm_chk = QCheckBox("🔄 Permanent Fence Mode (keep all fences selected)")
            perm_chk.setChecked(self.permanent_fence_mode)
            layout.addWidget(perm_chk)

        fence_list = QListWidget()
        fence_list.setStyleSheet("""
            QListWidget { background:#1e1e1e; border:1px solid #3a3a3a;
                          border-radius:4px; padding:4px; }
            QListWidget::item { background:transparent; border:none; padding:2px; }
        """)
        fence_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(fence_list)

        hover_actor  = [None]
        picker_sel   = {}

        def _make_actor(coords, color, width):
            """Create a polyline actor for highlighting."""
            # ✅ For curve tool, use Actor2D like the curve tool does
            try:
                import vtk
                pts = vtk.vtkPoints()
                pts.SetDataTypeToDouble()
                for c in coords:
                    z = float(c[2]) if len(c) > 2 else 0.0
                    pts.InsertNextPoint(float(c[0]), float(c[1]), z)
                
                pl = vtk.vtkPolyLine()
                pl.GetPointIds().SetNumberOfIds(len(coords))
                for i in range(len(coords)):
                    pl.GetPointIds().SetId(i, i)
                
                ca = vtk.vtkCellArray()
                ca.InsertNextCell(pl)
                
                pd = vtk.vtkPolyData()
                pd.SetPoints(pts)
                pd.SetLines(ca)
                
                # Use Mapper2D for consistent rendering with curve tool
                mapper = vtk.vtkPolyDataMapper2D()
                mapper.SetInputData(pd)
                
                coord = vtk.vtkCoordinate()
                coord.SetCoordinateSystemToWorld()
                mapper.SetTransformCoordinate(coord)
                
                ac = vtk.vtkActor2D()
                ac.SetMapper(mapper)
                ac.GetProperty().SetColor(*color)
                ac.GetProperty().SetLineWidth(width)
                ac.GetProperty().SetDisplayLocationToForeground()
                
                return ac
            except Exception:
                # Fallback to 3D actor
                try:
                    import vtk
                    pts = vtk.vtkPoints()
                    for c in coords:
                        pts.InsertNextPoint(float(c[0]), float(c[1]),
                                            float(c[2]) if len(c) > 2 else 0.0)
                    pl = vtk.vtkPolyLine()
                    pl.GetPointIds().SetNumberOfIds(len(coords))
                    for i in range(len(coords)):
                        pl.GetPointIds().SetId(i, i)
                    ca = vtk.vtkCellArray(); ca.InsertNextCell(pl)
                    pd = vtk.vtkPolyData(); pd.SetPoints(pts); pd.SetLines(ca)
                    mp = vtk.vtkPolyDataMapper(); mp.SetInputData(pd)
                    ac = vtk.vtkActor(); ac.SetMapper(mp)
                    ac.GetProperty().SetColor(*color)
                    ac.GetProperty().SetLineWidth(width)
                    return ac
                except Exception:
                    return None

        def _add(actor):
            """Add actor to renderer."""
            try:
                if actor:
                    if hasattr(actor, 'IsA') and actor.IsA('vtkActor2D'):
                        self.app.vtk_widget.renderer.AddViewProp(actor)
                    else:
                        self.app.vtk_widget.renderer.AddActor(actor)
                    self.app.vtk_widget.render()
            except Exception:
                pass

        def _rem(actor):
            """Remove actor from renderer."""
            if not actor: return
            try:
                self.app.vtk_widget.renderer.RemoveActor(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(actor)
            except Exception: pass
            try:
                self.app.vtk_widget.renderer.RemoveViewProp(actor)
            except Exception: pass

        def on_hover(shape):
            if hover_actor[0]: _rem(hover_actor[0]); hover_actor[0] = None
            if shape is None:
                try: self.app.vtk_widget.render()
                except: pass
                return
            coords = self._shape_coords(shape)
            if len(coords) == 0: return
            # Use yellow for hover
            ac = _make_actor(coords, (1, 1, 0), 6)
            if ac: hover_actor[0] = ac; _add(ac)

        def on_toggle(shape, checked):
            # Use unique ID based on source type
            if shape.get('source') == 'curve_tool':
                sid = id(shape.get('curve_data', shape))
            else:
                sid = id(shape)
            
            if not checked:
                if sid in picker_sel: _rem(picker_sel.pop(sid))
                try: self.app.vtk_widget.render()
                except: pass
                return
            if sid not in picker_sel:
                coords = self._shape_coords(shape)
                if len(coords) == 0: return
                # Use blue for selection
                ac = _make_actor(coords, (0, 0.5, 1), 5)
                if ac: picker_sel[sid] = ac; _add(ac)

        # Build current selection IDs
        current_ids = set()
        for f in self.selected_fences:
            if f.get('source') == 'curve_tool':
                current_ids.add(id(f.get('curve_data', f)))
            else:
                current_ids.add(id(f))

        initial_source_ids = {id(f) for f in valid}
        refresh_timer = QTimer(dlg)

        def _current_valid_fences():
            live_valid = []
            if digitize:
                drawings = getattr(digitize, 'drawings', [])
                for drawing in drawings:
                    stype = str(drawing.get('type', '')).strip().lower()
                    if self.allowed_shape_types and stype not in self.allowed_shape_types:
                        continue
                    live_valid.append(drawing)
            if self.include_curve_fences and curve_tool and hasattr(curve_tool, 'get_curves_as_fences'):
                live_valid.extend(curve_tool.get_curves_as_fences())
            return live_valid

        def _refresh_if_needed():
            if not dlg.isVisible():
                return
            live_valid = _current_valid_fences()
            live_ids = {id(f) for f in live_valid}
            if live_ids == initial_source_ids:
                return
            try:
                dlg.close()
            except Exception:
                pass
            self.open_fence_picker()

        refresh_timer.setInterval(400)
        refresh_timer.timeout.connect(_refresh_if_needed)
        refresh_timer.start()
        dlg._fence_picker_refresh_timer = refresh_timer

        custom_rows   = []

        for idx, shape in enumerate(valid):
            stype  = shape.get('type', 'unknown')
            coords = self._shape_coords(shape)
            is_curve = shape.get('source') == 'curve_tool'
            icon   = self._SHAPE_ICONS.get(stype, '◆')
            
            # ✅ Different label for curves
            if is_curve:
                curve_idx = shape.get('curve_index', idx)
                title_text = f"〰️ Curve #{curve_idx + 1}"
                source_tag = "Curve Tool"
            else:
                title_text = f"{icon} #{idx + 1}: {stype.capitalize()}"
                source_tag = "Digitizer"
            
            try:
                arr  = np.array(coords)
                w    = arr[:, 0].max() - arr[:, 0].min()
                h    = arr[:, 1].max() - arr[:, 1].min()
                size = f"{w:.1f}×{h:.1f}m"
            except Exception:
                size = ""

            item_widget = QWidget()
            
            # ✅ Different background color for curves
            if is_curve:
                bg_color = ThemeColors.get('bg_secondary')
            else:
                bg_color = ThemeColors.get('bg_button')
            
            item_widget.setStyleSheet(f"background:{bg_color}; border-radius:5px;")
            ilay = QHBoxLayout(item_widget); ilay.setContentsMargins(6, 4, 6, 4)

            # Icon swatch
            swatch = QLabel(icon)
            swatch.setFixedSize(28, 28); swatch.setAlignment(Qt.AlignCenter)
            
            # ✅ Different swatch color for curves
            if is_curve:
                curve_color = shape.get('color', (0, 1, 0))
                swatch_color = f"rgb({int(curve_color[0]*255)}, {int(curve_color[1]*255)}, {int(curve_color[2]*255)})"
                swatch.setStyleSheet(
                    f"background:{swatch_color}; border-radius:4px; color:white;"
                    " font-size:14px; font-weight:bold;")
            else:
                swatch.setStyleSheet(
                    f"background:{ThemeColors.get('accent')}; border-radius:4px; color:{ThemeColors.get('text_on_active')};"
                    " font-size:14px; font-weight:bold;")
            ilay.addWidget(swatch)

            # Info column
            info_col = QVBoxLayout(); info_col.setSpacing(1)
            title_l = QLabel(title_text)
            title_l.setStyleSheet(f"color:{ThemeColors.get('text_primary')}; font-weight:bold; font-size:11px;")
            sub_l   = QLabel(f"{len(coords)} pts | {size} | {source_tag}")
            sub_l.setStyleSheet(f"color:{ThemeColors.get('text_muted')}; font-size:10px;")
            info_col.addWidget(title_l); info_col.addWidget(sub_l)
            ilay.addLayout(info_col, 1)

           # Selected badge
            badge = QLabel("Selected")
            badge.setStyleSheet(
                f"background:{ThemeColors.get('accent')}; color:{ThemeColors.get('text_on_active')}; font-weight:bold;"
                " font-size:10px; border-radius:4px; padding:3px 8px;")
            
            # Check if currently selected
            if is_curve:
                is_current = id(shape.get('curve_data', shape)) in current_ids
            else:
                is_current = id(shape) in current_ids
            badge.setVisible(is_current)

            cb = QCheckBox()
            cb.setStyleSheet(f"""
                QCheckBox::indicator {{ width:20px; height:20px; }}
                QCheckBox::indicator:unchecked {{
                    background:{ThemeColors.get('bg_button')}; border:2px solid {ThemeColors.get('border_light')}; border-radius:3px; }}
                QCheckBox::indicator:checked {{
                    background:{ThemeColors.get('accent')}; border:2px solid {ThemeColors.get('accent')}; border-radius:3px; }}
            """)

            _connect_fence_picker_checkbox(
                shape,
                badge,
                cb,
                item_widget,
                is_curve,
                on_toggle,
            )

            if is_current:
                cb.setChecked(True)

            ilay.addWidget(badge); ilay.addWidget(cb)

            def _hover_bind(shp, wgt):
                def _e(ev): on_hover(shp)
                def _l(ev): on_hover(None)
                wgt.enterEvent = _e; wgt.leaveEvent = _l
            _hover_bind(shape, item_widget)

            li = QListWidgetItem(fence_list)
            li.setSizeHint(item_widget.sizeHint())
            fence_list.setItemWidget(li, item_widget)
            custom_rows.append((cb, shape, is_curve))

        brow = QHBoxLayout()

        def do_sel_all():
            for cb_, _, _ in custom_rows: cb_.setChecked(True)
        sel_all_btn = QPushButton("Select All")
        sel_all_btn.clicked.connect(do_sel_all)

        def do_clr_all():
            for sid_, ac_ in list(picker_sel.items()): _rem(ac_)
            picker_sel.clear()
            for cb_, _, _ in custom_rows: cb_.setChecked(False)
            try: self.app.vtk_widget.render()
            except: pass
        clr_all_btn = QPushButton("Clear All")
        clr_all_btn.clicked.connect(do_clr_all)

        apply_btn = QPushButton("Apply Selection")
        apply_btn.setStyleSheet(
            "background:#2e7d32; color:white; font-weight:bold;"
            " border-radius:3px; padding:5px 16px;")

        def do_apply():
            on_hover(None)
            chosen = [shp for cb_, shp, _ in custom_rows if cb_.isChecked()]
            if not chosen:
                QMessageBox.warning(dlg, "No Selection", "Select at least one fence.")
                return
            if self.allow_permanent_mode and perm_chk is not None:
                self.permanent_fence_mode = perm_chk.isChecked()
            else:
                self.permanent_fence_mode = False
            if not self.permanent_fence_mode:
                self._remove_sel_actors()
                self.selected_fences = []
            
            # Build existing IDs set
            existing_ids = set()
            for f in self.selected_fences:
                if f.get('source') == 'curve_tool':
                    existing_ids.add(id(f.get('curve_data', f)))
                else:
                    existing_ids.add(id(f))
            

            for shp in chosen:
                # Determine unique ID
                if shp.get('source') == 'curve_tool':
                    fence_id = id(shp.get('curve_data', shp))
                else:
                    fence_id = id(shp)

                if fence_id not in existing_ids:
                    # Close open shapes for polygon masking — work on a COPY
                    # so the original digitizer/curve shape dict is never mutated
                    if shp.get('type') in [
                            'line', 'smart_line', 'polyline', 'smartline', 'curve']:
                        coords = shp.get('coords', [])
                        if len(coords) > 1:
                            arr = np.array(coords)
                            if not np.allclose(arr[0], arr[-1]):
                                shp = dict(shp)   # shallow copy — original untouched
                                shp['coords'] = list(arr) + [list(arr[0])]

                    self.selected_fences.append(shp)
                    existing_ids.add(fence_id)

            # Store picker selection actors under stable UUIDs
            import uuid as _uuid
            for sid_, ac_ in picker_sel.items():
                stable_key = str(_uuid.uuid4())
                self._sel_actors[stable_key] = ac_
                self._fence_id_to_uuid[sid_] = stable_key  # bridge: id→uuid
            picker_sel.clear()
            self._update_status()
            try: self.app.vtk_widget.render()
            except: pass
            dlg.close()

        apply_btn.clicked.connect(do_apply)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            "background:#555; color:white; border-radius:3px; padding:5px 16px;")

        def do_close():
            on_hover(None)
            for ac_ in list(picker_sel.values()): _rem(ac_)
            picker_sel.clear()
            try: self.app.vtk_widget.render()
            except: pass
            dlg.close()
        close_btn.clicked.connect(do_close)

        def _dlg_close_event(event):
            on_hover(None)
            for ac_ in list(picker_sel.values()): _rem(ac_)
            picker_sel.clear()
            try: self.app.vtk_widget.render()
            except: pass
            event.accept()
        dlg.closeEvent = _dlg_close_event

        brow.addWidget(sel_all_btn); brow.addWidget(clr_all_btn)
        brow.addStretch()
        brow.addWidget(apply_btn); brow.addWidget(close_btn)
        layout.addLayout(brow)
        # Non-modal: keep the main application interactive while picker is open.
        self._active_picker_dialog = dlg
        dlg.destroyed.connect(lambda *_: setattr(self, "_active_picker_dialog", None))
        dlg.show()
        if self.picker_as_tool_window:
            parent = self.window()
            parent_top_left = parent.mapToGlobal(parent.rect().topLeft())
            child_x = parent_top_left.x() + max(
                0, (parent.width() - dlg.width()) // 2
            )
            child_y = parent_top_left.y() + max(
                0, (parent.height() - dlg.height()) // 2
            )
            dlg.move(child_x, child_y)
            dlg.raise_()
            dlg.activateWindow()
        else:
            # Centre the embedded child panel inside the parent tool dialog.
            parent_rect = self.window().rect()
            child_x = max(0, (parent_rect.width() - dlg.width()) // 2)
            child_y = max(0, (parent_rect.height() - dlg.height()) // 2)
            dlg.move(child_x, child_y)

    def _update_status(self):
        """Update status label with fence counts."""
        if not self.selected_fences:
            self.status_lbl.setText(self.empty_status_text)
            self.status_lbl.setStyleSheet("color:#888; font-size:10px; padding:2px 0;")
            return
        
        digitizer_count = sum(1 for f in self.selected_fences 
                             if f.get('source') != 'curve_tool')
        curve_count = sum(1 for f in self.selected_fences 
                         if f.get('source') == 'curve_tool')
        
        parts = []
        if digitizer_count:
            parts.append(f"📐 {digitizer_count} shape(s)")
        if curve_count:
            parts.append(f"〰️ {curve_count} curve(s)")
        
        text = " + ".join(parts) + " selected"
        self.status_lbl.setText(text)
        self.status_lbl.setStyleSheet("color:#4fc3f7; font-size:10px; padding:2px 0; font-weight:bold;")

    def clear_fences(self):
        """Clear all fence selections and actors."""
        if self._conversion_completed:
            # After classification, show the "Clear Classified Fences" dialog
            # so the user can choose which classified fences to remove.
            digitizer = getattr(self.app, 'digitizer', None)
            if digitizer is None:
                self._clear_selected_fences_only()
                return
            classified_fences = [
                d for d in getattr(digitizer, 'drawings', [])
                if d.get('classified_fence', False)
            ]
            if classified_fences:
                self._show_clear_fence_dialog(digitizer, classified_fences)
            else:
                # No classified fences found — just reset normally
                self._conversion_completed = False
                self._remove_hover()
                self._remove_sel_actors()
                self.selected_fences = []
                self._update_status()
                try:
                    self.app.vtk_widget.render()
                except Exception:
                    pass
            return
        self._remove_hover()
        self._remove_sel_actors()
        self.selected_fences = []
        self._update_status()
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────
    # CLEAR CLASSIFIED FENCES DIALOG
    # ──────────────────────────────────────────────────────────────

    def _get_clear_fence_dialog(self):
        dialog = getattr(self, '_clear_fence_dialog_ref', None)
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
            if widget:
                widget.deleteLater()

    def _create_clear_fence_dialog(self):
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QLabel,
            QScrollArea, QPushButton,
        )
        from PySide6.QtCore import Qt
        try:
            from gui.theme_manager import get_dialog_stylesheet, ThemeColors
        except Exception:
            get_dialog_stylesheet = lambda: ""
            class ThemeColors:
                @staticmethod
                def get(*args):
                    return args[1] if len(args) > 1 else "#888"

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
            f"color: {ThemeColors.get('danger', '#e74c3c')}; font-weight: bold; font-size: 13px; padding: 8px;"
        )
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QLabel("Unchecked fences will be kept. Checked fences will be removed.")
        subtitle.setStyleSheet(
            f"color: {ThemeColors.get('text_secondary', '#aaa')}; font-size: 10px; padding: 0 8px 8px 8px;"
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
            lambda: [cb.setChecked(True) for cb, _ in getattr(dialog, '_classified_fence_checkboxes', [])]
        )
        btn_row.addWidget(select_all_btn)

        clear_all_btn = QPushButton("Clear All")
        clear_all_btn.clicked.connect(
            lambda: [cb.setChecked(False) for cb, _ in getattr(dialog, '_classified_fence_checkboxes', [])]
        )
        btn_row.addWidget(clear_all_btn)

        btn_row.addStretch()

        unselect_btn = QPushButton("Unselect (Keep Fences)")
        unselect_btn.setToolTip(
            "Remove this tool's selection without deleting digitized fences"
        )
        unselect_btn.clicked.connect(
            lambda: self._clear_selected_fences_only(dialog)
        )
        btn_row.addWidget(unselect_btn)

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
        dialog.destroyed.connect(lambda: setattr(self, '_clear_fence_dialog_ref', None))
        dialog.finished.connect(lambda *_: self._clear_clear_fence_hover(dialog))
        return dialog

    def _clear_selected_fences_only(self, dialog=None):
        """Drop the active tool selection while preserving source drawings."""
        self._remove_hover()
        self._remove_sel_actors()
        self.selected_fences = []
        self._conversion_completed = False
        self._update_status()
        try:
            self.app.vtk_widget.render()
        except Exception:
            pass
        if dialog is not None:
            self._clear_clear_fence_hover(dialog)
            dialog.hide()

    def _coords_from_clear_fence(self, fence):
        if not isinstance(fence, dict):
            return []

        dtype = str(fence.get('type', '') or '').lower()
        if dtype == 'circle':
            center = fence.get('center')
            radius = fence.get('radius')
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
        for point in fence.get('coords', []) or []:
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

            if digitizer is not None and hasattr(digitizer, '_build_styled_polydata_world'):
                poly = digitizer._build_styled_polydata_world(coords, line_style='solid')
                origin = getattr(poly, '_world_origin', np.zeros(3, dtype=np.float64))
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
            if getattr(digitizer, 'overlay_renderer', None) is not None:
                return digitizer.overlay_renderer
            if hasattr(digitizer, '_ensure_overlay_renderer'):
                try:
                    return digitizer._ensure_overlay_renderer()
                except Exception:
                    pass
            return getattr(digitizer, 'renderer', None)
        return None

    def _render_clear_fence_hover(self, digitizer):
        try:
            if digitizer is not None and getattr(digitizer, 'interactor', None) is not None:
                digitizer.interactor.GetRenderWindow().Render()
        except Exception:
            pass
        try:
            if hasattr(self.app, 'vtk_widget'):
                self.app.vtk_widget.render()
        except Exception:
            pass

    def _clear_clear_fence_hover(self, dialog):
        actor = getattr(dialog, '_classified_fence_hover_actor', None)
        if actor is None:
            return

        digitizer = getattr(dialog, '_classified_fence_digitizer', None)
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

        digitizer = getattr(dialog, '_classified_fence_digitizer', None)
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
        from PySide6.QtWidgets import QLabel, QWidget, QHBoxLayout, QCheckBox, QFrame
        from PySide6.QtCore import Qt
        try:
            from gui.theme_manager import ThemeColors
        except Exception:
            class ThemeColors:
                @staticmethod
                def get(*args):
                    return args[1] if len(args) > 1 else "#888"

        shape_icons = {
            'rectangle': '▭', 'circle': '○', 'polygon': '⬟',
            'polyline': '⬡', 'line': '─', 'smartline': '⚡',
            'smart_line': '⚡', 'centerline': '≋', 'freehand': '✏️',
        }

        scroll_layout = getattr(dialog, '_classified_fence_scroll_layout', None)
        if scroll_layout is None:
            return

        self._clear_layout_widgets(scroll_layout)
        dialog._classified_fence_digitizer = digitizer
        dialog._classified_fence_checkboxes = []

        if not classified_fences:
            empty_label = QLabel("No classified fences available.")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet(
                f"color: {ThemeColors.get('text_secondary', '#aaa')}; font-size: 11px; padding: 20px;"
            )
            scroll_layout.addWidget(empty_label)
            scroll_layout.addStretch()
            return

        for idx, fence in enumerate(classified_fences):
            shape_type = fence.get('type', 'unknown')
            coords = fence.get('coords', [])
            icon = shape_icons.get(shape_type, '◆')
            coord_count = len(coords)

            if coord_count > 0:
                try:
                    pts = np.array(coords)
                    min_pt = pts.min(axis=0)
                    max_pt = pts.max(axis=0)
                    width = max_pt[0] - min_pt[0]
                    height = max_pt[1] - min_pt[1]
                    size_text = f"{width:.1f}x{height:.1f}m"
                except Exception:
                    size_text = "?"
            else:
                size_text = "?"

            row = QWidget()
            row.setStyleSheet(
                f"""QWidget {{
                    background-color: {ThemeColors.get('bg_button', '#2a2a2a')};
                    border: 1px solid {ThemeColors.get('border', '#444')};
                    border-radius: 6px; padding: 6px;
                }}
                QWidget:hover {{
                    background-color: {ThemeColors.get('bg_button_hover', '#333')};
                    border: 1px solid {ThemeColors.get('border_light', '#555')};
                }}"""
            )
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)

            checkbox = QCheckBox()
            checkbox.setStyleSheet(
                f"""QCheckBox::indicator {{
                    width: 18px; height: 18px;
                    border-radius: 3px; border: 1px solid {ThemeColors.get('border_light', '#555')};
                    background-color: {ThemeColors.get('bg_input', '#1e1e1e')};
                }}
                QCheckBox::indicator:checked {{
                    background-color: {ThemeColors.get('danger', '#e74c3c')};
                    border: 1px solid {ThemeColors.get('danger', '#e74c3c')};
                }}"""
            )
            row_layout.addWidget(checkbox)

            indicator = QFrame()
            indicator.setFixedSize(8, 30)
            indicator.setStyleSheet(
                f"background-color: {ThemeColors.get('accent', '#4fc3f7')}; border-radius: 2px;"
            )
            row_layout.addWidget(indicator)

            label = QLabel(
                f"{icon} #{idx + 1}: {shape_type.capitalize()}\n{coord_count} pts | {size_text}"
            )
            label.setStyleSheet(
                f"color: {ThemeColors.get('text_primary', '#fff')}; font-size: 10px;"
                f" background: transparent; padding-left: 4px;"
            )
            row_layout.addWidget(label, 1)

            def make_click(cb):
                def on_click(*_):
                    cb.setChecked(not cb.isChecked())
                return on_click

            row.mousePressEvent = make_click(checkbox)
            row.enterEvent = lambda event, f=fence: self._show_clear_fence_hover(dialog, f)
            row.leaveEvent = lambda event: self._clear_clear_fence_hover(dialog)
            row.setCursor(Qt.PointingHandCursor)

            dialog._classified_fence_checkboxes.append((checkbox, fence))
            scroll_layout.addWidget(row)

        scroll_layout.addStretch()

    def _delete_checked_classified_fences(self, dialog):
        digitizer = getattr(dialog, '_classified_fence_digitizer', None)
        checkboxes = list(getattr(dialog, '_classified_fence_checkboxes', []) or [])
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
            except Exception as e:
                print(f"⚠️ Failed to delete fence: {e}")

        # Purge deleted fences from our own selected_fences list
        try:
            removed_ids = {id(f) for f in fences_to_delete}

            def _coords_key(shape):
                try:
                    return tuple(tuple(c[:2]) for c in (shape.get('coords', []) or []))
                except Exception:
                    return ()

            removed_keys = {_coords_key(f) for f in fences_to_delete}
            removed_actor_ids = {
                id(f.get('actor')) for f in fences_to_delete
                if isinstance(f, dict) and f.get('actor') is not None
            }

            kept = []
            for fence in self.selected_fences:
                drop = (
                    id(fence) in removed_ids
                    or _coords_key(fence) in removed_keys
                    or (
                        isinstance(fence, dict)
                        and fence.get('actor') is not None
                        and id(fence.get('actor')) in removed_actor_ids
                    )
                )
                if not drop:
                    kept.append(fence)
            self.selected_fences = kept
        except Exception as e:
            print(f"⚠️ Fence list cleanup: {e}")

        # Reset classification-completed flag so the widget is reusable
        self._conversion_completed = False
        self._update_status()

        try:
            if hasattr(digitizer, 'overlay_renderer') and digitizer.overlay_renderer:
                digitizer.overlay_renderer.Modified()
            digitizer.renderer.Modified()
            digitizer.interactor.GetRenderWindow().Render()
            self.app.vtk_widget.render()
        except Exception:
            pass

        print(f"✅ Deleted {len(fences_to_delete)} classified fence(s)")
        dialog.hide()

    def _mark_fences_as_classified(self):
        """Mark selected fences as classified without clearing the selection."""
        digitizer = getattr(self.app, 'digitizer', None)
        if digitizer is None or not self.selected_fences:
            return

        drawings = list(getattr(digitizer, 'drawings', []) or [])

        def _coords_key(shape):
            try:
                pts = [tuple(c[:2]) for c in (shape.get('coords', []) or [])]
                if len(pts) > 1 and pts[0] == pts[-1]:
                    pts = pts[:-1]
                return tuple(pts)
            except Exception:
                return ()

        drawing_by_id = {id(d): d for d in drawings}
        drawing_by_actor = {}
        drawing_by_key = {}
        for d in drawings:
            actor = d.get('actor')
            if actor is not None:
                drawing_by_actor[id(actor)] = d
            key = _coords_key(d)
            if key:
                drawing_by_key[key] = d

        for fence in self.selected_fences:
            drawing = drawing_by_id.get(id(fence))
            if drawing is None:
                actor = fence.get('actor') if isinstance(fence, dict) else None
                if actor is not None:
                    drawing = drawing_by_actor.get(id(actor))
            if drawing is None:
                key = _coords_key(fence) if isinstance(fence, dict) else ()
                if key:
                    drawing = drawing_by_key.get(key)
            if drawing is None:
                drawing = fence

            for obj in (drawing, fence):
                if isinstance(obj, dict):
                    obj['classified_fence'] = True
                    obj['source'] = obj.get('source') or 'digitizer'

        # Keep the exact selection and its highlight active after a run.  This
        # makes repeated parameter tuning deterministic; only Select/Clear or
        # closing the parent tool changes the operator's chosen area.
        self._update_status()

        try:
            digitizer.interactor.GetRenderWindow().Render()
        except Exception:
            pass

    def _show_clear_fence_dialog(self, digitizer, classified_fences):
        dialog = self._get_clear_fence_dialog()
        if dialog is None:
            dialog = self._create_clear_fence_dialog()
            self._clear_fence_dialog_ref = dialog
        self._populate_clear_fence_dialog(dialog, digitizer, classified_fences)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        
# ═══════════════════════════════════════════════════════════════════════
# BACKGROUND WORKER
# ═══════════════════════════════════════════════════════════════════════
class _ClassificationWorker(QThread):
    progress = Signal(int, str)
    result_ready = Signal(object)
    error    = Signal(str)

    def __init__(self, func, kwargs):
        super().__init__()
        self.func = func; self.kwargs = kwargs; self._abort = False

    def abort(self): self._abort = True

    def run(self):
        try:
            result = self.func(progress_cb=self.progress.emit,
                               abort_check=lambda: self._abort,
                               **self.kwargs)
            self.result_ready.emit(result)
        except Exception as e:
            import traceback; traceback.print_exc(); self.error.emit(str(e))


# ═══════════════════════════════════════════════════════════════════════
# ALGORITHM 1 — CLASSIFY LOW POINTS
# TerraScan-compatible: local minima among source points in a 2D radius
# ═══════════════════════════════════════════════════════════════════════

def classify_low_points(xyz, classification, from_classes, to_class,
                        ground_classes=None,
                        fence_mask=None, search_mode="groups",
                        max_count=6, more_than=0.50, within=5.0,
                        progress_cb=None, abort_check=None,
                        batch_size=10_000):
    """
    Classify source points/groups that form the lowest local elevation tier.

    This follows TerraScan's documented Low Points conditions:
    source classes, maximum group count, minimum vertical separation, a 2D
    search radius, and an optional fence.  ``ground_classes`` remains in the
    signature only for backward call compatibility and is intentionally
    ignored; this routine does not compare against a ground surface.

    In group mode, the exact proprietary grouping implementation is not
    published.  The compatible rule used here finds the lowest elevation tier
    in each 2D neighborhood, separated from all higher tiers by ``more_than``,
    and accepts it only when its size does not exceed ``max_count``.
    """
    import time as _t

    xyz = np.asarray(xyz)
    classification = np.asarray(classification)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise ValueError("xyz must be an N×3 point array")
    if len(xyz) != len(classification):
        raise ValueError("xyz and classification must have the same length")

    dz = float(more_than)
    radius = float(within)
    group_limit = max(1, min(int(max_count), 99))
    chunk = max(1, int(batch_size))
    mode = "single" if str(search_mode).lower().startswith("single") else "groups"
    if dz < 0:
        raise ValueError("'More than' must be zero or positive for Low Points")
    if radius <= 0:
        raise ValueError("'Within' must be greater than zero")

    def _log(pct, msg):
        print(f"[LowPts {pct:3d}%] {msg}", flush=True)
        if progress_cb:
            progress_cb(pct, msg)

    _log(0, f"mode={mode}  separation>{dz}m  "
            f"2D radius={radius}m  max_group={group_limit}  "
            f"fence={'yes' if fence_mask is not None else 'no'}")

    any_source = (
        from_classes is None
        or (
            isinstance(from_classes, (list, tuple, np.ndarray))
            and (len(from_classes) == 0 or None in from_classes)
        )
    )
    finite_mask = np.all(np.isfinite(xyz[:, :3]), axis=1)
    if any_source:
        source_mask = finite_mask.copy()
    else:
        source_mask = np.isin(classification, from_classes) & finite_mask

    # A fence restricts which points may be changed. Source points just outside
    # it still provide surrounding context, preventing artificial edge noise.
    cand_mask = source_mask.copy()
    if fence_mask is not None:
        fence = np.asarray(fence_mask, dtype=bool)
        if len(fence) != len(classification):
            raise ValueError("fence_mask must match the point count")
        cand_mask &= fence

    source_idx = np.flatnonzero(source_mask)
    candidate_idx = np.where(cand_mask)[0]
    _log(
        2,
        f"Candidates: {len(candidate_idx):,}; "
        f"surrounding source points: {len(source_idx):,}",
    )
    if not len(candidate_idx) or len(source_idx) < 2:
        _log(100, "No candidates — nothing to do")
        return {
            "routine": "low_points",
            "changed": 0,
            "indices": np.array([], dtype=np.intp),
            "low_candidates": int(len(candidate_idx)),
            "evaluated_with_surrounding": 0,
            "low_accepted": 0,
            "rejected_no_surrounding": int(len(candidate_idx)),
            "rejected_not_low": 0,
            "search_mode": mode,
            "max_count": group_limit,
            "more_than": dz,
            "within": radius,
            "stop_reason": "fewer than 2 source points",
        }

    _log(5, f"Building 2D source KD-tree ({len(source_idx):,} pts) …")
    t0 = _t.time()
    tree = cKDTree(xyz[source_idx, :2])
    _log(10, f"2D source tree ready ({_t.time()-t0:.2f}s)")

    # A radius query returns Python lists, so cap each batch by an estimated
    # neighborhood budget. This prevents dense 32M-point clouds from producing
    # a multi-gigabyte temporary list while retaining large batches in sparse
    # data.
    sample_count = min(256, len(candidate_idx))
    sample_positions = np.linspace(
        0,
        len(candidate_idx) - 1,
        sample_count,
        dtype=np.intp,
    )
    sample_sizes = np.asarray(
        tree.query_ball_point(
            xyz[candidate_idx[sample_positions], :2],
            radius,
            return_length=True,
            workers=-1,
        ),
        dtype=np.intp,
    )
    estimated_neighbors = max(
        1,
        int(np.percentile(sample_sizes, 90)),
    )
    neighborhood_budget = 8_000_000
    chunk = min(
        chunk,
        max(16, neighborhood_budget // estimated_neighbors),
    )
    _log(
        12,
        f"Memory-safe batch={chunk:,}; estimated dense neighborhood="
        f"{estimated_neighbors:,} points",
    )

    accepted_mask = np.zeros(len(classification), dtype=bool)
    candidate_allowed = cand_mask
    no_surrounding = 0
    evaluated = 0
    aborted = False
    last_reported_pct = 12

    for start in range(0, len(candidate_idx), chunk):
        if abort_check and abort_check():
            aborted = True
            break

        stop = min(start + chunk, len(candidate_idx))
        query_global = candidate_idx[start:stop]
        neighborhoods = tree.query_ball_point(
            xyz[query_global, :2],
            radius,
            workers=-1,
        )

        for center_global, local_neighbors in zip(
            query_global, neighborhoods
        ):
            if len(local_neighbors) <= 1:
                no_surrounding += 1
                continue

            neighbor_global = source_idx[
                np.asarray(local_neighbors, dtype=np.intp)
            ]
            other_global = neighbor_global[
                neighbor_global != center_global
            ]
            if len(other_global) == 0:
                no_surrounding += 1
                continue

            evaluated += 1
            if mode == "single":
                if (
                    float(np.min(xyz[other_global, 2]))
                    - float(xyz[center_global, 2])
                    > dz
                ):
                    accepted_mask[center_global] = True
                continue

            # The first clear vertical gap above the lowest tier represents
            # the lowest point group found by this run.
            order = np.argsort(
                xyz[neighbor_global, 2],
                kind="stable",
            )
            sorted_global = neighbor_global[order]
            sorted_z = xyz[sorted_global, 2]
            gaps = np.diff(sorted_z)
            clear_gaps = np.flatnonzero(gaps > dz)
            if len(clear_gaps) == 0:
                continue
            low_count = int(clear_gaps[0]) + 1
            if low_count > group_limit:
                continue
            low_tier = sorted_global[:low_count]
            if np.any(low_tier == center_global):
                accepted_mask[
                    low_tier[candidate_allowed[low_tier]]
                ] = True

        pct = min(
            95,
            12 + int(83 * stop / max(1, len(candidate_idx))),
        )
        if pct >= last_reported_pct + 2 or stop == len(candidate_idx):
            _log(
                pct,
                f"Checked {stop:,}/{len(candidate_idx):,}; "
                f"accepted {int(np.count_nonzero(accepted_mask)):,}",
            )
            last_reported_pct = pct

    detected = np.flatnonzero(accepted_mask).astype(np.intp, copy=False)
    flagged = detected[
        classification[detected] != int(to_class)
    ]
    classification[flagged] = to_class
    stop_reason = "aborted" if aborted else "completed one lowest-tier pass"
    _log(100, f"Done - {len(flagged):,} low points -> class {to_class}")
    return {
        "routine": "low_points",
        "changed": int(len(flagged)),
        "indices": flagged,
        "low_candidates": int(len(candidate_idx)),
        "evaluated_with_surrounding": int(evaluated),
            "low_accepted": int(len(flagged)),
            "low_detected": int(len(detected)),
        "rejected_no_surrounding": int(no_surrounding),
        "rejected_not_low": int(max(0, evaluated - len(flagged))),
        "search_mode": mode,
        "max_count": group_limit,
        "more_than": dz,
        "within": radius,
        "stop_reason": stop_reason,
    }


# ═══════════════════════════════════════════════════════════════════════
# ALGORITHM 2 — CLASSIFY ISOLATED POINTS
# TerraScan-compatible: 3D sphere, self-excluded, From and In may differ
# ═══════════════════════════════════════════════════════════════════════
def classify_isolated_points(xyz, classification, from_classes, to_class,
                             in_classes, if_fewer_than=1, within=5.0,
                             fence_mask=None, iterative=False,
                             max_iterations=10,
                             height_from_ground=None,
                             ground_classes=None,
                             progress_cb=None, abort_check=None,
                             batch_size=500_000):
    """
    Classify points with fewer than the requested other points in a 3D sphere.

    ``iterative``, ``max_iterations``, ``height_from_ground`` and
    ``ground_classes`` remain accepted for API compatibility but are not part
    of TerraScan's Isolated Points routine and therefore do not alter this
    single-pass classification.
    """
    xyz = np.asarray(xyz)
    classification = np.asarray(classification)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise ValueError("xyz must be an N×3 point array")
    if len(xyz) != len(classification):
        raise ValueError("xyz and classification must have the same length")

    limit = max(1, int(if_fewer_than))
    radius = float(within)
    chunk = max(1, int(batch_size))
    if radius <= 0:
        raise ValueError("'Within' must be greater than zero")

    any_source = (
        from_classes is None
        or (
            isinstance(from_classes, (list, tuple, np.ndarray))
            and (len(from_classes) == 0 or None in from_classes)
        )
    )
    finite_mask = np.all(np.isfinite(xyz[:, :3]), axis=1)
    cand_mask = (
        finite_mask.copy()
        if any_source
        else np.isin(classification, from_classes) & finite_mask
    )
    if fence_mask is not None:
        fence = np.asarray(fence_mask, dtype=bool)
        if len(fence) != len(classification):
            raise ValueError("fence_mask must match the point count")
        cand_mask &= fence
    candidate_idx = np.flatnonzero(cand_mask)

    any_in = (
        in_classes is None
        or (
            isinstance(in_classes, (list, tuple, np.ndarray))
            and (len(in_classes) == 0 or None in in_classes)
        )
    )
    in_mask = (
        finite_mask.copy()
        if any_in
        else np.isin(classification, in_classes) & finite_mask
    )
    # Only affected candidates are fence-limited. Points immediately outside
    # the fence remain valid neighborhood evidence.
    in_idx = np.flatnonzero(in_mask)

    def _result(indices, counts, stop_reason, detected_count=None):
        indices = np.asarray(indices, dtype=np.intp)
        if detected_count is None:
            detected_count = len(indices)
        return {
            "routine": "isolated_points",
            "changed": int(len(indices)),
            "indices": indices,
            "isolated_candidates": int(len(candidate_idx)),
            "neighbor_reference_points": int(len(in_idx)),
            "isolated_accepted": int(len(indices)),
            "isolated_detected": int(detected_count),
            "rejected_by_neighbor_count": int(
                max(0, len(candidate_idx) - int(detected_count))
            ),
            "if_fewer_than": limit,
            "within": radius,
            "min_neighbor_count": (
                int(np.min(counts)) if len(counts) else None
            ),
            "median_neighbor_count": (
                float(np.median(counts)) if len(counts) else None
            ),
            "max_neighbor_count": (
                int(np.max(counts)) if len(counts) else None
            ),
            "stop_reason": stop_reason,
        }

    if progress_cb:
        progress_cb(
            0,
            f"Candidates={len(candidate_idx):,}; In-class context="
            f"{len(in_idx):,}; fewer than {limit} within {radius:g} m (3D)",
        )
    if not len(candidate_idx):
        if progress_cb:
            progress_cb(100, "No source candidates")
        return _result([], np.array([], dtype=np.intp), "no source candidates")

    # No In-class points means every candidate has zero qualifying neighbors.
    if not len(in_idx):
        counts = np.array([0], dtype=np.intp)
        detected = (
            candidate_idx
            if limit > 0
            else np.array([], dtype=np.intp)
        )
        flagged = detected[
            classification[detected] != int(to_class)
        ]
        classification[flagged] = to_class
        if progress_cb:
            progress_cb(
                100,
                f"Done - {len(flagged):,} pts -> class {to_class}; "
                "no In-class neighbors exist",
            )
        return _result(
            flagged,
            counts,
            "completed one 3D count pass",
            detected_count=len(detected),
        )

    if progress_cb:
        progress_cb(
            5,
            f"Building 3D In-class KD-tree ({len(in_idx):,} points) …",
        )
    tree3d = cKDTree(xyz[in_idx, :3])
    all_counts = np.empty(len(candidate_idx), dtype=np.int32)
    isolated_mask = np.zeros(len(candidate_idx), dtype=bool)
    processed = 0
    aborted = False

    for start in range(0, len(candidate_idx), chunk):
        if abort_check and abort_check():
            aborted = True
            break
        stop = min(start + chunk, len(candidate_idx))
        current = candidate_idx[start:stop]
        counts = np.asarray(
            tree3d.query_ball_point(
                xyz[current, :3],
                radius,
                return_length=True,
                workers=-1,
            ),
            dtype=np.intp,
        )
        # If the candidate itself belongs to In class, it is returned by the
        # sphere query but TerraScan's condition counts only "other" points.
        counts -= in_mask[current].astype(np.intp)
        all_counts[start:stop] = counts
        isolated_mask[start:stop] = counts < limit
        processed = stop
        if progress_cb:
            progress_cb(
                min(95, 5 + int(90 * stop / len(candidate_idx))),
                f"Counted {stop:,}/{len(candidate_idx):,} candidates",
            )

    all_counts = all_counts[:processed]
    detected = candidate_idx[:processed][isolated_mask[:processed]]
    detected = detected.astype(np.intp, copy=False)
    flagged = detected[
        classification[detected] != int(to_class)
    ]
    classification[flagged] = to_class
    stop_reason = "aborted" if aborted else "completed one 3D count pass"
    if progress_cb:
        progress_cb(100, f"Done - {len(flagged):,} pts -> class {to_class}")
    return _result(
        flagged,
        all_counts,
        stop_reason,
        detected_count=len(detected),
    )


# ═══════════════════════════════════════════════════════════════════════
# ALGORITHM 3 — CLASSIFY GROUND (PTD — Axelsson 2000, vectorized)
# ═══════════════════════════════════════════════════════════════════════
def _select_ground_class0_holdout(
    xyz,
    accepted_indices,
    percent,
    *,
    protected_indices=None,
):
    """
    Select a deterministic, spatially distributed QC sample for class 0.

    Initial TIN seeds can be protected so density control never removes the
    anchor points that define the accepted terrain surface.
    """
    accepted = np.asarray(accepted_indices, dtype=np.intp)
    pct = max(0.0, min(float(percent or 0.0), 10.0))
    target = int(np.floor(len(accepted) * pct / 100.0))
    if target <= 0 or len(accepted) == 0:
        return np.array([], dtype=np.intp)

    eligible = accepted
    if protected_indices is not None:
        protected = np.asarray(protected_indices, dtype=np.intp)
        if len(protected):
            eligible = eligible[~np.isin(eligible, protected)]
    if len(eligible) == 0:
        return np.array([], dtype=np.intp)

    target = min(target, len(eligible))
    coords = np.asarray(xyz)[eligible, :2]
    # Stable XY ordering plus evenly spaced picks avoids random clusters and
    # makes undo/re-run results reproducible.
    order = np.lexsort((coords[:, 0], coords[:, 1]))
    positions = np.floor(
        (np.arange(target, dtype=np.float64) + 0.5) * len(order) / target
    ).astype(np.intp)
    positions = np.clip(positions, 0, len(order) - 1)
    return eligible[order[positions]]


def _subsample_ground_tin_points(points, ground_local_indices, max_points=400_000):
    """Return aligned local indices and coordinates for a bounded TIN build."""
    ground_indices = np.asarray(ground_local_indices, dtype=np.intp)
    limit = max(3, int(max_points))
    if len(ground_indices) > limit:
        step = int(np.ceil(len(ground_indices) / limit))
        ground_indices = ground_indices[::step]
    return ground_indices, np.asarray(points)[ground_indices]


def _bounded_ground_seed_indices(points, ground_local_indices, max_points=150_000):
    """Deterministically retain spatially distributed existing-ground anchors."""
    indices = np.asarray(ground_local_indices, dtype=np.intp)
    limit = max(3, int(max_points))
    if len(indices) <= limit:
        return indices

    # Bound the ordering work as well as the final TIN. Existing LAZ/LAS point
    # order is deterministic; an even stride retains coverage while preventing
    # a multi-million-row lexsort just to create a 150k anchor surface.
    prelimit = limit * 4
    if len(indices) > prelimit:
        stride = int(np.ceil(len(indices) / prelimit))
        indices = indices[::stride]

    xy = np.asarray(points)[indices, :2]
    x_min = float(np.min(xy[:, 0])); y_min = float(np.min(xy[:, 1]))
    x_span = max(float(np.ptp(xy[:, 0])), 1e-9)
    y_span = max(float(np.ptp(xy[:, 1])), 1e-9)
    side = max(1, int(np.sqrt(limit)))
    gx = np.minimum(((xy[:, 0] - x_min) * side / x_span).astype(np.int64), side - 1)
    gy = np.minimum(((xy[:, 1] - y_min) * side / y_span).astype(np.int64), side - 1)
    cell = gx * side + gy

    # Lowest existing-ground point in each spatial cell is a stable terrain
    # anchor. Fill any unused budget with an even deterministic stride.
    z = np.asarray(points)[indices, 2]
    order = np.lexsort((indices, z, cell))
    ordered_cell = cell[order]
    first = np.empty(len(order), dtype=bool)
    first[0] = True
    first[1:] = ordered_cell[1:] != ordered_cell[:-1]
    selected = indices[order[first]]
    if len(selected) < limit:
        remaining = indices[::max(1, int(np.ceil(len(indices) / (limit - len(selected)))))]
        selected = np.union1d(selected, remaining)[:limit]
    return np.asarray(selected[:limit], dtype=np.intp)


GROUND_TERRAIN_PRESETS = {
    "flat_urban": {
        "terrain_angle": 88.0,
        "iteration_angle": 4.0,
        "iteration_distance": 0.50,
    },
    "rolling": {
        "terrain_angle": 35.0,
        "iteration_angle": 6.0,
        "iteration_distance": 0.80,
    },
    "hilly": {
        "terrain_angle": 55.0,
        "iteration_angle": 8.0,
        "iteration_distance": 1.10,
    },
    "mountain": {
        "terrain_angle": 75.0,
        "iteration_angle": 10.0,
        "iteration_distance": 1.40,
    },
}


def _estimate_adaptive_ground_thresholds(seed_points):
    """
    Derive conservative PTD thresholds from a sparse local-low seed surface.

    The robust 90th-percentile triangle slope avoids letting isolated extreme
    triangles dictate the entire classification while still adapting to hilly
    and mountainous terrain.
    """
    points = np.asarray(seed_points, dtype=np.float64)
    fallback = {
        "estimated_slope": None,
        "terrain_angle": 88.0,
        "iteration_angle": 6.0,
        "iteration_distance": 1.0,
    }
    if points.ndim != 2 or points.shape[1] < 3 or len(points) < 3:
        return fallback

    finite = np.all(np.isfinite(points[:, :3]), axis=1)
    points = points[finite, :3]
    if len(points) < 3:
        return fallback

    if len(points) > 100_000:
        step = int(np.ceil(len(points) / 100_000))
        points = points[::step]
    if np.ptp(points[:, 0]) <= 1e-12 or np.ptp(points[:, 1]) <= 1e-12:
        return fallback

    try:
        tri = Delaunay(points[:, :2])
        simplex_points = points[tri.simplices]
        edge_a = simplex_points[:, 1] - simplex_points[:, 0]
        edge_b = simplex_points[:, 2] - simplex_points[:, 0]
        normals = np.cross(edge_a, edge_b)
        lengths = np.linalg.norm(normals, axis=1)
        valid = lengths > 1e-12
        if not np.any(valid):
            return fallback
        nz = np.abs(normals[valid, 2]) / lengths[valid]
        slopes = np.degrees(np.arccos(np.clip(nz, 0.0, 1.0)))
        slopes = slopes[np.isfinite(slopes)]
        if not len(slopes):
            return fallback
        robust_slope = float(np.percentile(slopes, 90.0))
    except Exception:
        return fallback

    terrain_angle = float(np.clip(robust_slope + 12.0, 15.0, 88.0))
    iteration_angle = float(np.clip(4.0 + 6.0 * robust_slope / 45.0, 4.0, 10.0))
    iteration_distance = float(
        np.clip(0.50 + 0.90 * robust_slope / 45.0, 0.50, 1.40)
    )
    return {
        "estimated_slope": robust_slope,
        "terrain_angle": terrain_angle,
        "iteration_angle": iteration_angle,
        "iteration_distance": iteration_distance,
    }


def classify_ground_ptd(xyz, classification, from_classes, to_class,
                        current_ground=2, seed_method="aerial_low_ground",
                        max_building_size=60.0, terrain_angle=88.0,
                        iteration_angle=6.0, iteration_distance=1.40,
                        reduce_angle_edge=True, edge_length_threshold=5.0,
                        stop_triangulation=False, stop_edge_length=2.0,
                        use_distance_as_rating=False, distance_weight=50.0,
                        add_only_upward=False, class0_holdout_percent=0.0,
                        adaptive_thresholds=False, fence_mask=None,
                        progress_cb=None, abort_check=None):
    xyz = np.asarray(xyz)
    classification = np.asarray(classification)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise ValueError("xyz must be an N×3 point array")
    if len(xyz) != len(classification):
        raise ValueError("xyz and classification must have the same length")
    finite_mask = np.all(np.isfinite(xyz[:, :3]), axis=1)

    # Handle "Any Class" selection
    if from_classes is None or (isinstance(from_classes, list) and (not from_classes or None in from_classes)):
        from_mask = finite_mask.copy()
    else:
        from_mask = np.isin(classification, from_classes) & finite_mask

    if current_ground is None or (isinstance(current_ground, list) and (not current_ground or None in current_ground)):
        cg_mask = finite_mask.copy()
    else:
        cg_mask = np.isin(classification, current_ground) & finite_mask

    if fence_mask is not None:
        fence = np.asarray(fence_mask, dtype=bool)
        if len(fence) != len(classification):
            raise ValueError("fence_mask must match the point count")
        from_mask &= fence
        cg_mask   &= fence
    work_idx = np.where(from_mask | cg_mask)[0]
    if not len(work_idx):
        return {
            "changed": 0,
            "indices": np.array([], dtype=np.intp),
            "source_candidates": 0,
            "accepted_candidates": 0,
            "ground_count": 0,
            "class0_count": 0,
            "stop_reason": "no eligible points",
        }
    pts = xyz[work_idx]; n = len(pts)
    is_from_local = from_mask[work_idx]; is_cg_local = cg_mask[work_idx]

    if progress_cb: progress_cb(2, "Phase 1 — Seeds …")
    seed_local = set()
    cell_seed_local = set()

    def _cell_seeds(add_to_ground=True):
        xmn,ymn = pts[:,0].min(),pts[:,1].min()
        xmx,ymx = pts[:,0].max(),pts[:,1].max()
        cell=max_building_size
        nx=max(1,int(np.ceil((xmx-xmn)/cell))); ny=max(1,int(np.ceil((ymx-ymn)/cell)))
        cx=np.clip(((pts[:,0]-xmn)/cell).astype(int),0,nx-1)
        cy=np.clip(((pts[:,1]-ymn)/cell).astype(int),0,ny-1)
        cid=cx*ny+cy
        # Linear-time segmented minimum. The former full lexsort was O(N log N)
        # over every existing-ground point even though only a few hundred cells
        # are produced by the configured search size.
        cell_count = nx * ny
        min_z = np.full(cell_count, np.inf, dtype=np.float64)
        np.minimum.at(min_z, cid, pts[:, 2])
        is_low = pts[:, 2] == min_z[cid]
        low_indices = np.flatnonzero(is_low)
        chosen = np.full(cell_count, n, dtype=np.intp)
        np.minimum.at(chosen, cid[low_indices], low_indices)
        for seed_idx in chosen[chosen < n]:
            seed_idx = int(seed_idx)
            cell_seed_local.add(seed_idx)
            if add_to_ground:
                seed_local.add(seed_idx)

    if seed_method in ("aerial_low_ground","lowest_only"): _cell_seeds()
    existing_ground_local = np.flatnonzero(is_cg_local)
    existing_ground_count = int(len(existing_ground_local))
    if seed_method in ("aerial_low_ground","ground_only"):
        bounded_ground = _bounded_ground_seed_indices(
            pts, existing_ground_local, max_points=150_000
        )
        seed_local.update(bounded_ground.tolist())
    if len(seed_local) < 3 and seed_method == "ground_only": _cell_seeds()
    if len(seed_local) < 3:
        # A valid TIN cannot be constructed.  The previous fallback promoted
        # every source point to ground, which is unsafe and caused fence-wide
        # classification floods.
        if progress_cb:
            progress_cb(100, "Stopped — fewer than 3 valid ground seeds")
        return {
            "changed": 0,
            "indices": np.array([], dtype=np.intp),
            "source_candidates": int(np.count_nonzero(from_mask)),
            "accepted_candidates": 0,
            "ground_count": 0,
            "class0_count": 0,
            "seed_count": len(seed_local),
            "iterations": 0,
            "stop_reason": "fewer than 3 valid ground seeds",
        }

    adaptive_result = None
    if adaptive_thresholds:
        if len(cell_seed_local) < 3:
            _cell_seeds(add_to_ground=False)
        adaptive_seed_local = (
            cell_seed_local if len(cell_seed_local) >= 3 else seed_local
        )
        adaptive_seed_points = pts[
            np.asarray(sorted(adaptive_seed_local), dtype=np.intp)
        ]
        adaptive_result = _estimate_adaptive_ground_thresholds(
            adaptive_seed_points
        )
        terrain_angle = adaptive_result["terrain_angle"]
        iteration_angle = adaptive_result["iteration_angle"]
        iteration_distance = adaptive_result["iteration_distance"]
        if progress_cb:
            slope_text = (
                f"{adaptive_result['estimated_slope']:.1f}°"
                if adaptive_result["estimated_slope"] is not None
                else "unavailable"
            )
            progress_cb(
                8,
                "Auto terrain: "
                f"slope {slope_text}, terrain {terrain_angle:.1f}°, "
                f"iteration {iteration_angle:.1f}° / {iteration_distance:.2f} m",
            )

    if progress_cb: progress_cb(10, f"Phase 2 — TIN from {len(seed_local)} seeds …")
    ground_set=set(seed_local); terrain_rad=np.radians(terrain_angle); in_ground=np.zeros(n,dtype=bool)
    iterations_completed = 0
    stop_reason = "stable"

    source_candidate_count = int(np.count_nonzero(is_from_local))
    dense_ground_fast_path = (
        existing_ground_count >= 100_000
        and existing_ground_count >= source_candidate_count
    )
    max_iterations = 1 if dense_ground_fast_path else 100
    if dense_ground_fast_path and progress_cb:
        progress_cb(
            10,
            "Dense existing-ground fast path — one bounded TIN evaluation "
            f"({len(seed_local):,} anchors, {source_candidate_count:,} candidates)",
        )

    for iteration in range(max_iterations):
        if abort_check and abort_check():
            stop_reason = "cancelled"
            break
        ga=np.array(sorted(ground_set),dtype=np.intp)
        if len(ga)<3:
            stop_reason = "fewer than 3 TIN points"
            break
        if progress_cb:
            progress_cb(
                12 + int(iteration / max_iterations * 80),
                f"Iter {iteration+1}/{max_iterations}: {len(ga):,} anchors …",
            )
        # Cap seed triangulation size to prevent Qhull memory spikes on huge seed sets.
        ga_tri, tri_points = _subsample_ground_tin_points(pts, ga)
        if len(tri_points) < 3:
            stop_reason = "fewer than 3 sampled TIN points"
            break
        xy_tri = tri_points[:, :2]
        if not np.isfinite(xy_tri).all():
            stop_reason = "non-finite TIN coordinates"
            break
        if np.ptp(xy_tri[:,0]) <= 1e-12 or np.ptp(xy_tri[:,1]) <= 1e-12:
            stop_reason = "degenerate TIN extent"
            break
        try:
            tri = Delaunay(xy_tri)
        except Exception:
            stop_reason = "TIN construction failed"
            break
        finally:
            # Release the numpy slice used for triangulation immediately;
            # Delaunay has already copied what it needs into C memory.
            del xy_tri
        # Existing ground guides the TIN but is never a source candidate. Only
        # a bounded anchor subset is triangulated; mark every trusted-ground
        # point processed so millions of unsampled class-2 points do not enter
        # the expensive candidate query.
        in_ground[:] = is_cg_local
        in_ground[ga] = True
        ng=np.where(~in_ground)[0]
        if not len(ng):
            stop_reason = "all candidates processed"
            del tri
            break
        sids=tri.find_simplex(pts[ng,:2]); valid=sids!=-1
        if not valid.any():
            stop_reason = "no candidates inside TIN"
            del tri
            break
        vng=ng[valid]; vsids=sids[valid]
        usids,inv=np.unique(vsids,return_inverse=True)
        sv=tri.simplices[usids]
        # Delaunay simplex indices refer to the possibly subsampled tri_points,
        # never to the full ground array.  Mixing these arrays creates invalid
        # triangle planes and can accept most or all source points.
        A3=tri_points[sv[:,0]]; B3=tri_points[sv[:,1]]; C3=tri_points[sv[:,2]]
        me=np.maximum(np.maximum(np.linalg.norm(B3-A3,axis=1),np.linalg.norm(C3-B3,axis=1)),np.linalg.norm(A3-C3,axis=1))
        nor=np.cross(B3-A3,C3-A3); nl=np.linalg.norm(nor,axis=1,keepdims=True)
        good=nl[:,0]>1e-12; nor=nor/np.where(nl>0,nl,1.0); nor[nor[:,2]<0]*=-1
        sl=np.arccos(np.clip(nor[:,2],-1,1)); tok=good&(sl<=terrain_rad)
        pn=nor[inv]; pme=me[inv]; pto=tok[inv]; pA=A3[inv]; pa=pts[vng]
        act=pto.copy()
        if stop_triangulation: act&=pme>=stop_edge_length
        d=np.einsum('ij,ij->i',pn,pa-pA); pd=np.abs(d)
        act&=pd<=iteration_distance
        if add_only_upward: act&=d>=0
        pp=pa-d[:,np.newaxis]*pn; psv=sv[inv]
        nr=np.minimum(np.minimum(np.linalg.norm(tri_points[psv[:,0]]-pp,axis=1),np.linalg.norm(tri_points[psv[:,1]]-pp,axis=1)),np.linalg.norm(tri_points[psv[:,2]]-pp,axis=1))
        alpha=np.where(nr<1e-12,90.0,np.degrees(np.arctan2(pd,nr)))
        ea=np.full(len(vng),iteration_angle,dtype=float)
        if reduce_angle_edge:
            sm=pme<edge_length_threshold; ea[sm]=iteration_angle*pme[sm]/edge_length_threshold
        if use_distance_as_rating:
            w=distance_weight/100.0
            act&=(1-w)*np.where(ea>0,alpha/ea,1.0)+w*(pd/iteration_distance if iteration_distance>0 else np.ones_like(pd))<=1.0
        else:
            act&=alpha<=ea
        np_ = vng[act]
        if not len(np_):
            stop_reason = "thresholds reached"
            del tri
            break
        ground_set.update(np_.tolist())
        iterations_completed = iteration + 1
        del tri

    gl=np.array(sorted(ground_set),dtype=np.intp)
    rl=gl[is_from_local[gl]]
    accepted_global=work_idx[rl]
    before=classification[accepted_global].copy()
    classification[accepted_global]=to_class

    protected_global = work_idx[np.asarray(sorted(seed_local), dtype=np.intp)]
    holdout_candidates = accepted_global[before != 0]
    class0_indices = np.array([], dtype=np.intp)
    if float(class0_holdout_percent or 0.0) > 0.0 and int(to_class) != 0:
        class0_indices = _select_ground_class0_holdout(
            xyz,
            holdout_candidates,
            class0_holdout_percent,
            protected_indices=protected_global,
        )
        classification[class0_indices] = 0

    after=classification[accepted_global]
    changed_mask=after != before
    changed_indices=accepted_global[changed_mask]
    ground_count=int(np.count_nonzero(after[changed_mask] == int(to_class)))
    class0_count=int(np.count_nonzero(after[changed_mask] == 0))

    if progress_cb:
        progress_cb(
            100,
            f"Done — {ground_count:,} ground, {class0_count:,} reserved in class 0",
        )
    return {
        "changed": len(changed_indices),
        "indices": changed_indices,
        "source_candidates": int(np.count_nonzero(from_mask)),
        "accepted_candidates": len(accepted_global),
        "ground_count": ground_count,
        "class0_count": class0_count,
        "seed_count": len(seed_local),
        "existing_ground_count": existing_ground_count,
        "dense_ground_fast_path": dense_ground_fast_path,
        "iterations": iterations_completed,
        "stop_reason": stop_reason,
        "adaptive_thresholds": bool(adaptive_thresholds),
        "estimated_slope": (
            adaptive_result.get("estimated_slope")
            if adaptive_result is not None
            else None
        ),
        "effective_terrain_angle": float(terrain_angle),
        "effective_iteration_angle": float(iteration_angle),
        "effective_iteration_distance": float(iteration_distance),
    }


# ═══════════════════════════════════════════════════════════════════════
# ALGORITHM 4 — CLASSIFY SURFACE POINTS
# TerraScan equivalent: locally smooth planar or rounded surfaces
# ═══════════════════════════════════════════════════════════════════════
def classify_surface_points(
        xyz, classification, from_classes, to_class,
        tolerance=0.05, num_neighbors=16, fence_mask=None,
        batch_size=100_000, progress_cb=None, abort_check=None):
    """
    Classify points that belong to a locally smooth 3D surface.

    TerraScan exposes only the surface tolerance for this routine.  The local
    neighborhood is therefore deliberately an implementation detail.  A
    robust PCA tangent plane is fitted to nearby source points in full XYZ so
    horizontal ground, sloped roofs, rounded surfaces, and vertical walls are
    handled by the same geometry.

    Processing is chunked because a full ``point_count × neighbor_count × 3``
    array is too expensive for production-size LiDAR files.
    """
    points = np.asarray(xyz)
    classes = np.asarray(classification)
    if points.ndim != 2 or points.shape[1] < 3:
        raise ValueError("xyz must be an N×3 point array")
    if len(points) != len(classes):
        raise ValueError("xyz and classification must have the same length")
    tol = max(float(tolerance), 1e-9)
    requested_neighbors = max(6, int(num_neighbors))
    chunk_size = max(1_000, int(batch_size))

    if from_classes is None or (
            isinstance(from_classes, list)
            and (not from_classes or None in from_classes)):
        source_mask = np.ones(len(classes), dtype=bool)
    else:
        source_mask = np.isin(classes, from_classes)
    if fence_mask is not None:
        fence = np.asarray(fence_mask, dtype=bool)
        if len(fence) != len(classes):
            raise ValueError("fence_mask must match the point count")
        source_mask &= fence

    source_indices = np.flatnonzero(source_mask)
    source_count = int(len(source_indices))
    minimum_points = requested_neighbors + 1
    if source_count < minimum_points:
        if progress_cb:
            progress_cb(
                100,
                f"Stopped — need at least {minimum_points} source points",
            )
        return {
            "changed": 0,
            "indices": np.array([], dtype=np.intp),
            "surface_candidates": source_count,
            "surface_accepted": 0,
            "rejected_tolerance": 0,
            "rejected_roughness": 0,
            "rejected_degenerate": source_count,
            "tolerance": tol,
            "neighbor_count": requested_neighbors,
            "stop_reason": "too few source points",
            "routine": "surface_points",
        }

    source_points = points[source_indices]
    if not np.isfinite(source_points).all():
        finite_local = np.all(np.isfinite(source_points), axis=1)
    else:
        finite_local = np.ones(source_count, dtype=bool)

    if progress_cb:
        progress_cb(
            3,
            f"Building 3D surface index for {source_count:,} points …",
        )
    tree = cKDTree(source_points[finite_local])
    finite_lookup = np.flatnonzero(finite_local)
    finite_count = int(len(finite_lookup))
    if finite_count < minimum_points:
        if progress_cb:
            progress_cb(100, "Stopped — too few finite source points")
        return {
            "changed": 0,
            "indices": np.array([], dtype=np.intp),
            "surface_candidates": source_count,
            "surface_accepted": 0,
            "rejected_tolerance": 0,
            "rejected_roughness": 0,
            "rejected_degenerate": source_count,
            "tolerance": tol,
            "neighbor_count": requested_neighbors,
            "stop_reason": "too few finite source points",
            "routine": "surface_points",
        }

    k = min(requested_neighbors + 1, finite_count)
    accepted_parts = []
    rejected_tolerance = 0
    rejected_roughness = 0
    rejected_degenerate = source_count - finite_count
    processed = 0
    cancelled = False

    for start in range(0, finite_count, chunk_size):
        if abort_check and abort_check():
            cancelled = True
            break
        stop = min(start + chunk_size, finite_count)
        query_local = finite_lookup[start:stop]
        query_points = source_points[query_local]
        _, raw_neighbors = tree.query(query_points, k=k, workers=-1)
        if raw_neighbors.ndim == 1:
            raw_neighbors = raw_neighbors[:, np.newaxis]

        # cKDTree normally returns the query point first.  Explicitly remove
        # its finite-tree index so duplicate XYZ samples cannot leave the
        # candidate inside its own reference plane by accident.
        query_tree_indices = np.arange(start, stop, dtype=np.intp)
        not_self = raw_neighbors != query_tree_indices[:, np.newaxis]
        ranks = np.cumsum(not_self, axis=1)
        keep = not_self & (ranks <= requested_neighbors)
        neighbor_tree_indices = raw_neighbors[keep].reshape(
            len(query_points), -1
        )
        if neighbor_tree_indices.shape[1] < 3:
            rejected_degenerate += len(query_points)
            processed += len(query_points)
            continue

        neighbor_local = finite_lookup[neighbor_tree_indices]
        neighbor_points = source_points[neighbor_local]

        # A coordinate-wise median makes the local tangent plane less
        # sensitive to a minority of vegetation/noise returns.
        centers = np.median(neighbor_points, axis=1)
        centered = neighbor_points - centers[:, np.newaxis, :]
        covariance = np.einsum(
            "nki,nkj->nij", centered, centered
        ) / max(1, neighbor_points.shape[1])
        eigenvalues, eigenvectors = np.linalg.eigh(covariance)
        normals = eigenvectors[:, :, 0]

        neighbor_signed = np.einsum(
            "nki,ni->nk", centered, normals
        )
        plane_shift = np.median(neighbor_signed, axis=1)
        neighbor_residual = np.abs(
            neighbor_signed - plane_shift[:, np.newaxis]
        )
        local_roughness = np.median(neighbor_residual, axis=1)
        candidate_offset = np.abs(
            np.einsum("ni,ni->n", query_points - centers, normals)
            - plane_shift
        )

        # The second eigenvalue must contain real two-dimensional support.
        # This prevents wires and nearly collinear scan fragments from being
        # mistaken for surfaces.
        support_scale = np.maximum(eigenvalues[:, 2], 1e-12)
        support_ratio = eigenvalues[:, 1] / support_scale
        valid = (
            np.all(np.isfinite(eigenvalues), axis=1)
            & np.isfinite(candidate_offset)
            & np.isfinite(local_roughness)
            & (eigenvalues[:, 1] > 1e-12)
            & (support_ratio >= 0.01)
        )
        rough = valid & (local_roughness > tol)
        too_far = valid & ~rough & (candidate_offset > tol)
        accepted = valid & ~rough & ~too_far

        rejected_degenerate += int(np.count_nonzero(~valid))
        rejected_roughness += int(np.count_nonzero(rough))
        rejected_tolerance += int(np.count_nonzero(too_far))
        if np.any(accepted):
            accepted_parts.append(
                source_indices[query_local[accepted]]
            )

        processed += len(query_points)
        if progress_cb:
            progress_cb(
                8 + int(87 * processed / max(1, finite_count)),
                f"Testing local surfaces: {processed:,}/{finite_count:,}",
            )

    if cancelled:
        accepted_indices = np.array([], dtype=np.intp)
        stop_reason = "cancelled"
    else:
        accepted_indices = (
            np.concatenate(accepted_parts).astype(np.intp, copy=False)
            if accepted_parts
            else np.array([], dtype=np.intp)
        )
        stop_reason = "completed"

    changed_indices = accepted_indices[
        classes[accepted_indices] != int(to_class)
    ]
    classes[changed_indices] = int(to_class)

    if progress_cb:
        progress_cb(
            100,
            f"Done — {len(changed_indices):,} locally smooth surface points",
        )
    return {
        "changed": int(len(changed_indices)),
        "indices": changed_indices,
        "surface_candidates": source_count,
        "surface_accepted": int(len(accepted_indices)),
        "rejected_tolerance": int(rejected_tolerance),
        "rejected_roughness": int(rejected_roughness),
        "rejected_degenerate": int(rejected_degenerate),
        "tolerance": tol,
        "neighbor_count": requested_neighbors,
        "stop_reason": stop_reason,
        "routine": "surface_points",
    }


# ═══════════════════════════════════════════════════════════════════════
# ALGORITHM 5 — CLASSIFY BELOW SURFACE
# TerraScan equivalent: elevation tolerance + limit × standard deviation
# ═══════════════════════════════════════════════════════════════════════
def classify_below_surface(xyz, classification, from_classes, to_class,
                           surface_type="planar", limit=4.0,
                           z_tolerance=0.10, num_neighbors=25,
                           fence_mask=None, iterative=False,
                           max_iterations=5,
                           progress_cb=None, abort_check=None,
                           batch_size=50_000):
    """
    Classify points clearly below a local surface fitted to neighbors.

    Neighbor queries and fitting arrays are bounded by ``batch_size``.  This
    preserves the existing local-surface rule without allocating an
    N×neighbors×XYZ tensor for an entire multi-million-point cloud.
    """
    xyz = np.asarray(xyz)
    classification = np.asarray(classification)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise ValueError("xyz must be an N×3 point array")
    if len(xyz) != len(classification):
        raise ValueError("xyz and classification must have the same length")

    requested_neighbors = max(3, int(num_neighbors))
    chunk_size = max(1, int(batch_size))
    deviation_limit = max(0.0, float(limit))
    elevation_tolerance = max(0.0, float(z_tolerance))
    iteration_limit = max(1, int(max_iterations)) if iterative else 1
    fence = None
    if fence_mask is not None:
        fence = np.asarray(fence_mask, dtype=bool)
        if len(fence) != len(classification):
            raise ValueError("fence_mask must match the point count")

    total_changed = 0
    all_flagged = []
    source_candidates = 0
    iterations_completed = 0
    stop_reason = "completed"

    for iteration in range(iteration_limit):
        if abort_check and abort_check():
            stop_reason = "cancelled"
            break

        iter_label = f" (iter {iteration+1})" if iterative else ""

        if from_classes is None or (
            isinstance(from_classes, list)
            and (not from_classes or None in from_classes)
        ):
            src_mask = np.ones(len(classification), dtype=bool)
        else:
            src_mask = np.isin(classification, from_classes)

        src_mask &= np.all(np.isfinite(xyz[:, :3]), axis=1)
        if fence is not None:
            src_mask &= fence
        src_idx = np.flatnonzero(src_mask)
        if iteration == 0:
            source_candidates = int(len(src_idx))

        if len(src_idx) < requested_neighbors + 1:
            if progress_cb:
                progress_cb(100, f"Too few source pts{iter_label}")
            stop_reason = "too few source points"
            break

        if progress_cb:
            base_pct = int(iteration / iteration_limit * 80)
            progress_cb(base_pct, f"Building KD-Tree{iter_label} …")

        src_pts = xyz[src_idx]
        tree = cKDTree(src_pts)
        total = len(src_idx)
        k = min(requested_neighbors + 1, total)

        if progress_cb:
            progress_cb(base_pct + 3,
                        f"Chunked KNN{iter_label} ({total:,} × {k}) …")

        use_curved = (surface_type == "curved")
        flagged_parts = []
        cancelled_iteration = False

        for start in range(0, total, chunk_size):
            if abort_check and abort_check():
                cancelled_iteration = True
                break
            stop = min(start + chunk_size, total)
            query_points = src_pts[start:stop]
            _, raw_neighbors = tree.query(
                query_points,
                k=k,
                workers=-1,
            )
            if raw_neighbors.ndim == 1:
                raw_neighbors = raw_neighbors[:, np.newaxis]

            if not use_curved:
                nbr_idx = raw_neighbors[:, 1:]
                nbr_pts = src_pts[nbr_idx]
                neighbor_count = nbr_pts.shape[1]
                A = np.empty(
                    (len(query_points), neighbor_count, 3),
                    dtype=np.result_type(nbr_pts.dtype, np.float64),
                )
                A[:, :, :2] = nbr_pts[:, :, :2]
                A[:, :, 2] = 1.0
                b = nbr_pts[:, :, 2]
                AtA = np.einsum("nki,nkj->nij", A, A)
                Atb = np.einsum("nki,nk->ni", A, b)

                try:
                    coeffs = np.linalg.solve(AtA, Atb)
                except Exception:
                    coeffs = np.full(
                        (len(query_points), 3),
                        np.nan,
                        dtype=np.float64,
                    )
                bad = ~np.all(np.isfinite(coeffs), axis=1)
                for row in np.flatnonzero(bad):
                    try:
                        coeffs[row], _, _, _ = np.linalg.lstsq(
                            A[row],
                            b[row],
                            rcond=None,
                        )
                    except Exception:
                        coeffs[row] = np.nan

                fitted = np.einsum("nki,ni->nk", A, coeffs)
                normal_scale = np.sqrt(
                    coeffs[:, 0] ** 2
                    + coeffs[:, 1] ** 2
                    + 1.0
                )
                neighbor_distance = (
                    nbr_pts[:, :, 2] - fitted
                ) / normal_scale[:, np.newaxis]
                deviation = np.std(neighbor_distance, axis=1)
                predicted_z = (
                    coeffs[:, 0] * query_points[:, 0]
                    + coeffs[:, 1] * query_points[:, 1]
                    + coeffs[:, 2]
                )
                offset = (
                    predicted_z - query_points[:, 2]
                ) / normal_scale
                valid = (
                    np.all(np.isfinite(coeffs), axis=1)
                    & np.isfinite(deviation)
                    & np.isfinite(offset)
                )
                flagged_local = (
                    valid
                    & (offset > 0)
                    & (offset >= elevation_tolerance)
                    & (
                        (deviation < 1e-9)
                        | (offset > deviation_limit * deviation)
                    )
                )
                if np.any(flagged_local):
                    flagged_parts.append(
                        src_idx[start:stop][flagged_local]
                    )
            else:
                curved_flagged = []
                for row, local_index in enumerate(range(start, stop)):
                    if abort_check and abort_check():
                        cancelled_iteration = True
                        break
                    nl = raw_neighbors[row, 1:]
                    nl = nl[nl != local_index]
                    if len(nl) < 6:
                        continue

                    neighbors = src_pts[nl]
                    point = src_pts[local_index]
                    cx = neighbors[:, 0].mean()
                    cy = neighbors[:, 1].mean()
                    xn = neighbors[:, 0] - cx
                    yn = neighbors[:, 1] - cy
                    design = np.column_stack(
                        (
                            xn ** 2,
                            yn ** 2,
                            xn * yn,
                            xn,
                            yn,
                            np.ones(len(neighbors)),
                        )
                    )
                    try:
                        coeffs, _, _, _ = np.linalg.lstsq(
                            design,
                            neighbors[:, 2],
                            rcond=None,
                        )
                    except Exception:
                        continue

                    fitted = design @ coeffs
                    px = point[0] - cx
                    py = point[1] - cy
                    predicted_z = np.dot(
                        [px ** 2, py ** 2, px * py, px, py, 1.0],
                        coeffs,
                    )
                    dz_dx = (
                        2.0 * coeffs[0] * px
                        + coeffs[2] * py
                        + coeffs[3]
                    )
                    dz_dy = (
                        2.0 * coeffs[1] * py
                        + coeffs[2] * px
                        + coeffs[4]
                    )
                    normal_scale = np.sqrt(
                        dz_dx ** 2 + dz_dy ** 2 + 1.0
                    )
                    deviation = np.std(
                        (neighbors[:, 2] - fitted) / normal_scale
                    )
                    offset = (
                        predicted_z - point[2]
                    ) / normal_scale
                    if (
                        offset > 0
                        and offset >= elevation_tolerance
                        and (
                            deviation < 1e-9
                            or offset > deviation_limit * deviation
                        )
                    ):
                        curved_flagged.append(src_idx[local_index])
                if curved_flagged:
                    flagged_parts.append(
                        np.asarray(curved_flagged, dtype=np.intp)
                    )
                if cancelled_iteration:
                    break

            if progress_cb:
                run_fraction = (iteration + stop / total) / iteration_limit
                progress_cb(
                    min(94, 5 + int(89 * run_fraction)),
                    f"Fitting local surfaces{iter_label}: "
                    f"{stop:,}/{total:,}",
                )

        if cancelled_iteration:
            stop_reason = "cancelled"
            break

        detected = (
            np.concatenate(flagged_parts).astype(np.intp, copy=False)
            if flagged_parts
            else np.array([], dtype=np.intp)
        )
        flagged = detected[
            classification[detected] != int(to_class)
        ]

        if len(flagged) == 0:
            stop_reason = "stable"
            if progress_cb:
                progress_cb(100, f"Stable after {iteration+1} iteration(s) — "
                                 f"total {total_changed:,} pts reclassified")
            break

        classification[flagged] = to_class
        total_changed += len(flagged)
        all_flagged.append(flagged)
        iterations_completed = iteration + 1

        if progress_cb:
            progress_cb(
                int((iteration + 1) / iteration_limit * 95),
                f"Iter {iteration+1}: {len(flagged):,} pts flagged{iter_label}")

        if not iterative:
            break

    all_indices = (
        np.unique(np.concatenate(all_flagged))
        if all_flagged
        else np.array([], dtype=np.intp)
    )
    total_changed = int(len(all_indices))

    if progress_cb:
        progress_cb(
            100,
            f"Done - {total_changed:,} pts -> class {to_class}",
        )

    return {
        "changed": total_changed,
        "indices": all_indices,
        "below_surface_candidates": source_candidates,
        "below_surface_flagged": total_changed,
        "iterations": iterations_completed,
        "limit": deviation_limit,
        "z_tolerance": elevation_tolerance,
        "stop_reason": stop_reason,
        "routine": "below_surface",
    }


# ═══════════════════════════════════════════════════════════════════════
# BASE DIALOG  ← MINIMIZE FEATURE LIVES HERE
# ═══════════════════════════════════════════════════════════════════════
class _BaseClassifyDialog(QDialog):
    """
    Base class for all classification dialogs.

    MINIMIZE BEHAVIOUR
    ──────────────────
    • Dialogs use native OS minimize/restore so minimized windows live in
      the Windows taskbar (no floating restore chips above the taskbar).
    • Calling the public-API open_* function while the dialog already exists
      restores/raises it instead of creating a duplicate.
    """

    _persist_prefix = ""

    def __init__(self, app, title, parent=None):
        # Parentless top-level window:
        # avoids Windows desktop mini-bars on minimize for owned dialogs.
        super().__init__(None, Qt.Window)
        self.app = app
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.setWindowTitle(title)
        self.setStyleSheet(_get_dialog_style())
        _apply_dialog_icon(self, app)

        self.setWindowFlags(
            Qt.Dialog |
            Qt.WindowCloseButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowMinimizeButtonHint
        )
        self.setMinimumWidth(420)

        self._header_layout = QHBoxLayout()
        self._header_layout.setContentsMargins(0, 0, 0, 0)
        self._header_layout.addStretch()
        
        # Subclasses should add their widgets to self._content_layout
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet("QScrollArea { background: transparent; }")
        
        self._container = QWidget()
        self._container.setObjectName("dialogContainer")
        # Let word-wrapped notes and form rows adapt to the viewport instead
        # of forcing a hidden horizontal overflow width.
        self._container.setMinimumWidth(0)
        self._container.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        self._container.setStyleSheet("QWidget#dialogContainer { background: transparent; }")
        self._scroll.setWidget(self._container)
        
        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)
        self._main_layout.addLayout(self._header_layout)
        self._main_layout.addWidget(self._scroll)
        
        self._content_layout = QVBoxLayout(self._container)
        self._content_layout.setContentsMargins(18, 5, 18, 10)
        self._content_layout.setSpacing(12)

        self._worker = None
        self._active_old_cls = None
        self._focus_conn = None
        self._connect_focus_watcher()

        # ── Minimize state ────────────────────────────────────────
        self._chip: _MinimizedChip | None = None
        self._saved_geometry: QRect | None = None
        # Native taskbar minimize is required for these dialogs.
        # Keep chip helpers available for backward compatibility, but disabled.
        self._enable_chip_minimize = False

    def _connect_focus_watcher(self):
        pass

    def _disconnect_focus_watcher(self):
        pass

    def _on_focus_window_changed(self, focused_window):
        pass

    def refresh_theme(self):
        """Re-apply current theme styles to this dialog."""
        try:
            self.setStyleSheet(_get_dialog_style())
            if self._chip is not None:
                self._chip.setStyleSheet(_get_chip_style())
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────
    # MINIMIZE / RESTORE  — native OS taskbar behavior
    # ──────────────────────────────────────────────────────────────
    def changeEvent(self, event):
        """Keep native minimize behavior (no custom chip interception)."""
        super().changeEvent(event)

    def showEvent(self, event):
        self.refresh_theme()
        super().showEvent(event)

    def _do_minimize_to_chip(self):
        """
        Backward-compatible entrypoint used by older call sites.
        Force native minimize so this dialog appears in taskbar.
        """
        self._cleanup_chip()
        self.showMinimized()

    def _do_restore_from_chip(self):
        """Backward-compatible restore helper with native unminimize."""
        self._cleanup_chip()
        if self.windowState() & Qt.WindowMinimized:
            self.showNormal()
        else:
            try:
                self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
            except Exception:
                pass
        self.show()
        self.raise_()
        self.activateWindow()

    def minimize_to_chip(self):
        self._do_minimize_to_chip()

    def restore_from_chip(self):
        self._do_restore_from_chip()

    @property
    def _is_minimized_to_chip(self):
        return self._chip is not None and not self.isVisible()

    # ──────────────────────────────────────────────────────────────
    # PERSISTENCE HELPERS
    # ──────────────────────────────────────────────────────────────
    def _pk(self, suffix: str) -> str:
        return f"{self._persist_prefix}.{suffix}" if self._persist_prefix else suffix

    def _persist_spin(self, spin, key_suffix, default):
        val = PersistentClassSettings.get_value(self._pk(key_suffix), default)
        spin.setValue(val)
        spin.valueChanged.connect(
            lambda v: PersistentClassSettings.set_value(self._pk(key_suffix), v)
        )

    def _persist_combo_index(self, combo, key_suffix, default_index):
        idx = PersistentClassSettings.get_value(self._pk(key_suffix), default_index)
        if 0 <= idx < combo.count():
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(
            lambda i: PersistentClassSettings.set_value(self._pk(key_suffix), i)
        )

    def _persist_checkbox(self, chk, key_suffix, default_checked):
        val = PersistentClassSettings.get_value(self._pk(key_suffix), default_checked)
        chk.setChecked(bool(val))
        chk.stateChanged.connect(
            lambda s: PersistentClassSettings.set_value(self._pk(key_suffix), bool(s))
        )

    def _get_fence_mask(self, fence_widget: FenceSelectorWidget):
        if not fence_widget.selected_fences or self.app.data is None:
            return None
        return fence_widget.get_fence_mask(self.app.data["xyz"])

    # ──────────────────────────────────────────────────────────────
    # RUN ALGORITHM
    # ──────────────────────────────────────────────────────────────
    def _run_algorithm(self, func, kwargs, name):
        if self.app.data is None:
            QMessageBox.warning(self.app, "No Data", "Load a point cloud first.")
            if not getattr(self, "_keep_visible_during_run", False):
                self.close()
            return

        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(
                self,
                "Classification Already Running",
                "This classification is already running. Wait for it to "
                "finish or cancel it before starting again.",
            )
            return

        active_dialog = getattr(
            self.app,
            "_lidar_classification_active_dialog",
            None,
        )
        if active_dialog is not None and active_dialog is not self:
            active_worker = getattr(active_dialog, "_worker", None)
            if active_worker is not None and active_worker.isRunning():
                QMessageBox.information(
                    self,
                    "Another Classification Is Running",
                    "Only one LiDAR classification can modify the point cloud "
                    "at a time. Finish or cancel the active classification "
                    "before starting this one.",
                )
                return
        self.app._lidar_classification_active_dialog = self

        self._suppress_restore_after_run = False
        if getattr(self, "_keep_visible_during_run", False):
            self._set_algorithm_running(True)
        else:
            self.hide()

        # Store only a snapshot reference; the slice is taken AFTER
        # the algorithm runs, inside _on_done, once indices are known.
        old_cls_full = self.app.data["classification"].copy()
        self._active_old_cls = old_cls_full
        kwargs["xyz"] = self.app.data["xyz"]
        kwargs["classification"] = self.app.data["classification"]

        # The fence mask is the writable candidate region for every LiDAR
        # algorithm. Hidden Main View flight lines remain usable as spatial
        # context, but their points cannot be changed.
        from gui.flight_line_filter import flight_line_visibility_mask
        line_mask = flight_line_visibility_mask(
            self.app, len(old_cls_full), slot=0
        )
        user_fence = kwargs.get("fence_mask")
        if user_fence is None:
            kwargs["fence_mask"] = line_mask
        else:
            user_fence = np.asarray(user_fence, dtype=bool).ravel()
            if len(user_fence) != len(line_mask):
                self._active_old_cls = None
                self.app._lidar_classification_active_dialog = None
                self._restore_after_algorithm()
                QMessageBox.warning(
                    self,
                    "Invalid Fence",
                    "Fence mask does not match the loaded point cloud.",
                )
                return
            kwargs["fence_mask"] = user_fence & line_mask

        # Restrict fence to visible classes — hidden classes cannot be reclassified.
        _palette = getattr(self.app, "class_palette", None)
        if isinstance(_palette, dict) and _palette:
            _vis = [int(c) for c, i in _palette.items() if isinstance(i, dict) and i.get("show", True)]
            if _vis and "classification" in kwargs:
                _vis_mask = np.isin(kwargs["classification"], _vis)
                _cur = kwargs.get("fence_mask")
                kwargs["fence_mask"] = (_vis_mask & _cur) if _cur is not None else _vis_mask

        self._prog = QProgressDialog(f"Running {name}…", "Cancel", 0, 100, self.app)
        self._prog.setWindowModality(Qt.NonModal)
        self._prog.setWindowTitle(name)
        self._prog.setMinimumDuration(0)
        self._prog.setValue(0)
        self._prog.setAttribute(Qt.WA_DeleteOnClose)
        self._prog.show()

        worker = _ClassificationWorker(func, kwargs)
        self._worker = worker
        registry = getattr(
            self.app,
            "_lidar_classification_workers",
            None,
        )
        if not isinstance(registry, list):
            registry = []
            self.app._lidar_classification_workers = registry
        registry.append(worker)

        worker.progress.connect(self._on_prog)
        worker.result_ready.connect(
            lambda r: self._on_done(r, old_cls_full, name)
        )
        worker.error.connect(
            lambda msg: self._on_err(msg, old_cls_full)
        )
        worker.finished.connect(
            lambda active_worker=worker: self._finalize_worker(
                active_worker
            )
        )
        self._prog.canceled.connect(worker.abort)
        try:
            worker.start()
        except Exception:
            self._rollback_classification_snapshot(old_cls_full)
            self._active_old_cls = None
            self._finalize_worker(worker)
            if hasattr(self, "_prog") and self._prog is not None:
                try:
                    self._prog.close()
                except Exception:
                    pass
                self._prog = None
            self._restore_after_algorithm()
            raise

    def _on_prog(self, pct, msg):
        if hasattr(self, "_prog") and self._prog:
            self._prog.setValue(pct)
            self._prog.setLabelText(msg)

    # ──────────────────────────────────────────────────────────────
    # SECTION SYNC
    # ──────────────────────────────────────────────────────────────
    def _sync_section_classifications(self, changed_indices):
        sc = getattr(self.app, "section_controller", None)
        if sc is None or self.app.data is None:
            return

        new_cls = self.app.data["classification"]
        ch_idx = np.asarray(changed_indices)
        synced = False

        for attr_name in (
            "_section_data", "section_data", "_sections",
            "sections", "_view_data", "view_data",
            "_stored_sections"
        ):
            storage = getattr(sc, attr_name, None)
            if not isinstance(storage, dict) or not storage:
                continue
            for view_id, sd in storage.items():
                if sd is not None:
                    self._sync_one_section(sd, new_cls, ch_idx, view_id)
            synced = True
            break

        if synced:
            return

        for gi_attr, cls_attr in [
            ("global_indices", "classification"),
            ("_global_indices", "_classification"),
            ("section_indices", "section_cls"),
        ]:
            gi = getattr(sc, gi_attr, None)
            cl = getattr(sc, cls_attr, None)
            if gi is not None and cl is not None and isinstance(cl, np.ndarray):
                gi_arr = np.asarray(gi)
                mask = np.isin(gi_arr, ch_idx)
                if mask.any():
                    cl[mask] = new_cls[gi_arr[mask]]
                    print(f"   📋 Synced {int(mask.sum())} section cls values")
                return

    def _sync_one_section(self, section_data, new_cls, changed_indices, view_id):
        GI_KEYS = ("global_indices", "indices", "point_indices", "idx", "core_indices", "all_indices")
        CLS_KEYS = ("classification", "cls", "classes", "point_classes")

        def _get(obj, keys):
            for k in keys:
                v = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
                if v is not None:
                    return v
            return None

        gi = _get(section_data, GI_KEYS)
        cl = _get(section_data, CLS_KEYS)

        if gi is None or cl is None or not isinstance(cl, np.ndarray):
            return

        gi_arr = np.asarray(gi)
        if len(gi_arr) == 0:
            return

        mask = np.isin(gi_arr, changed_indices)
        n = int(mask.sum())
        if n > 0:
            cl[mask] = new_cls[gi_arr[mask]]
            print(f"   📋 Synced {n} classification values in section view {view_id}")

    def _notify_classification_views(
        self,
        result,
        changed_mask,
        changed_indices,
    ):
        """
        Route algorithm completion through the application's live view bus.

        Copying a section-side class array is not enough for unified GPU actors:
        ``classification_finished`` refreshes their RGB and Classification VTK
        buffers, renders every open section, runs cut/surface hooks, and updates
        point statistics.
        """
        if changed_mask is None or not np.any(changed_mask):
            return False

        signal = getattr(self.app, "classification_finished", None)
        emit = getattr(signal, "emit", None)
        if not callable(emit):
            return False

        changed_indices = np.asarray(changed_indices, dtype=np.intp)
        self.app._last_changed_mask = changed_mask
        self.app._last_changed_indices = changed_indices.copy()
        try:
            emit(changed_mask)
            print(
                "   ⚡ Live cross-section refresh dispatched: "
                f"{len(changed_indices):,} changed points"
            )
            return True
        except Exception as exc:
            print(f"   ⚠️ Live cross-section refresh dispatch failed: {exc}")
            return False

    # Backward-compatible name retained for tests/plugins written against the
    # first Low/Isolated refresh correction.
    def _notify_low_noise_views(self, result, changed_mask, changed_indices):
        return self._notify_classification_views(
            result,
            changed_mask,
            changed_indices,
        )

    def _rollback_classification_snapshot(self, old_cls):
        """Restore the shared classification array after error/cancellation."""
        data = getattr(self.app, "data", None)
        if not isinstance(data, dict):
            return np.array([], dtype=np.intp)
        current = data.get("classification")
        if (
            current is None
            or old_cls is None
            or len(current) != len(old_cls)
        ):
            return np.array([], dtype=np.intp)
        changed = np.flatnonzero(current != old_cls).astype(
            np.intp,
            copy=False,
        )
        if len(changed):
            current[:] = old_cls
        return changed

    def _finalize_worker(self, worker):
        """Release one stopped worker and its application-wide ownership."""
        if worker is None:
            return
        try:
            if worker.isRunning():
                return
        except RuntimeError:
            pass

        registry = getattr(
            self.app,
            "_lidar_classification_workers",
            None,
        )
        if isinstance(registry, list) and worker in registry:
            registry.remove(worker)
        if (
            getattr(
                self.app,
                "_lidar_classification_active_dialog",
                None,
            )
            is self
        ):
            self.app._lidar_classification_active_dialog = None
        if self._worker is worker:
            self._worker = None
        try:
            worker.deleteLater()
        except RuntimeError:
            pass

    @staticmethod
    def _clear_sparse_change_cache(app, expected_mask):
        """Clear this run's sparse cache without erasing a newer operation."""
        if getattr(app, "_last_changed_mask", None) is expected_mask:
            app._last_changed_mask = None
            app._last_changed_indices = None

    # ──────────────────────────────────────────────────────────────
    # ON DONE
    # ──────────────────────────────────────────────────────────────
    def _on_done(self, result, old_cls, name):
        if hasattr(self, "_prog") and self._prog:
            try:
                self._prog.close()
            except Exception:
                pass
            self._prog = None

        result = dict(result or {})
        stop_reason_lc = str(
            result.get("stop_reason", "")
        ).strip().lower()
        cancelled = stop_reason_lc in {
            "cancelled",
            "canceled",
            "aborted",
        }
        if cancelled:
            self._rollback_classification_snapshot(old_cls)
            result["changed"] = 0
            result["indices"] = np.array([], dtype=np.intp)

        # Defensive postcondition: no LiDAR worker may commit a reported
        # change outside Main View's enabled flight lines, even if a future
        # algorithm implementation forgets to honor fence_mask internally.
        reported_indices = np.asarray(
            result.get("indices", np.array([], dtype=np.intp)),
            dtype=np.intp,
        )
        if not cancelled and reported_indices.size:
            from gui.flight_line_filter import filter_visible_flight_line_indices
            allowed_indices = filter_visible_flight_line_indices(
                self.app,
                reported_indices,
                len(old_cls),
                slot=0,
            )
            allowed_set_mask = np.zeros(reported_indices.size, dtype=bool)
            if allowed_indices.size:
                allowed_set_mask = np.isin(
                    reported_indices, allowed_indices, assume_unique=False
                )
            rejected_indices = reported_indices[~allowed_set_mask]
            current_cls = self.app.data["classification"]
            if rejected_indices.size:
                current_cls[rejected_indices] = old_cls[rejected_indices]
            if allowed_indices.size:
                allowed_indices = allowed_indices[
                    current_cls[allowed_indices] != old_cls[allowed_indices]
                ]
            result["indices"] = allowed_indices.astype(np.intp, copy=False)
            result["changed"] = int(allowed_indices.size)

        changed = int(result.get("changed", 0))
        indices = np.asarray(
            result.get("indices", np.array([], dtype=np.intp)),
            dtype=np.intp,
        )

        to_class = None
        from_classes = None
        mask = None

        if changed > 0 and len(indices) > 0:
            mask = np.zeros(len(self.app.data["xyz"]), dtype=bool)
            mask[indices] = True
            old_c = old_cls[indices]        # slice only — no full array in undo entry
            new_c = self.app.data["classification"][indices].copy()
            del old_cls                     # release the full copy immediately

            to_class_arr = np.unique(new_c)
            to_class = int(to_class_arr[0]) if len(to_class_arr) == 1 else None
            from_classes = list(set(int(c) for c in np.unique(old_c)))
            if to_class is not None and to_class not in from_classes:
                from_classes.append(to_class)

            self.app._last_changed_mask = mask
            self.app._last_changed_indices = np.asarray(
                indices,
                dtype=np.intp,
            ).copy()
            self._sync_section_classifications(indices)

            _stack = getattr(self.app, "undo_stack", getattr(self.app, "undostack", None))
            if _stack is not None:
                try:
                    _stack.append({
                        "mask": mask,
                        "old_classes": old_c,
                        "oldclasses": old_c,    # compat
                        "new_classes": new_c,
                        "newclasses": new_c,    # compat
                        "is_cut_locked": False,
                    })
                    _redo = getattr(self.app, "redo_stack",
                                    getattr(self.app, "redostack", None))
                    if _redo is not None:
                        _redo.clear()

                    max_steps = getattr(self.app, "_max_undo_steps", 30)
                    while len(_stack) > max_steps:
                        from gui.memory_manager import _free_undo_entry
                        _free_undo_entry(_stack.pop(0))
                except Exception as e:
                    print(f"⚠️ Undo stack push failed: {e}")
        else:
            del old_cls                     # nothing changed — still must release
        self._active_old_cls = None

        print(f"🔄 Classification done — refreshing view ({changed:,} pts changed)…")
        if "ground_count" in result:
            source_count = int(result.get("source_candidates", 0))
            accepted_count = int(result.get("accepted_candidates", 0))
            ground_count = int(result.get("ground_count", 0))
            class0_count = int(result.get("class0_count", 0))
            accepted_ratio = (
                accepted_count / source_count * 100.0
                if source_count > 0
                else 0.0
            )
            print(
                "   Ground diagnostics: "
                f"{accepted_count:,}/{source_count:,} accepted "
                f"({accepted_ratio:.1f}%), {ground_count:,} → ground, "
                f"{class0_count:,} → class 0, "
                f"seeds={int(result.get('seed_count', 0)):,}, "
                f"iterations={int(result.get('iterations', 0))}, "
                f"stop={result.get('stop_reason', 'unknown')}"
            )
            if "effective_terrain_angle" in result:
                slope = result.get("estimated_slope")
                slope_text = (
                    f"{float(slope):.1f}°"
                    if slope is not None
                    else "unavailable"
                )
                mode = (
                    "auto-adaptive"
                    if result.get("adaptive_thresholds")
                    else "fixed/custom"
                )
                print(
                    "   Terrain thresholds: "
                    f"mode={mode}, estimated slope={slope_text}, "
                    f"terrain={float(result['effective_terrain_angle']):.1f}°, "
                    f"iteration="
                    f"{float(result['effective_iteration_angle']):.1f}° / "
                    f"{float(result['effective_iteration_distance']):.2f} m"
                )
        elif result.get("routine") == "surface_points":
            candidates = int(result.get("surface_candidates", 0))
            accepted = int(result.get("surface_accepted", 0))
            ratio = accepted / candidates * 100.0 if candidates else 0.0
            print(
                "   Surface diagnostics: "
                f"{accepted:,}/{candidates:,} accepted ({ratio:.1f}%), "
                f"tolerance rejects={int(result.get('rejected_tolerance', 0)):,}, "
                f"rough rejects={int(result.get('rejected_roughness', 0)):,}, "
                f"non-surface rejects={int(result.get('rejected_degenerate', 0)):,}, "
                f"tolerance={float(result.get('tolerance', 0.0)):.3f} m, "
                f"stop={result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "below_surface":
            print(
                "   Below-surface diagnostics: "
                f"{int(result.get('below_surface_flagged', 0)):,}/"
                f"{int(result.get('below_surface_candidates', 0)):,} flagged, "
                f"limit={float(result.get('limit', 0.0)):.1f}σ, "
                f"Z tolerance={float(result.get('z_tolerance', 0.0)):.3f} m, "
                f"iterations={int(result.get('iterations', 0))}, "
                f"stop={result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "low_points":
            print(
                "   Low-point diagnostics: "
                f"{int(result.get('low_accepted', 0)):,}/"
                f"{int(result.get('low_candidates', 0)):,} accepted, "
                f"mode={result.get('search_mode', 'unknown')}, "
                f"no surrounding={int(result.get('rejected_no_surrounding', 0)):,}, "
                f"not low={int(result.get('rejected_not_low', 0)):,}, "
                f"gap>{float(result.get('more_than', 0.0)):.2f} m, "
                f"2D radius={float(result.get('within', 0.0)):.2f} m, "
                f"stop={result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "isolated_points":
            median_count = result.get("median_neighbor_count")
            median_text = (
                f"{float(median_count):.1f}"
                if median_count is not None
                else "unavailable"
            )
            print(
                "   Isolated-point diagnostics: "
                f"{int(result.get('isolated_accepted', 0)):,}/"
                f"{int(result.get('isolated_candidates', 0)):,} accepted, "
                f"rule=count<{int(result.get('if_fewer_than', 1))} "
                f"within {float(result.get('within', 0.0)):.2f} m (3D), "
                f"median neighbors={median_text}, "
                f"stop={result.get('stop_reason', 'unknown')}"
            )
        live_refresh_dispatched = False
        if changed > 0 and mask is not None:
            live_refresh_dispatched = self._notify_classification_views(
                result,
                mask,
                indices,
            )
        if not live_refresh_dispatched:
            self._refresh(to_class=to_class, from_classes=from_classes)
        else:
            print(
                "   ⚡ Sparse view refresh used; full main-actor rebuild avoided"
            )
        print("✅ View refresh complete")

        _app_ref = self.app
        _mask_ref = mask
        QTimer.singleShot(
            200,
            lambda: self._clear_sparse_change_cache(_app_ref, _mask_ref),
        )

        if hasattr(self, '_fence_sel') and self._fence_sel is not None:
            self._fence_sel._conversion_completed = True
            self._fence_sel._mark_fences_as_classified()

        mb = QMessageBox(self.app)
        mb.setIcon(QMessageBox.Information)
        mb.setWindowTitle(name)
        message = (
            f"{name} cancelled.\nNo changes were committed."
            if cancelled
            else f"{name} complete.\nPoints reclassified: {changed:,}"
        )
        if "ground_count" in result:
            source_count = int(result.get("source_candidates", 0))
            accepted_count = int(result.get("accepted_candidates", 0))
            accepted_ratio = (
                accepted_count / source_count * 100.0
                if source_count > 0
                else 0.0
            )
            message += (
                f"\n\nGround accepted: {int(result.get('ground_count', 0)):,}"
                f"\nReserved in Class 0: {int(result.get('class0_count', 0)):,}"
                f"\nAcceptance ratio: {accepted_count:,}/{source_count:,} "
                f"({accepted_ratio:.1f}%)"
                f"\nTIN seeds: {int(result.get('seed_count', 0)):,}"
                f"\nIterations: {int(result.get('iterations', 0))}"
                f"\nStopped: {result.get('stop_reason', 'unknown')}"
            )
            if "effective_terrain_angle" in result:
                slope = result.get("estimated_slope")
                slope_text = (
                    f"{float(slope):.1f}°"
                    if slope is not None
                    else "unavailable"
                )
                mode = (
                    "Auto-adaptive"
                    if result.get("adaptive_thresholds")
                    else "Fixed / custom"
                )
                message += (
                    f"\n\nTerrain mode: {mode}"
                    f"\nEstimated slope: {slope_text}"
                    f"\nEffective thresholds: "
                    f"{float(result['effective_terrain_angle']):.1f}° terrain, "
                    f"{float(result['effective_iteration_angle']):.1f}° / "
                    f"{float(result['effective_iteration_distance']):.2f} m "
                    "iteration"
                )
        elif result.get("routine") == "surface_points":
            candidates = int(result.get("surface_candidates", 0))
            accepted = int(result.get("surface_accepted", 0))
            ratio = accepted / candidates * 100.0 if candidates else 0.0
            message += (
                f"\n\nSurface accepted: {accepted:,}/{candidates:,} "
                f"({ratio:.1f}%)"
                f"\nOutside tolerance: "
                f"{int(result.get('rejected_tolerance', 0)):,}"
                f"\nRough neighborhoods: "
                f"{int(result.get('rejected_roughness', 0)):,}"
                f"\nNot a 2D surface: "
                f"{int(result.get('rejected_degenerate', 0)):,}"
                f"\nTolerance: "
                f"{float(result.get('tolerance', 0.0)):.3f} m"
                f"\nStopped: {result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "below_surface":
            message += (
                f"\n\nBelow-surface points: "
                f"{int(result.get('below_surface_flagged', 0)):,}/"
                f"{int(result.get('below_surface_candidates', 0)):,}"
                f"\nLimit: {float(result.get('limit', 0.0)):.1f} × σ"
                f"\nZ tolerance: "
                f"{float(result.get('z_tolerance', 0.0)):.3f} m"
                f"\nIterations: {int(result.get('iterations', 0))}"
                f"\nStopped: {result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "low_points":
            message += (
                f"\n\nLow points accepted: "
                f"{int(result.get('low_accepted', 0)):,}/"
                f"{int(result.get('low_candidates', 0)):,}"
                f"\nSearch: {result.get('search_mode', 'unknown')}"
                f"\nNo surrounding points: "
                f"{int(result.get('rejected_no_surrounding', 0)):,}"
                f"\nNot separated low points: "
                f"{int(result.get('rejected_not_low', 0)):,}"
                f"\nRule: vertical gap > "
                f"{float(result.get('more_than', 0.0)):.2f} m "
                f"inside {float(result.get('within', 0.0)):.2f} m (2D)"
                f"\nStopped: {result.get('stop_reason', 'unknown')}"
            )
        elif result.get("routine") == "isolated_points":
            median_count = result.get("median_neighbor_count")
            median_text = (
                f"{float(median_count):.1f}"
                if median_count is not None
                else "unavailable"
            )
            message += (
                f"\n\nIsolated points accepted: "
                f"{int(result.get('isolated_accepted', 0)):,}/"
                f"{int(result.get('isolated_candidates', 0)):,}"
                f"\nRule: fewer than "
                f"{int(result.get('if_fewer_than', 1))} other In-class "
                f"points within {float(result.get('within', 0.0)):.2f} m (3D)"
                f"\nMedian neighbor count: {median_text}"
                f"\nKept by neighbor count: "
                f"{int(result.get('rejected_by_neighbor_count', 0)):,}"
                f"\nStopped: {result.get('stop_reason', 'unknown')}"
            )
        mb.setText(message)
        mb.setWindowModality(Qt.NonModal)
        mb.setAttribute(Qt.WA_DeleteOnClose)
        self._restore_after_algorithm()
        mb.show()

    def _on_err(self, msg, old_cls=None):
        if hasattr(self, "_prog") and self._prog:
            try:
                self._prog.close()
            except Exception:
                pass
            self._prog = None

        rolled_back = self._rollback_classification_snapshot(old_cls)
        self._active_old_cls = None
        if len(rolled_back):
            rollback_mask = np.zeros(
                len(self.app.data["classification"]),
                dtype=bool,
            )
            rollback_mask[rolled_back] = True
            self.app._last_changed_mask = rollback_mask
            self.app._last_changed_indices = rolled_back.copy()
            self._refresh()
            self._notify_classification_views(
                {"routine": "error_rollback"},
                rollback_mask,
                rolled_back,
            )
        self._restore_after_algorithm()
        eb = QMessageBox(self.app)
        eb.setIcon(QMessageBox.Critical)
        eb.setWindowTitle("Error")
        eb.setText(f"Classification failed:\n{msg}")
        eb.setWindowModality(Qt.NonModal)
        eb.setAttribute(Qt.WA_DeleteOnClose)
        eb.show()

    def _release_old_cls(self, ref):
        """Backward-compatible no-op; worker callbacks own the snapshot."""
        del ref

    def _set_algorithm_running(self, running):
        """Lock editable controls while retaining a persistent tool window."""
        if hasattr(self, "_scroll") and self._scroll is not None:
            self._scroll.setEnabled(not running)
        if hasattr(self, "_apply_btn") and self._apply_btn is not None:
            self._apply_btn.setEnabled(not running)
            self._apply_btn.setText(
                "Classification Running…"
                if running
                else "Apply Classification"
            )
        if hasattr(self, "_close_btn") and self._close_btn is not None:
            self._close_btn.setToolTip(
                "Close this tool and cancel the active run"
                if running
                else "Close this classification tool"
            )

    def _restore_after_algorithm(self):
        """Restore an opt-in persistent tool after success or failure."""
        if not getattr(self, "_keep_visible_during_run", False):
            return
        self._set_algorithm_running(False)
        if getattr(self, "_suppress_restore_after_run", False):
            return
        self.show()
        self.raise_()
        self.activateWindow()

    def _make_buttons(self, layout=None):
        """Standard OK/Cancel button row at the bottom."""
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(18, 10, 18, 18)
        btn_row.setSpacing(12)

        self._apply_btn = QPushButton("Apply Classification")
        self._apply_btn.setObjectName("okButton")
        self._apply_btn.clicked.connect(self._on_ok)
        
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.reject)
        
        btn_row.addStretch()
        btn_row.addWidget(self._close_btn)
        btn_row.addWidget(self._apply_btn)
        
        # Add to the MAIN layout, outside the scroll area
        self._main_layout.addLayout(btn_row)

    def _refresh(self, to_class=None, from_classes=None):
        try:
            # Force a full palette sync if colors are reported as wrong
            if hasattr(self.app, "refresh_class_palette"):
                self.app.refresh_class_palette()

            display_mode = getattr(self.app, "display_mode", "class")

            if display_mode == "class":
                # Ensure we have the latest colors from the app state
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
                
            elif display_mode == "shaded_class":
                try:
                    from gui.shading_display import clear_shading_cache, update_shaded_class
                    clear_shading_cache("classification changed")
                    update_shaded_class(
                        self.app,
                        getattr(self.app, "last_shade_azimuth", 45.0),
                        getattr(self.app, "last_shade_angle", 45.0),
                        getattr(self.app, "shade_ambient", 0.2),
                        force_rebuild=True
                    )
                except Exception:
                    pass
            
            # Force VTK render to be sure
            if hasattr(self.app, "vtk_widget"):
                self.app.vtk_widget.render()
        except Exception as e:
            print(f"⚠️ Refresh failed: {e}")
            if hasattr(self.app, "apply_display_mode"):
                self.app.apply_display_mode()
            elif hasattr(self.app, "vtk_widget"):
                try: self.app.vtk_widget.render()
                except: pass

            if hasattr(self.app, "section_controller"):
                try:
                    self.app.section_controller._refresh_all_section_colors()
                except Exception as e:
                    print(f"⚠️ Section refresh: {e}")

        except Exception as e:
            print(f"⚠️ View refresh failed: {e}")
            import traceback
            traceback.print_exc()
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass


    # ──────────────────────────────────────────────────────────────
    # CLOSE / REJECT
    # ──────────────────────────────────────────────────────────────
    def _cleanup_chip(self):
        if self._chip is not None:
            try:
                self._chip.restore_requested.disconnect()
            except Exception:
                pass
            self._chip.hide()
            self._chip.deleteLater()
            self._chip = None

    def _stop_worker_before_close(self):
        """
        Cancel and join this dialog's worker before allowing Qt to destroy it.

        A QThread cannot be safely discarded while its numerical function is
        still mutating the shared classification array.  If cooperative
        cancellation takes longer than the bounded wait, keep the dialog alive
        and let the user close it after the worker reaches its next abort
        checkpoint.
        """
        worker = self._worker
        if worker is None:
            return True
        try:
            running = worker.isRunning()
        except RuntimeError:
            running = False
        if not running:
            self._finalize_worker(worker)
            return True

        if hasattr(self, "_prog") and self._prog is not None:
            try:
                self._prog.canceled.disconnect()
            except Exception:
                pass
            try:
                self._prog.setLabelText("Cancelling safely…")
                self._prog.setCancelButton(None)
            except Exception:
                pass

        worker.abort()
        if not worker.wait(5000):
            self._suppress_restore_after_run = False
            if getattr(self, "_keep_visible_during_run", False):
                self.show()
                self.raise_()
            return False

        # The worker's queued result/error callback cannot run while this GUI
        # thread is blocked in wait().  Restore any partial mutations here;
        # deleting the dialog will discard those queued callbacks.
        self._rollback_classification_snapshot(
            getattr(self, "_active_old_cls", None)
        )
        self._active_old_cls = None
        if hasattr(self, "_prog") and self._prog is not None:
            try:
                self._prog.close()
            except Exception:
                pass
            self._prog = None
        self._finalize_worker(worker)
        return True

    def closeEvent(self, event):
        self._suppress_restore_after_run = True
        if not self._stop_worker_before_close():
            event.ignore()
            return
        self._disconnect_focus_watcher()
        if hasattr(self, '_fence_sel'):
            self._fence_sel.cleanup_actors()
        self._cleanup_chip()
        super().closeEvent(event)

    def reject(self):
        self._suppress_restore_after_run = True
        if not self._stop_worker_before_close():
            return
        self._disconnect_focus_watcher()
        if hasattr(self, '_fence_sel'):
            self._fence_sel.cleanup_actors()
        self._cleanup_chip()
        super().reject()

    def add_fences_from_selection(self, fence_shapes):
        """Pre-load fence shapes sent from the Element Selection tool.

        When fences arrive via the selection panel the manual "Select Fence(s)"
        button is hidden — the fence is already determined by the selected
        drawings, matching InsideFenceDialog behaviour.
        """
        fence_sel = getattr(self, '_fence_sel', None)
        if fence_sel is None or not fence_shapes:
            return

        existing_ids = {id(f) for f in fence_sel.selected_fences}
        added = 0
        for shape in fence_shapes:
            coords = shape.get('coords', [])
            if not coords:
                continue
            fence_id = id(shape)
            if fence_id in existing_ids:
                continue
            s = dict(shape)
            s['_selection_panel_fence'] = True
            if s.get('type') in ['line', 'smart_line', 'polyline', 'smartline', 'curve']:
                c = s.get('coords', [])
                if not isinstance(c, np.ndarray):
                    c = np.array(c)
                if len(c) > 0 and not np.allclose(c[0], c[-1]):
                    s['coords'] = list(np.vstack([c, c[0]]))
            fence_sel.selected_fences.append(s)
            existing_ids.add(fence_id)
            added += 1

        fence_sel.select_btn.setVisible(False)
        fence_sel.clear_btn.setVisible(False)
        fence_sel._update_status()

        self.show()
        self.raise_()
        self.activateWindow()

        if added:
            print(f"🔷 LiDAR classif: {added} fence(s) pre-loaded from selection")

    def restore_fence_mode(self):
        """Reset fence selector to direct-open (toolbar) mode.

        Called by _open_or_restore so that reopening a dialog from the toolbar
        after it was used via the Select panel shows the fence buttons again and
        starts with a clean fence list.
        """
        fence_sel = getattr(self, '_fence_sel', None)
        if fence_sel is None:
            return
        fence_sel.selected_fences.clear()
        fence_sel.select_btn.setVisible(True)
        fence_sel.clear_btn.setVisible(True)
        fence_sel._update_status()

# ═══════════════════════════════════════════════════════════════════════
# FENCE GROUP FACTORY
# ═══════════════════════════════════════════════════════════════════════
def _fence_group(dialog, *, picker_as_tool_window=False) -> tuple:
    g = QGroupBox("Fence / Spatial Filter")
    lay = QVBoxLayout(g); lay.setContentsMargins(8, 6, 8, 6)
    sel = FenceSelectorWidget(
        dialog.app,
        parent=g,
        picker_as_tool_window=picker_as_tool_window,
    )
    lay.addWidget(sel)
    dialog._fence_sel = sel
    return g, sel


# ═══════════════════════════════════════════════════════════════════════
# DIALOG 1 — CLASSIFY LOW POINTS
# ═══════════════════════════════════════════════════════════════════════
class ClassifyLowPointsDialog(_BaseClassifyDialog):
    _persist_prefix = "low_points"
    _keep_visible_during_run = True
    _preserve_fence_on_restore = True

    def __init__(self, app, parent=None):
        super().__init__(app, "Classify Low Points", parent)
        self._build_ui()
        self.setMinimumSize(460, 560)
        self.resize(500, 760)

    def _build_ui(self):
        L = self._content_layout
        L.setContentsMargins(10, 8, 10, 8)
        L.setSpacing(8)

        source_default, target_default = _resolve_noise_class_defaults(
            self.app,
            isolated=False,
        )
        
        # ── Classes ──────────────────────────────────────────────────
        g = QGroupBox("Classes Selection")
        f = QFormLayout(g)
        f.setLabelAlignment(Qt.AlignRight)
        f.setSpacing(8)
        f.setContentsMargins(12, 8, 12, 8)

        r1, self._get_from = _make_class_row(
            self.app, [source_default], multi=True,
            persist_key=self._pk("from_class"), extended=True)
        r2, self._get_to = _make_class_row(
            self.app, [target_default], multi=False,
            single_default=target_default,
            persist_key=self._pk("to_class"))

        f.addRow("From class:",     _w(r1))
        f.addRow("To class:",       _w(r2))
        L.addWidget(g)

        # ── Search ───────────────────────────────────────────────────
        g2 = QGroupBox("Low Point Conditions")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setSpacing(8)
        f2.setContentsMargins(12, 8, 12, 8)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Single point", "Groups of points"])
        self._persist_combo_index(self.mode_combo, "search_mode", 1)
        self.mode_combo.setToolTip(
            "Single: only one separated low point is accepted.\n"
            "Groups: the lowest separated tier is accepted only when its "
            "point count is at or below Max count."
        )
        f2.addRow("Search:", self.mode_combo)

        self._max_count_lbl = QLabel("Max count:")
        self.max_spin = QSpinBox()
        self.max_spin.setRange(1, 99)
        self._persist_spin(self.max_spin, "max_count", 6)
        self.max_spin.setToolTip(
            "Largest low group that may be moved. A larger low patch remains "
            "unchanged."
        )
        mc_row = QHBoxLayout()
        mc_row.addWidget(self.max_spin)
        mc_row.addWidget(QLabel("points maximum"))
        self._max_count_widget = _w(mc_row)
        f2.addRow(self._max_count_lbl, self._max_count_widget)

        def _update_max_count_visibility(index):
            is_groups = (index == 1)
            self._max_count_lbl.setVisible(is_groups)
            self._max_count_widget.setVisible(is_groups)

        self.mode_combo.currentIndexChanged.connect(_update_max_count_visibility)
        _update_max_count_visibility(self.mode_combo.currentIndex())

        self.mt_spin = QDoubleSpinBox()
        self.mt_spin.setRange(0.01, 999.0)
        self.mt_spin.setDecimals(2)
        self._persist_spin(self.mt_spin, "more_than", 0.50)
        self.mt_spin.setToolTip(
            "Minimum vertical gap between the low point/group and every "
            "higher surrounding source point. TerraScan normally uses "
            "0.3–1.0 m."
        )
        mr = QHBoxLayout()
        mr.addWidget(self.mt_spin)
        mr.addWidget(QLabel("m  vertical separation"))
        mr.addStretch()
        f2.addRow("More than:", _w(mr))

        self.within_spin = QDoubleSpinBox()
        self.within_spin.setRange(0.1, 9999.0)
        self.within_spin.setDecimals(2)
        self._persist_spin(self.within_spin, "within", 5.00)
        self.within_spin.setToolTip(
            "Horizontal (2D) radius used to find surrounding From-class "
            "points. TerraScan normally uses 2–8 m."
        )
        wr = QHBoxLayout()
        wr.addWidget(self.within_spin)
        wr.addWidget(QLabel("m  horizontal radius"))
        f2.addRow("Within:", _w(wr))
        L.addWidget(g2)

        explanation = QLabel(
            "<b>Simple rule:</b> Look sideways within the 2D radius. "
            "Move only the lowest From-class point or small group when the "
            "next surrounding level is higher by more than the selected "
            "vertical gap.<br><br>"
            "This tool does <b>not</b> use Ground class, terrain angle, or a "
            "TIN. One run finds the lowest level; run it again only when a "
            "second, higher noise level must also be removed."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(_note_text_style())
        L.addWidget(explanation)

        # ── Fence ────────────────────────────────────────────────────
        fg, self._fence_sel = _fence_group(
            self,
            picker_as_tool_window=True,
        )
        L.addWidget(fg)

        L.addStretch()
        self._make_buttons()

    def _on_ok(self):
        self._run_algorithm(classify_low_points, {
            "from_classes":   self._get_from(),
            "to_class":       self._get_to()[0],
            "fence_mask":     self._get_fence_mask(self._fence_sel),
            "search_mode":    "single" if self.mode_combo.currentIndex() == 0
                              else "groups",
            "max_count":      self.max_spin.value(),
            "more_than":      self.mt_spin.value(),
            "within":         self.within_spin.value(),
        }, "Classify Low Points")


# ═══════════════════════════════════════════════════════════════════════
# DIALOG 2 — CLASSIFY ISOLATED POINTS
# ═══════════════════════════════════════════════════════════════════════
class ClassifyIsolatedPointsDialog(_BaseClassifyDialog):
    _persist_prefix = "isolated"
    _keep_visible_during_run = True
    _preserve_fence_on_restore = True

    def __init__(self, app, parent=None):
        super().__init__(app, "Classify Isolated Points", parent)
        self._build_ui()
        self.setMinimumSize(460, 550)
        self.resize(500, 710)

    def _build_ui(self):
        L = self._content_layout
        L.setContentsMargins(10, 8, 10, 8)
        L.setSpacing(8)

        source_default, target_default = _resolve_noise_class_defaults(
            self.app,
            isolated=True,
        )

        # ── Classes group ─────────────────────────────────────────────
        g = QGroupBox("Classes Selection")
        f = QFormLayout(g)
        f.setLabelAlignment(Qt.AlignRight)
        f.setSpacing(8)
        f.setContentsMargins(12, 8, 12, 8)

        # ── FROM CLASS ────────────────────────────────────────────────
        r1, self._get_from = _make_class_row(
            self.app, [source_default], multi=True,
            persist_key=self._pk("from_class"), extended=True)
        f.addRow("From class:", _w(r1))

        # ── TO CLASS ──────────────────────────────────────────────────
        r2, self._get_to = _make_class_row(
            self.app, [target_default], multi=False,
            single_default=target_default,
            persist_key=self._pk("to_class"))
        f.addRow("To class:", _w(r2))
        L.addWidget(g)

        # ── Isolation criteria group ──────────────────────────────────
        g2 = QGroupBox("Isolation Criteria")
        f2 = QFormLayout(g2)
        f2.setLabelAlignment(Qt.AlignRight)
        f2.setSpacing(8)
        f2.setContentsMargins(12, 8, 12, 8)

        self.fewer_spin = QSpinBox()
        self.fewer_spin.setRange(1, 9999)
        self._persist_spin(self.fewer_spin, "if_fewer_than", 1)
        self.fewer_spin.setToolTip(
            "Move the candidate when the number of other In-class points in "
            "the sphere is smaller than this value. The candidate itself is "
            "never counted."
        )
        fr = QHBoxLayout(); fr.addWidget(self.fewer_spin)
        fr.addWidget(QLabel("other points"))
        f2.addRow("If fewer than:", _w(fr))

        r3, self._get_in = _make_class_row(
            self.app, [source_default], multi=True,
            persist_key=self._pk("in_class"), extended=True)
        f2.addRow("In class:", _w(r3))

        self.within_spin = QDoubleSpinBox()
        self.within_spin.setRange(0.1, 9999.0); self.within_spin.setDecimals(2)
        self._persist_spin(self.within_spin, "within", 5.00)
        self.within_spin.setToolTip(
            "True 3D sphere radius. Both horizontal and vertical distance "
            "must fit inside this radius."
        )
        wr = QHBoxLayout(); wr.addWidget(self.within_spin)
        wr.addWidget(QLabel("m  3D sphere radius"))
        f2.addRow("Within:", _w(wr))

        L.addWidget(g2)

        info = QLabel(
            "<b>Simple rule:</b> For every From-class point, draw a 3D "
            "sphere and count the <b>other</b> points belonging to In class. "
            "If the count is smaller than “If fewer than”, move the point to "
            "To class.<br><br>"
            "Example: “fewer than 2 within 1.0 m” moves a point with 0 or 1 "
            "neighbor, but keeps a point with 2 or more. A fence limits which "
            "points may change; points just outside it still count as valid "
            "neighbors."
        )
        info.setStyleSheet(_note_text_style())
        info.setWordWrap(True)
        L.addWidget(info)

        # ── Fence ─────────────────────────────────────────────────────
        fg, self._fence_sel = _fence_group(
            self,
            picker_as_tool_window=True,
        )
        L.addWidget(fg)
        L.addStretch(); self._make_buttons()

    def _on_ok(self):
        from_codes = self._get_from()
        if not from_codes:
            QMessageBox.warning(self, "No Selection",
                                "Please select at least one 'From class'.")
            return

        kwargs = {
            "from_classes":    from_codes,
            "to_class":        self._get_to()[0],
            "in_classes":      self._get_in(),
            "if_fewer_than":   self.fewer_spin.value(),
            "within":          self.within_spin.value(),
            "fence_mask":      self._get_fence_mask(self._fence_sel),
        }

        self._run_algorithm(classify_isolated_points, kwargs,
                            "Classify Isolated Points")

# ═══════════════════════════════════════════════════════════════════════
# DIALOG 3 — CLASSIFY GROUND
# ═══════════════════════════════════════════════════════════════════════
class ClassifyGroundDialog(_BaseClassifyDialog):
    _persist_prefix = "ground"
    _keep_visible_during_run = True
    _preserve_fence_on_restore = True

    def __init__(self, app, parent=None):
        super().__init__(app, "Classify Ground", parent)
        self._build_ui()
        self.setMinimumSize(470, 560)
        self.resize(520, 640)

    def _build_ui(self):
        L = self._content_layout
        L.setContentsMargins(8, 6, 8, 8)
        L.setSpacing(6)

        def _page():
            page = QWidget()
            page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(14, 12, 14, 12)
            page_layout.setSpacing(9)
            return page, page_layout

        def _section(layout, text):
            label = QLabel(text)
            label.setStyleSheet(
                f"color:{ThemeColors.get('accent')}; "
                "font-size:12px; font-weight:600;"
            )
            layout.addWidget(label)
            return label

        def _separator(layout):
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setFrameShadow(QFrame.Sunken)
            layout.addWidget(line)

        def _note(text, *, warning=False):
            label = QLabel(text)
            label.setWordWrap(True)
            color = ThemeColors.get(
                'warning' if warning else 'text_muted'
            )
            label.setStyleSheet(
                f"color:{color}; font-size:10px; line-height:1.25;"
            )
            return label

        def _form():
            form = QFormLayout()
            form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
            form.setContentsMargins(0, 0, 0, 0)
            form.setHorizontalSpacing(10)
            form.setVerticalSpacing(8)
            form.setFieldGrowthPolicy(
                QFormLayout.AllNonFixedFieldsGrow
            )
            return form

        def _add_row(form, text, field, explanation):
            label = QLabel(text)
            label.setToolTip(explanation)
            if isinstance(field, QWidget):
                field.setToolTip(explanation)
                for child in field.findChildren(QWidget):
                    if not child.toolTip():
                        child.setToolTip(explanation)
            form.addRow(label, field)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.setMovable(False)
        self._tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        L.addWidget(self._tabs, 1)

        # ── SETUP TAB ──────────────────────────────────────────────
        setup_page, setup = _page()
        setup.addWidget(_note(
            "Choose which points may change, which ground surface guides the "
            "TIN, and the exact spatial area to process."
        ))

        _section(setup, "Classes")
        f1 = _form()
        f1.setLabelAlignment(Qt.AlignRight)
        source_default, ground_default = _resolve_ground_class_defaults(self.app)
        self._detected_ground_class = ground_default
        r1, self._get_from = _make_class_row(
            self.app, [source_default], multi=True,
            persist_key=self._pk("from_class"), extended=True)
        r2, self._get_to = _make_class_row(
            self.app, [ground_default], multi=False, single_default=ground_default,
            persist_key=self._pk("to_class"))
        r3, self._get_cur = _make_class_row(
            self.app, [ground_default], multi=True,
            persist_key=self._pk("current_ground"), extended=True)
        _add_row(
            f1, "From class:", _w(r1),
            "Only points in these source classes are candidates for change."
        )
        _add_row(
            f1, "To class:", _w(r2),
            "Accepted candidates are assigned to this ground class."
        )
        _add_row(
            f1, "Current ground:", _w(r3),
            "Existing trusted ground points guide the TIN but are not "
            "reclassified by this run."
        )
        detected_label = _get_las_classes(self.app).get(
            ground_default,
            str(ground_default),
        )
        class_note = QLabel(f"Detected ground destination: {detected_label}")
        class_note.setWordWrap(True)
        class_note.setStyleSheet(
            f"color:{ThemeColors.get('text_muted')}; font-size:10px;"
        )
        f1.addRow("", class_note)
        setup.addLayout(f1)

        _separator(setup)
        _section(setup, "Initial TIN")
        f2 = _form()
        self.select_combo = QComboBox()
        self.select_combo.addItems([
            "Aerial low + Ground points", "Lowest point only", "Ground points only"])
        self._persist_combo_index(self.select_combo, "seed_method", 0)
        _add_row(
            f2, "Seed selection:", self.select_combo,
            "Aerial low + Ground uses one local-low point per search cell plus "
            "current ground. Lowest only ignores current ground. Ground only "
            "continues from current ground and falls back to local lows if "
            "fewer than three seeds exist."
        )
        self.max_bldg_spin = QDoubleSpinBox()
        self.max_bldg_spin.setRange(1, 9999); self.max_bldg_spin.setDecimals(1)
        self._persist_spin(self.max_bldg_spin, "max_building_size", 60.0)
        self.max_bldg_spin.setSuffix(" m")
        _add_row(
            f2, "Search-cell size:", self.max_bldg_spin,
            "Side length of the seed-search grid. Each occupied cell contributes "
            "its lowest point. Use a value near the largest building dimension."
        )
        setup.addLayout(f2)

        _separator(setup)
        _section(setup, "Processing Area")
        self._fence_sel = FenceSelectorWidget(
            self.app,
            parent=setup_page,
            picker_as_tool_window=True,
        )
        setup.addWidget(self._fence_sel)
        setup.addWidget(_note(
            "No selection processes all eligible points. A selected fence "
            "remains active after every run until you replace it, clear it, or "
            "close this tool."
        ))
        setup.addStretch()
        self._tabs.addTab(setup_page, "Setup")

        # ── TERRAIN TAB ────────────────────────────────────────────
        terrain_page, terrain = _page()
        terrain.addWidget(_note(
            "These controls decide whether a point is geometrically close "
            "enough to the evolving ground TIN."
        ))
        _section(terrain, "Terrain Profile")
        self.terrain_profile_combo = QComboBox()
        self.terrain_profile_combo.addItem(
            "Auto-adaptive (Recommended)", "auto"
        )
        self.terrain_profile_combo.addItem("Flat / Urban", "flat_urban")
        self.terrain_profile_combo.addItem("Rolling Terrain", "rolling")
        self.terrain_profile_combo.addItem("Hilly Terrain", "hilly")
        self.terrain_profile_combo.addItem(
            "Mountain / Steep Terrain", "mountain"
        )
        self.terrain_profile_combo.addItem("Custom", "custom")
        self._persist_combo_index(
            self.terrain_profile_combo,
            "terrain_profile",
            0,
        )
        self.terrain_profile_combo.setToolTip(
            "Auto estimates thresholds from the local-low seed TIN. Named "
            "profiles apply locked repeatable values. Custom enables editing."
        )
        terrain.addWidget(self.terrain_profile_combo)
        self.terrain_profile_note = QLabel()
        self.terrain_profile_note.setWordWrap(True)
        self.terrain_profile_note.setStyleSheet(
            f"color:{ThemeColors.get('text_muted')}; font-size:10px;"
        )
        terrain.addWidget(self.terrain_profile_note)

        _separator(terrain)
        _section(terrain, "Acceptance Thresholds")
        f3 = _form()
        self.terrain_spin = QDoubleSpinBox()
        self.terrain_spin.setRange(0.1, 90); self.terrain_spin.setDecimals(2)
        self._persist_spin(self.terrain_spin, "terrain_angle", 88.00)
        ta = QHBoxLayout(); ta.addWidget(self.terrain_spin); ta.addWidget(QLabel("degrees"))
        _add_row(
            f3, "Terrain angle:", _w(ta),
            "Steepest containing TIN triangle allowed to accept new points. "
            "Triangles steeper than this limit do not densify."
        )
        self.ia_spin = QDoubleSpinBox()
        self.ia_spin.setRange(0.1, 90); self.ia_spin.setDecimals(2)
        self._persist_spin(self.ia_spin, "iteration_angle", 6.00)
        ia = QHBoxLayout(); ia.addWidget(self.ia_spin); ia.addWidget(QLabel("degrees to plane"))
        _add_row(
            f3, "Iteration angle:", _w(ia),
            "Main acceptance/density limit: angle from the candidate's plane "
            "projection to the closest triangle vertex. Larger values follow "
            "small terrain variation and low objects more aggressively."
        )
        self.id_spin = QDoubleSpinBox()
        self.id_spin.setRange(0.01, 999); self.id_spin.setDecimals(2)
        self._persist_spin(self.id_spin, "iteration_distance", 1.40)
        self.id_spin.setSuffix(" m")
        _add_row(
            f3, "Iteration distance:", self.id_spin,
            "Maximum absolute perpendicular distance from a candidate to its "
            "TIN triangle plane. Smaller values reject low vegetation and low "
            "objects more strongly."
        )
        terrain.addLayout(f3)
        self.terrain_profile_combo.currentIndexChanged.connect(
            self._apply_terrain_profile
        )
        self._apply_terrain_profile()

        _separator(terrain)
        _section(terrain, "Decision Logic")
        terrain.addWidget(_note(
            "A source point is accepted only when its triangle slope, "
            "point-to-plane distance and iteration angle all pass. Every "
            "accepted point joins the next TIN iteration."
        ))
        terrain.addWidget(_note(
            "Flat terrain usually needs a smaller iteration angle and "
            "distance; steep natural terrain needs more tolerance. Larger "
            "values increase ground recall but also the risk of accepting low "
            "vegetation or structures.",
            warning=True,
        ))
        terrain.addStretch()
        self._tabs.addTab(terrain_page, "Terrain")

        # ── STRATEGY TAB ───────────────────────────────────────────
        strategy_page, strategy = _page()
        strategy.addWidget(_note(
            "Fine-tune small-triangle density, weighted acceptance and the "
            "optional Naksha Class-0 quality-control sample."
        ))
        _section(strategy, "TIN Densification")
        self.reduce_chk = QCheckBox("Reduce angle in small triangles")
        self._persist_checkbox(self.reduce_chk, "reduce_angle_edge", True)
        self.reduce_chk.setToolTip(
            "When a triangle's longest edge is below the limit, Naksha scales "
            "the effective iteration angle linearly toward zero."
        )
        er = QHBoxLayout()
        er.addWidget(self.reduce_chk)
        er.addStretch()
        er.addWidget(QLabel("Edge <"))
        self.edge_spin = QDoubleSpinBox()
        self.edge_spin.setRange(0.1, 999); self.edge_spin.setDecimals(1)
        self._persist_spin(self.edge_spin, "edge_length_threshold", 5.0)
        self.edge_spin.setSuffix(" m")
        self.edge_spin.setToolTip(self.reduce_chk.toolTip())
        er.addWidget(self.edge_spin)
        strategy.addLayout(er)
        self.reduce_chk.stateChanged.connect(lambda s: self.edge_spin.setEnabled(bool(s)))
        self.edge_spin.setEnabled(self.reduce_chk.isChecked())

        _separator(strategy)
        self.stop_chk = QCheckBox("Stop small-triangle densification")
        self._persist_checkbox(self.stop_chk, "stop_triangulation", False)
        self.stop_chk.setToolTip(
            "Do not add more points inside a triangle once its longest edge is "
            "shorter than this limit."
        )
        sr = QHBoxLayout()
        sr.addWidget(self.stop_chk)
        sr.addStretch()
        sr.addWidget(QLabel("Edge <"))
        self.stop_edge_spin = QDoubleSpinBox()
        self.stop_edge_spin.setRange(0.01, 999); self.stop_edge_spin.setDecimals(2)
        self._persist_spin(self.stop_edge_spin, "stop_edge_length", 2.00)
        self.stop_edge_spin.setSuffix(" m")
        self.stop_edge_spin.setToolTip(self.stop_chk.toolTip())
        self.stop_edge_spin.setEnabled(self.stop_chk.isChecked())
        sr.addWidget(self.stop_edge_spin)
        strategy.addLayout(sr)
        self.stop_chk.stateChanged.connect(lambda s: self.stop_edge_spin.setEnabled(bool(s)))

        _separator(strategy)
        self.dist_rating_chk = QCheckBox(
            "Use TerraScan distance attribute (not loaded)"
        )
        self._persist_checkbox(self.dist_rating_chk, "use_distance_as_rating", False)
        self.dist_rating_chk.setToolTip(
            "TerraScan's option requires a stored vegetation-index, echo-"
            "length, or deviation-distance attribute. Naksha currently loads "
            "only XYZ and Classification, so enabling a look-alike formula "
            "would not provide MicroStation parity."
        )
        self.dist_rating_chk.setChecked(False)
        self.dist_rating_chk.setEnabled(False)
        wr = QHBoxLayout()
        wr.addWidget(self.dist_rating_chk)
        wr.addStretch()
        wr.addWidget(QLabel("Weight:"))
        self.weight_spin = QSpinBox()
        self.weight_spin.setRange(0, 100); self.weight_spin.setSuffix("  %")
        self._persist_spin(self.weight_spin, "distance_weight", 50)
        self.weight_spin.setToolTip(self.dist_rating_chk.toolTip())
        self.weight_spin.setEnabled(False)
        wr.addWidget(self.weight_spin)
        strategy.addLayout(wr)
        self.dist_rating_chk.stateChanged.connect(
            lambda s: self.weight_spin.setEnabled(bool(s)))

        _separator(strategy)
        self.upward_chk = QCheckBox("Add only upward points")
        self._persist_checkbox(self.upward_chk, "add_only_upward", False)
        self.upward_chk.setToolTip(
            "Reject candidates below the current triangle plane. This protects "
            "the surface from low-error observations."
        )
        strategy.addWidget(self.upward_chk)
        strategy.addWidget(_note(
            "Reduction and stop limits control how densely small triangles "
            "continue to collect ground points. They do not randomly remove "
            "points from the TIN."
        ))

        _separator(strategy)
        _section(strategy, "Ground Density / QC")
        self.class0_holdout_chk = QCheckBox(
            "Reserve Class-0 QC sample"
        )
        self._persist_checkbox(
            self.class0_holdout_chk,
            "class0_holdout_enabled",
            True,
        )
        density_row = QHBoxLayout()
        density_row.addWidget(self.class0_holdout_chk)
        density_row.addStretch()
        density_row.addWidget(QLabel("Per 100:"))
        self.class0_holdout_spin = QSpinBox()
        self.class0_holdout_spin.setRange(1, 10)
        self.class0_holdout_spin.setSuffix("  %")
        self._persist_spin(self.class0_holdout_spin, "class0_holdout_percent", 5)
        density_row.addWidget(self.class0_holdout_spin)
        density_explanation = (
            "After geometric classification, this deterministic spatial sample "
            "moves the selected percentage of newly accepted points to Class 0. "
            "Initial seeds and existing ground stay protected. Disable it for "
            "strict TerraScan parity."
        )
        self.class0_holdout_chk.setToolTip(density_explanation)
        self.class0_holdout_spin.setToolTip(density_explanation)
        strategy.addLayout(density_row)
        strategy.addWidget(_note(density_explanation, warning=True))
        self.class0_holdout_chk.stateChanged.connect(
            lambda state: self.class0_holdout_spin.setEnabled(bool(state))
        )
        self.class0_holdout_spin.setEnabled(
            self.class0_holdout_chk.isChecked()
        )
        strategy.addStretch()
        self._tabs.addTab(strategy_page, "Strategy & QC")

        saved_tab = int(
            PersistentClassSettings.get_value(self._pk("active_tab"), 0)
        )
        self._tabs.setCurrentIndex(
            min(max(saved_tab, 0), self._tabs.count() - 1)
        )
        self._tabs.currentChanged.connect(
            lambda index: PersistentClassSettings.set_value(
                self._pk("active_tab"), index
            )
        )
        self._make_buttons()

    def _apply_terrain_profile(self):
        profile = self.terrain_profile_combo.currentData() or "auto"
        is_custom = profile == "custom"

        if profile in GROUND_TERRAIN_PRESETS:
            values = GROUND_TERRAIN_PRESETS[profile]
            self.terrain_spin.setValue(values["terrain_angle"])
            self.ia_spin.setValue(values["iteration_angle"])
            self.id_spin.setValue(values["iteration_distance"])

        for spin in (self.terrain_spin, self.ia_spin, self.id_spin):
            spin.setEnabled(is_custom)

        if profile == "auto":
            note = (
                "Thresholds are calculated for each run from the local-low "
                "seed TIN inside the active fence. The effective values and "
                "estimated slope are reported when classification finishes."
            )
        elif profile in GROUND_TERRAIN_PRESETS:
            values = GROUND_TERRAIN_PRESETS[profile]
            note = (
                "Locked values: terrain "
                f"{values['terrain_angle']:.0f}°, iteration "
                f"{values['iteration_angle']:.0f}°, distance "
                f"{values['iteration_distance']:.2f} m."
            )
        else:
            note = (
                "Manual threshold editing is enabled. Larger iteration angle "
                "or distance accepts points more aggressively."
            )
        self.terrain_profile_note.setText(note)

    def _on_ok(self):
        to_codes = self._get_to()
        if not to_codes:
            QMessageBox.warning(
                self,
                "No Ground Destination",
                "Select a destination ground class.",
            )
            return

        to_class = int(to_codes[0])
        detected_ground = int(
            getattr(self, "_detected_ground_class", to_class)
        )
        if to_class != detected_ground:
            labels = _get_las_classes(self.app)
            selected_label = labels.get(to_class, str(to_class))
            ground_label = labels.get(detected_ground, str(detected_ground))
            answer = QMessageBox.question(
                self,
                "Destination Is Not Labeled Ground",
                f"The selected destination is {selected_label}.\n"
                f"The active class map labels {ground_label} as Ground.\n\n"
                "Continue with the selected non-ground destination?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        sm = {0: "aerial_low_ground", 1: "lowest_only", 2: "ground_only"}
        terrain_profile = (
            self.terrain_profile_combo.currentData() or "auto"
        )
        self._run_algorithm(classify_ground_ptd, {
            "from_classes":          self._get_from(),
            "to_class":              to_class,
            "current_ground":        self._get_cur(),
            "seed_method":           sm.get(self.select_combo.currentIndex(),
                                            "aerial_low_ground"),
            "max_building_size":     self.max_bldg_spin.value(),
            "terrain_angle":         self.terrain_spin.value(),
            "iteration_angle":       self.ia_spin.value(),
            "iteration_distance":    self.id_spin.value(),
            "reduce_angle_edge":     self.reduce_chk.isChecked(),
            "edge_length_threshold": self.edge_spin.value(),
            "stop_triangulation":    self.stop_chk.isChecked(),
            "stop_edge_length":      self.stop_edge_spin.value(),
            "use_distance_as_rating":self.dist_rating_chk.isChecked(),
            "distance_weight":       float(self.weight_spin.value()),
            "add_only_upward":       self.upward_chk.isChecked(),
            "adaptive_thresholds":   terrain_profile == "auto",
            "class0_holdout_percent":(
                float(self.class0_holdout_spin.value())
                if self.class0_holdout_chk.isChecked()
                else 0.0
            ),
            "fence_mask":            self._get_fence_mask(self._fence_sel),
        }, "Classify Ground")


# ═══════════════════════════════════════════════════════════════════════
# DIALOG 4 — CLASSIFY SURFACE POINTS
# ═══════════════════════════════════════════════════════════════════════
class ClassifySurfacePointsDialog(_BaseClassifyDialog):
    """TerraScan-style Surface Points tool with its published controls."""
    _persist_prefix = "surface_points"
    _keep_visible_during_run = True
    _preserve_fence_on_restore = True

    def __init__(self, app, parent=None):
        super().__init__(app, "Classify Surface Points", parent)
        self._build_ui()
        self.resize(500, 580)

    def _build_ui(self):
        layout = self._content_layout
        source_default, target_default = _resolve_surface_class_defaults(
            self.app
        )

        classes_group = QGroupBox("Classes Selection")
        classes_form = QFormLayout(classes_group)
        classes_form.setLabelAlignment(Qt.AlignRight)
        classes_form.setContentsMargins(15, 10, 15, 10)
        classes_form.setSpacing(12)
        from_row, self._get_from = _make_class_row(
            self.app,
            [source_default],
            multi=True,
            persist_key=self._pk("from_class"),
            extended=True,
        )
        to_row, self._get_to = _make_class_row(
            self.app,
            [target_default],
            multi=False,
            single_default=target_default,
            persist_key=self._pk("to_class"),
        )
        classes_form.addRow("From class:", _w(from_row))
        classes_form.addRow("To class:", _w(to_row))
        layout.addWidget(classes_group)

        settings_group = QGroupBox("Surface Recognition")
        settings_form = QFormLayout(settings_group)
        settings_form.setLabelAlignment(Qt.AlignRight)
        settings_form.setContentsMargins(15, 10, 15, 10)
        settings_form.setSpacing(12)

        self.tolerance_spin = QDoubleSpinBox()
        self.tolerance_spin.setRange(0.001, 10.0)
        self.tolerance_spin.setDecimals(3)
        self.tolerance_spin.setSingleStep(0.005)
        self._persist_spin(self.tolerance_spin, "tolerance", 0.05)
        self.tolerance_spin.setToolTip(
            "Maximum perpendicular offset from the locally smooth surface. "
            "Smaller values keep cleaner surfaces; larger values include more "
            "surface noise."
        )
        tolerance_row = QHBoxLayout()
        tolerance_row.addWidget(self.tolerance_spin)
        tolerance_row.addWidget(QLabel("m from local surface"))
        settings_form.addRow("Tolerance:", _w(tolerance_row))
        layout.addWidget(settings_group)

        explanation = QLabel(
            "Finds locally smooth surfaces in full 3D. It can recognize flat "
            "ground, sloped roofs, rounded objects, and vertical walls. Points "
            "farther than the tolerance or in rough/scattered neighborhoods "
            "remain unchanged."
        )
        explanation.setWordWrap(True)
        explanation.setStyleSheet(_note_text_style())
        explanation.setToolTip(
            "MicroStation/TerraScan Surface Points uses From class, To class, "
            "Tolerance, and Inside fence only. The local neighborhood is "
            "selected automatically."
        )
        layout.addWidget(explanation)

        fence_group, self._fence_sel = _fence_group(
            self,
            picker_as_tool_window=True,
        )
        layout.addWidget(fence_group)
        layout.addStretch()
        self._make_buttons()

    def _on_ok(self):
        self._run_algorithm(
            classify_surface_points,
            {
                "from_classes": self._get_from(),
                "to_class": self._get_to()[0],
                "tolerance": self.tolerance_spin.value(),
                "fence_mask": self._get_fence_mask(self._fence_sel),
            },
            "Classify Surface Points",
        )


# ═══════════════════════════════════════════════════════════════════════
# DIALOG 5 — CLASSIFY BELOW SURFACE
# ═══════════════════════════════════════════════════════════════════════
class ClassifyBelowSurfaceDialog(_BaseClassifyDialog):
    _persist_prefix = "below_surface"
    _keep_visible_during_run = True
    _preserve_fence_on_restore = True

    def __init__(self, app, parent=None):
        super().__init__(app, "Classify Below Surface", parent)
        self._build_ui()
        self.resize(520, 650)

    def _build_ui(self):
        L = self._content_layout
        _, ground_default = _resolve_ground_class_defaults(self.app)
        _, noise_default = _resolve_noise_class_defaults(
            self.app,
            isolated=False,
        )
        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        L.addWidget(self._tabs, 1)

        setup_page = QWidget()
        setup = QVBoxLayout(setup_page)
        setup.setContentsMargins(8, 8, 8, 8)
        setup.setSpacing(10)
        cleanup_page = QWidget()
        cleanup = QVBoxLayout(cleanup_page)
        cleanup.setContentsMargins(8, 8, 8, 8)
        cleanup.setSpacing(10)

        # ── Classes group ─────────────────────────────────────────────
        g1 = QGroupBox("Classes Selection")
        f1 = QFormLayout(g1)
        f1.setLabelAlignment(Qt.AlignRight)
        f1.setContentsMargins(15, 10, 15, 10)
        f1.setSpacing(12)
        r1, self._get_from = _make_class_row(
            self.app, [ground_default], multi=True,
            persist_key=self._pk("from_class"), extended=True)
        r2, self._get_to = _make_class_row(
            self.app, [noise_default], multi=False,
            single_default=noise_default,
            persist_key=self._pk("to_class"))
        f1.addRow("From class:", _w(r1)); f1.addRow("To class:", _w(r2))
        setup.addWidget(g1)

        # ── Surface fitting group ─────────────────────────────────────
        g2 = QGroupBox("Surface Fitting")
        f2 = QFormLayout(g2); f2.setLabelAlignment(Qt.AlignRight)

        self.surface_combo = QComboBox()
        self.surface_combo.addItems(["Planar  (z = ax + by + c)",
                                     "Curved  (z = ax² + by² + cxy + dx + ey + f)"])
        self._persist_combo_index(self.surface_combo, "surface_type", 0)
        f2.addRow("Surface:", self.surface_combo)

        self.limit_spin = QDoubleSpinBox()
        self.limit_spin.setRange(0.1, 999); self.limit_spin.setDecimals(1)
        self._persist_spin(self.limit_spin, "limit", 4.0)
        lr = QHBoxLayout(); lr.addWidget(self.limit_spin)
        lr.addWidget(QLabel("× standard deviation"))
        f2.addRow("Limit:", _w(lr))

        self.ztol_spin = QDoubleSpinBox()
        self.ztol_spin.setRange(0.001, 99); self.ztol_spin.setDecimals(2)
        self._persist_spin(self.ztol_spin, "z_tolerance", 0.10)
        zr = QHBoxLayout(); zr.addWidget(self.ztol_spin); zr.addWidget(QLabel("m"))
        f2.addRow("Z tolerance:", _w(zr))

        self.nn_spin = QSpinBox(); self.nn_spin.setRange(3, 200)
        self._persist_spin(self.nn_spin, "num_neighbors", 25)
        nr = QHBoxLayout(); nr.addWidget(self.nn_spin)
        nr.addWidget(QLabel("nearest neighbors for surface fit"))
        f2.addRow("K neighbors:", _w(nr))

        setup.addWidget(g2)
        setup.addStretch()
        self._tabs.addTab(setup_page, "Surface Rule")

        # ── Options group ─────────────────────────────────────────────
        g3 = QGroupBox("Processing Options")
        ol = QVBoxLayout(g3)
        ol.setContentsMargins(15, 20, 15, 15)
        ol.setSpacing(8)

        self.iter_chk = QCheckBox("🔄 Iterate until stable (progressive cleanup)")
        self._persist_checkbox(self.iter_chk, "iterative", False)
        ol.addWidget(self.iter_chk)

        ir = QHBoxLayout(); ir.addSpacing(24); ir.addWidget(QLabel("Max iterations:"))
        self.max_iter_spin = QSpinBox(); self.max_iter_spin.setRange(1, 50)
        self._persist_spin(self.max_iter_spin, "max_iterations", 5)
        self.max_iter_spin.setEnabled(self.iter_chk.isChecked())
        ir.addWidget(self.max_iter_spin); ir.addStretch(); ol.addLayout(ir)
        self.iter_chk.stateChanged.connect(
            lambda s: self.max_iter_spin.setEnabled(bool(s)))

        info = QLabel(
            "ℹ️ Fits local surface through K neighbors.\n"
            "   Flags points below surface by > limit × standard deviation.\n"
            "   MicroStation equivalent: post-ground cleanup.")
        info.setStyleSheet(_note_text_style())
        info.setWordWrap(True)
        ol.addWidget(info)

        cleanup.addWidget(g3)

        # ── Fence ─────────────────────────────────────────────────────
        fg, self._fence_sel = _fence_group(
            self,
            picker_as_tool_window=True,
        )
        cleanup.addWidget(fg)
        cleanup.addStretch()
        self._tabs.addTab(cleanup_page, "Processing Area")
        self._make_buttons()

    def _on_ok(self):
        self._run_algorithm(classify_below_surface, {
            "from_classes":   self._get_from(),
            "to_class":       self._get_to()[0],
            "surface_type":   "planar" if self.surface_combo.currentIndex() == 0
                              else "curved",
            "limit":          self.limit_spin.value(),
            "z_tolerance":    self.ztol_spin.value(),
            "num_neighbors":  self.nn_spin.value(),
            "fence_mask":     self._get_fence_mask(self._fence_sel),
            "iterative":      self.iter_chk.isChecked(),
            "max_iterations": self.max_iter_spin.value(),
        }, "Classify Below Surface")


# ═══════════════════════════════════════════════════════════════════════
# PUBLIC API  — restore-aware open helpers
# ═══════════════════════════════════════════════════════════════════════
def _open_or_restore(app, attr: str, cls):
    """
    If the dialog exists and is chip-minimized → restore it.
    If it exists and is visible               → just raise it.
    Otherwise create a fresh instance.
    """
    existing = getattr(app, attr, None)
    if existing is not None:
        try:
            if (
                hasattr(existing, 'restore_fence_mode')
                and not getattr(existing, '_preserve_fence_on_restore', False)
            ):
                existing.restore_fence_mode()
            if existing._is_minimized_to_chip:
                existing._do_restore_from_chip()
            else:
                if existing.windowState() & Qt.WindowMinimized:
                    existing.showNormal()
                existing.show()
                existing.raise_()
                existing.activateWindow()
            return existing
        except RuntimeError:
            # C++ object already deleted — create fresh
            setattr(app, attr, None)

    dlg = cls(app, parent=app)
    dlg.setAttribute(Qt.WA_DeleteOnClose)
    setattr(app, attr, dlg)
    dlg.destroyed.connect(
        lambda _obj=None, ref=dlg: setattr(app, attr, None)
        if getattr(app, attr, None) is ref
        else None
    )
    dlg.show()
    return dlg

def open_classify_low_points(app):
    return _open_or_restore(
        app, '_classify_low_points_dlg', ClassifyLowPointsDialog
    )

def open_classify_isolated_points(app):
    return _open_or_restore(
        app,
        '_classify_isolated_points_dlg',
        ClassifyIsolatedPointsDialog,
    )

def open_classify_ground(app):
    return _open_or_restore(
        app, '_classify_ground_dlg', ClassifyGroundDialog
    )

def open_classify_surface_points(app):
    return _open_or_restore(
        app,
        '_classify_surface_points_dlg',
        ClassifySurfacePointsDialog,
    )

def open_classify_below_surface(app):
    return _open_or_restore(
        app,
        '_classify_below_surface_dlg',
        ClassifyBelowSurfaceDialog,
    )
