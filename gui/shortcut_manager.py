from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QFileDialog, QComboBox, QHeaderView, QMessageBox, QDialog,
    QListWidget, QListWidgetItem, QLabel, QCheckBox, QDoubleSpinBox, QMenu,
    QSizePolicy
)
from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect
from PySide6.QtWidgets import QFrame, QAbstractItemView
from PySide6.QtCore import Signal, Qt, QSettings, QEvent
from PySide6.QtGui import QColor, QAction, QActionGroup
from torch import layout

from .class_picker import ClassPicker
from .theme_manager import get_dialog_stylesheet
from .shading_preset_quality import (
    SHADING_QUALITY_CHOICES,
    normalize_shading_preset_quality,
    shading_quality_label,
)

TOOLS = [
    "AboveLine", "BelowLine", "ParallelLine", "Rectangle", "Circle",
    "Polygon", "Freehand", "Brush", "Point",
    "CrossSectionRect", "CutSectionRect",
    "CutFromCross", "CutFromCut",
    "TopView",
    "DisplayMode",
    "Line",
    "ShadingMode", "Depth",
    "RGB",
    "Intensity",
    "Elevation",
    "Class",
    "Surface",
    "DrawSettings",
    "SyncViews",
    "MeasureLine", "MeasurePath", "ClearMeasurements",
    "Pan",
    "Save", "SaveAs",
]

SIMPLE_SHORTCUT_TOOLS = (
    "CrossSectionRect", "CutSectionRect", "CutFromCross", "CutFromCut",
    "TopView", "Depth", "RGB", "Intensity", "Elevation", "Line", "Class", "Surface",
    "MeasureLine", "MeasurePath", "ClearMeasurements",
    "Pan",
    "Save", "SaveAs",
)

DISPLAY_VISIBILITY_TOOLS = ("Depth", "RGB", "Intensity", "Elevation", "Line")
CLASS_VISIBILITY_PICKER_MODES = (
    "shading", "surface", "depth", "rgb", "intensity", "elevation", "line",
)

import json
import os
import warnings

def encode_classes(from_cls, to_cls):
    return json.dumps({"from": from_cls, "to": to_cls})

def decode_classes(text):
    try:
        data = json.loads(text)
        return data.get("from"), data.get("to")
    except Exception:
        return None, None


def shortcut_search_matches(search_text, modifier="", key="", tool="", classes=""):
    """Return whether a shortcut row matches the search/capture text."""
    query = str(search_text or "").casefold().strip()
    if not query:
        return True

    fields = [modifier, key, tool, classes]
    searchable_text = " ".join(str(value or "").casefold() for value in fields)
    if query in searchable_text:
        return True

    # Keyboard capture produces values such as ``alt+f1``, while Modifier and
    # Key are stored in separate table columns. Include their canonical
    # combined form so captured and manually typed shortcut combinations match.
    compact_query = "".join(query.split())
    shortcut_combo = (
        f"{str(modifier or '').casefold()}+{str(key or '').casefold()}"
    ).replace(" ", "")
    return compact_query in shortcut_combo


def encode_line_mode_preset(payload: dict) -> str:
    classes = {
        str(int(code)): {"show": bool(info.get("show", True))}
        for code, info in dict(payload.get("classes", {}) or {}).items()
        if isinstance(info, dict)
    }
    raw_lines = payload.get("flight_lines", payload.get("lines", {}))
    lines = {
        str(int(line_id)): bool(shown)
        for line_id, shown in dict(raw_lines or {}).items()
    }
    return json.dumps({
        "__type__": "line_mode_preset",
        "target_view": 0,
        "classes": classes,
        "lines": lines,
    })


def decode_line_mode_preset(value):
    try:
        data = value if isinstance(value, dict) else json.loads(value)
        if not isinstance(data, dict):
            return None
        if data.get("__type__") not in (None, "line_mode_preset"):
            return None
        return {
            "target_view": 0,
            "classes": {
                int(code): {"show": bool(info.get("show", True))}
                for code, info in dict(data.get("classes", {}) or {}).items()
                if isinstance(info, dict)
            },
            "lines": {
                int(line_id): bool(shown)
                for line_id, shown in dict(
                    data.get("lines", data.get("flight_lines", {})) or {}
                ).items()
            },
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def summarize_line_mode_preset(preset: dict) -> str:
    classes = dict((preset or {}).get("classes", {}) or {})
    class_visible = sum(
        1 for info in classes.values()
        if isinstance(info, dict) and info.get("show", True)
    )
    lines = dict((preset or {}).get("lines", {}) or {})
    visible = sum(1 for shown in lines.values() if shown)
    return (
        f"Line: {class_visible}/{len(classes)} classes, "
        f"{visible}/{len(lines)} flight lines"
    )


def line_mode_to_display_visibility_preset(preset):
    """Convert legacy LineMode data to the canonical Main View Line preset."""
    preset = dict(preset or {})
    return {
        "mode": "Line",
        "target_view": 0,
        "classes": dict(preset.get("classes", {}) or {}),
        "flight_lines": {
            int(line_id): bool(shown)
            for line_id, shown in dict(
                preset.get("flight_lines", preset.get("lines", {})) or {}
            ).items()
        },
    }


def _is_owned_qt_object(owner, candidate):
    """Return True when candidate is owner or belongs to its Qt parent chain."""
    current = candidate
    visited = set()
    while current is not None and id(current) not in visited:
        if current is owner:
            return True
        visited.add(id(current))
        parent_getter = getattr(current, "parent", None)
        if not callable(parent_getter):
            break
        try:
            current = parent_getter()
        except RuntimeError:
            break
    return False


def encode_display_preset(payload: dict) -> str:
    try:
        # v3 structure: views contain ONLY show/weight per class.
        # description/color/draw/lvl are NEVER persisted — they always
        # come from the currently loaded PTC's class_palette at apply time.
        views = payload.get("views", {})
        views_json = {}

        for view_idx, classes in views.items():
            view_key = str(int(view_idx))
            classes_json = {}

            for k, v in classes.items():
                code = str(int(k))
                classes_json[code] = {
                    "show": bool(v.get("show", False)),
                    "weight": float(v.get("weight", 1.0)),
                }

            views_json[view_key] = classes_json

        preset = {
            "__type__": "display_mode_preset_v3",
            "display_mode": str(payload.get("display_mode", "class") or "class"),
            "border_percent": float(payload.get("border_percent", 0)),
            "border_type": int(payload.get("border_type", 0)),
            "force_refresh": bool(payload.get("force_refresh", True)),
            "views": views_json,
        }
        return json.dumps(preset)
    except Exception as e:
        print(f"❌ encode_display_preset error: {e}")
        return json.dumps({"__type__": "display_mode_preset_v3", "views": {}})

def decode_display_preset(text):
    try:
        # Handle both JSON strings and raw Python dicts (Qt may return either)
        if isinstance(text, dict):
            data = text
        elif isinstance(text, str):
            data = json.loads(text)
        else:
            return None

        if not isinstance(data, dict):
            return None

        preset_type = data.get("__type__")

        # v3 format — thin preset (show/weight only). description/color/
        # draw/lvl are filled in later by rebase_display_preset_to_current_ptc().
        if preset_type == "display_mode_preset_v3":
            views = {}
            for view_idx_str, classes in (data.get("views", {}) or {}).items():
                view_idx = int(view_idx_str)
                views[view_idx] = {
                    int(code_str): {
                        "show": bool(v.get("show", False)),
                        "weight": float(v.get("weight", 1.0)),
                    }
                    for code_str, v in classes.items()
                }
            return {
                "display_mode": str(data.get("display_mode", "class") or "class"),
                "border_percent": float(data.get("border_percent", 0)),
                "border_type": int(data.get("border_type", 0)),
                "force_refresh": bool(data.get("force_refresh", True)),
                "views": views,
            }

        # Handle new multi-view format
        if preset_type == "display_mode_preset_v2":
            views = {}
            for view_idx_str, classes in (data.get("views", {}) or {}).items():
                view_idx = int(view_idx_str)
                classes_dict = {}

                for code_str, v in classes.items():
                    code = int(code_str)
                    classes_dict[code] = {
                        "show": bool(v.get("show", False)),
                        "description": str(v.get("description", "")),
                        "color": tuple(v.get("color", (128, 128, 128))),
                        "weight": float(v.get("weight", 1.0)),
                        "draw": v.get("draw", ""),
                        "lvl": v.get("lvl", ""),
                    }

                views[view_idx] = classes_dict

            return {
                "display_mode": str(data.get("display_mode", "class") or "class"),
                "border_percent": float(data.get("border_percent", 0)),
                "border_type": int(data.get("border_type", 0)),
                "force_refresh": bool(data.get("force_refresh", True)),
                "views": views,
            }

        # Legacy single-view format (backwards compatibility)
        elif preset_type == "display_mode_preset":
            classes = {}
            for code_str, v in (data.get("classes", {}) or {}).items():
                code = int(code_str)
                classes[code] = {
                    "show": bool(v.get("show", False)),
                    "description": str(v.get("description", "")),
                    "color": tuple(v.get("color", (128, 128, 128))),
                    "weight": float(v.get("weight", 1.0)),
                    "draw": v.get("draw", ""),
                    "lvl": v.get("lvl", ""),
                }

            # Convert to new format
            target_view = data.get("target_view", data.get("slot", 0))
            return {
                "display_mode": str(data.get("display_mode", "class") or "class"),
                "border_percent": float(data.get("border_percent", 0)),
                "border_type": int(data.get("border_type", 0)),
                "force_refresh": bool(data.get("force_refresh", True)),
                "views": {target_view: classes},
            }

        # Raw dict with no __type__ — treat as v2 if it has "views" key
        if "views" in data:
            views = {}
            for view_idx_str, classes in (data.get("views", {}) or {}).items():
                view_idx = int(view_idx_str)
                classes_dict = {}
                for code_str, v in classes.items():
                    code = int(code_str)
                    classes_dict[code] = {
                        "show": bool(v.get("show", False)),
                        "description": str(v.get("description", "")),
                        "color": tuple(v.get("color", (128, 128, 128))),
                        "weight": float(v.get("weight", 1.0)),
                        "draw": v.get("draw", ""),
                        "lvl": v.get("lvl", ""),
                    }
                views[view_idx] = classes_dict
            return {
                "display_mode": str(data.get("display_mode", "class") or "class"),
                "border_percent": float(data.get("border_percent", 0)),
                "border_type": int(data.get("border_type", 0)),
                "force_refresh": bool(data.get("force_refresh", True)),
                "views": views,
            }

        return None
    except Exception as e:
        print(f"❌ decode_display_preset error: {e}")
        return None
def rebase_display_preset_to_current_ptc(preset: dict, app_window) -> dict:
    """
    Rebuild a DisplayMode preset's per-class data using the CURRENTLY
    LOADED PTC's app.class_palette.

    - Preserves: shortcut's show/weight values, border_percent,
      border_type, force_refresh.
    - Replaces:  description/color/draw/lvl with the live values from
      app.class_palette (so a stale preset from a different PTC can
      never reapply old class metadata).
    - Classes present in the preset but missing from the current PTC's
      class_palette are dropped.
    """
    current_palette = getattr(app_window, "class_palette", {}) or {}

    rebased_views = {}
    for view_idx, classes in (preset.get("views", {}) or {}).items():
        view_idx = int(view_idx)
        rebased = {}

        for code, info in classes.items():
            code = int(code)
            live = current_palette.get(code)
            if live is None:
                # Class no longer exists in the newly loaded PTC — skip it.
                continue
            rebased[code] = {
                "show":        bool(info.get("show", False)),
                "weight":      float(info.get("weight", 1.0)),
                "description": live.get("description", ""),
                "color":       tuple(live.get("color", (128, 128, 128))),
                "draw":        live.get("draw", ""),
                "lvl":         live.get("lvl", ""),
            }

        rebased_views[view_idx] = rebased

    return {
        "display_mode":   str(preset.get("display_mode", "class") or "class"),
        "border_percent": float(preset.get("border_percent", 0.0)),
        "border_type":    int(preset.get("border_type", 0)),
        "force_refresh":  bool(preset.get("force_refresh", True)),
        "views":          rebased_views,
    }


def summarize_display_like_preset(preset: dict, prefix: str = "Preset") -> str:
    """
    Build a short human-readable summary for DisplayMode-style presets.

    Surface uses the same thin preset format, so we can summarize both tool
    types with one helper without duplicating parsing logic.
    """
    views = preset.get("views", {}) or {}
    if not views:
        return f"{prefix}: Not configured yet"

    parts = []
    border_pct = float(preset.get("border_percent", 0.0))
    mode_key = str(preset.get("display_mode", "class") or "class")
    mode_label = {
        "class": "By Classification",
        "shaded_class": "Shaded Classification",
        "depth": "Depth",
        "intensity": "Intensity",
        "rgb": "RGB",
        "elevation": "Elevation",
        "surface": "Surface",
        "line": "Line",
    }.get(mode_key, mode_key)
    for view_idx in sorted(int(k) for k in views.keys()):
        view_classes = views.get(view_idx, views.get(str(view_idx), {}))
        if not isinstance(view_classes, dict):
            continue
        visible = sum(1 for c in view_classes.values() if c.get("show"))
        weights = [float(c.get("weight", 1.0)) for c in view_classes.values()]
        view_name = (["Main", "V1", "V2", "V3", "V4", "Cut"][view_idx]
                     if view_idx < 6 else f"V{view_idx}")
        if weights:
            unique_weights = sorted(set(weights))
            weight_info = (f"W={unique_weights[0]:.1f}" if len(unique_weights) == 1
                           else f"W={min(weights):.1f}-{max(weights):.1f}")
        else:
            weight_info = "W=1.0"
        parts.append(
            f"{view_name}: {mode_label}, {visible}vis, "
            f"B={border_pct:.1f}%, {weight_info}"
        )
    return "; ".join(parts) if parts else f"{prefix}: Not configured yet"


def summarize_surface_preset(preset: dict) -> str:
    """Human-readable one-liner for Surface shortcut presets."""
    classes = preset.get("classes", {}) or {}
    visible = sum(1 for info in classes.values() if info.get("show"))
    azimuth = float(preset.get("azimuth", 45.0))
    angle = float(preset.get("angle", 45.0))
    max_edge = float(preset.get("max_edge", 0.0))
    return (
        f"Surface: {azimuth:.0f}°/{angle:.0f}°, "
        f"{visible} visible, Edge={max_edge:.2f}"
    )



DARK_STYLESHEET = """
/* Main window background */
QWidget {
    background-color: #1e1e1e;
    color: #e0e0e0;
    font-family: "Segoe UI", "SF Pro Display", Roboto, sans-serif;
    font-size: 12px;
}

/* Table styling */
QTableWidget {
    background-color: #252526;
    gridline-color: #3e3e42;
    border: 1px solid #3e3e42;
    selection-background-color: #0e639c;
    alternate-background-color: #2d2d30;
}

QTableWidget::item {
    padding: 8px;
    border-bottom: 1px solid #3e3e42;
}

QTableWidget::item:selected {
    background-color: #0e639c;
    color: white;
}

QTableWidget::item:hover {
    background-color: #2a2d3e;
}

QTableCornerButton::section {
    background-color: #252526;
    border: 1px solid #3e3e42;
    border-right: 1px solid #3e3e42;
}

/* Header styling */
QHeaderView::section {
    background-color: #2d2d30;
    border: none;
    padding: 8px;
    border-bottom: 1px solid #3e3e42;
    border-right: 1px solid #3e3e42;
    font-weight: bold;
    color: #cccccc;
}

QHeaderView::section:hover {
    background-color: #3e3e42;
}

/* ComboBox styling */
QComboBox {
    background-color: #3c3c3c;
    border: 1px solid #404040;
    border-radius: 4px;
    padding: 4px 8px;
    color: #e0e0e0;
    min-height: 24px;
}

QComboBox:hover {
    background-color: #454545;
    border-color: #569cd6;
}

QComboBox:focus {
    background-color: #454545;
    border-color: #007acc;
}

QComboBox::drop-down {
    border: none;
    width: 20px;
}

QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid #cccccc;
}

QComboBox::down-arrow:on {
    border-top: none;
    border-bottom: 5px solid #cccccc;
}

QComboBox QAbstractItemView {
    background-color: #3c3c3c;
    border: 1px solid #404040;
    selection-background-color: #0e639c;
    color: #e0e0e0;
    selection-color: white;
}

/* Button styling */
QPushButton {
    background-color: #3c3c3c;
    border: 1px solid #404040;
    border-radius: 4px;
    padding: 8px 16px;
    color: #e0e0e0;
    font-weight: 500;
    min-height: 32px;
}

QPushButton:hover {
    background-color: #454545;
    border-color: #569cd6;
}

QPushButton:pressed {
    background-color: #2d2d30;
}

QPushButton#add_btn, QPushButton#del_btn {
    background-color: #3c3c3c;
    border-color: #404040;
    min-width: 40px;
    padding: 8px;
}

QPushButton#add_btn:hover {
    background-color: #1a3d1a;
    border-color: #39ff14;
    color: #39ff14;
}

QPushButton#del_btn:hover {
    background-color: #3d1a1a;
    border-color: #ff4444;
    color: #ff4444;
}

QPushButton#apply_btn {
    background-color: #0b851c;
    border-color: #0e9f26;
}

QPushButton#apply_btn:hover {
    background-color: #149928;
}

QPushButton#save_btn {
    background-color: #cd7f32;
    border-color: #d18e4f;
}

QPushButton#save_btn:hover {
    background-color: #e6a757;
}

/* Scroll bars */
QScrollBar:vertical {
    background: #2d2d30;
    width: 12px;
    border-radius: 6px;
}

QScrollBar::handle:vertical {
    background: #404040;
    border-radius: 6px;
    min-height: 20px;
}

QScrollBar::handle:vertical:hover {
    background: #569cd6;
}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}

QScrollBar:horizontal {
    background: #2d2d30;
    height: 12px;
    border-radius: 6px;
}

QScrollBar::handle:horizontal {
    background: #404040;
    border-radius: 6px;
    min-width: 20px;
}

/* SpinBox styling */
QSpinBox, QDoubleSpinBox {
    background-color: #3c3c3c;
    border: 1px solid #404040;
    border-radius: 4px;
    padding: 4px;
    color: #e0e0e0;
    selection-background-color: #0e639c;
    selection-color: white;
}

QSpinBox:focus, QDoubleSpinBox:focus {
    background-color: #3c3c3c;
    border-color: #007acc;
}

QSpinBox::up-button, QDoubleSpinBox::up-button {
    background-color: #0e639c;
    border-left: 1px solid #007acc;
    width: 16px;
}

QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover {
    background-color: #1177bb;
}

QSpinBox::down-button, QDoubleSpinBox::down-button {
    background-color: #0e639c;
    border-left: 1px solid #007acc;
    width: 16px;
}

QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #1177bb;
}

QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 4px solid white;
}

QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 4px solid white;
}
"""


def encode_shading_preset(payload: dict) -> str:
    """Encode shading parameters + class visibility into JSON"""
    try:
        classes = payload.get("classes", {}) or {}
        classes_json = {}
        for k, v in classes.items():
            code = str(int(k))
            classes_json[code] = {
                "show": bool(v.get("show", False)),
                "color": list(v.get("color", (128, 128, 128))),
            }

        quality_mode = normalize_shading_preset_quality(
            payload.get("quality_mode"),
            payload.get("speed"),
        )
        preset = {
            "__type__": "shading_mode_preset",
            "azimuth": float(payload.get("azimuth", 45.0)),
            "angle": float(payload.get("angle", 45.0)),
            "ambient": float(payload.get("ambient", 0.1)),
            "quality": float(payload.get("quality", 100.0)),
            "quality_mode": quality_mode,
            "classes": classes_json,
        }
        return json.dumps(preset)
    except Exception:
        return json.dumps({"__type__": "shading_mode_preset", "classes": {}})


def decode_shading_preset(text: str):
    """Decode shading preset from JSON"""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("__type__") != "shading_mode_preset":
            return None

        classes = {}
        for code_str, v in (data.get("classes", {}) or {}).items():
            code = int(code_str)
            classes[code] = {
                "show": bool(v.get("show", False)),
                "color": tuple(v.get("color", (128, 128, 128))),
            }

        return {
            "azimuth": float(data.get("azimuth", 45.0)),
            "angle": float(data.get("angle", 45.0)),
            "ambient": float(data.get("ambient", 0.1)),
            "quality": float(data.get("quality", 100.0)),
            "quality_mode": normalize_shading_preset_quality(
                data.get("quality_mode"),
                data.get("speed"),
            ),
            "classes": classes,
        }
    except Exception:
        return None


def encode_surface_preset(payload: dict) -> str:
    """Encode Surface shortcut preset using shading-style lighting fields."""
    try:
        classes = payload.get("classes", {}) or {}
        classes_json = {}
        for k, v in classes.items():
            code = str(int(k))
            classes_json[code] = {
                "show": bool(v.get("show", False)),
                "color": list(v.get("color", (128, 128, 128))),
            }

        preset = {
            "__type__": "surface_mode_preset",
            "azimuth": float(payload.get("azimuth", 45.0)),
            "angle": float(payload.get("angle", 45.0)),
            "ambient": float(payload.get("ambient", 0.1)),
            "quality": float(payload.get("quality", 100.0)),
            "speed": int(payload.get("speed", 1)),
            "max_edge": float(payload.get("max_edge", 0.0)),
            "classes": classes_json,
        }
        return json.dumps(preset)
    except Exception:
        return json.dumps({"__type__": "surface_mode_preset", "classes": {}})


def decode_surface_preset(text):
    """
    Decode Surface preset JSON.

    Backward compatibility:
    - `surface_mode_preset` is the new shading-style Surface format.
    - old DisplayMode-like Surface payloads are converted into a class-only
      Surface preset so existing saved shortcuts do not hard-break.
    """
    try:
        data = json.loads(text) if isinstance(text, str) else text
        if not isinstance(data, dict):
            return None

        if data.get("__type__") == "surface_mode_preset":
            classes = {}
            for code_str, v in (data.get("classes", {}) or {}).items():
                code = int(code_str)
                classes[code] = {
                    "show": bool(v.get("show", False)),
                    "color": tuple(v.get("color", (128, 128, 128))),
                }
            return {
                "azimuth": float(data.get("azimuth", 45.0)),
                "angle": float(data.get("angle", 45.0)),
                "ambient": float(data.get("ambient", 0.1)),
                "quality": float(data.get("quality", 100.0)),
                "speed": int(data.get("speed", 1)),
                "max_edge": float(data.get("max_edge", 0.0)),
                "classes": classes,
            }

        legacy_display = decode_display_preset(data)
        if legacy_display:
            classes = {}
            views = legacy_display.get("views", {}) or {}
            first_view = min(int(k) for k in views.keys()) if views else 0
            for code, info in (views.get(first_view, views.get(str(first_view), {})) or {}).items():
                classes[int(code)] = {
                    "show": bool(info.get("show", False)),
                    "color": tuple(info.get("color", (128, 128, 128))),
                }
            return {
                "azimuth": 45.0,
                "angle": 45.0,
                "ambient": 0.1,
                "quality": 100.0,
                "speed": 1,
                "max_edge": 0.0,
                "classes": classes,
            }
        return None
    except Exception:
        return None


def encode_display_visibility_preset(payload: dict) -> str:
    """Encode class visibility for RGB/Elevation/Depth/Intensity shortcuts."""
    try:
        classes = payload.get("classes", {}) or {}
        return json.dumps({
            "__type__": "display_visibility_preset",
            "mode": str(payload.get("mode", "")),
            "target_view": int(payload.get("target_view", 0) or 0),
            "classes": {
                str(int(code)): {"show": bool(info.get("show", True))}
                for code, info in classes.items()
            },
            "flight_lines": {
                str(int(line_id)): bool(shown)
                for line_id, shown in dict(
                    payload.get("flight_lines", {}) or {}
                ).items()
            },
        })
    except Exception:
        return json.dumps({
            "__type__": "display_visibility_preset",
            "mode": str(payload.get("mode", "")) if isinstance(payload, dict) else "",
            "classes": {},
        })


def decode_display_visibility_preset(value):
    """Decode a display visibility preset from JSON or QSettings data."""
    try:
        data = json.loads(value) if isinstance(value, str) else value
        if not isinstance(data, dict):
            return None
        if data.get("__type__") not in (None, "display_visibility_preset"):
            return None
        classes = {
            int(code): {"show": bool(info.get("show", True))}
            for code, info in (data.get("classes", {}) or {}).items()
            if isinstance(info, dict)
        }
        return {
            "mode": str(data.get("mode", "")),
            "target_view": int(data.get("target_view", 0) or 0),
            "classes": classes,
            "flight_lines": {
                int(line_id): bool(shown)
                for line_id, shown in dict(
                    data.get("flight_lines", {}) or {}
                ).items()
            },
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def summarize_display_visibility_preset(preset: dict, tool: str) -> str:
    classes = (preset or {}).get("classes", {}) or {}
    visible = sum(1 for info in classes.values() if info.get("show", True))
    if tool == "Line":
        lines = dict((preset or {}).get("flight_lines", {}) or {})
        line_visible = sum(1 for shown in lines.values() if shown)
        return (
            f"Line: {visible}/{len(classes)} classes, "
            f"{line_visible}/{len(lines)} flight lines"
        )
    return f"{tool}: {visible} of {len(classes)} classes visible"


def encode_draw_preset(payload: dict) -> str:
    """Encode draw tool styles into JSON"""
    try:
        tools = payload.get("tools", {}) or {}
        tools_json = {}
        for tool_key, style in tools.items():
            tools_json[str(tool_key)] = {
                "color": list(style.get("color", (1.0, 0.0, 0.0))),
                "width": int(style.get("width", 2)),
                "style": str(style.get("style", "solid")),
            }
        preset = {
            "__type__": "draw_settings_preset",
            "active_tool": str(payload.get("active_tool", "smartline")),
            "tools": tools_json,
        }
        return json.dumps(preset)
    except Exception as e:
        print(f"❌ encode_draw_preset error: {e}")
        return json.dumps({"__type__": "draw_settings_preset", "tools": {}})


def decode_draw_preset(text: str):
    """Decode draw settings preset from JSON"""
    try:
        data = json.loads(text)
        if not isinstance(data, dict) or data.get("__type__") != "draw_settings_preset":
            return None
        tools = {}
        for tool_key, style in (data.get("tools", {}) or {}).items():
            tools[str(tool_key)] = {
                "color": tuple(style.get("color", (1.0, 0.0, 0.0))),
                "width": int(style.get("width", 2)),
                "style": str(style.get("style", "solid")),
            }
        return {"active_tool": str(data.get("active_tool", "smartline")), "tools": tools}
    except Exception as e:
        print(f"❌ decode_draw_preset error: {e}")
        return None


def encode_sync_preset(payload: dict) -> str:
    """Encode view sync rows into JSON."""
    try:
        rows = []
        for r in payload.get("rows", []):
            rows.append({
                "view":   int(r.get("view", 1)),
                "mode":   int(r.get("mode", 0)),
                "source": int(r.get("source", 1)),
            })
        return json.dumps({"__type__": "sync_views_preset", "rows": rows})
    except Exception as e:
        print(f"❌ encode_sync_preset error: {e}")
        return json.dumps({"__type__": "sync_views_preset", "rows": []})


def decode_sync_preset(text) -> dict | None:
    """Decode view sync preset from JSON."""
    try:
        data = json.loads(text) if isinstance(text, str) else text
        if not isinstance(data, dict) or data.get("__type__") != "sync_views_preset":
            return None
        rows = []
        for r in data.get("rows", []):
            rows.append({
                "view":   int(r.get("view", 1)),
                "mode":   int(r.get("mode", 0)),
                "source": int(r.get("source", 1)),
            })
        return {"rows": rows}
    except Exception as e:
        print(f"❌ decode_sync_preset error: {e}")
        return None


def _sync_preset_summary(preset: dict) -> str:
    """Human-readable one-liner for a sync preset."""
    rows = preset.get("rows", [])
    active = [r for r in rows if r.get("mode") == 1]
    if not active:
        return "No synch"
    parts = [f"V{r['view']}→V{r['source']}" for r in active]
    return "Sync: " + ", ".join(parts)


class SyncViewsPicker(QDialog):
    """Modal dialog to configure a Sync Views preset for a shortcut."""

    NUM_VIEWS = 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Sync Views Preset")
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.WindowCloseButtonHint)
        self.setMinimumWidth(340)
        self._rows = []
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        info = QLabel(
            "Configure which views should sync when this shortcut is pressed.\n"
            "\"View A: Match → View B\" means View B will follow View A's camera."
        )
        info.setWordWrap(True)
        info.setStyleSheet("color: #aaaaaa; font-style: italic; font-size: 11px;")
        layout.addWidget(info)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #3e3e42;")
        layout.addWidget(sep)

        for v in range(1, self.NUM_VIEWS + 1):
            row_h = QHBoxLayout()
            row_h.setSpacing(8)

            lbl = QLabel(f"View {v}:")
            lbl.setFixedWidth(55)
            row_h.addWidget(lbl)

            mode_combo = QComboBox()
            mode_combo.addItems(["No synch", "Match"])
            mode_combo.setFixedWidth(90)
            row_h.addWidget(mode_combo)

            row_h.addWidget(QLabel("→"))

            source_combo = QComboBox()
            for s in range(1, self.NUM_VIEWS + 1):
                source_combo.addItem(f"View {s}", s)
            source_combo.setEnabled(False)
            row_h.addWidget(source_combo, stretch=1)

            mode_combo.currentIndexChanged.connect(
                lambda idx, sc=source_combo: sc.setEnabled(idx == 1)
            )

            layout.addLayout(row_h)
            self._rows.append({"view_num": v, "mode": mode_combo, "source": source_combo})

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #3e3e42;")
        layout.addWidget(sep2)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn     = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.setAutoDefault(False)
        ok_btn.setDefault(False)
        cancel_btn.setAutoDefault(False)
        cancel_btn.setDefault(False)
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

    # ── data access ──────────────────────────────────────────────────

    def get_preset(self) -> dict:
        rows = []
        for r in self._rows:
            rows.append({
                "view":   r["view_num"],
                "mode":   r["mode"].currentIndex(),
                "source": r["source"].currentData() or 1,
            })
        return {"rows": rows}

    def set_preset(self, preset: dict):
        rows_data = {r["view"]: r for r in preset.get("rows", [])}
        for r in self._rows:
            v = r["view_num"]
            if v not in rows_data:
                continue
            mode   = rows_data[v].get("mode", 0)
            source = rows_data[v].get("source", 1)
            r["mode"].setCurrentIndex(mode)
            idx = r["source"].findData(source)
            if idx >= 0:
                r["source"].setCurrentIndex(idx)
            r["source"].setEnabled(mode == 1)

    def get_summary(self) -> str:
        preset = self.get_preset()
        return _sync_preset_summary(preset)


class DrawSettingsPicker(QDialog):
    """Lightweight dialog to configure draw tool styles for a shortcut preset."""

    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app_window = app_window
        self.setWindowTitle("Configure Draw Settings Preset")
        self.setModal(False)
        self.setWindowFlags(Qt.Window)
        self.resize(560, 480)

        from gui.draw_settings_dialog import (
            DEFAULT_DRAW_STYLES, TOOL_ORDER, TOOL_DISPLAY_NAMES,
            vtk_color_to_qcolor, qcolor_to_vtk, load_draw_settings,
        )
        self._TOOL_ORDER = TOOL_ORDER
        self._TOOL_DISPLAY_NAMES = TOOL_DISPLAY_NAMES
        self._vtk_to_q = vtk_color_to_qcolor
        self._q_to_vtk = qcolor_to_vtk

        # Working copy
        if (hasattr(app_window, 'digitizer') and
                hasattr(app_window.digitizer, 'draw_tool_styles')):
            self._styles = {k: dict(v) for k, v in app_window.digitizer.draw_tool_styles.items()}
        else:
            try:
                self._styles = load_draw_settings()
            except Exception:
                self._styles = {k: dict(v) for k, v in DEFAULT_DRAW_STYLES.items()}

        self._build_ui()
        self.setStyleSheet(DARK_STYLESHEET)

    def _build_ui(self):
        from PySide6.QtWidgets import QGridLayout, QScrollArea, QGroupBox
        layout = QVBoxLayout(self)

        info = QLabel("Configure draw tool styles for this shortcut:")
        info.setStyleSheet("color: #cccccc; font-style: italic;")
        layout.addWidget(info)

        # ── Tool selector ──────────────────────────────────────────────
        tool_row = QHBoxLayout()
        tool_row.addWidget(QLabel("Activate Tool:"))
        self._active_tool_combo = QComboBox()
        for key in self._TOOL_ORDER:
            self._active_tool_combo.addItem(
                self._TOOL_DISPLAY_NAMES.get(key, key), key
            )
        self._active_tool_combo.setCurrentIndex(0)
        self._active_tool_combo.setStyleSheet(
            "QComboBox { padding: 4px; font-weight: bold; }"
        )
        tool_row.addWidget(self._active_tool_combo, stretch=1)
        layout.addLayout(tool_row)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_w = QWidget()
        grid = QGridLayout(scroll_w)

        for col, header in enumerate(["Tool", "Color", "Width", "Style"]):
            lbl = QLabel(header)
            lbl.setStyleSheet("font-weight: bold; color: #cccccc;")
            grid.addWidget(lbl, 0, col)

        self._color_buttons = {}
        self._width_combos = {}
        self._style_combos = {}

        for idx, key in enumerate(self._TOOL_ORDER):
            row = idx + 1
            style = self._styles.get(key, {"color": (1, 0, 0), "width": 2, "style": "solid"})

            grid.addWidget(QLabel(self._TOOL_DISPLAY_NAMES.get(key, key)), row, 0)

            color_btn = QPushButton()
            color_btn.setFixedSize(40, 24)
            qc = self._vtk_to_q(style["color"])
            color_btn.setStyleSheet(
                f"background-color: {qc.name()}; border: 2px solid #fff; border-radius: 4px;"
            )
            color_btn.setCursor(Qt.PointingHandCursor)
            color_btn.clicked.connect(lambda checked=False, k=key: self._pick_color(k))
            self._color_buttons[key] = color_btn
            grid.addWidget(color_btn, row, 1)

            w_combo = QComboBox()
            w_combo.addItems([str(i) for i in range(1, 11)])
            w_combo.setCurrentText(str(style.get("width", 2)))
            self._width_combos[key] = w_combo
            grid.addWidget(w_combo, row, 2)

            s_combo = QComboBox()
            s_combo.addItems(["solid", "dashed", "dotted", "dash-dot", "dash-dot-dot"])
            s_combo.setCurrentText(style.get("style", "solid"))
            self._style_combos[key] = s_combo
            grid.addWidget(s_combo, row, 3)

        scroll.setWidget(scroll_w)
        layout.addWidget(scroll, stretch=1)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        ok_btn = QPushButton("OK")
        cancel_btn = QPushButton("Cancel")
        ok_btn.clicked.connect(self.accept)
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(ok_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

    def _pick_color(self, tool_key):
        from PySide6.QtWidgets import QColorDialog
        cur = self._vtk_to_q(self._styles.get(tool_key, {}).get("color", (1, 0, 0)))
        color = QColorDialog.getColor(cur, self, f"Choose color for {tool_key}")
        if color.isValid():
            self._styles[tool_key]["color"] = self._q_to_vtk(color)
            self._color_buttons[tool_key].setStyleSheet(
                f"background-color: {color.name()}; border: 2px solid #fff; border-radius: 4px;"
            )

    def get_preset(self) -> dict:
        """Return the configured draw styles as a preset dict."""
        tools = {}
        for key in self._TOOL_ORDER:
            tools[key] = {
                "color": self._styles.get(key, {}).get("color", (1, 0, 0)),
                "width": int(self._width_combos[key].currentText()),
                "style": self._style_combos[key].currentText(),
            }
        active_tool = self._active_tool_combo.currentData() or self._TOOL_ORDER[0]
        return {"active_tool": active_tool, "tools": tools}

    def set_preset(self, preset: dict):
        """Load an existing preset into the UI."""
        active = preset.get("active_tool", self._TOOL_ORDER[0])
        idx = self._active_tool_combo.findData(active)
        if idx >= 0:
            self._active_tool_combo.setCurrentIndex(idx)

        tools = preset.get("tools", {})
        for key in self._TOOL_ORDER:
            if key not in tools:
                continue
            style = tools[key]
            self._styles[key] = dict(style)
            qc = self._vtk_to_q(style.get("color", (1, 0, 0)))
            self._color_buttons[key].setStyleSheet(
                f"background-color: {qc.name()}; border: 2px solid #fff; border-radius: 4px;"
            )
            self._width_combos[key].setCurrentText(str(style.get("width", 2)))
            self._style_combos[key].setCurrentText(style.get("style", "solid"))


# ═══════════════════════════════════════════════════════════════════
# SETTINGS KEY — shared across all methods
# ═══════════════════════════════════════════════════════════════════
_COL_WIDTHS_KEY = "display_preset_picker/column_widths"
_GEOMETRY_KEY   = "display_preset_picker/geometry"
_DEFAULT_WIDTHS = [57, 79, 122, 147, 112, 213]


class LineModePresetDialog(QDialog):
    """Shortcut preset editor containing only Main View flight-line settings."""

    def __init__(self, app_window, preset=None, parent=None):
        super().__init__(parent)
        self.app_window = app_window
        self.setProperty("themeStyledDialog", True)
        self.setWindowTitle("Configure Line Mode Shortcut")
        self.setModal(False)
        self.setWindowFlags(Qt.Window)
        self.resize(390, 470)

        root = QVBoxLayout(self)
        note = QLabel(
            "Select the flight lines that this shortcut should display. "
            "Pressing the shortcut always switches Main View to Line mode."
        )
        note.setWordWrap(True)
        note.setObjectName("dialogInlineNote")
        root.addWidget(note)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Show", "Flight line / Color"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.Stretch
        )
        root.addWidget(self.table, stretch=1)

        source_ids = None
        data = getattr(app_window, "data", None)
        if isinstance(data, dict):
            source_ids = data.get("point_source_id")
        line_ids = []
        if source_ids is not None:
            import numpy as np
            line_ids = [int(value) for value in np.unique(source_ids)]

        saved = dict((preset or {}).get("lines", {}) or {})
        if not saved:
            saved = dict(getattr(app_window, "flight_line_visibility", {}) or {})
        colors = dict(getattr(app_window, "flight_line_colors", {}) or {})
        self.checks = {}
        for row, line_id in enumerate(line_ids):
            self.table.insertRow(row)
            check = QCheckBox()
            check.setChecked(bool(saved.get(line_id, True)))
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(0, 0, 0, 0)
            holder_layout.setAlignment(Qt.AlignCenter)
            holder_layout.addWidget(check)
            self.table.setCellWidget(row, 0, holder)
            self.checks[line_id] = check

            rgb = colors.get(
                line_id,
                (
                    (line_id * 67 + 53) % 256,
                    (line_id * 131 + 97) % 256,
                    (line_id * 193 + 181) % 256,
                ),
            )
            item = QTableWidgetItem(f"Line {line_id}")
            item.setBackground(QColor(*rgb))
            item.setForeground(
                QColor(0, 0, 0) if sum(rgb) > 390 else QColor(255, 255, 255)
            )
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(row, 1, item)

        actions = QHBoxLayout()
        all_on = QPushButton("All on")
        invert = QPushButton("Invert")
        all_off = QPushButton("All off")
        actions.addWidget(all_on)
        actions.addWidget(invert)
        actions.addWidget(all_off)
        root.addLayout(actions)
        all_on.clicked.connect(
            lambda: [check.setChecked(True) for check in self.checks.values()]
        )
        invert.clicked.connect(
            lambda: [
                check.setChecked(not check.isChecked())
                for check in self.checks.values()
            ]
        )
        all_off.clicked.connect(
            lambda: [check.setChecked(False) for check in self.checks.values()]
        )

        footer = QHBoxLayout()
        footer.addStretch()
        ok_button = QPushButton("OK")
        close_button = QPushButton("Close")
        footer.addWidget(ok_button)
        footer.addWidget(close_button)
        root.addLayout(footer)
        ok_button.clicked.connect(self.accept)
        close_button.clicked.connect(self.reject)

    def get_preset(self):
        return {
            "target_view": 0,
            "lines": {
                int(line_id): check.isChecked()
                for line_id, check in self.checks.items()
            },
        }


class ClassVisibilityPicker(QDialog):
    """Lightweight dialog — Display/Shading Mode preset configurator"""

    settings_changed = Signal(dict)

    # ── QSETTINGS HELPERS ────────────────────────────────────────────
    @staticmethod
    def _load_col_widths():
        s = QSettings("NakshaAI", "LidarApp")
        raw = s.value(_COL_WIDTHS_KEY, None)
        if raw and isinstance(raw, list) and len(raw) == 6:
            try:
                widths = [int(w) for w in raw]
                if all(w >= 20 for w in widths):
                    return widths
            except (ValueError, TypeError):
                pass
        return list(_DEFAULT_WIDTHS)

    @staticmethod
    def _save_col_widths(widths):
        QSettings("NakshaAI", "LidarApp").setValue(_COL_WIDTHS_KEY, widths)

    def _resolve_shading_palette_source(self):
        """
        Build a current palette with complete metadata for Shading and Surface.

        The first live source defines the current class set and state. Later
        sources only backfill missing metadata, preventing a stale slot palette
        from blanking Lvl values that still exist in the Display table or the
        app-level palette.
        """
        dialog = getattr(self.app_window, "display_mode_dialog", None)
        if dialog is None:
            dialog = getattr(self.app_window, "display_dialog", None)

        def table_palette():
            table = getattr(dialog, "table", None) if dialog is not None else None
            if table is None:
                return {}

            result = {}
            for row in range(table.rowCount()):
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

                checkbox = table.cellWidget(row, 0)
                if checkbox is not None and hasattr(checkbox, "isChecked"):
                    entry["show"] = checkbox.isChecked()

                color_item = table.item(row, 5)
                if color_item is not None:
                    brush = color_item.background()
                    if brush.style() != Qt.BrushStyle.NoBrush:
                        entry["color"] = brush.color().getRgb()[:3]

                weight_item = table.item(row, 6)
                if weight_item is not None:
                    try:
                        entry["weight"] = float(weight_item.text())
                    except (TypeError, ValueError):
                        pass

                result[code] = entry
            return result

        candidates = [table_palette()]
        if dialog is not None and isinstance(getattr(dialog, "view_palettes", None), dict):
            dialog_palettes = dialog.view_palettes
            current_slot = int(getattr(dialog, "current_slot", 0) or 0)
            candidates.extend(
                dialog_palettes.get(slot_idx)
                for slot_idx in dict.fromkeys((current_slot, 0, 1, 2, 3, 4, 5))
            )

        app_palette = getattr(self.app_window, "class_palette", {}) or {}
        candidates.append(app_palette)

        app_palettes = getattr(self.app_window, "view_palettes", None)
        if isinstance(app_palettes, dict):
            candidates.extend(app_palettes.get(slot_idx) for slot_idx in range(6))

        primary = next(
            (palette for palette in candidates if isinstance(palette, dict) and palette),
            {},
        )
        if not primary:
            return {}

        merged = {}
        for raw_code, raw_entry in primary.items():
            try:
                code = int(raw_code)
            except (TypeError, ValueError):
                continue
            merged[code] = dict(raw_entry) if isinstance(raw_entry, dict) else {}

        metadata_fields = ("description", "lvl", "draw", "color")
        for palette in candidates:
            if not isinstance(palette, dict):
                continue
            for raw_code, raw_entry in palette.items():
                try:
                    code = int(raw_code)
                except (TypeError, ValueError):
                    continue
                if code not in merged or not isinstance(raw_entry, dict):
                    continue
                target = merged[code]
                for field in metadata_fields:
                    current_value = target.get(field)
                    fallback_value = raw_entry.get(field)
                    if field == "color":
                        is_missing = current_value is None
                        has_fallback = fallback_value is not None
                    else:
                        is_missing = not str(current_value or "").strip()
                        has_fallback = bool(str(fallback_value or "").strip())
                    if is_missing and has_fallback:
                        target[field] = fallback_value

        for code, entry in merged.items():
            if not str(entry.get("lvl") or "").strip():
                description = str(entry.get("description") or "").strip()
                generic_labels = {str(code), f"class {code}", f"code {code}", "-"}
                entry["lvl"] = (
                    description
                    if description and description.lower() not in generic_labels
                    else str(code)
                )

        return merged

    # ─────────────────────────────────────────────────────────────────
    def __init__(self, app_window, mode="display", parent=None):
        super().__init__(parent)
        self.app_window  = app_window
        self.mode        = mode
        self._col_resize_blocked = False

        from PySide6.QtWidgets import (
            QGroupBox, QSpinBox, QDoubleSpinBox, QGridLayout, QScrollArea
        )
        self.setProperty("themeStyledDialog", True)
        mode_title = "RGB" if mode == "rgb" else mode.title()
        self.setWindowTitle(f"Configure {mode_title} Mode Preset")
        self.setModal(False)
        self.setWindowFlags(Qt.Window)

        # Restore geometry (size + position) from last session
        s = QSettings("NakshaAI", "LidarApp")
        saved_geo = s.value(_GEOMETRY_KEY)
        if saved_geo:
            self.restoreGeometry(saved_geo)
        else:
            self.resize(820, 620)
            self.setMinimumSize(720, 500)

        # ── Install app-level event filter to detect tab/window switches ──
        from PySide6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

        layout = QVBoxLayout(self)

        # A Line shortcut saves Main View class and flight-line filters together.
        self.line_visibility = {}
        if mode == "line":
            by_slot = getattr(app_window, "flight_line_visibility_by_slot", {})
            if isinstance(by_slot, dict):
                self.line_visibility = dict(
                    by_slot.get(0, by_slot.get("0", {})) or {}
                )
            if not self.line_visibility:
                self.line_visibility = dict(
                    getattr(app_window, "flight_line_visibility", {}) or {}
                )
            line_header = QHBoxLayout()
            line_header.setSpacing(10)
            line_header.addWidget(QLabel("Target View:"))
            self.view_selector = QComboBox()
            self.view_selector.addItem("Main View", 0)
            self.view_selector.setMinimumWidth(130)
            line_header.addWidget(self.view_selector)
            line_header.addWidget(QLabel("Display Mode:"))
            self.display_mode_selector = QComboBox()
            self.display_mode_selector.addItem("Line", "line")
            self.display_mode_selector.setMinimumWidth(150)
            line_header.addWidget(self.display_mode_selector)
            self.lines_button = QPushButton("Lines")
            self.lines_button.setObjectName("displayLinesButton")
            self.lines_button.clicked.connect(self._open_line_selection)
            line_header.addWidget(self.lines_button)
            line_header.addStretch()
            layout.addLayout(line_header)

        # ── shading parameters (shading mode only) ───────────────────
        if mode in ("shading", "surface"):
            shading_group = QGroupBox(
                "Shading Parameters" if mode == "shading" else "Surface Parameters"
            )
            shading_layout = QGridLayout()

            shading_layout.addWidget(QLabel("Azimuth (°):"), 0, 0)
            self.az_spin = QDoubleSpinBox()
            self.az_spin.setRange(0, 360)
            self.az_spin.setValue(45.0)
            shading_layout.addWidget(self.az_spin, 0, 1)

            shading_layout.addWidget(QLabel("Sharpness:" if mode == "shading" else "Angle (°):"), 1, 0)
            self.angle_spin = QDoubleSpinBox()
            if mode == "shading":
                self.angle_spin.setRange(0, 999)
                self.angle_spin.setSingleStep(5.0)
                self.angle_spin.setToolTip(
                    "0..90 = established sharpness response; "
                    "91..999 = progressive overdrive. "
                    "Ambient remains the brightness control."
                )
            else:
                # Surface still uses a real physical light angle.
                self.angle_spin.setRange(0, 90)
            self.angle_spin.setValue(45.0)
            shading_layout.addWidget(self.angle_spin, 1, 1)

            shading_layout.addWidget(QLabel("Ambient:"), 2, 0)
            self.ambient_spin = QDoubleSpinBox()
            self.ambient_spin.setRange(0, 1)
            self.ambient_spin.setValue(0.1)
            shading_layout.addWidget(self.ambient_spin, 2, 1)

            shading_layout.addWidget(QLabel("Quality (%):"), 3, 0)
            self.quality_spin = QDoubleSpinBox()
            self.quality_spin.setRange(0, 100)
            self.quality_spin.setValue(100.0)
            shading_layout.addWidget(self.quality_spin, 3, 1)

            shading_layout.addWidget(QLabel("Speed:"), 4, 0)
            if mode == "shading":
                self.shading_quality_combo = QComboBox()
                for label, quality_key in SHADING_QUALITY_CHOICES:
                    self.shading_quality_combo.addItem(label, quality_key)
                self.shading_quality_combo.setCurrentIndex(1)
                self.shading_quality_combo.setToolTip(
                    "Fast uses 1M representatives, Normal uses 3M, and "
                    "Slow triangulates every eligible point."
                )
                shading_layout.addWidget(self.shading_quality_combo, 4, 1)
            else:
                self.speed_spin = QSpinBox()
                self.speed_spin.setRange(1, 10)
                self.speed_spin.setValue(1)
                shading_layout.addWidget(self.speed_spin, 4, 1)

            if mode == "surface":
                shading_layout.addWidget(QLabel("Max Edge:"), 5, 0)
                self.max_edge_spin = QDoubleSpinBox()
                self.max_edge_spin.setRange(0, 100000)
                self.max_edge_spin.setDecimals(2)
                self.max_edge_spin.setValue(0.0)
                shading_layout.addWidget(self.max_edge_spin, 5, 1)

            shading_group.setLayout(shading_layout)
            layout.addWidget(shading_group)

        # ── class grid (fallback for non-display modes) ──────────────
        self.class_checkboxes = {}
        self.weight_spinboxes = {}

        if mode != "display":
            # ✅ Build proper table for shading mode, grid for others
            if mode in CLASS_VISIBILITY_PICKER_MODES:
                table_note = QLabel(
                    f"Select which classes to include in this {mode_title} preset:"
                )
                table_note.setObjectName("dialogInlineNote")
                layout.addWidget(table_note)

                # ── table + buttons ───────────────────────────────────────────
                table_and_btns = QHBoxLayout()
                table_and_btns.setSpacing(8)

                self.class_table = QTableWidget(0, 5)
                self.class_table.setObjectName("shadingClassTable")
                self.class_table.setHorizontalHeaderLabels(
                    ["Show", "Code", "Description", "Color", "Lvl"]
                )
                self.class_table.setAlternatingRowColors(True)
                self.class_table.verticalHeader().setVisible(False)
                self.class_table.verticalHeader().setDefaultSectionSize(34)
                self.class_table.setSelectionBehavior(QAbstractItemView.SelectRows)
                self.class_table.setSelectionMode(QAbstractItemView.SingleSelection)
                self.class_table.setFocusPolicy(Qt.NoFocus)
                self.class_table.setWordWrap(False)
                self.class_table.setShowGrid(False)

                hdr = self.class_table.horizontalHeader()
                hdr.setDefaultAlignment(Qt.AlignCenter)
                hdr.setHighlightSections(False)
                hdr.setStretchLastSection(True)

                for col in range(5):
                    hdr.setSectionResizeMode(col, QHeaderView.Interactive)

                widths = self._load_col_widths()
                self._col_resize_blocked = True
                for col, w in enumerate(widths[:5]):
                    self.class_table.setColumnWidth(col, w)
                self._col_resize_blocked = False

                hdr.sectionResized.connect(self._on_column_resized)
                table_and_btns.addWidget(self.class_table, stretch=1)

                # ── action buttons ────────────────────────────────────────────
                action_col = QVBoxLayout()
                action_col.setContentsMargins(0, 0, 0, 0)
                action_col.setSpacing(6)

                self.refresh_btn    = QPushButton("Refresh")
                self.select_all_btn = QPushButton("Select All")
                self.clear_all_btn  = QPushButton("Clear All")
                for btn in (self.refresh_btn, self.select_all_btn, self.clear_all_btn):
                    btn.setObjectName("displayActionButton")
                    btn.setMinimumHeight(30)
                    btn.setFixedWidth(80)
                    btn.setAutoDefault(False)
                    btn.setDefault(False)
                    btn.setFocusPolicy(Qt.NoFocus)
                    action_col.addWidget(btn)
                action_col.addStretch()
                table_and_btns.addLayout(action_col)

                layout.addLayout(table_and_btns, stretch=1)

                # ── connections ───────────────────────────────────────────────
                self.refresh_btn.clicked.connect(self._refresh_classes)
                self.select_all_btn.clicked.connect(self._select_all)
                self.clear_all_btn.clicked.connect(self._clear_all)
            else:
                # Other non-display modes use grid
                scroll_widget = QWidget()
                self.class_grid = QGridLayout(scroll_widget)
                self.class_grid.setColumnStretch(1, 1)
                scroll = QScrollArea()
                scroll.setWidgetResizable(True)
                scroll.setWidget(scroll_widget)
                layout.addWidget(scroll, stretch=1)

            btn_layout = QHBoxLayout()
            btn_layout.addStretch()
            self.ok_btn     = QPushButton("OK")
            self.cancel_btn = QPushButton("Cancel")
            btn_layout.addWidget(self.ok_btn)
            btn_layout.addWidget(self.cancel_btn)
            layout.addLayout(btn_layout)

            self.ok_btn.clicked.connect(self._on_ok_clicked)
            self.cancel_btn.clicked.connect(self.reject)
            self._populate_classes()
        else:
            # Display mode — full table UI
            self._rebuild_display_mode_ui(layout)

    def _open_line_selection(self):
        """Edit the local flight-line portion of this Line shortcut preset."""
        dialog = LineModePresetDialog(
            self.app_window,
            preset={"target_view": 0, "lines": self.line_visibility},
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            self.line_visibility = dict(dialog.get_preset().get("lines", {}))

    def set_line_visibility(self, visibility):
        self.line_visibility = {
            int(line_id): bool(shown)
            for line_id, shown in dict(visibility or {}).items()
        }

    def get_line_visibility(self):
        return dict(self.line_visibility)

    # ─────────────────────────────────────────────────────────────────
    # EVENT FILTER — hide when another top-level window is activated
    # ─────────────────────────────────────────────────────────────────
    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.WindowActivate and self.isVisible():
            activated = obj
            is_owned = _is_owned_qt_object(self, activated)
            is_parent = (
                self.parent() is not None
                and activated is self.parent()
            )
            if not is_owned and not is_parent:
                if hasattr(activated, 'isWindow') and activated.isWindow():
                    self.hide()
        return super().eventFilter(obj, event)

    def changeEvent(self, event):
        """Intercept minimize — hide instead of collapsing to ugly mini title-bar."""
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                event.ignore()
                self.setWindowState(Qt.WindowNoState)
                self.hide()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        # Disconnect display_mode border signal to prevent stale callbacks
        dialog = self._get_display_mode_dialog()
        if dialog is not None and hasattr(dialog, "border_changed"):
            try:
                dialog.border_changed.disconnect(self._on_display_mode_border_changed)
            except Exception:
                pass
        # Save geometry
        QSettings("NakshaAI", "LidarApp").setValue(
            _GEOMETRY_KEY, self.saveGeometry()
        )
        # Save column widths one final time
        self._persist_col_widths()
        # Remove event filter
        from PySide6.QtWidgets import QApplication
        QApplication.instance().removeEventFilter(self)
        super().closeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.setStyleSheet(get_dialog_stylesheet())
        # Restore geometry every show so position is correct
        s = QSettings("NakshaAI", "LidarApp")
        saved_geo = s.value(_GEOMETRY_KEY)
        if saved_geo:
            self.restoreGeometry(saved_geo)
        # Position next to parent window
        if self.parent():
            parent_geo = self.parent().geometry()
            self.move(parent_geo.x() + parent_geo.width() + 10, parent_geo.y())
        # Re-seed border from display_mode each time dialog is shown
        self._seed_border_from_display_mode()

    # ── column-width helpers ─────────────────────────────────────────
    def _on_column_resized(self, logical_index, old_size, new_size):
        """Debounced save — fires 800 ms after the user STOPS dragging."""
        if self._col_resize_blocked:
            return
        if not hasattr(self, '_col_save_timer'):
            from PySide6.QtCore import QTimer
            self._col_save_timer = QTimer(self)
            self._col_save_timer.setSingleShot(True)
            self._col_save_timer.timeout.connect(self._persist_col_widths)
        self._col_save_timer.start(800)

    def _persist_col_widths(self):
        """Save current column widths to QSettings."""
        if not hasattr(self, 'class_table') or self.class_table is None:
            return
        widths = [self.class_table.columnWidth(c)
                  for c in range(self.class_table.columnCount())]
        QSettings("NakshaAI", "LidarApp").setValue(_COL_WIDTHS_KEY, widths)
        print(f"💾 Column widths saved: {widths}")

    # ─────────────────────────────────────────────────────────────────
    # MAIN TABLE UI
    # ─────────────────────────────────────────────────────────────────
    def _rebuild_display_mode_ui(self, layout):
        """Build the Display Mode preset UI."""
        # ── clear anything __init__ already added ────────────────────
        while layout.count():
            item = layout.takeAt(0)
            w  = item.widget()
            cl = item.layout()
            if w:
                w.deleteLater()
            elif cl:
                while cl.count():
                    ci = cl.takeAt(0)
                    cw = ci.widget()
                    if cw:
                        cw.deleteLater()

        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        intro = QLabel(
            "Save a Display Mode preset: target view, class visibility, weights, and border."
        )
        intro.setObjectName("dialogInlineNote")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # ── controls card ────────────────────────────────────────────
        # Matches the live Display Mode dialog's own rounded/bordered
        # "displayControlsCard" container (gui/display_mode.py ~1057-1061)
        # -- previously these rows sat directly on the dialog background
        # with no card, so even with matching per-widget object names the
        # overall look didn't match the live dialog's grouped, pill-style
        # control bar.
        controls_card = QFrame()
        controls_card.setObjectName("displayControlsCard")
        controls_card_layout = QVBoxLayout(controls_card)
        controls_card_layout.setContentsMargins(10, 8, 10, 8)
        controls_card_layout.setSpacing(8)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

        controls_row.addWidget(QLabel("Target View:"))
        self.view_selector = QComboBox()
        self.view_selector.setMinimumWidth(130)
        self.view_selector.addItems([
            "Main View", "View 1", "View 2",
            "View 3", "View 4",
        ])
        self.view_selector.setCurrentIndex(0)
        self.view_selector.currentIndexChanged.connect(self._on_view_selector_changed)
        controls_row.addWidget(self.view_selector)

        controls_row.addSpacing(12)
        controls_row.addWidget(QLabel("Display Mode:"))
        self.display_mode_selector = QComboBox()
        self.display_mode_selector.setMinimumWidth(165)
        for label, mode_key in (
            ("By Classification", "class"),
            ("Shaded Classification", "shaded_class"),
            ("Depth", "depth"),
            ("Intensity", "intensity"),
            ("RGB", "rgb"),
            ("Elevation", "elevation"),
            ("Surface", "surface"),
            ("Line", "line"),
        ):
            self.display_mode_selector.addItem(label, mode_key)
        live_mode = str(
            getattr(self.app_window, "display_mode", "class") or "class"
        ).lower()
        live_mode_idx = self.display_mode_selector.findData(live_mode)
        self.display_mode_selector.setCurrentIndex(
            live_mode_idx if live_mode_idx >= 0 else 0
        )
        controls_row.addWidget(self.display_mode_selector)

        controls_row.addSpacing(12)
        controls_row.addWidget(QLabel("Border %:"))

        self.border_spin = QDoubleSpinBox()
        self.border_spin.setRange(0, 100)
        self.border_spin.setDecimals(2)
        self.border_spin.setValue(0)
        self.border_spin.setSingleStep(5.0)
        self.border_spin.setFixedWidth(85)
        controls_row.addWidget(self.border_spin)
        
        self.border_setting_btn = QPushButton()
        self.border_setting_btn.setObjectName("displayBorderButton")
        from gui.icon_provider import get_icon
        self.border_setting_btn.setIcon(get_icon("settings_gear", size=14))
        from PySide6.QtCore import QSize
        self.border_setting_btn.setIconSize(QSize(14, 14))
        self.border_setting_btn.setFixedSize(22, 22)
        self.border_setting_btn.setFocusPolicy(Qt.NoFocus)

        self.border_setting_menu = QMenu(self)
        self.border_logic_point = QAction("Per-Point", self.border_setting_menu)
        self.border_logic_point.setCheckable(True)
        self.border_logic_point.setChecked(True)
        
        self.border_logic_object = QAction("Structured", self.border_setting_menu)
        self.border_logic_object.setCheckable(True)

        self.border_logic_hybrid = QAction("Hybrid", self.border_setting_menu)
        self.border_logic_hybrid.setCheckable(True)
        
        from PySide6.QtGui import QActionGroup
        self.border_action_group = QActionGroup(self)
        self.border_action_group.addAction(self.border_logic_point)
        self.border_action_group.addAction(self.border_logic_object)
        self.border_action_group.addAction(self.border_logic_hybrid)
        self.border_action_group.setExclusive(True)
        
        self.border_setting_menu.addAction(self.border_logic_point)
        self.border_setting_menu.addAction(self.border_logic_object)
        self.border_setting_menu.addAction(self.border_logic_hybrid)
        
        self.border_setting_btn.clicked.connect(
            lambda: self.border_setting_menu.exec(
                self.border_setting_btn.mapToGlobal(
                    self.border_setting_btn.rect().bottomLeft()
                )
            )
        )
        controls_row.addWidget(self.border_setting_btn)
        
        controls_row.addStretch()
        controls_card_layout.addLayout(controls_row)

        # Speed/Quality (Fast/Normal/Slow) -- only meaningful for Shaded
        # Classification and Surface, matching the live Display Mode
        # dialog's own "Speed" control (gui/display_mode.py:1095-1123).
        # Previously the shortcut editor had no way to set this at all, so
        # a Shaded/Surface shortcut always used whatever quality happened
        # to be the current global setting rather than one saved with the
        # shortcut itself. Kept on its own row (rather than crammed into
        # controls_row above) so it can't push that already-full row wider
        # than intended.
        quality_row = QHBoxLayout()
        quality_row.setSpacing(10)
        self.shading_quality_label = QLabel("Speed")
        quality_row.addWidget(self.shading_quality_label)
        self.shading_quality_selector = QComboBox()
        self.shading_quality_selector.setObjectName("displayShadingQuality")
        self.shading_quality_selector.setMinimumWidth(150)
        self.shading_quality_selector.addItem("Fast", "fast")
        self.shading_quality_selector.addItem("Normal", "normal")
        self.shading_quality_selector.addItem("Slow – all points", "slow")
        self.shading_quality_selector.setCurrentIndex(1)
        quality_row.addWidget(self.shading_quality_selector)
        quality_row.addStretch()
        controls_card_layout.addLayout(quality_row)

        def _sync_quality_visibility():
            _mode = str(self.display_mode_selector.currentData() or "class")
            _visible = _mode in ("shaded_class", "surface")
            self.shading_quality_label.setVisible(_visible)
            self.shading_quality_selector.setVisible(_visible)

        self.display_mode_selector.currentIndexChanged.connect(
            lambda _idx: _sync_quality_visibility()
        )
        _sync_quality_visibility()

        # Flight-line selection -- only meaningful for Line mode. Reuses
        # the same "Lines" button / picker dialog / self.line_visibility
        # storage already built for the standalone Line tool's own picker
        # (mode="line"; see _open_line_selection/set_line_visibility/
        # get_line_visibility below, all mode-agnostic class methods) --
        # previously the DisplayMode editor's Line option had no way to
        # choose which flight lines to show, unlike the standalone tool.
        lines_row = QHBoxLayout()
        lines_row.setSpacing(10)
        self.lines_button = QPushButton("Lines…")
        self.lines_button.setObjectName("displayLinesButton")
        self.lines_button.clicked.connect(self._open_line_selection)
        lines_row.addWidget(self.lines_button)
        lines_row.addStretch()
        controls_card_layout.addLayout(lines_row)
        layout.addWidget(controls_card)

        def _sync_lines_visibility():
            _mode = str(self.display_mode_selector.currentData() or "class")
            self.lines_button.setVisible(_mode == "line")

        self.display_mode_selector.currentIndexChanged.connect(
            lambda _idx: _sync_lines_visibility()
        )
        _sync_lines_visibility()

        table_note = QLabel("Choose which classes this preset should display:")
        table_note.setObjectName("dialogInlineNote")
        layout.addWidget(table_note)

        # ── table card ───────────────────────────────────────────────
        # Matches the live Display Mode dialog's own "displayTableCard"
        # container: table on top, a horizontal row of action buttons
        # ("displayActionRail") below it -- previously this picker put the
        # buttons in a vertical column beside the table instead, so even
        # with matching per-button object names the overall arrangement
        # didn't line up with the live dialog's look. Same 3 buttons
        # (Refresh/Select All/Clear All), same click handlers -- only the
        # container/arrangement changes.
        table_card = QFrame()
        table_card.setObjectName("displayTableCard")
        table_card_layout = QVBoxLayout(table_card)
        table_card_layout.setContentsMargins(12, 12, 12, 12)
        table_card_layout.setSpacing(12)

        self.class_table = QTableWidget(0, 6)
        self.class_table.setObjectName("displayClassTable")
        self.class_table.setHorizontalHeaderLabels(
            ["Show", "Code", "Description", "Lvl", "Color", "Weight"]
        )
        self.class_table.setAlternatingRowColors(True)
        self.class_table.verticalHeader().setVisible(False)
        self.class_table.verticalHeader().setDefaultSectionSize(34)
        self.class_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.class_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.class_table.setFocusPolicy(Qt.NoFocus)
        self.class_table.setWordWrap(False)
        self.class_table.setShowGrid(False)

        hdr = self.class_table.horizontalHeader()
        hdr.setDefaultAlignment(Qt.AlignCenter)
        hdr.setHighlightSections(False)
        hdr.setStretchLastSection(True)

        for col in range(6):
            hdr.setSectionResizeMode(col, QHeaderView.Interactive)

        # ✅ RESTORE saved widths — or fall back to defaults
        widths = self._load_col_widths()
        self._col_resize_blocked = True
        for col, w in enumerate(widths):
            self.class_table.setColumnWidth(col, w)
        self._col_resize_blocked = False
        print(f"✅ Column widths restored: {widths}")

        hdr.sectionResized.connect(self._on_column_resized)
        table_card_layout.addWidget(self.class_table, stretch=1)

        # ── action buttons (horizontal rail below the table, matching the
        #    live dialog's displayActionRail) ──────────────────────────
        action_rail = QFrame()
        action_rail.setObjectName("displayActionRail")
        action_row = QHBoxLayout(action_rail)
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(8)

        self.refresh_btn    = QPushButton("Refresh")
        self.select_all_btn = QPushButton("Select All")
        self.clear_all_btn  = QPushButton("Clear All")
        for btn in (self.refresh_btn, self.select_all_btn, self.clear_all_btn):
            btn.setObjectName("displayActionButton")
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setFocusPolicy(Qt.NoFocus)
            action_row.addWidget(btn, stretch=1)
        table_card_layout.addWidget(action_rail)

        layout.addWidget(table_card, stretch=1)

        # ── OK / Cancel ───────────────────────────────────────────────
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.ok_btn     = QPushButton("OK")
        self.ok_btn.setObjectName("primaryBtn")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("secondaryBtn")
        for btn in (self.ok_btn, self.cancel_btn):
            btn.setAutoDefault(False)
            btn.setDefault(False)
            btn.setFocusPolicy(Qt.NoFocus)
        btn_layout.addWidget(self.ok_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

        # ── connections ───────────────────────────────────────────────
        self.refresh_btn.clicked.connect(self._refresh_classes)
        self.select_all_btn.clicked.connect(self._select_all)
        self.clear_all_btn.clicked.connect(self._clear_all)
        self.ok_btn.clicked.connect(self._on_ok_clicked)
        self.cancel_btn.clicked.connect(self.reject)

        self.class_checkboxes = {}
        self.weight_spinboxes = {}
        self._populate_classes()
        self._seed_border_from_display_mode()
        self._connect_display_mode_border_signal()

    def _get_display_mode_dialog(self):
        """Return the live DisplayModeDialog if available, else None."""
        app = self.app_window
        dialog = getattr(app, "display_mode_dialog", None) or getattr(app, "display_dialog", None)
        return dialog if dialog is not None else None

    def _seed_border_from_display_mode(self):
        """Seed border_spin and border_logic from display_mode for the currently selected view."""
        if self.mode != "display":
            return
        dialog = self._get_display_mode_dialog()
        if dialog is None:
            return
        selected_view = (self.view_selector.currentIndex()
                         if hasattr(self, "view_selector") else 0)
        if hasattr(self, "border_spin"):
            border_val = float(dialog.view_borders.get(selected_view, 0))
            self.border_spin.setValue(border_val)
        if hasattr(dialog, "get_border_mode"):
            self.set_border_logic_mode(int(dialog.get_border_mode()))

    def _connect_display_mode_border_signal(self):
        """One-way: display_mode border changes → update this dialog. Never the reverse."""
        if self.mode != "display":
            return
        dialog = self._get_display_mode_dialog()
        if dialog is None or not hasattr(dialog, "border_changed"):
            return
        try:
            dialog.border_changed.connect(self._on_display_mode_border_changed)
        except Exception:
            pass

    def _on_display_mode_border_changed(self, slot_idx: int, border_percent: float, logic_mode: int):
        """Receive border update from display_mode — only update if our selected view matches the changed slot."""
        if not hasattr(self, "border_spin"):
            return
        selected_view = (self.view_selector.currentIndex()
                         if hasattr(self, "view_selector") else 0)
        if selected_view != slot_idx:
            return
        self.border_spin.blockSignals(True)
        self.border_spin.setValue(border_percent)
        self.border_spin.blockSignals(False)
        self.set_border_logic_mode(logic_mode)

    def _refresh_classes(self):
        """Reload classes from app_window.class_palette, preserving selections."""
        print(f"\n🔄 REFRESH CLASSES CALLED")

        current_selections = {}
        current_weights = {}

        if self.mode == "display" and hasattr(self, 'view_configs') and hasattr(self, 'view_selector'):
            current_view_idx = self.view_selector.currentIndex()
            print(f"   📍 Current view: {current_view_idx}")
            if current_view_idx in self.view_configs:
                print(f"   ✅ Found preset data for view {current_view_idx}")
                for code, config in self.view_configs[current_view_idx].items():
                    current_selections[code] = config.get("show", True)
                    current_weights[code] = config.get("weight", 1.0)
            else:
                print(f"   ⚠️ No preset data for view {current_view_idx}, using checkbox states")
                for code, checkbox in self.class_checkboxes.items():
                    current_selections[code] = checkbox.isChecked()
                    if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                        current_weights[code] = self.weight_spinboxes[code].value()
        else:
            for code, checkbox in self.class_checkboxes.items():
                current_selections[code] = checkbox.isChecked()
                if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                    current_weights[code] = self.weight_spinboxes[code].value()

        self._populate_classes()

        for code, is_checked in current_selections.items():
            if code in self.class_checkboxes:
                self.class_checkboxes[code].setChecked(is_checked)
                print(f"   ✅ Restored class {code}: checked={is_checked}")

        for code, weight in current_weights.items():
            if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                self.weight_spinboxes[code].setValue(weight)
                print(f"   ✅ Restored weight for class {code}: {weight}")

        print(f"✅ Refreshed {len(self.class_checkboxes)} classes with preserved states")

    def _populate_classes(self):
        """Load classes — transparent holders for checkbox and weight cells."""
        if hasattr(self, 'class_table') and self.class_table is not None:
            self.class_table.setRowCount(0)
            self.class_checkboxes.clear()
            self.weight_spinboxes = {}

            if self.mode in CLASS_VISIBILITY_PICKER_MODES:
                palette_source = self._resolve_shading_palette_source()
            else:
                palette_source = getattr(self.app_window, 'class_palette', {}) or {}

            if not palette_source:
                return

            current_view_idx = 0
            has_preset_data  = False
            preset_data      = {}

            if self.mode == "display" and hasattr(self, 'view_selector'):
                current_view_idx = self.view_selector.currentIndex()

            if (self.mode == "display"
                    and hasattr(self, 'view_configs')
                    and current_view_idx in self.view_configs):
                has_preset_data = True
                preset_data     = self.view_configs[current_view_idx]

            for code, entry in sorted(palette_source.items()):
                row = self.class_table.rowCount()
                self.class_table.insertRow(row)

                # ── Show checkbox ──────────────────────────────────────
                default_checked = bool(entry.get("show", True))
                if has_preset_data and code in preset_data:
                    default_checked = preset_data[code].get("show", True)

                checkbox = QCheckBox()
                checkbox.setChecked(default_checked)
                checkbox.setFocusPolicy(Qt.NoFocus)
                checkbox.setCursor(Qt.PointingHandCursor)
                checkbox.setStyleSheet("QCheckBox { background: transparent; }")

                # ✅ Transparent holder widget — kills the black cell background
                cb_holder = QWidget()
                cb_holder.setAttribute(Qt.WA_TranslucentBackground, True)
                cb_holder.setStyleSheet("background: transparent;")
                cb_layout = QHBoxLayout(cb_holder)
                cb_layout.setContentsMargins(0, 0, 0, 0)
                cb_layout.setAlignment(Qt.AlignCenter)
                cb_layout.addWidget(checkbox)
                self.class_table.setCellWidget(row, 0, cb_holder)
                self.class_checkboxes[code] = checkbox

                # ── Code ───────────────────────────────────────────────
                code_item = QTableWidgetItem(str(code))
                code_item.setTextAlignment(Qt.AlignCenter)
                self.class_table.setItem(row, 1, code_item)

                # ── Description ────────────────────────────────────────
                desc_item = QTableWidgetItem(entry.get("description") or f"Class {code}")
                desc_item.setToolTip(desc_item.text())
                self.class_table.setItem(row, 2, desc_item)

                # ── Color swatch ───────────────────────────────────────
                color = entry.get("color", (128, 128, 128))
                qcolor = QColor(*color) if isinstance(color, tuple) else QColor(color)
                border_color = qcolor.darker(145)

                swatch = QFrame()
                swatch.setFixedSize(52, 18)
                swatch.setStyleSheet(
                    f"background-color: rgb({qcolor.red()},{qcolor.green()},{qcolor.blue()});"
                    f"border: 1px solid {border_color.name()};"
                    "border-radius: 5px;"
                )
                swatch.setAttribute(Qt.WA_TransparentForMouseEvents, True)

                holder = QWidget()
                holder.setAttribute(Qt.WA_TranslucentBackground, True)
                holder.setStyleSheet("background: transparent;")
                holder_layout = QHBoxLayout(holder)
                holder_layout.setContentsMargins(0, 0, 0, 0)
                holder_layout.setAlignment(Qt.AlignCenter)
                holder_layout.addWidget(swatch)
                
                # Lighting modes use Color=3/Lvl=4; Display uses Lvl=3/Color=4.
                if self.mode in CLASS_VISIBILITY_PICKER_MODES:
                    self.class_table.setCellWidget(row, 3, holder)
                    # ── Lvl (shading: column 4) ────────────────────────
                    lvl_item = QTableWidgetItem(str(entry.get("lvl", "") or "-"))
                    lvl_item.setTextAlignment(Qt.AlignCenter)
                    lvl_item.setToolTip(lvl_item.text())
                    self.class_table.setItem(row, 4, lvl_item)
                else:
                    self.class_table.setCellWidget(row, 4, holder)
                    # ── Lvl (display: column 3) ────────────────────────
                    lvl_item = QTableWidgetItem(str(entry.get("lvl", "") or "-"))
                    lvl_item.setTextAlignment(Qt.AlignCenter)
                    lvl_item.setToolTip(lvl_item.text())
                    self.class_table.setItem(row, 3, lvl_item)

                    # ── Weight spinbox (display mode only) ──────────────────
                    default_weight = 1.0
                    if has_preset_data and code in preset_data:
                        default_weight = preset_data[code].get("weight", 1.0)

                    weight_spin = QDoubleSpinBox()
                    weight_spin.setRange(0.1, 12.0)
                    weight_spin.setDecimals(2)
                    weight_spin.setSingleStep(0.1)
                    weight_spin.setAlignment(Qt.AlignCenter)
                    weight_spin.setFixedWidth(72)
                    weight_spin.setValue(default_weight)
                    weight_spin.setStyleSheet(
                        "QDoubleSpinBox { background: transparent; border: none; }"
                        "QDoubleSpinBox:focus { border: 1px solid #007acc; border-radius:3px; }"
                    )
                    weight_spin.valueChanged.connect(
                        lambda val, c=code: self._on_weight_changed(c, val)
                    )

                    # ✅ Transparent holder for weight too
                    w_holder = QWidget()
                    w_holder.setAttribute(Qt.WA_TranslucentBackground, True)
                    w_holder.setStyleSheet("background: transparent;")
                    w_layout = QHBoxLayout(w_holder)
                    w_layout.setContentsMargins(0, 0, 0, 0)
                    w_layout.setAlignment(Qt.AlignCenter)
                    w_layout.addWidget(weight_spin)
                    self.class_table.setCellWidget(row, 5, w_holder)
                    self.weight_spinboxes[code] = weight_spin

            if self.mode == "display":
                if not hasattr(self, 'view_configs'):
                    self.view_configs = {}
                self._last_view_idx = (
                    self.view_selector.currentIndex()
                    if hasattr(self, 'view_selector') else 0
                )

            print(f"✅ Populated {len(self.class_checkboxes)} classes"
                  f"{' from preset' if has_preset_data else ' (defaults)'}")
            return
        
    def _set_class_color_cell(self, row, color):
        """Color swatch cell — transparent holder, no black background."""
        if not hasattr(self, 'class_table') or self.class_table is None:
            return

        qcolor = QColor(*color) if isinstance(color, tuple) else QColor(color)
        border_color = qcolor.darker(145)

        swatch = QFrame()
        swatch.setFixedSize(52, 18)
        swatch.setStyleSheet(
            f"background-color: rgb({qcolor.red()},{qcolor.green()},{qcolor.blue()});"
            f"border: 1px solid {border_color.name()};"
            "border-radius: 5px;"
        )
        swatch.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # ✅ transparent holder — no black fill
        holder = QWidget()
        holder.setAttribute(Qt.WA_TranslucentBackground, True)
        holder.setStyleSheet("background: transparent;")
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(0, 0, 0, 0)
        holder_layout.setAlignment(Qt.AlignCenter)
        holder_layout.addWidget(swatch)
        self.class_table.setCellWidget(row, 4, holder)

    def _select_all(self):
        """Check all class checkboxes"""
        for checkbox in self.class_checkboxes.values():
            checkbox.setChecked(True)

    def _clear_all(self):
        """Uncheck all class checkboxes"""
        for checkbox in self.class_checkboxes.values():
            checkbox.setChecked(False)

    def get_selected_classes(self):
        """Return dict of selected classes with their info + updated weights"""
        result = {}
        palette_source = (
            self._resolve_shading_palette_source()
            if self.mode in CLASS_VISIBILITY_PICKER_MODES
            else (getattr(self.app_window, "class_palette", {}) or {})
        )
        for code, checkbox in self.class_checkboxes.items():
            entry = palette_source.get(code, {})
            weight = 1.0
            if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                weight = self.weight_spinboxes[code].value()
            result[code] = {
                "show": checkbox.isChecked(),
                "description": entry.get("description") or f"Class {code}",
                "color": entry.get("color", (128, 128, 128)),
                "weight": weight,
                "draw": entry.get("draw", ""),
                "lvl": entry.get("lvl", "")
            }
        return result

    def get_shading_parameters(self):
        """Return lighting parameters for shading/surface presets."""
        if self.mode not in ("shading", "surface"):
            return {}
        params = {
            "azimuth": self.az_spin.value(),
            "angle": self.angle_spin.value(),
            "ambient": self.ambient_spin.value(),
            "quality": self.quality_spin.value(),
        }
        if self.mode == "shading":
            params["quality_mode"] = str(
                self.shading_quality_combo.currentData() or "normal"
            )
        else:
            params["speed"] = self.speed_spin.value()
        if self.mode == "surface" and hasattr(self, "max_edge_spin"):
            params["max_edge"] = self.max_edge_spin.value()
        return params

    def set_selected_classes(self, classes_dict):
        """Pre-select classes from existing preset"""
        for code, checkbox in self.class_checkboxes.items():
            checkbox.blockSignals(True)
            try:
                checkbox.setChecked(False)
            finally:
                checkbox.blockSignals(False)
        for code, checkbox in self.class_checkboxes.items():
            if code in classes_dict:
                is_visible = classes_dict[code].get("show", True)
                checkbox.blockSignals(True)
                try:
                    checkbox.setChecked(is_visible)
                finally:
                    checkbox.blockSignals(False)

    def set_shading_parameters(self, params):
        """Load existing lighting parameters for shading/surface presets."""
        if self.mode not in ("shading", "surface"):
            return
        self.az_spin.setValue(params.get("azimuth", 45.0))
        self.angle_spin.setValue(params.get("angle", 45.0))
        self.ambient_spin.setValue(params.get("ambient", 0.1))
        self.quality_spin.setValue(params.get("quality", 100.0))
        if self.mode == "shading":
            quality_mode = normalize_shading_preset_quality(
                params.get("quality_mode"),
                params.get("speed"),
            )
            quality_index = self.shading_quality_combo.findData(quality_mode)
            self.shading_quality_combo.setCurrentIndex(
                quality_index if quality_index >= 0 else 1
            )
        else:
            self.speed_spin.setValue(params.get("speed", 1))
        if self.mode == "surface" and hasattr(self, "max_edge_spin"):
            self.max_edge_spin.setValue(params.get("max_edge", 0.0))

    def get_target_view(self):
        """Return selected target view index. Only valid for display mode."""
        if self.mode == "display" and hasattr(self, 'view_selector'):
            return self.view_selector.currentIndex()
        return 0

    def set_target_view(self, view_idx):
        """Set target view from existing preset."""
        if self.mode == "display" and hasattr(self, 'view_selector'):
            if 0 <= view_idx < self.view_selector.count():
                self.view_selector.setCurrentIndex(view_idx)

    def get_border_logic_mode(self):
        if hasattr(self, 'border_logic_hybrid') and self.border_logic_hybrid.isChecked(): return 2
        if hasattr(self, 'border_logic_object') and self.border_logic_object.isChecked(): return 1
        return 0

    def set_border_logic_mode(self, mode):
        """Set the border logic mode (0=Per-Point, 1=Structured, 2=Hybrid)."""
        if not hasattr(self, 'border_logic_point'):
            return
        # In an exclusive QActionGroup, unchecking one and checking another can
        # sometimes fail due to Qt state machine ordering. Be explicit:
        # temporarily disable exclusivity, set all states, then re-enable.
        if hasattr(self, 'border_action_group'):
            self.border_action_group.setExclusive(False)
        try:
            self.border_logic_point.setChecked(mode == 0)
            self.border_logic_object.setChecked(mode == 1)
            self.border_logic_hybrid.setChecked(mode == 2)
        finally:
            if hasattr(self, 'border_action_group'):
                self.border_action_group.setExclusive(True)
        print(f"   🔵 Border logic mode set to: {['Per-Point','Structured','Hybrid'][mode] if mode < 3 else mode}")


    def _on_weight_changed(self, code, value):
        """
        ✅ FIX: Only update LOCAL picker state (view_configs).
        Do NOT touch app_window.class_palette, view_palettes, or refresh the view.
        """
        if self.mode == "display" and hasattr(self, 'view_selector'):
            current_view_idx = self.view_selector.currentIndex()
            if not hasattr(self, 'view_configs'):
                self.view_configs = {}
            if current_view_idx not in self.view_configs:
                self.view_configs[current_view_idx] = {}
            if code in self.view_configs[current_view_idx]:
                self.view_configs[current_view_idx][code]["weight"] = value
            else:
                self.view_configs[current_view_idx][code] = {"weight": value}

    def _sync_from_display_mode(self):
        """Sync border and weights from current Display Mode state"""
        if hasattr(self.app_window, 'display_border_percent'):
            self.border_spin.setValue(self.app_window.display_border_percent)
            print(f"✅ Synced border: {self.app_window.display_border_percent}%")
        for code, entry in self.app_window.class_palette.items():
            if code in self.weight_spinboxes:
                weight = entry.get("weight", 1.0)
                self.weight_spinboxes[code].setValue(weight)
        print("✅ Synced all weights from Display Mode")

    def _sync_border(self):
        """Sync border from current Display Mode state"""
        if hasattr(self.app_window, 'display_border_percent'):
            self.border_spin.setValue(self.app_window.display_border_percent)
            print(f"✅ Synced border: {self.app_window.display_border_percent}%")
        else:
            print("⚠️ No display_border_percent found in app_window")

    def get_all_view_configs(self):
        """Return configuration for SINGLE selected view"""
        if not hasattr(self, 'view_configs'):
            self.view_configs = {}

        selected_view_idx = 0
        if hasattr(self, 'view_selector'):
            selected_view_idx = self.view_selector.currentIndex()

        result = {
            "display_mode": (
                str(self.display_mode_selector.currentData() or "class")
                if hasattr(self, "display_mode_selector") else "class"
            ),
            "quality_mode": (
                str(self.shading_quality_selector.currentData() or "normal")
                if hasattr(self, "shading_quality_selector") else "normal"
            ),
            "flight_lines": self.get_line_visibility(),
            "border_percent": self.border_spin.value() if hasattr(self, 'border_spin') else 0,
            "border_type": self.get_border_logic_mode(),
            "views": {}
        }

        if selected_view_idx in self.view_configs:
            result["views"][selected_view_idx] = self.view_configs[selected_view_idx]
            print(f"🔍 Returning config for SINGLE view {selected_view_idx} with user-edited weights")

        return result

    def _on_ok_clicked(self):
        """Save configuration for SINGLE selected view and close dialog"""
        if self.mode == "display" and hasattr(self, 'view_selector'):
            selected_view_idx = self.view_selector.currentIndex()

            print(f"\n{'='*60}")
            print(f"💾 SAVING SINGLE VIEW CONFIGURATION")
            print(f"{'='*60}")
            print(f"   Selected View: {selected_view_idx} ({self.view_selector.currentText()})")

            config = {}
            for code, checkbox in self.class_checkboxes.items():
                if code in self.app_window.class_palette:
                    entry = self.app_window.class_palette[code]
                    actual_weight = 1.0
                    if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                        actual_weight = self.weight_spinboxes[code].value()
                    config[code] = {
                        "show": checkbox.isChecked(),
                        "weight": actual_weight,
                        "description": entry.get("description", ""),
                        "color": entry.get("color", (128, 128, 128)),
                        "draw": entry.get("draw", ""),
                        "lvl": entry.get("lvl", "")
                    }

            if not hasattr(self, 'view_configs'):
                self.view_configs = {}
            self.view_configs[selected_view_idx] = config

            visible_count = sum(1 for c in config.values() if c['show'])
            print(f"   ✅ Saved {visible_count} visible classes with user-edited weights")
            print(f"{'='*60}\n")

        self.accept()

    def _on_view_selector_changed(self, new_index):
        """
        When user changes view selection, load state for that view.
        ✅ FIX: Each shortcut has INDEPENDENT state per view.
        """
        print(f"\n📍 View selector changed to index {new_index}")

        # ── Save current view's state before switching ──
        if hasattr(self, '_last_view_idx') and hasattr(self, 'view_configs'):
            old_idx = self._last_view_idx
            config = {}
            for code, checkbox in self.class_checkboxes.items():
                if code in self.app_window.class_palette:
                    entry = self.app_window.class_palette[code]
                    weight = 1.0
                    if hasattr(self, 'weight_spinboxes') and code in self.weight_spinboxes:
                        weight = self.weight_spinboxes[code].value()
                    config[code] = {
                        "show": checkbox.isChecked(),
                        "weight": weight,
                        "description": entry.get("description", ""),
                        "color": entry.get("color", (128, 128, 128)),
                        "draw": entry.get("draw", ""),
                        "lvl": entry.get("lvl", ""),
                    }
            self.view_configs[old_idx] = config
            print(f"   💾 Saved state for view {old_idx}")

        self._last_view_idx = new_index

        if not hasattr(self, 'view_configs'):
            self.view_configs = {}

        if new_index in self.view_configs:
            # ✅ FIX: Use cached config AS-IS — fully independent per shortcut.
            print(f"   ✅ Using cached config for view {new_index} (independent state)")
        else:
            # No cached config — seed from live state (first time visiting this view)
            parent_sm = self.parent()
            live_classes = None
            live_border  = 0
            live_border_type = 0
            if parent_sm and hasattr(parent_sm, '_get_live_display_state_for_view'):
                live_classes, live_border, live_border_type = parent_sm._get_live_display_state_for_view(new_index)

            if live_classes:
                self.view_configs[new_index] = live_classes
                if hasattr(self, 'border_spin'):
                    self.border_spin.setValue(live_border)
                self.set_border_logic_mode(live_border_type)
                print(f"   ✅ Seeded from live Display Mode state for view {new_index}")
            else:
                print(f"   ℹ️ No live state for view {new_index}, using defaults")

        # Repopulate classes with correct data
        self._populate_classes()

        # Apply checkbox and weight states from view_configs
        if new_index in self.view_configs:
            for code, checkbox in self.class_checkboxes.items():
                if code in self.view_configs[new_index]:
                    checkbox.setChecked(self.view_configs[new_index][code].get("show", True))
            if hasattr(self, 'weight_spinboxes'):
                for code, spin in self.weight_spinboxes.items():
                    if code in self.view_configs[new_index]:
                        spin.setValue(self.view_configs[new_index][code].get("weight", 1.0))

        view_name = (["Main View", "View 1", "View 2", "View 3", "View 4"]
                     [new_index] if new_index < 5 else f"View {new_index}")
        print(f"   ✅ Repopulated classes for {view_name}")
        # Re-seed border from display_mode for the newly selected view
        self._seed_border_from_display_mode()

    def sync_from_app(self):
        """Refresh shading parameters from app state."""
        if self.mode != "shading":
            return
        try:
            self.az_spin.setValue(getattr(self.app_window, 'last_shade_azimuth', 45.0))
            self.angle_spin.setValue(getattr(
                self.app_window, 'shading_sharpness_angle',
                getattr(self.app_window, 'last_shade_angle', 45.0)
            ))
            self.ambient_spin.setValue(getattr(self.app_window, 'shade_ambient', 0.25))
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# ShortcutManager
# ═══════════════════════════════════════════════════════════════════
class ShortcutManager(QWidget):
    # Use object to preserve Python-native shortcut payloads (tuple keys, nested presets)
    # and avoid Qt QVariantMap conversion warnings/crashes.
    applied = Signal(object)
    instance = None

    # ── Column indices ────────────────────────────────────────────────
    COL_CHECK    = 0   # hidden until select mode is active
    COL_MODIFIER = 1
    COL_KEY      = 2
    COL_TOOL     = 3
    COL_CLASSES  = 4

    def __init__(self, app_window, parent=None):
        # Parentless top-level window: gives the dialog its own taskbar button
        # so minimize sends it to the taskbar instead of hiding/closing it.
        super().__init__(None)
        self.setProperty("themeStyledDialog", True)
        self.setWindowTitle("Configure Shortcuts")
        self.setWindowFlags(
            Qt.Window |
            Qt.WindowStaysOnTopHint |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowCloseButtonHint
        )
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self.setAttribute(Qt.WA_QuitOnClose, False)
        self.resize(980, 560)

        # Window icon — use app logo so the taskbar entry has the right icon
        try:
            from PySide6.QtGui import QIcon
            from PySide6.QtCore import QSize
            _icon_dir = os.path.join(os.path.dirname(__file__), "icons")
            _logo = os.path.join(_icon_dir, "logo.png")
            if os.path.exists(_logo):
                _icon = QIcon(_logo)
                for _sz in (16, 20, 24, 32, 48):
                    _icon.addFile(_logo, QSize(_sz, _sz))
                self.setWindowIcon(_icon)
        except Exception:
            pass

        self.app_window = app_window
        self.app_window.shortcut_manager = self

        self.settings = QSettings("NakshaAI", "LidarApp")
        saved_mnu_path = self.settings.value("shortcut_mnu_path", "")
        self.current_mnu_path = str(saved_mnu_path).strip() or None

        # ── Tracks select mode and which rows are checked ─────────────
        self._selected_rows: set = set()
        self._select_mode: bool  = False

        self.setStyleSheet(get_dialog_stylesheet())

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(12)

        intro = QLabel(
            "Configure shortcut keys and attach saved Display Mode, Shading Mode, Draw Settings, or class presets."
        )
        intro.setObjectName("dialogInlineNote")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        selection_hint = QLabel(
            "Right-click a row number to select/delete rows. Double-click the Classes cell to configure a preset."
        )
        selection_hint.setObjectName("dialogInlineNote")
        selection_hint.setWordWrap(True)
        layout.addWidget(selection_hint)

        # ═══════════════════════════════════════════════════════════════
        # ✅ NEW: Enhanced search bar with keyboard capture mode
        # ═══════════════════════════════════════════════════════════════
        from PySide6.QtWidgets import QLineEdit, QCheckBox

        class ShortcutCaptureLineEdit(QLineEdit):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.capture_mode = False
                self.parent_manager = parent
                self._pressed_modifiers = set()
                
            def keyPressEvent(self, event):
                if not self.capture_mode:
                    # Normal typing mode
                    super().keyPressEvent(event)
                    return
                    
                # Keyboard capture mode - build shortcut string
                key = event.key()
                
                # Track modifier keys being held
                if key == Qt.Key_Control:
                    self._pressed_modifiers.add("ctrl")
                elif key == Qt.Key_Alt:
                    self._pressed_modifiers.add("alt")
                elif key == Qt.Key_Shift:
                    self._pressed_modifiers.add("shift")
                
                # Build modifier list from currently pressed modifiers
                modifiers = []
                if event.modifiers() & Qt.ControlModifier or "ctrl" in self._pressed_modifiers:
                    if "ctrl" not in modifiers:
                        modifiers.append("ctrl")
                if event.modifiers() & Qt.AltModifier or "alt" in self._pressed_modifiers:
                    if "alt" not in modifiers:
                        modifiers.append("alt")
                if event.modifiers() & Qt.ShiftModifier or "shift" in self._pressed_modifiers:
                    if "shift" not in modifiers:
                        modifiers.append("shift")
                
                # Sort modifiers in standard order
                modifier_order = {"ctrl": 0, "alt": 1, "shift": 2}
                modifiers.sort(key=lambda x: modifier_order.get(x, 99))
                
                # Determine the actual key (non-modifier)
                key_text = ""
                is_modifier_only = False
                
                # Check if it's ONLY a modifier key press
                if key in (Qt.Key_Control, Qt.Key_Alt, Qt.Key_Shift):
                    is_modifier_only = True
                    # Show modifiers being held
                    if modifiers:
                        search_text = "+".join(modifiers)
                        self.setText(search_text)
                else:
                    # Function keys F1-F12
                    if Qt.Key_F1 <= key <= Qt.Key_F12:
                        key_text = f"F{key - Qt.Key_F1 + 1}"
                    # Letters A-Z
                    elif Qt.Key_A <= key <= Qt.Key_Z:
                        key_text = chr(key).upper()
                    # Space
                    elif key == Qt.Key_Space:
                        key_text = "Space"
                    # Numbers 0-9
                    elif Qt.Key_0 <= key <= Qt.Key_9:
                        key_text = chr(key)
                    # Escape to clear
                    elif key == Qt.Key_Escape:
                        self.clear()
                        self._pressed_modifiers.clear()
                        event.accept()
                        return
                    else:
                        # Unknown key - ignore
                        event.accept()
                        return
                    
                    # Build complete search string with modifiers + key
                    if modifiers and key_text:
                        search_text = "+".join(modifiers) + "+" + key_text
                    elif key_text:
                        search_text = key_text
                    else:
                        event.accept()
                        return
                    
                    self.setText(search_text.lower())
                    self._pressed_modifiers.clear()  # Reset after capturing full combo
                
                event.accept()
            
            def keyReleaseEvent(self, event):
                if self.capture_mode:
                    # Track when modifiers are released
                    key = event.key()
                    if key == Qt.Key_Control:
                        self._pressed_modifiers.discard("ctrl")
                    elif key == Qt.Key_Alt:
                        self._pressed_modifiers.discard("alt")
                    elif key == Qt.Key_Shift:
                        self._pressed_modifiers.discard("shift")
                super().keyReleaseEvent(event)

        search_layout = QHBoxLayout()
        search_layout.setSpacing(8)
        search_layout.setContentsMargins(0, 4, 0, 8)

        search_label = QLabel("🔍 Search:")
        search_label.setObjectName("dialogInlineNote")
        search_layout.addWidget(search_label)

        self.search_box = ShortcutCaptureLineEdit(self)
        self.search_box.setPlaceholderText("Type to search or enable capture mode to press keys...")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(300)
        self.search_box.textChanged.connect(self._filter_shortcuts)
        self.search_box.setObjectName("shortcutSearchBox")
        search_layout.addWidget(self.search_box, stretch=1)

        # Capture mode toggle
        self.capture_mode_check = QCheckBox("Capture Keys")
        self.capture_mode_check.setToolTip(
            "Enable to press actual keyboard shortcuts (e.g., Shift+F1) instead of typing"
        )
        self.capture_mode_check.toggled.connect(self._toggle_capture_mode)
        search_layout.addWidget(self.capture_mode_check)

        layout.addLayout(search_layout)
        # ═══════════════════════════════════════════════════════════════

        # ── Table: 5 columns (col 0 = checkbox, hidden until select mode) ──
        self.table = QTableWidget(0, 5)
        self.table.setObjectName("displayClassTable")
        self.table.setHorizontalHeaderLabels(["", "Modifier", "Key", "Tool", "Classes"])
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setAutoScroll(False)

        # Vertical header — row numbers; right-click opens context menu
        vhdr = self.table.verticalHeader()
        vhdr.setVisible(True)
        vhdr.setDefaultSectionSize(38)
        vhdr.setFixedWidth(36)
        vhdr.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        vhdr.setDefaultAlignment(Qt.AlignCenter)
        vhdr.setStyleSheet("QHeaderView::section { font-weight: normal; font-size: 11px; }")
        vhdr.setContextMenuPolicy(Qt.CustomContextMenu)
        vhdr.customContextMenuRequested.connect(self._show_row_header_context_menu)

        self.table.setStyleSheet("""
            QTableWidget {
                padding-bottom: 0px;
            }
            QTableWidget::item {
                padding: 3px;
            }
            QTableWidget::item:hover {
                background: transparent;
            }
            QTableWidget::item:selected,
            QTableWidget::item:selected:!active {
                background-color: #2a5a8a;
                color: #ffffff;
            }
        """)


        layout.addWidget(self.table, stretch=1)
        layout.addSpacing(25)

        header = self.table.horizontalHeader()
        header.setHighlightSections(False)
        # Col 0: checkbox — fixed, hidden until select mode activates
        header.setSectionResizeMode(self.COL_CHECK,    QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(self.COL_CHECK, 32)
        self.table.setColumnHidden(self.COL_CHECK, True)
        header.setSectionResizeMode(self.COL_MODIFIER, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_KEY,      QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_TOOL,     QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(self.COL_CLASSES,  QHeaderView.ResizeMode.Stretch)

        # ── Table body right-click ────────────────────────────────────
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)

        # ── Buttons ───────────────────────────────────────────────────
        btns = QHBoxLayout()
        btns.setContentsMargins(0, 0, 0, 0)
        btns.setSpacing(10)
        self.add_btn    = QPushButton("Add");              self.add_btn.setObjectName("add_btn")
        self.del_btn    = QPushButton("Delete Selected");  self.del_btn.setObjectName("del_btn")
        self.del_btn.setToolTip("Deletes selected rows (right-click row number to select).")
        self.load_btn   = QPushButton("Load");             self.load_btn.setObjectName("displayActionButton")
        self.save_btn   = QPushButton("Save");             self.save_btn.setObjectName("displayActionButton")
        self.save_as_btn = QPushButton("Save As...");      self.save_as_btn.setObjectName("displayActionButton")
        self.apply_btn  = QPushButton("Apply");            self.apply_btn.setObjectName("primaryBtn")
        self.cancel_btn = QPushButton("Close");            self.cancel_btn.setObjectName("secondaryBtn")
        self.cancel_btn.clicked.connect(self.close)

        footer_buttons = [
            self.add_btn, self.del_btn, self.load_btn, self.save_btn,
            self.save_as_btn, self.apply_btn, self.cancel_btn,
        ]
        for b in footer_buttons:
            b.setStyleSheet("min-height: 32px; padding: 7px 10px;")
            b.setFixedHeight(48)
            b.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            b.setAutoDefault(False)
            b.setDefault(False)
            b.setFocusPolicy(Qt.NoFocus)
            btns.addWidget(b, 1)
        layout.addLayout(btns)

        self.add_btn.clicked.connect(self.on_add)
        self.del_btn.clicked.connect(self.on_delete)
        self.load_btn.clicked.connect(self.load_mnu)
        self.save_btn.clicked.connect(self.save_mnu)
        self.save_as_btn.clicked.connect(self.save_mnu_as)
        self.apply_btn.clicked.connect(self.on_apply)

        self._update_save_button_tooltip()

        self.table.cellDoubleClicked.connect(self.on_class_edit)
        self.table.itemClicked.connect(self.on_item_clicked)

        self._active_row = None
        self.is_editing_shortcuts = False
        self._pending_display_mode_row = None
        self._is_loading_shortcuts = False
        self._display_border_sync_connected = False
        self.auto_load_shortcuts()

    def _ensure_display_border_sync(self):
        """Lazily connect to display_mode.border_changed for table label updates."""
        if self._display_border_sync_connected:
            return
        app = self.app_window
        dialog = getattr(app, "display_mode_dialog", None) or getattr(app, "display_dialog", None)
        if dialog is None or not hasattr(dialog, "border_changed"):
            return
        try:
            dialog.border_changed.connect(self._on_table_border_changed)
            self._display_border_sync_connected = True
        except Exception:
            pass

    def _on_table_border_changed(self, slot_idx: int, border_percent: float, logic_mode: int):
        """Update DisplayMode shortcut summaries when a live view border changes.

        This callback is connected directly to DisplayModeDialog.border_changed,
        so it must not depend on ClassVisibilityPicker-local variables.  The
        shortcut preset stored in the table cell is the authoritative source for
        the saved display-mode label and per-view class configuration.
        """
        _VIEW_NAMES = ["Main", "View 1", "View 2", "View 3", "View 4", "Cut"]
        _MODE_LABELS = {
            "class": "By Classification",
            "shaded_class": "Shaded Classification",
            "depth": "Depth",
            "intensity": "Intensity",
            "rgb": "RGB",
            "elevation": "Elevation",
            "surface": "Surface",
            "line": "Line",
        }

        try:
            slot_idx = int(slot_idx)
            border_percent = float(border_percent)
            logic_mode = int(logic_mode)
        except (TypeError, ValueError):
            return

        for row in range(self.table.rowCount()):
            try:
                tool_w = self.table.cellWidget(row, self.COL_TOOL)
                if tool_w is None:
                    continue

                tool = tool_w.currentText() if hasattr(tool_w, "currentText") else ""
                if tool != "DisplayMode":
                    continue

                cell = self.table.item(row, self.COL_CLASSES)
                if cell is None:
                    continue

                preset = decode_display_preset(cell.data(Qt.UserRole))
                if preset is None:
                    continue

                views = preset.get("views", {}) or {}
                if not views:
                    continue

                # Current DisplayMode shortcuts are single-view presets.  Keep
                # compatibility with either integer or JSON-string view keys.
                view_idx = next(iter(views.keys()))
                try:
                    view_idx_int = int(view_idx)
                except (TypeError, ValueError):
                    continue

                if view_idx_int != slot_idx:
                    continue

                view_classes = views.get(view_idx)
                if view_classes is None:
                    view_classes = views.get(view_idx_int, views.get(str(view_idx_int), {}))
                if not isinstance(view_classes, dict):
                    continue

                # Update only the border fields.  Visibility, weights, target
                # view and saved display mode remain untouched.
                preset["border_percent"] = border_percent
                preset["border_type"] = logic_mode

                visible = sum(
                    1 for class_info in view_classes.values()
                    if isinstance(class_info, dict) and class_info.get("show")
                )

                weights = [
                    float(class_info.get("weight", 1.0))
                    for class_info in view_classes.values()
                    if isinstance(class_info, dict)
                ]
                if not weights:
                    weight_info = "W=1.0"
                elif len(set(weights)) == 1:
                    weight_info = f"W={weights[0]:.1f}"
                else:
                    weight_info = f"W={min(weights):.1f}-{max(weights):.1f}"

                view_name = (
                    _VIEW_NAMES[slot_idx]
                    if 0 <= slot_idx < len(_VIEW_NAMES)
                    else f"View {slot_idx}"
                )

                mode_key = str(preset.get("display_mode", "class") or "class")
                mode_label = _MODE_LABELS.get(mode_key, mode_key)

                summary = (
                    f"{view_name}: {mode_label}, {visible}vis, "
                    f"B={border_percent:.1f}%, {weight_info}"
                )
                cell.setText(summary)
                cell.setData(Qt.UserRole, encode_display_preset(preset))

            except Exception as exc:
                # A malformed/stale shortcut row must never crash the entire
                # application when the Display Mode border changes.
                print(
                    f"⚠️ DisplayMode shortcut border sync skipped for row {row}: {exc}"
                )

    def _restore_app_focus(self):
        """Return keyboard focus to the main app/view after closing this manager."""
        app = getattr(self, "app_window", None)
        if app is None:
            return

        def _focus_once():
            try:
                app.raise_()
            except Exception:
                pass
            try:
                app.activateWindow()
            except Exception:
                pass

            candidates = []

            try:
                sc = getattr(app, "section_controller", None)
                active_view = getattr(sc, "active_view", None)
                section_vtks = getattr(app, "section_vtks", None)
                if isinstance(section_vtks, dict) and active_view in section_vtks:
                    active_vtk = section_vtks.get(active_view)
                    if active_vtk is not None:
                        inter = getattr(active_vtk, "interactor", None)
                        if inter is not None:
                            candidates.append(inter)
                        candidates.append(active_vtk)
            except Exception:
                pass

            main_vtk = getattr(app, "vtk_widget", None)
            if main_vtk is not None:
                inter = getattr(main_vtk, "interactor", None)
                if inter is not None:
                    candidates.append(inter)
                candidates.append(main_vtk)

            for widget in candidates:
                try:
                    if hasattr(widget, "isVisible") and callable(widget.isVisible) and not widget.isVisible():
                        continue
                    if hasattr(widget, "setFocus"):
                        widget.setFocus(Qt.ActiveWindowFocusReason)
                        return
                except Exception:
                    continue

        QTimer.singleShot(0, _focus_once)
        QTimer.singleShot(50, _focus_once)

    # ── Modifier / Key options ────────────────────────────────────────
    def _modifier_options(self):
        return [
            "alt", "ctrl", "shift",
            "alt+shift", "ctrl+alt",
            "ctrl+shift", "ctrl+alt+shift", "none"
        ]

    def _key_options(self):
        keys = [f"F{i}" for i in range(1, 13)]
        keys += [chr(i) for i in range(65, 91)]
        keys.append("Space")
        return keys

    # ─────────────────────────────────────────────────────────────────
    # CHECKBOX COLUMN helpers
    # ─────────────────────────────────────────────────────────────────
    def _get_row_checkbox(self, row):
        """Return the QCheckBox widget in col 0 for the given row, or None."""
        holder = self.table.cellWidget(row, self.COL_CHECK)
        return getattr(holder, '_cb', None) if holder else None

    def _install_row_checkbox(self, row, checked=False):
        """Put a real QCheckBox into col 0. Only visible when select mode is on."""
        cb = QCheckBox()
        cb.setChecked(checked)
        cb.setFocusPolicy(Qt.NoFocus)
        cb.setCursor(Qt.PointingHandCursor)

        holder = QWidget()
        holder.setStyleSheet("background: transparent;")
        lay = QHBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setAlignment(Qt.AlignCenter)
        lay.addWidget(cb)
        holder._cb = cb

        cb.toggled.connect(lambda state, r=row: self._on_checkbox_toggled(r, state))
        self.table.setCellWidget(row, self.COL_CHECK, holder)

    def _on_checkbox_toggled(self, row, checked):
        if checked:
            self._selected_rows.add(row)
        else:
            self._selected_rows.discard(row)

    def _checked_rows(self) -> list:
        """Return rows whose checkbox is checked."""
        rows = []
        for r in range(self.table.rowCount()):
            cb = self._get_row_checkbox(r)
            if cb and cb.isChecked():
                rows.append(r)
        return sorted(rows)

    # ─────────────────────────────────────────────────────────────────
    # SELECT MODE  on / off
    # ─────────────────────────────────────────────────────────────────
    def _activate_select_mode(self):
        """Show the checkbox column — all rows get empty boxes ready to check."""
        self._select_mode = True
        for r in range(self.table.rowCount()):
            if self._get_row_checkbox(r) is None:
                self._install_row_checkbox(r, checked=False)
            else:
                cb = self._get_row_checkbox(r)
                if cb:
                    cb.setChecked(False)
        self._selected_rows.clear()
        self.table.setColumnHidden(self.COL_CHECK, False)

    def _deactivate_select_mode(self):
        """Hide checkbox column and clear all selections."""
        self._select_mode = False
        self._selected_rows.clear()
        for r in range(self.table.rowCount()):
            cb = self._get_row_checkbox(r)
            if cb:
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
        self.table.setColumnHidden(self.COL_CHECK, True)

    def _check_all_rows(self, state: bool):
        for r in range(self.table.rowCount()):
            cb = self._get_row_checkbox(r)
            if cb:
                cb.setChecked(state)

    # ─────────────────────────────────────────────────────────────────
    # VERTICAL HEADER right-click context menu
    # ─────────────────────────────────────────────────────────────────
    def _show_row_header_context_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        header = self.table.verticalHeader()
        row = header.logicalIndexAt(pos)

        menu = QMenu(self)

        if row >= 0:
            act_delete = menu.addAction("Delete")
            act_delete.triggered.connect(lambda _=False, r=row: self._delete_selected_rows([r]))
            menu.addSeparator()

        if self._select_mode:
            act_all   = menu.addAction("Select All")
            act_none  = menu.addAction("Deselect All")
            menu.addSeparator()
            act_deact = menu.addAction("Deactivate select mode")
            act_all.triggered.connect(lambda: self._check_all_rows(True))
            act_none.triggered.connect(lambda: self._check_all_rows(False))
            act_deact.triggered.connect(self._deactivate_select_mode)
        else:
            act_act = menu.addAction("Activate select mode")
            act_act.triggered.connect(self._activate_select_mode)

        menu.exec(header.viewport().mapToGlobal(pos))

    def _update_row_header(self, row: int):
        """Plain row number always — selection is shown via the checkbox column."""
        item = QTableWidgetItem(str(row + 1))
        item.setTextAlignment(Qt.AlignCenter)
        item.setFlags(Qt.ItemIsEnabled)
        self.table.setVerticalHeaderItem(row, item)

    def _refresh_all_row_headers(self):
        for r in range(self.table.rowCount()):
            self._update_row_header(r)

    def _filter_shortcuts(self, search_text):
        """
        Filter shortcuts table based on search text.
        Searches across modifier, key, tool, and classes/details columns.
        """
        search_text = search_text.lower().strip()
        
        visible_count = 0
        
        for row in range(self.table.rowCount()):
            # Get cell widgets and items
            mod_combo = self.table.cellWidget(row, self.COL_MODIFIER)
            key_combo = self.table.cellWidget(row, self.COL_KEY)
            tool_combo = self.table.cellWidget(row, self.COL_TOOL)
            classes_item = self.table.item(row, self.COL_CLASSES)
            
            # Show/hide row based on search match
            if shortcut_search_matches(
                search_text,
                mod_combo.currentText() if mod_combo else "",
                key_combo.currentText() if key_combo else "",
                tool_combo.currentText() if tool_combo else "",
                classes_item.text() if classes_item else "",
            ):
                self.table.setRowHidden(row, False)
                visible_count += 1
            else:
                self.table.setRowHidden(row, True)
        
        # Optional: Update window title with filter info
        total_count = self.table.rowCount()
        if search_text:
            self.setWindowTitle(f"Configure Shortcuts - Showing {visible_count} of {total_count}")
        else:
            self.setWindowTitle("Configure Shortcuts")

    def _toggle_capture_mode(self, enabled):
        """Toggle between typing mode and keyboard capture mode."""
        self.search_box.capture_mode = enabled
        
        if enabled:
            self.search_box.setPlaceholderText("Press a keyboard shortcut (e.g., Shift+F1)...")
            self.search_box.setStyleSheet(
                "QLineEdit { background-color: #2d4a2e; border: 2px solid #4a7c4e; }"
            )
            self.search_box.clear()
            self.search_box.setFocus()
        else:
            self.search_box.setPlaceholderText("Type to search: modifier, key, tool, or configuration...")
            self.search_box.setStyleSheet("")

    def _clear_selection(self):
        self._deactivate_select_mode()

    def _get_selected_rows(self) -> list:
        checked = self._checked_rows()
        if checked:
            return checked
        current_row = self.table.currentRow()
        return [current_row] if current_row >= 0 else []

    def _set_current_row(self, row: int):
        if row >= 0:
            self.table.selectRow(row)

    def _install_row_widgets(self, row, modifier="alt", key="F1", tool="AboveLine"):
        """Install all widgets for a row, including the (hidden) checkbox in col 0."""
        self._install_row_checkbox(row)

        mod_combo = QComboBox()
        mod_combo.addItems(self._modifier_options())
        mod_combo.setCurrentText(modifier)
        mod_combo.setFocusPolicy(Qt.StrongFocus)
        mod_combo.installEventFilter(self)
        mod_combo.currentTextChanged.connect(lambda _, r=row: self._on_key_changed(r))
        self.table.setCellWidget(row, self.COL_MODIFIER, mod_combo)

        key_combo = QComboBox()
        key_combo.addItems(self._key_options())
        key_combo.setCurrentText(key)
        key_combo.setFocusPolicy(Qt.StrongFocus)
        key_combo.installEventFilter(self)
        key_combo.currentTextChanged.connect(lambda _, r=row: self._on_key_changed(r))
        self.table.setCellWidget(row, self.COL_KEY, key_combo)

        tool_combo = QComboBox()
        tool_combo.addItems(TOOLS)
        tool_combo.blockSignals(True)
        tool_combo.setCurrentText(tool)
        tool_combo.blockSignals(False)
        tool_combo.setFocusPolicy(Qt.StrongFocus)
        tool_combo.installEventFilter(self)
        tool_combo.currentTextChanged.connect(lambda t, r=row: self._toggle_class_cell(r, t))
        self.table.setCellWidget(row, self.COL_TOOL, tool_combo)

        self._update_row_header(row)

    # ─────────────────────────────────────────────────────────────────
    # TABLE BODY right-click context menu
    # ─────────────────────────────────────────────────────────────────
    def _show_context_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        clicked_item = self.table.itemAt(pos)
        clicked_row  = clicked_item.row() if clicked_item else -1

        menu = QMenu(self)

        sel_rows = self._get_selected_rows()
        if not sel_rows and clicked_row >= 0:
            sel_rows = [clicked_row]

        if sel_rows:
            if len(sel_rows) == 1:
                row = sel_rows[0]
                mc = self.table.cellWidget(row, self.COL_MODIFIER)
                kc = self.table.cellWidget(row, self.COL_KEY)
                tc = self.table.cellWidget(row, self.COL_TOOL)
                label = (f"Delete  {mc.currentText() if mc else '?'}+"
                         f"{kc.currentText() if kc else '?'} → "
                         f"{tc.currentText() if tc else '?'}")
            else:
                label = f"Delete {len(sel_rows)} selected shortcuts"

            act_delete = menu.addAction(label)
            act_delete.setIcon(self.style().standardIcon(
                self.style().StandardPixmap.SP_TrashIcon
            ))
            act_delete.triggered.connect(lambda: self._delete_selected_rows(sel_rows))
            menu.addSeparator()

        if self._select_mode:
            act_all   = menu.addAction("Select All")
            act_none  = menu.addAction("Deselect All")
            menu.addSeparator()
            act_deact = menu.addAction("Deactivate select mode")
            act_all.triggered.connect(lambda: self._check_all_rows(True))
            act_none.triggered.connect(lambda: self._check_all_rows(False))
            act_deact.triggered.connect(self._deactivate_select_mode)
        else:
            act_act = menu.addAction("Activate select mode")
            act_act.triggered.connect(self._activate_select_mode)

        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _delete_selected_rows(self, rows=None):
        sel_rows = sorted(set(rows if rows is not None else self._get_selected_rows()))
        if not sel_rows:
            return

        if len(sel_rows) == 1:
            row = sel_rows[0]
            mc = self.table.cellWidget(row, self.COL_MODIFIER)
            kc = self.table.cellWidget(row, self.COL_KEY)
            tc = self.table.cellWidget(row, self.COL_TOOL)
            msg = (f"Delete  {mc.currentText() if mc else '?'} + "
                   f"{kc.currentText() if kc else '?'}  →  "
                   f"{tc.currentText() if tc else '?'}?")
        else:
            lines = []
            for row in sel_rows:
                mc = self.table.cellWidget(row, self.COL_MODIFIER)
                kc = self.table.cellWidget(row, self.COL_KEY)
                tc = self.table.cellWidget(row, self.COL_TOOL)
                lines.append(
                    f"  • Row {row + 1}:  "
                    f"{mc.currentText() if mc else '?'}+"
                    f"{kc.currentText() if kc else '?'} → "
                    f"{tc.currentText() if tc else '?'}"
                )
            msg = f"Delete {len(sel_rows)} shortcuts?\n\n" + "\n".join(lines)

        reply = QMessageBox.question(
            self, "Delete shortcuts", msg,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for row in sorted(sel_rows, reverse=True):
            self.table.removeRow(row)

        self._selected_rows.clear()
        for r in range(self.table.rowCount()):
            cb = self._get_row_checkbox(r)
            if cb and cb.isChecked():
                self._selected_rows.add(r)
        self._refresh_all_row_headers()

        new_row = min(min(sel_rows), self.table.rowCount() - 1)
        if new_row >= 0:
            self._set_current_row(new_row)

    def on_item_clicked(self, item):
        """Prevent Classes column from being edited with single click"""
        if item.column() == self.COL_CLASSES:
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)

    # ── Duplicate key check ───────────────────────────────────────────
    def _is_key_duplicate(self, mod, key, current_row):
        """Check if this modifier+key combo already exists (excluding current row)"""
        for row in range(self.table.rowCount()):
            if row == current_row:
                continue
            mod_combo = self.table.cellWidget(row, self.COL_MODIFIER)
            key_combo = self.table.cellWidget(row, self.COL_KEY)
            if mod_combo and key_combo:
                if (mod_combo.currentText().lower() == mod.lower() and
                        key_combo.currentText().upper() == key.upper()):
                    return True, row
        return False, -1

    def _on_key_changed(self, row):
        """Called when modifier or key changes — check for duplicates."""
        if self._is_loading_shortcuts:
            return
        mod_combo = self.table.cellWidget(row, self.COL_MODIFIER)
        key_combo = self.table.cellWidget(row, self.COL_KEY)
        if not mod_combo or not key_combo:
            return
        mod = mod_combo.currentText()
        key = key_combo.currentText()
        is_dup, dup_row = self._is_key_duplicate(mod, key, row)
        if is_dup:
            QMessageBox.warning(
                self,
                "Duplicate Shortcut",
                f"⚠️ {mod}+{key} is already assigned to row {dup_row + 1}!\n\n"
                "Each shortcut key can only be used once.\n"
                "Please choose a different key or modifier."
            )
            # ✅ Reset to F1 to avoid conflict (restored from old code)
            key_combo.setCurrentText("F1")

    def on_add(self):
        # A filtered table can hide the appended row. Adding starts a new edit,
        # so return to the full list before revealing the row at the bottom.
        self.search_box.clear()

        row = self.table.rowCount()
        self.table.insertRow(row)
        self._install_row_widgets(row)

        item = QTableWidgetItem("Any → Any")
        item.setData(Qt.UserRole, encode_classes(None, None))
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        self.table.setItem(row, self.COL_CLASSES, item)
        self._set_current_row(row)

        def _reveal_new_row():
            self.table.scrollToBottom()
            modifier_combo = self.table.cellWidget(row, self.COL_MODIFIER)
            if modifier_combo is not None:
                modifier_combo.setFocus()

        # Wait for the inserted row's geometry and scrollbar range to update.
        QTimer.singleShot(0, _reveal_new_row)

    @staticmethod
    def apply_shortcuts_from_settings(app_window):
        """
        ✅ FIXED: Ctrl+Shift+S — reload shortcut DEFINITIONS only.
        NEVER touch view_palettes or class_palette.
        """
        print(f"\n{'='*60}")
        print(f"⚡ RELOADING SHORTCUTS FROM SETTINGS (Ctrl+Shift+S)")
        print(f"{'='*60}")

        try:
            settings = QSettings("NakshaAI", "LidarApp")
            shortcuts_data = settings.value("shortcuts", None)

            if shortcuts_data is None:
                print("   ⚠️ No saved shortcuts found in QSettings")
                if hasattr(app_window, 'statusBar'):
                    app_window.statusBar().showMessage(
                        "⚠️ No shortcuts configured — open Shortcut Manager first", 3000
                    )
                return

            if isinstance(shortcuts_data, str):
                shortcuts_list = json.loads(shortcuts_data)
            else:
                shortcuts_list = shortcuts_data

            print(f"   📋 Found {len(shortcuts_list)} shortcuts in storage")

            shortcuts = {}
            simple_tools = SIMPLE_SHORTCUT_TOOLS

            for entry in shortcuts_list:
                modifier  = entry.get("modifier", "alt")
                key       = entry.get("key", "F1")
                tool      = entry.get("tool", "AboveLine")
                mod       = modifier.lower()
                key_upper = key.upper()

                if tool == "LineMode":
                    preset_payload = line_mode_to_display_visibility_preset(
                        entry.get("line_mode_preset")
                    )
                    shortcuts[(mod, key_upper)] = {
                        "tool": "Line",
                        "preset": preset_payload,
                    }
                    continue

                if tool == "DisplayMode":
                    preset_payload = entry.get("display_preset")
                    if preset_payload:
                        shortcuts[(mod, key_upper)] = {
                            "tool": "DisplayMode",
                            "preset": preset_payload
                        }
                        print(f"      ✅ {mod}+{key_upper} → DisplayMode [stored only]")
                    continue

                if tool == "Surface":
                    preset_payload = entry.get("surface_preset")
                    shortcuts[(mod, key_upper)] = {
                        "tool": "Surface",
                        "preset": preset_payload
                    }
                    print(f"      Surface stored shortcut: {mod}+{key_upper}")
                    continue

                if tool == "ShadingMode":
                    preset_payload = entry.get("shading_preset")
                    if preset_payload:
                        shortcuts[(mod, key_upper)] = {
                            "tool": "ShadingMode",
                            "preset": preset_payload
                        }
                        print(f"      ✅ {mod}+{key_upper} → ShadingMode [stored only]")
                    continue

                if tool == "SyncViews":

                    preset_payload = entry.get("sync_preset")

                    if not preset_payload:

                        preset_payload = {"rows": []}
                
                    shortcuts[(mod, key_upper)] = {

                        "tool": "SyncViews",

                        "preset": preset_payload

                    }
                
                    print(f"      ✅ {mod}+{key_upper} → SyncViews [stored only]")

                    continue
 

                if tool in DISPLAY_VISIBILITY_TOOLS:
                    preset_payload = entry.get("visibility_preset")
                    shortcuts[(mod, key_upper)] = {
                        "tool": tool,
                        "preset": preset_payload or {"mode": tool, "classes": {}},
                    }
                    continue

                if tool in simple_tools:
                    shortcuts[(mod, key_upper)] = {
                        "tool": tool, "from": None, "to": None
                    }
                    print(f"      ✅ {mod}+{key_upper} → {tool}")
                else:
                    from_cls = entry.get("from_classes")
                    to_cls   = entry.get("to_class")
                    shortcuts[(mod, key_upper)] = {
                        "tool": tool, "from": from_cls, "to": to_cls
                    }
                    print(f"      ✅ {mod}+{key_upper} → {tool} [{from_cls} → {to_cls}]")

            # ✅ ONLY update the shortcuts lookup table — nothing else
            app_window.shortcuts = shortcuts

            if hasattr(app_window, 'statusBar'):
                app_window.statusBar().showMessage(
                    f"✅ {len(shortcuts)} shortcuts reloaded (press key to apply)", 2500
                )

            print(f"   ✅ {len(shortcuts)} shortcuts loaded — view_palettes/class_palette UNTOUCHED")
            print(f"{'='*60}\n")

        except Exception as e:
            print(f"❌ apply_shortcuts_from_settings failed: {e}")
            import traceback
            traceback.print_exc()
            if hasattr(app_window, 'statusBar'):
                app_window.statusBar().showMessage(f"❌ Failed: {e}", 3000)

    def on_delete(self):
        sel_rows = self._get_selected_rows()
        if not sel_rows:
            last = self.table.rowCount() - 1
            if last >= 0:
                self._set_current_row(last)
                sel_rows = [last]
            else:
                return
        self._delete_selected_rows(sel_rows)

    def eventFilter(self, obj, event):
        """Block mouse wheel scrolling on combo boxes; track clicks for row selection."""
        if isinstance(obj, QComboBox):
            if event.type() == QEvent.Wheel:
                event.ignore()
                return True
            if event.type() == QEvent.MouseButtonPress:
                for row in range(self.table.rowCount()):
                    for col in range(self.COL_MODIFIER, self.COL_TOOL + 1):
                        if self.table.cellWidget(row, col) is obj:
                            self._set_current_row(row)
                            break
        return super().eventFilter(obj, event)

    def changeEvent(self, event):
        """Intercept minimize — hide instead of collapsing to ugly mini title-bar."""
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowMinimized:
                event.ignore()
                self.setWindowState(Qt.WindowNoState)
                self.hide()
                return
        super().changeEvent(event)

    def _toggle_class_cell(self, row, tool_text):
        """Disable class selection for some tools."""
        no_config_tools = tuple(
            t for t in SIMPLE_SHORTCUT_TOOLS
            if t != "Surface" and t not in DISPLAY_VISIBILITY_TOOLS
        )

        if tool_text in no_config_tools:
            item = QTableWidgetItem("N/A")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_classes(None, None))
            self.table.setItem(row, self.COL_CLASSES, item)
            return

        if tool_text in DISPLAY_VISIBILITY_TOOLS:
            item = QTableWidgetItem(
                f"Click/Double-click to configure {tool_text} class visibility"
            )
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_display_visibility_preset({
                "mode": tool_text, "classes": {}
            }))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_display_visibility_for_row(row, tool_text)
            return

        if tool_text == "LineMode":
            item = QTableWidgetItem(
                "Click/Double-click to configure flight lines"
            )
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(
                Qt.UserRole,
                encode_line_mode_preset({"lines": {}}),
            )
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_line_mode_for_row(row)
            return

        if tool_text == "DisplayMode":
            item = QTableWidgetItem("Click/Double-click to configure display preset")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_display_preset({
                "classes": {}, "slot": 0, "target_view": 0,
                "color_mode": 0, "border_percent": 0, "force_refresh": True
            }))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_display_mode_for_row(row)
            return

        if tool_text == "Surface":
            item = QTableWidgetItem("Click/Double-click to configure surface preset")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_surface_preset({
                "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                "quality": 100.0, "speed": 1, "max_edge": 0.0, "classes": {}
            }))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_surface_for_row(row)
            return

        if tool_text == "ShadingMode":
            item = QTableWidgetItem("Preset: Not configured yet")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_shading_preset({
                "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                "quality": 100.0, "quality_mode": "normal", "classes": {}
            }))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_shading_mode_for_row(row)
            return

        if tool_text == "DrawSettings":
            item = QTableWidgetItem("Click/Double-click to configure draw preset")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_draw_preset({"tools": {}}))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_draw_settings_for_row(row)
            return

        if tool_text == "SyncViews":
            item = QTableWidgetItem("Double-click to configure sync preset")
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_sync_preset({"rows": []}))
            self.table.setItem(row, self.COL_CLASSES, item)
            if not self._is_loading_shortcuts:
                self._open_sync_views_for_row(row)
            return

# ── Classification tool (AboveLine, BelowLine, Brush, Freehand, etc.) ──
        # When the user switches between two classification tools, the existing
        # from/to class configuration must be preserved.  Only reset to
        # "Any → Any" when the previous cell held a preset (DisplayMode /
        # ShadingMode / DrawSettings) or an N/A entry — i.e. when the stored
        # JSON does NOT contain the "from" / "to" keys that encode_classes writes.
        if not self._is_loading_shortcuts:
            existing_cell = self.table.item(row, self.COL_CLASSES)
            if existing_cell is not None:
                raw = existing_cell.data(Qt.UserRole)
                if raw is not None:
                    try:
                        import json as _json
                        data = _json.loads(raw) if isinstance(raw, str) else raw
                        if isinstance(data, dict) and "from" in data and "to" in data:
                            # Cell already holds class data from a classification
                            # tool — keep it exactly as-is, nothing to do.
                            return
                    except Exception:
                        pass  # malformed data → fall through and reset below

        item = QTableWidgetItem("Any → Any")
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        item.setData(Qt.UserRole, encode_classes(None, None))
        self.table.setItem(row, self.COL_CLASSES, item)

    @staticmethod
    def _safe_disconnect(signal, slot):
        """Disconnect a Qt signal-slot pair without runtime warning noise."""
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"Failed to disconnect .*",
                    category=RuntimeWarning,
                )
                signal.disconnect(slot)
        except Exception:
            pass

    @staticmethod
    def _qobject_alive(obj) -> bool:
        """
        Return True only if `obj` is a Qt wrapper whose C++ side is still alive.

        After a dialog/widget is closed and Qt sweeps the C++ object, the Python
        wrapper survives but any attribute access on it raises
        ``RuntimeError: Signal source has been deleted`` / ``Internal C++ object
        ... already deleted``. The naive ``if obj is None`` check does NOT
        catch this — which is exactly how shortcut_manager.on_class_edit
        crashed in the field (`gui\\shortcut_manager.py:2588`). Probe with a
        cheap ``objectName()`` call inside try/except — any QObject supports
        it and it raises immediately if the C++ object is gone.
        """
        if obj is None:
            return False
        try:
            obj.objectName()
            return True
        except (RuntimeError, ReferenceError):
            return False
        except Exception:
            return False

    def _ensure_class_picker(self):
        """
        Return a usable ClassPicker. Recreates it if the previous instance's
        C++ side has been destroyed (e.g. project was cleared while a stale
        Python ref lingered, or the picker window was closed and swept).
        """
        picker = getattr(self.app_window, "class_picker", None)
        if (
            self._qobject_alive(picker)
            and self._qobject_alive(getattr(picker, "from_list", None))
            and self._qobject_alive(getattr(picker, "to_combo", None))
        ):
            return picker

        # Drop the stale wrapper before constructing the replacement so that
        # any later code path can rely on the standard `is None` check.
        try:
            self.app_window.class_picker = None
        except Exception:
            pass

        from .class_picker import ClassPicker
        # Keep ClassPicker owned by app window (not ShortcutManager) so closing
        # this dialog never destroys the picker and leaves a stale wrapper.
        picker = ClassPicker(self.app_window, parent=self.app_window)
        self.app_window.class_picker = picker
        return picker

    def on_class_edit(self, row, col):
        """Only opens on DOUBLE-CLICK"""
        if col != self.COL_CLASSES:
            return

        tool_combo = self.table.cellWidget(row, self.COL_TOOL)
        tool = tool_combo.currentText() if tool_combo else ""

        if tool in SIMPLE_SHORTCUT_TOOLS:
            print(f"ℹ️ {tool} has no class configuration - N/A")
            return

        if tool == "DisplayMode":
            self._open_display_mode_for_row(row)
            return

        if tool == "ShadingMode":
            self._open_shading_mode_for_row(row)
            return

        if tool == "DrawSettings":
            self._open_draw_settings_for_row(row)
            return

        if tool == "SyncViews":
            self._open_sync_views_for_row(row)
            return

        self._active_row = row
        self.is_editing_shortcuts = True

        # Validity-aware fetch — recreates the picker if its Qt object is dead.
        picker = self._ensure_class_picker()

        # Belt-and-suspenders: even with the validity probe above there is a
        # narrow window where the C++ object could be swept between the check
        # and the use (Qt event loop). Wrap the whole picker-touching block in
        # try/except so a death here is recoverable instead of fatal.
        try:
            # ✅ Always disconnect old connections first (prevents duplicates)
            self._safe_disconnect(
                picker.from_list.itemSelectionChanged,
                self.update_classes_from_picker,
            )
            self._safe_disconnect(
                picker.to_combo.currentIndexChanged,
                self.update_classes_from_picker,
            )

            picker.from_list.itemSelectionChanged.connect(self.update_classes_from_picker)
            picker.to_combo.currentIndexChanged.connect(self.update_classes_from_picker)

            print(f"✅ Signals reconnected for row {row}")

            picker.show()
            picker.raise_()
            picker.activateWindow()

            cell = self.table.item(row, self.COL_CLASSES)
            if cell and cell.data(Qt.UserRole):
                from_cls, to_cls = decode_classes(cell.data(Qt.UserRole))

                if from_cls is not None:
                    picker.from_list.clearSelection()
                    from_list = from_cls if isinstance(from_cls, list) else [from_cls]
                    for i in range(picker.from_list.count()):
                        item = picker.from_list.item(i)
                        if item.data(Qt.UserRole) in from_list:
                            item.setSelected(True)
                    print(f"   📋 Loaded FROM classes: {from_list}")
                else:
                    for i in range(picker.from_list.count()):
                        item = picker.from_list.item(i)
                        if item.data(Qt.UserRole) is None:
                            item.setSelected(True)
                            break
                    print(f"   📋 Loaded FROM: Any")

                if to_cls is not None:
                    idx = picker.to_combo.findData(to_cls)
                    if idx >= 0:
                        picker.to_combo.setCurrentIndex(idx)
                        print(f"   📋 Loaded TO class: {to_cls}")
                else:
                    idx = picker.to_combo.findData(None)
                    if idx >= 0:
                        picker.to_combo.setCurrentIndex(idx)
                        print(f"   📋 Loaded TO: Any")
        except RuntimeError as exc:
            # Underlying C++ object disappeared mid-flight. Clear the dead
            # reference so the next double-click rebuilds a fresh picker.
            print(f"⚠️ Class picker became invalid during edit: {exc}")
            try:
                self.app_window.class_picker = None
            except Exception:
                pass
            try:
                self.is_editing_shortcuts = False
            except Exception:
                pass

    def _get_or_create_display_mode_dialog(self):
        if hasattr(self.app_window, "ensure_display_mode_dialog"):
            if self.app_window.ensure_display_mode_dialog():
                return getattr(self.app_window, "display_mode_dialog", None)
            return None
        dlg = getattr(self.app_window, "display_mode_dialog", None)
        if dlg is None:
            from gui.display_mode import DisplayModeDialog
            dlg = DisplayModeDialog(self.app_window)
            self.app_window.display_mode_dialog = dlg
        return dlg

    def _open_display_mode_for_row(self, row: int):
        """Open lightweight class picker for Display Mode — Single view selection.

        ✅ FIX: Each shortcut has INDEPENDENT visibility/weight state.
        The table cell is the sole source of truth for THIS shortcut's preset.
        """
        self._pending_display_mode_row = row

        existing_preset = None
        mod_combo = self.table.cellWidget(row, self.COL_MODIFIER)
        key_combo = self.table.cellWidget(row, self.COL_KEY)

        # 1️⃣  Table cell first — canonical preset for THIS shortcut
        cell = self.table.item(row, self.COL_CLASSES)
        if cell:
            raw_cell_data = cell.data(Qt.UserRole)
            existing_preset = decode_display_preset(raw_cell_data)
            if existing_preset:
                _bt = existing_preset.get("border_type", 0)
                _bp = existing_preset.get("border_percent", 0)
                print(f"   ✅ Loaded preset from table cell — border_percent={_bp}, border_type={_bt} ({['Per-Point','Structured','Hybrid'][_bt] if _bt < 3 else _bt})")

        # 2️⃣  Fall back to app.shortcuts only when the table cell has no data
        if existing_preset is None and mod_combo and key_combo:
            mod = mod_combo.currentText().lower()
            key = key_combo.currentText().upper()
            shortcut_info = getattr(self.app_window, 'shortcuts', {}).get((mod, key))
            if shortcut_info and shortcut_info.get("tool") == "DisplayMode":
                existing_preset = shortcut_info.get("preset")
                if existing_preset:
                    print(f"   ⚠️  Loaded preset from app.shortcuts (table cell empty)")

        # ✅ Always create a FRESH picker (don't reuse)
        if hasattr(self, '_display_picker') and self._display_picker is not None:
            try:
                self._display_picker.close()
                self._display_picker.deleteLater()
            except Exception:
                pass

        self._display_picker = ClassVisibilityPicker(self.app_window, mode="display", parent=self)
        picker = self._display_picker

        if existing_preset:
            views = existing_preset.get("views", {})
            border_percent = existing_preset.get("border_percent", 0)

            if views:
                views = {int(k): v for k, v in views.items()}
                first_view_idx = min(views.keys())
                view_classes = {int(k): dict(v) for k, v in views[first_view_idx].items()}
                border_type = existing_preset.get("border_type", 0)

                picker.border_spin.setValue(border_percent)
                preset_mode = str(existing_preset.get("display_mode", "class") or "class")
                mode_idx = picker.display_mode_selector.findData(preset_mode)
                picker.display_mode_selector.setCurrentIndex(
                    mode_idx if mode_idx >= 0 else 0
                )
                if hasattr(picker, "shading_quality_selector"):
                    preset_quality = str(
                        existing_preset.get("quality_mode", "normal") or "normal"
                    )
                    quality_idx = picker.shading_quality_selector.findData(preset_quality)
                    picker.shading_quality_selector.setCurrentIndex(
                        quality_idx if quality_idx >= 0 else 1
                    )
                picker.set_line_visibility(existing_preset.get("flight_lines", {}))
                picker.set_border_logic_mode(border_type)
                picker.view_selector.blockSignals(True)
                picker.view_selector.setCurrentIndex(first_view_idx)
                picker.view_selector.blockSignals(False)

                if not hasattr(picker, 'view_configs'):
                    picker.view_configs = {}
                picker.view_configs[first_view_idx] = view_classes

                picker._populate_classes()

                for code, checkbox in picker.class_checkboxes.items():
                    checkbox.setChecked(view_classes.get(code, {}).get("show", True))
                if hasattr(picker, 'weight_spinboxes'):
                    for code, spin in picker.weight_spinboxes.items():
                        spin.setValue(view_classes.get(code, {}).get("weight", 1.0))

                visible_count = sum(1 for c in view_classes.values() if c.get('show'))
                print(f"📂 Loaded existing preset for view {first_view_idx}: "
                      f"{visible_count} visible (independent per-shortcut state)")
            else:
                border_type = existing_preset.get("border_type", 0)
                picker.border_spin.setValue(border_percent)
                preset_mode = str(existing_preset.get("display_mode", "class") or "class")
                mode_idx = picker.display_mode_selector.findData(preset_mode)
                picker.display_mode_selector.setCurrentIndex(
                    mode_idx if mode_idx >= 0 else 0
                )
                if hasattr(picker, "shading_quality_selector"):
                    preset_quality = str(
                        existing_preset.get("quality_mode", "normal") or "normal"
                    )
                    quality_idx = picker.shading_quality_selector.findData(preset_quality)
                    picker.shading_quality_selector.setCurrentIndex(
                        quality_idx if quality_idx >= 0 else 1
                    )
                picker.set_line_visibility(existing_preset.get("flight_lines", {}))
                picker.set_border_logic_mode(border_type)
                print(f"📋 Preset has no views configured")
                picker._populate_classes()
        else:
            # ── FRESH PRESET: no saved data yet ──
            print(f"📋 Creating fresh preset - defaulting to Main View")

            picker.view_selector.blockSignals(True)
            picker.view_selector.setCurrentIndex(0)
            picker.view_selector.blockSignals(False)

            # Seed from live Display Mode state (only for brand-new presets)
            fresh_classes, fresh_border, fresh_border_type = self._get_live_display_state_for_view(0)
            if fresh_classes:
                if not hasattr(picker, 'view_configs'):
                    picker.view_configs = {}
                picker.view_configs[0] = fresh_classes
                picker.border_spin.setValue(fresh_border)
                picker.set_border_logic_mode(fresh_border_type)

            picker._populate_classes()

            if fresh_classes:
                for code, checkbox in picker.class_checkboxes.items():
                    checkbox.setChecked(fresh_classes.get(code, {}).get("show", True))
                if hasattr(picker, 'weight_spinboxes'):
                    for code, spin in picker.weight_spinboxes.items():
                        spin.setValue(fresh_classes.get(code, {}).get("weight", 1.0))

            print(f"✅ Fresh preset initialized for Main View")

        # ── Connect OK handler ────────────────────────────────────────
        def on_accepted():
            all_configs = picker.get_all_view_configs()
            border_percent = all_configs.get("border_percent", 0)
            border_type = all_configs.get("border_type", 0)
            view_configs   = all_configs.get("views", {})

            if not view_configs:
                print("⚠️ No view configured")
                return

            view_idx    = list(view_configs.keys())[0]
            view_classes = view_configs[view_idx]

            preset = {
                "display_mode": all_configs.get("display_mode", "class"),
                "quality_mode": all_configs.get("quality_mode", "normal"),
                "flight_lines": all_configs.get("flight_lines", {}),
                "border_percent": border_percent,
                "border_type": border_type,
                "views": {view_idx: view_classes},
                "force_refresh": True
            }

            visible = sum(1 for c in view_classes.values() if c.get("show"))
            view_name = (["Main", "View 1", "View 2", "View 3", "View 4", "Cut"][view_idx]
                         if view_idx < 6 else f"View {view_idx}")

            weights = [c.get("weight", 1.0) for c in view_classes.values()]
            unique_weights = sorted(set(weights))
            weight_info = (f"W={unique_weights[0]:.1f}" if len(unique_weights) == 1
                           else f"W={min(weights):.1f}-{max(weights):.1f}")

            summary = f"{view_name}: {visible}vis, B={border_percent:.1f}%, {weight_info}"

            item = QTableWidgetItem(summary)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_display_preset(preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            # Update app.shortcuts if it exists
            _mod_w = self.table.cellWidget(row, self.COL_MODIFIER)
            _key_w = self.table.cellWidget(row, self.COL_KEY)
            if _mod_w and _key_w:
                _m = _mod_w.currentText().lower()
                _k = _key_w.currentText().upper()
                _sh = getattr(self.app_window, 'shortcuts', {})
                if (_m, _k) in _sh and _sh[(_m, _k)].get('tool') == 'DisplayMode':
                    _sh[(_m, _k)]['preset'] = preset
                    print(f"   🔗 app.shortcuts[{_m}+{_k}] preset updated")

            print(f"✅ Saved: {summary}")

            try:
                self.auto_save_shortcuts()
                print(f"   💾 QSettings updated — preset will survive restart")
            except Exception as _e:
                print(f"   ⚠️ auto_save_shortcuts failed: {_e}")

            picker.hide()
            QTimer.singleShot(200, picker.deleteLater)
            self._display_picker = None

        picker.accepted.connect(on_accepted)

        def on_rejected():
            print("❌ Display mode configuration cancelled")
            picker.hide()
            QTimer.singleShot(200, picker.deleteLater)
            self._display_picker = None

        picker.rejected.connect(on_rejected)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _open_display_visibility_for_row(self, row: int, tool: str):
        """Configure a class visibility preset for a color display mode."""
        cell = self.table.item(row, self.COL_CLASSES)
        existing_preset = (
            decode_display_visibility_preset(cell.data(Qt.UserRole)) if cell else None
        )
        existing_classes = (existing_preset or {}).get("classes", {})

        attr_name = f"_{tool.lower()}_visibility_picker"
        old_picker = getattr(self, attr_name, None)
        if old_picker is not None:
            try:
                old_picker.close()
                old_picker.deleteLater()
            except Exception:
                pass

        picker = ClassVisibilityPicker(
            self.app_window, mode=tool.lower(), parent=self
        )
        setattr(self, attr_name, picker)
        if existing_classes:
            picker.set_selected_classes(existing_classes)
        if tool == "Line" and existing_preset:
            picker.set_line_visibility(
                existing_preset.get("flight_lines", {})
            )

        def cleanup_picker():
            picker.hide()
            QTimer.singleShot(200, picker.deleteLater)
            if getattr(self, attr_name, None) is picker:
                setattr(self, attr_name, None)

        def on_accepted():
            selected_classes = picker.get_selected_classes()
            preset = {
                "mode": tool,
                "target_view": 0,
                "classes": selected_classes,
            }
            if tool == "Line":
                preset["flight_lines"] = picker.get_line_visibility()

            item = QTableWidgetItem(
                summarize_display_visibility_preset(preset, tool)
            )
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_display_visibility_preset(preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            mod_widget = self.table.cellWidget(row, self.COL_MODIFIER)
            key_widget = self.table.cellWidget(row, self.COL_KEY)
            if mod_widget and key_widget:
                combo = (
                    mod_widget.currentText().lower(),
                    key_widget.currentText().upper(),
                )
                shortcuts = getattr(self.app_window, "shortcuts", {})
                if combo in shortcuts and shortcuts[combo].get("tool") == tool:
                    shortcuts[combo]["preset"] = preset

            self.auto_save_shortcuts()
            cleanup_picker()

        picker.accepted.connect(on_accepted)
        picker.rejected.connect(cleanup_picker)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _open_surface_for_row(self, row: int):
        """Open a shading-style preset editor for Surface shortcuts."""
        self._pending_surface_mode_row = row

        cell = self.table.item(row, self.COL_CLASSES)
        existing_preset = decode_surface_preset(cell.data(Qt.UserRole)) if cell else None
        existing_classes = existing_preset.get("classes", {}) if existing_preset else {}

        if not hasattr(self, '_surface_picker') or self._surface_picker is None:
            self._surface_picker = ClassVisibilityPicker(self.app_window, mode="surface", parent=self)

        picker = self._surface_picker
        picker._populate_classes()

        if existing_classes:
            picker.set_selected_classes(existing_classes)
        if existing_preset:
            picker.set_shading_parameters(existing_preset)
        else:
            picker.set_shading_parameters({
                "azimuth": float(getattr(self.app_window, "last_shade_azimuth", 45.0)),
                "angle": float(getattr(self.app_window, "last_shade_angle", 45.0)),
                "ambient": float(getattr(self.app_window, "shade_ambient", 0.1)),
                "quality": 100.0,
                "speed": 1,
                "max_edge": float(getattr(self.app_window, "surface_max_edge", 0.0) or 0.0),
            })

        try:
            picker.accepted.disconnect()
        except Exception:
            pass

        def on_accepted():
            selected_classes = picker.get_selected_classes()
            surface_params = picker.get_shading_parameters()
            preset = {
                "azimuth": surface_params.get("azimuth", 45.0),
                "angle": surface_params.get("angle", 45.0),
                "ambient": surface_params.get("ambient", 0.1),
                "quality": surface_params.get("quality", 100.0),
                "speed": surface_params.get("speed", 1),
                "max_edge": surface_params.get("max_edge", 0.0),
                "classes": selected_classes,
            }

            item = QTableWidgetItem(summarize_surface_preset(preset))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_surface_preset(preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            mod_widget = self.table.cellWidget(row, self.COL_MODIFIER)
            key_widget = self.table.cellWidget(row, self.COL_KEY)
            if mod_widget and key_widget:
                mod = mod_widget.currentText().lower()
                key = key_widget.currentText().upper()
                shortcuts = getattr(self.app_window, 'shortcuts', {})
                if (mod, key) in shortcuts and shortcuts[(mod, key)].get("tool") == "Surface":
                    shortcuts[(mod, key)]["preset"] = preset

            try:
                self.auto_save_shortcuts()
            except Exception as save_err:
                print(f"auto_save_shortcuts failed for Surface preset: {save_err}")

            picker.hide()

        picker.accepted.connect(on_accepted)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _save_display_like_preset_to_row(self, row: int, preset: dict, tool_name: str):
        """Persist a DisplayMode-style preset summary into a shortcut row."""
        prefix = "Surface" if tool_name == "Surface" else "Display"
        summary = summarize_display_like_preset(preset, prefix=prefix)
        item = QTableWidgetItem(summary)
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if tool_name == "Surface":
            item.setData(Qt.UserRole, encode_surface_preset(preset))
        else:
            item.setData(Qt.UserRole, encode_display_preset(preset))
        self.table.setItem(row, self.COL_CLASSES, item)

    def _open_surface_for_row_display_legacy(self, row: int):
        """
        Surface shortcuts reuse the DisplayMode preset editor and storage shape.
        Only the execution path differs: Surface applies the preset, then renders
        the terrain mesh.
        """
        self._pending_display_mode_row = row

        cell = self.table.item(row, self.COL_CLASSES)
        existing_preset = decode_surface_preset(cell.data(Qt.UserRole)) if cell else None

        if hasattr(self, '_display_picker') and self._display_picker is not None:
            try:
                self._display_picker.close()
                self._display_picker.deleteLater()
            except Exception:
                pass

        self._display_picker = ClassVisibilityPicker(self.app_window, mode="display", parent=self)
        picker = self._display_picker

        if existing_preset:
            views = {int(k): v for k, v in (existing_preset.get("views", {}) or {}).items()}
            border_percent = float(existing_preset.get("border_percent", 0))
            border_type = int(existing_preset.get("border_type", 0))
            if views:
                first_view_idx = min(views.keys())
                view_classes = {int(k): dict(v) for k, v in views[first_view_idx].items()}
                picker.border_spin.setValue(border_percent)
                picker.set_border_logic_mode(border_type)
                picker.view_selector.blockSignals(True)
                picker.view_selector.setCurrentIndex(first_view_idx)
                picker.view_selector.blockSignals(False)
                if not hasattr(picker, 'view_configs'):
                    picker.view_configs = {}
                picker.view_configs[first_view_idx] = view_classes
                picker._populate_classes()
                for code, checkbox in picker.class_checkboxes.items():
                    checkbox.setChecked(view_classes.get(code, {}).get("show", True))
                if hasattr(picker, 'weight_spinboxes'):
                    for code, spin in picker.weight_spinboxes.items():
                        spin.setValue(view_classes.get(code, {}).get("weight", 1.0))
            else:
                picker.border_spin.setValue(border_percent)
                picker.set_border_logic_mode(border_type)
                picker._populate_classes()
        else:
            picker.view_selector.blockSignals(True)
            picker.view_selector.setCurrentIndex(0)
            picker.view_selector.blockSignals(False)
            fresh_classes, fresh_border, fresh_border_type = self._get_live_display_state_for_view(0)
            if fresh_classes:
                if not hasattr(picker, 'view_configs'):
                    picker.view_configs = {}
                picker.view_configs[0] = fresh_classes
                picker.border_spin.setValue(fresh_border)
                picker.set_border_logic_mode(fresh_border_type)
            picker._populate_classes()
            if fresh_classes:
                for code, checkbox in picker.class_checkboxes.items():
                    checkbox.setChecked(fresh_classes.get(code, {}).get("show", True))
                if hasattr(picker, 'weight_spinboxes'):
                    for code, spin in picker.weight_spinboxes.items():
                        spin.setValue(fresh_classes.get(code, {}).get("weight", 1.0))

        def on_accepted():
            all_configs = picker.get_all_view_configs()
            border_percent = all_configs.get("border_percent", 0)
            border_type = all_configs.get("border_type", 0)
            view_configs = all_configs.get("views", {})
            if not view_configs:
                return
            view_idx = list(view_configs.keys())[0]
            preset = {
                "display_mode": all_configs.get("display_mode", "class"),
                "border_percent": border_percent,
                "border_type": border_type,
                "views": {view_idx: view_configs[view_idx]},
                "force_refresh": True,
            }
            self._save_display_like_preset_to_row(row, preset, "Surface")

            mod_widget = self.table.cellWidget(row, self.COL_MODIFIER)
            key_widget = self.table.cellWidget(row, self.COL_KEY)
            if mod_widget and key_widget:
                mod = mod_widget.currentText().lower()
                key = key_widget.currentText().upper()
                shortcuts = getattr(self.app_window, 'shortcuts', {})
                if (mod, key) in shortcuts and shortcuts[(mod, key)].get("tool") == "Surface":
                    shortcuts[(mod, key)]["preset"] = preset
            try:
                self.auto_save_shortcuts()
            except Exception as save_err:
                print(f"   âš ï¸ auto_save_shortcuts failed for Surface preset: {save_err}")
            picker.hide()
            QTimer.singleShot(200, picker.deleteLater)
            self._display_picker = None

        def on_rejected():
            picker.hide()
            QTimer.singleShot(200, picker.deleteLater)
            self._display_picker = None

        picker.accepted.connect(on_accepted)
        picker.rejected.connect(on_rejected)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _open_line_mode_for_row(self, row):
        cell = self.table.item(row, self.COL_CLASSES)
        preset = (
            decode_line_mode_preset(cell.data(Qt.UserRole))
            if cell is not None else None
        )
        picker = ClassVisibilityPicker(
            self.app_window, mode="line", parent=self
        )
        if preset and preset.get("classes"):
            picker.set_selected_classes(preset["classes"])
        if preset:
            picker.set_line_visibility(preset.get("lines", {}))
        self._line_mode_picker = picker

        def on_accepted():
            line_preset = {
                "target_view": 0,
                "classes": picker.get_selected_classes(),
                "lines": picker.get_line_visibility(),
            }
            item = QTableWidgetItem(summarize_line_mode_preset(line_preset))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_line_mode_preset(line_preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            mod_widget = self.table.cellWidget(row, self.COL_MODIFIER)
            key_widget = self.table.cellWidget(row, self.COL_KEY)
            if mod_widget is not None and key_widget is not None:
                combo = (
                    mod_widget.currentText().lower(),
                    key_widget.currentText().upper(),
                )
                shortcuts = getattr(self.app_window, "shortcuts", {})
                if combo in shortcuts:
                    shortcuts[combo] = {
                        "tool": "Line",
                        "preset": line_mode_to_display_visibility_preset(
                            line_preset
                        ),
                    }
            try:
                self.auto_save_shortcuts()
            except Exception as exc:
                print(f"LineMode shortcut auto-save failed: {exc}")

        picker.accepted.connect(on_accepted)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def on_class_edit(self, row, col):
        """Only opens on DOUBLE-CLICK."""
        if col != self.COL_CLASSES:
            return
        tool_combo = self.table.cellWidget(row, self.COL_TOOL)
        tool = tool_combo.currentText() if tool_combo else ""

        if (tool in SIMPLE_SHORTCUT_TOOLS
                and tool != "Surface"
                and tool not in DISPLAY_VISIBILITY_TOOLS):
            print(f"{tool} has no class configuration - N/A")
            return
        if tool == "DisplayMode":
            self._open_display_mode_for_row(row)
            return
        if tool == "LineMode":
            self._open_line_mode_for_row(row)
            return
        if tool == "Surface":
            self._open_surface_for_row(row)
            return
        if tool in DISPLAY_VISIBILITY_TOOLS:
            self._open_display_visibility_for_row(row, tool)
            return
        if tool == "ShadingMode":
            self._open_shading_mode_for_row(row)
            return
        if tool == "DrawSettings":
            self._open_draw_settings_for_row(row)
            return
        if tool == "SyncViews":
            self._open_sync_views_for_row(row)
            return

        self._active_row = row
        self.is_editing_shortcuts = True
        picker = self._ensure_class_picker()
        try:
            self._safe_disconnect(
                picker.from_list.itemSelectionChanged,
                self.update_classes_from_picker,
            )
            self._safe_disconnect(
                picker.to_combo.currentIndexChanged,
                self.update_classes_from_picker,
            )
            picker.from_list.itemSelectionChanged.connect(self.update_classes_from_picker)
            picker.to_combo.currentIndexChanged.connect(self.update_classes_from_picker)
            picker.show()
            picker.raise_()
            picker.activateWindow()

            cell = self.table.item(row, self.COL_CLASSES)
            if cell and cell.data(Qt.UserRole):
                from_cls, to_cls = decode_classes(cell.data(Qt.UserRole))
                if from_cls is not None:
                    picker.from_list.clearSelection()
                    from_list = from_cls if isinstance(from_cls, list) else [from_cls]
                    for i in range(picker.from_list.count()):
                        item = picker.from_list.item(i)
                        if item.data(Qt.UserRole) in from_list:
                            item.setSelected(True)
                else:
                    for i in range(picker.from_list.count()):
                        item = picker.from_list.item(i)
                        if item.data(Qt.UserRole) is None:
                            item.setSelected(True)
                            break
                if to_cls is not None:
                    idx = picker.to_combo.findData(to_cls)
                    if idx >= 0:
                        picker.to_combo.setCurrentIndex(idx)
                else:
                    idx = picker.to_combo.findData(None)
                    if idx >= 0:
                        picker.to_combo.setCurrentIndex(idx)
        except RuntimeError as exc:
            print(f"Class picker became invalid during edit: {exc}")
            try:
                self.app_window.class_picker = None
            except Exception:
                pass
            try:
                self.is_editing_shortcuts = False
            except Exception:
                pass

    def _get_live_display_state_for_view(self, view_idx):
        """
        Get fresh class config from live Display Mode dialog for a specific view.
        Used only when creating a FRESH preset (no table cell data exists).
        Returns: (classes_dict, border_value, border_type) or (None, 0, 0)
        """
        try:
            dlg = getattr(self.app_window, 'display_mode_dialog', None)

            if dlg is not None and hasattr(dlg, 'view_palettes') and view_idx in dlg.view_palettes:
                live_palette = dlg.view_palettes[view_idx]
                if live_palette:
                    classes = {}
                    for code, info in live_palette.items():
                        code_int = int(code)
                        classes[code_int] = {
                            'show': info.get('show', True),
                            'weight': float(info.get('weight', 1.0)),
                            'description': info.get('description', ''),
                            'color': tuple(info.get('color', (128, 128, 128))),
                            'draw': info.get('draw', ''),
                            'lvl': info.get('lvl', ''),
                        }
                    border = 0
                    border_type = 0
                    if hasattr(dlg, 'view_borders') and view_idx in dlg.view_borders:
                        border = float(dlg.view_borders[view_idx])
                    if hasattr(dlg, 'border_logic_hybrid') and dlg.border_logic_hybrid.isChecked():
                        border_type = 2
                    elif hasattr(dlg, 'border_logic_object') and dlg.border_logic_object.isChecked():
                        border_type = 1
                    print(f"   📋 Seeded from Display Mode dialog view {view_idx}: "
                          f"{len(classes)} classes, border={border}%, type={border_type}")
                    return classes, border, border_type

            app_palettes = getattr(self.app_window, 'view_palettes', {})
            if view_idx in app_palettes and app_palettes[view_idx]:
                classes = {}
                for code, info in app_palettes[view_idx].items():
                    code_int = int(code)
                    classes[code_int] = {
                        'show': info.get('show', True),
                        'weight': float(info.get('weight', 1.0)),
                        'description': info.get('description', ''),
                        'color': tuple(info.get('color', (128, 128, 128))),
                        'draw': info.get('draw', ''),
                        'lvl': info.get('lvl', ''),
                    }
                print(f"   📋 Seeded from app.view_palettes[{view_idx}]: {len(classes)} classes")
                return classes, 0, 0

            if view_idx == 0 and hasattr(self.app_window, 'class_palette'):
                classes = {}
                for code, info in self.app_window.class_palette.items():
                    classes[int(code)] = {
                        'show': info.get('show', True),
                        'weight': float(info.get('weight', 1.0)),
                        'description': info.get('description', ''),
                        'color': tuple(info.get('color', (128, 128, 128))),
                        'draw': info.get('draw', ''),
                        'lvl': info.get('lvl', ''),
                    }
                print(f"   📋 Seeded from app.class_palette: {len(classes)} classes")
                return classes, 0, 0

            return None, 0, 0

        except Exception as e:
            print(f"   ⚠️ _get_live_display_state_for_view failed: {e}")
            return None, 0, 0

    def _open_shading_mode_for_row(self, row: int):
        """Open lightweight class picker for shading mode (non-modal)"""
        self._pending_shading_mode_row = row

        cell = self.table.item(row, self.COL_CLASSES)
        existing_preset = decode_shading_preset(cell.data(Qt.UserRole)) if cell else None
        existing_classes = existing_preset.get("classes", {}) if existing_preset else {}

        # ✅ Create or reuse picker
        if not hasattr(self, '_shading_picker') or self._shading_picker is None:
            self._shading_picker = ClassVisibilityPicker(self.app_window, mode="shading", parent=self)

        picker = self._shading_picker
        picker._populate_classes()

        if existing_classes:
            picker.set_selected_classes(existing_classes)
        if existing_preset:
            picker.set_shading_parameters(existing_preset)

        try:
            picker.accepted.disconnect()
        except Exception:
            pass

        def on_accepted():
            selected_classes = picker.get_selected_classes()
            shading_params   = picker.get_shading_parameters()

            preset = {
                "azimuth": shading_params.get("azimuth", 45.0),
                "angle":   shading_params.get("angle",   45.0),
                "ambient": shading_params.get("ambient", 0.1),
                "quality": shading_params.get("quality", 100.0),
                "quality_mode": shading_params.get("quality_mode", "normal"),
                "classes": selected_classes
            }

            visible_count = sum(1 for c in selected_classes.values() if c.get("show"))
            quality_label = shading_quality_label(preset["quality_mode"])
            summary = (f"Shading: {preset['azimuth']}°/{preset['angle']}°, "
                       f"{visible_count} visible, Speed={quality_label}")

            item = QTableWidgetItem(summary)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_shading_preset(preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            print(f"✅ Saved ShadingMode preset: {visible_count} classes visible")
            picker.hide()

        picker.accepted.connect(on_accepted)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _open_draw_settings_for_row(self, row: int):
        """Open draw settings picker for a shortcut row."""
        cell = self.table.item(row, self.COL_CLASSES)
        existing_preset = decode_draw_preset(cell.data(Qt.UserRole)) if cell else None

        if hasattr(self, '_draw_picker') and self._draw_picker is not None:
            try:
                self._draw_picker.close()
                self._draw_picker.deleteLater()
            except Exception:
                pass

        self._draw_picker = DrawSettingsPicker(self.app_window, parent=self)
        picker = self._draw_picker

        if existing_preset:
            picker.set_preset(existing_preset)

        def on_accepted():
            preset      = picker.get_preset()
            active_tool = preset.get("active_tool", "smartline")
            tools       = preset.get("tools", {})

            from gui.draw_settings_dialog import vtk_color_to_qcolor
            parts = []
            if active_tool in tools:
                st = tools[active_tool]
                qc = vtk_color_to_qcolor(st.get("color", (1, 0, 0)))
                parts.append(f"[{active_tool.upper()}] {qc.name()}, {st.get('width', 2)}px")
            for tk, st in tools.items():
                if tk == active_tool:
                    continue
                qc = vtk_color_to_qcolor(st.get("color", (1, 0, 0)))
                parts.append(f"{tk}: {qc.name()}")
            summary = "; ".join(parts) if parts else "Draw preset"
            if len(summary) > 80:
                summary = summary[:77] + "..."

            item = QTableWidgetItem(summary)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_draw_preset(preset))
            self.table.setItem(row, self.COL_CLASSES, item)

            print(f"✅ Saved DrawSettings preset into row {row + 1}: {summary}")
            picker.close()
            picker.deleteLater()
            self._draw_picker = None

        def on_rejected():
            print("❌ Draw settings configuration cancelled")
            picker.close()
            picker.deleteLater()
            self._draw_picker = None

        picker.accepted.connect(on_accepted)
        picker.rejected.connect(on_rejected)
        picker.show()
        picker.raise_()
        picker.activateWindow()

    def _open_sync_views_for_row(self, row: int):
        """Open SyncViewsPicker for a shortcut row."""
        cell = self.table.item(row, self.COL_CLASSES)
        existing = decode_sync_preset(cell.data(Qt.UserRole)) if cell else None

        picker = SyncViewsPicker(parent=self)
        picker.setStyleSheet(get_dialog_stylesheet())
        if existing and existing.get("rows"):
            picker.set_preset(existing)

        if picker.exec() == QDialog.Accepted:
            preset  = picker.get_preset()
            summary = picker.get_summary()
            item = self.table.item(row, self.COL_CLASSES)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(row, self.COL_CLASSES, item)
            item.setText(summary)
            item.setData(Qt.UserRole, encode_sync_preset(preset))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            print(f"✅ Saved SyncViews preset into row {row + 1}: {summary}")

    def _capture_display_mode_preset(self, payload: dict):
        row = self._pending_display_mode_row
        if row is None:
            return

        try:
            classes = payload.get("classes", {}) or {}
            visible = [c for c, info in classes.items() if info.get("show")]
            slot = payload.get("target_view", payload.get("slot", 0))
            view_name = ("Main View" if slot == 0 else f"View {slot}")
            summary = f"Preset: {len(visible)} visible ({view_name})"

            item = QTableWidgetItem(summary)
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            item.setData(Qt.UserRole, encode_display_preset(payload))
            self.table.setItem(row, self.COL_CLASSES, item)

            print(f"✅ Saved DisplayMode preset into row {row + 1}: {summary}")

        finally:
            self._pending_display_mode_row = None
            dlg = getattr(self.app_window, "display_mode_dialog", None)
            if dlg is not None:
                try:
                    dlg.applied.disconnect(self._capture_display_mode_preset)
                except Exception:
                    pass

    def update_classes_from_picker(self):
        """
        Update shortcut table from ClassPicker.
        ✅ FIXED: More robust with better logging.
        ✅ FIXED: Validity-guarded against deleted-Qt-object crash. If the
            picker (or its child widgets) was destroyed between the signal
            emission and this slot running, we drop the stale reference and
            return cleanly instead of crashing with
            "Signal source has been deleted".
        """
        if not self.is_editing_shortcuts:
            print("⏭️ update_classes_from_picker: Not in editing mode")
            return

        if self._active_row is None:
            print("⏭️ update_classes_from_picker: No active row")
            return

        picker = getattr(self.app_window, 'class_picker', None)
        if not (
            self._qobject_alive(picker)
            and self._qobject_alive(getattr(picker, "from_list", None))
            and self._qobject_alive(getattr(picker, "to_combo", None))
        ):
            print("❌ update_classes_from_picker: ClassPicker is gone or stale!")
            try:
                self.app_window.class_picker = None
            except Exception:
                pass
            return

        try:
            selected_items = picker.from_list.selectedItems()

            print(f"\n{'='*60}")
            print(f"📝 UPDATE_CLASSES_FROM_PICKER (Row {self._active_row})")
            print(f"{'='*60}")
            print(f"   Selected items count: {len(selected_items)}")

            # ✅ Build FROM classes
            if not selected_items:
                from_cls = None
                from_txt = "Any"
                print(f"   FROM: None (Any)")
            else:
                any_selected = any(item.data(Qt.UserRole) is None for item in selected_items)
                if any_selected:
                    from_cls = None
                    from_txt = "Any"
                    print(f"   FROM: 'Any' option selected")
                else:
                    from_cls = [item.data(Qt.UserRole) for item in selected_items]
                    from_txt = ", ".join(str(c) for c in from_cls)
                    print(f"   FROM: {from_cls}")

            # ✅ Build TO class
            to_cls = picker.to_combo.currentData()
            to_txt = picker.to_combo.currentText().split(" - ")[0] if to_cls is not None else "Any"
            print(f"   TO: {to_cls} ({to_txt})")

            display_text = f"{from_txt} → {to_txt}"
            item = QTableWidgetItem(display_text)
            item.setData(Qt.UserRole, encode_classes(from_cls, to_cls))
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(self._active_row, self.COL_CLASSES, item)

            print(f"   ✅ Updated cell: '{display_text}'")
            print(f"{'='*60}\n")
        except RuntimeError as exc:
            # Slot fired AFTER picker was destroyed (or its widgets swept).
            # Drop the dead wrapper and bail — no user-visible damage.
            print(f"⚠️ update_classes_from_picker: picker destroyed mid-call: {exc}")
            try:
                self.app_window.class_picker = None
            except Exception:
                pass

    def _write_mnu(self, path):
        """Write the current shortcut table using the existing .mnu format."""
        simple_tools = SIMPLE_SHORTCUT_TOOLS

        with open(path, "w", encoding="utf-8", newline="") as f:
            for row in range(self.table.rowCount()):
                mod_combo  = self.table.cellWidget(row, self.COL_MODIFIER)
                key_combo  = self.table.cellWidget(row, self.COL_KEY)
                tool_combo = self.table.cellWidget(row, self.COL_TOOL)
                mod  = mod_combo.currentText().lower() if mod_combo else "alt"
                key  = key_combo.currentText().upper() if key_combo else "F1"
                tool = tool_combo.currentText() if tool_combo else "AboveLine"
                cell = self.table.item(row, self.COL_CLASSES)

                if tool == "DisplayMode":
                    preset_json  = cell.data(Qt.UserRole) if cell else ""
                    display_text = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{display_text}\n")
                    continue

                if tool == "LineMode":
                    preset_json = cell.data(Qt.UserRole) if cell else ""
                    display_text = cell.text() if cell else ""
                    f.write(
                        f"{mod}\t{key}\t{tool}\t"
                        f"{preset_json}\t{display_text}\n"
                    )
                    continue

                if tool == "Surface":
                    preset_json  = cell.data(Qt.UserRole) if cell else ""
                    surface_text = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{surface_text}\n")
                    continue

                if tool == "ShadingMode":
                    preset_json  = cell.data(Qt.UserRole) if cell else ""
                    shading_text = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{shading_text}\n")
                    continue

                if tool == "DrawSettings":
                    preset_json = cell.data(Qt.UserRole) if cell else ""
                    draw_text   = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{draw_text}\n")
                    continue

                if tool == "SyncViews":
                    preset_json = cell.data(Qt.UserRole) if cell else ""
                    sync_text   = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{sync_text}\n")
                    continue

                if tool in DISPLAY_VISIBILITY_TOOLS:
                    preset_json = cell.data(Qt.UserRole) if cell else ""
                    summary = cell.text() if cell else ""
                    f.write(f"{mod}\t{key}\t{tool}\t{preset_json}\t{summary}\n")
                    continue

                if tool in simple_tools:
                    f.write(f"{mod}\t{key}\t{tool}\t\t\n")
                else:
                    from_cls, to_cls = decode_classes(cell.data(Qt.UserRole)) if cell else (None, None)
                    if isinstance(from_cls, list):
                        from_str = ",".join(str(c) for c in from_cls)
                    elif from_cls is None:
                        from_str = ""
                    else:
                        from_str = str(from_cls)
                    to_str = "" if to_cls is None else str(to_cls)
                    f.write(f"{mod}\t{key}\t{tool}\t{from_str}\t{to_str}\n")

        self.current_mnu_path = path
        self.settings.setValue("shortcut_mnu_path", path)
        self.auto_save_shortcuts()
        self._update_save_button_tooltip()
        print(f"Shortcuts saved to: {path}")

    def _save_mnu_to_path(self, path):
        try:
            self._write_mnu(path)
            return True
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could Not Save Shortcuts",
                f"The shortcut file could not be saved:\n{path}\n\n{exc}",
            )
            return False

    def _update_save_button_tooltip(self):
        if self.current_mnu_path:
            self.save_btn.setToolTip(
                f"Overwrite shortcut file: {self.current_mnu_path}"
            )
        else:
            self.save_btn.setToolTip(
                "Load a shortcut file before using Save, or use Save As..."
            )

    def save_mnu(self):
        """Overwrite only the loaded .mnu file; never open Save As."""
        if not self.current_mnu_path:
            QMessageBox.warning(
                self,
                "No Shortcut File Loaded",
                "Save can only overwrite a loaded shortcut file.\n\n"
                "Use Load to open an existing .mnu file, or use Save As... "
                "to create a new one.",
            )
            return
        self._save_mnu_to_path(self.current_mnu_path)

    def save_mnu_as(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Shortcut File",
            self.current_mnu_path or "",
            "Menu Files (*.mnu)",
        )
        if not path:
            return
        if not path.lower().endswith(".mnu"):
            path += ".mnu"
        self._save_mnu_to_path(path)

    def load_mnu(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Shortcut File", "", "Menu Files (*.mnu)")
        if not path:
            return

        self._is_loading_shortcuts = True
        self._selected_rows.clear()

        try:
            self.table.setRowCount(0)

            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                for line in f:
                    parts = line.strip().split("\t")
                    if len(parts) < 3:
                        continue

                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self._install_row_widgets(
                        row,
                        modifier=parts[0],
                        key=parts[1],
                        tool="Line" if parts[2] == "LineMode" else parts[2]
                    )

                    if parts[2] == "DisplayMode":
                        if len(parts) > 3 and parts[3].strip():
                            preset_payload = decode_display_preset(parts[3])
                            saved_text = parts[4] if len(parts) > 4 else ""
                            if saved_text:
                                item = QTableWidgetItem(saved_text)
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload and preset_payload.get("views"):
                                views      = preset_payload.get("views", {})
                                border_pct = preset_payload.get("border_percent", 0)
                                view_summaries = []
                                for view_idx in sorted(views.keys()):
                                    visible = sum(1 for c in views[view_idx].values() if c.get("show"))
                                    view_name = (["Main","V1","V2","V3","V4","Cut"][view_idx]
                                                 if view_idx < 6 else f"V{view_idx}")
                                    weights = [c.get("weight", 1.0) for c in views[view_idx].values()]
                                    uw = sorted(set(weights))
                                    weight_info = (f"W={uw[0]:.1f}" if len(uw) == 1
                                                   else f"W={min(weights):.1f}-{max(weights):.1f}")
                                    view_summaries.append(
                                        f"{view_name}: {visible}vis, B={border_pct:.1f}%, {weight_info}"
                                    )
                                item = QTableWidgetItem("; ".join(view_summaries))
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload and preset_payload.get("classes"):
                                # Legacy single-view format
                                visible = [c for c, info in preset_payload["classes"].items()
                                           if info.get("show")]
                                slot = preset_payload.get("target_view", preset_payload.get("slot", 0))
                                view_name = ("Main View" if slot == 0 else f"View {slot}")
                                item = QTableWidgetItem(
                                    f"Preset: {len(visible)} visible ({view_name})")
                                item.setData(Qt.UserRole, parts[3])
                            else:
                                item = QTableWidgetItem("Click/Double-click to configure display preset")
                                item.setData(Qt.UserRole, encode_display_preset({
                                    "classes": {}, "slot": 0, "target_view": 0,
                                    "color_mode": 0, "border_percent": 0, "force_refresh": True
                                }))
                        else:
                            item = QTableWidgetItem("Click/Double-click to configure display preset")
                            item.setData(Qt.UserRole, encode_display_preset({
                                "classes": {}, "slot": 0, "target_view": 0,
                                "color_mode": 0, "border_percent": 0, "force_refresh": True
                            }))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] in ("Line", "LineMode"):
                        preset_payload = (
                            decode_line_mode_preset(parts[3])
                            if len(parts) > 3 and parts[3].strip()
                            else {"target_view": 0, "lines": {}}
                        )
                        saved_text = parts[4] if len(parts) > 4 else ""
                        item = QTableWidgetItem(
                            saved_text or summarize_line_mode_preset(
                                preset_payload or {"lines": {}}
                            )
                        )
                        item.setData(
                            Qt.UserRole,
                            encode_line_mode_preset(
                                preset_payload or {"lines": {}}
                            ),
                        )
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] == "Surface":
                        if len(parts) > 3 and parts[3].strip():
                            preset_payload = decode_surface_preset(parts[3])
                            saved_text = parts[4] if len(parts) > 4 else ""
                            if saved_text:
                                item = QTableWidgetItem(saved_text)
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload:
                                item = QTableWidgetItem(summarize_surface_preset(preset_payload))
                                item.setData(Qt.UserRole, encode_surface_preset(preset_payload))
                            else:
                                item = QTableWidgetItem("Click/Double-click to configure surface preset")
                                item.setData(Qt.UserRole, encode_surface_preset({
                                    "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                                    "quality": 100.0, "speed": 1, "max_edge": 0.0, "classes": {}
                                }))
                        else:
                            item = QTableWidgetItem("Click/Double-click to configure surface preset")
                            item.setData(Qt.UserRole, encode_surface_preset({
                                "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                                "quality": 100.0, "speed": 1, "max_edge": 0.0, "classes": {}
                            }))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] == "DrawSettings":
                        if len(parts) > 3 and parts[3].strip():
                            preset_payload = decode_draw_preset(parts[3])
                            saved_text = parts[4] if len(parts) > 4 else ""
                            if saved_text:
                                item = QTableWidgetItem(saved_text)
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload:
                                item = QTableWidgetItem("Draw preset")
                                item.setData(Qt.UserRole, parts[3])
                            else:
                                item = QTableWidgetItem("Click/Double-click to configure draw preset")
                                item.setData(Qt.UserRole, encode_draw_preset({"tools": {}}))
                        else:
                            item = QTableWidgetItem("Click/Double-click to configure draw preset")
                            item.setData(Qt.UserRole, encode_draw_preset({"tools": {}}))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] == "ShadingMode":
                        if len(parts) > 3 and parts[3].strip():
                            preset_payload = decode_shading_preset(parts[3])
                            saved_text = parts[4] if len(parts) > 4 else ""
                            if saved_text:
                                item = QTableWidgetItem(saved_text)
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload and preset_payload.get("classes"):
                                visible = [c for c, info in preset_payload["classes"].items()
                                           if info.get("show")]
                                az    = preset_payload.get("azimuth", 45.0)
                                ang   = preset_payload.get("angle",   45.0)
                                quality_label = shading_quality_label(
                                    preset_payload.get("quality_mode", "normal")
                                )
                                item = QTableWidgetItem(
                                    f"Shading: {az}°/{ang}°, {len(visible)} visible, Speed={quality_label}")
                                item.setData(Qt.UserRole, parts[3])
                            else:
                                item = QTableWidgetItem("Preset: Not configured yet")
                                item.setData(Qt.UserRole, encode_shading_preset({
                                    "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                                    "quality": 100.0, "quality_mode": "normal", "classes": {}
                                }))
                        else:
                            item = QTableWidgetItem("Preset: Not configured yet")
                            item.setData(Qt.UserRole, encode_shading_preset({
                                "azimuth": 45.0, "angle": 45.0, "ambient": 0.1,
                                "quality": 100.0, "quality_mode": "normal", "classes": {}
                            }))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] == "SyncViews":
                        if len(parts) > 3 and parts[3].strip():
                            preset_payload = decode_sync_preset(parts[3])
                            saved_text = parts[4] if len(parts) > 4 else ""
                            if saved_text:
                                item = QTableWidgetItem(saved_text)
                                item.setData(Qt.UserRole, parts[3])
                            elif preset_payload:
                                item = QTableWidgetItem(_sync_preset_summary(preset_payload))
                                item.setData(Qt.UserRole, parts[3])
                            else:
                                item = QTableWidgetItem("Double-click to configure sync preset")
                                item.setData(Qt.UserRole, encode_sync_preset({"rows": []}))
                        else:
                            item = QTableWidgetItem("Double-click to configure sync preset")
                            item.setData(Qt.UserRole, encode_sync_preset({"rows": []}))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] in DISPLAY_VISIBILITY_TOOLS:
                        preset_payload = (
                            decode_display_visibility_preset(parts[3])
                            if len(parts) > 3 and parts[3].strip()
                            else None
                        )
                        if preset_payload:
                            summary = (
                                parts[4] if len(parts) > 4 and parts[4]
                                else summarize_display_visibility_preset(
                                    preset_payload, parts[2]
                                )
                            )
                            item = QTableWidgetItem(summary)
                            item.setData(
                                Qt.UserRole,
                                encode_display_visibility_preset(preset_payload),
                            )
                        else:
                            item = QTableWidgetItem(
                                f"Click/Double-click to configure {parts[2]} class visibility"
                            )
                            item.setData(Qt.UserRole, encode_display_visibility_preset({
                                "mode": parts[2], "classes": {}
                            }))
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    if parts[2] in SIMPLE_SHORTCUT_TOOLS:
                        item = QTableWidgetItem("N/A")
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_classes(None, None))
                        self.table.setItem(row, self.COL_CLASSES, item)
                        continue

                    from_cls = None
                    if len(parts) > 3 and parts[3].strip():
                        s = parts[3].strip()
                        if "," in s:
                            from_cls = [int(x) for x in s.split(",")]
                        else:
                            from_cls = [int(s)]

                    to_cls = None
                    if len(parts) > 4 and parts[4].strip():
                        to_cls = int(parts[4].strip())

                    from_txt = ", ".join(str(c) for c in from_cls) if from_cls else "Any"
                    to_txt   = str(to_cls) if to_cls is not None else "Any"

                    item = QTableWidgetItem(f"{from_txt} → {to_txt}")
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    item.setData(Qt.UserRole, encode_classes(from_cls, to_cls))
                    self.table.setItem(row, self.COL_CLASSES, item)

            self.current_mnu_path = path
            self.settings.setValue("shortcut_mnu_path", path)
            self._update_save_button_tooltip()
            if self.table.rowCount() > 0:
                self._set_current_row(0)
            print(f"✅ Shortcuts loaded from: {path}")

        finally:
            self._is_loading_shortcuts = False

    def on_apply(self):
        if getattr(self, "_applying_shortcuts", False):
            print("⏭️ on_apply skipped: apply already in progress")
            return

        # ✅ Set flag to preserve user-edited weights during shortcut sync
        self._applying_shortcuts = True

        try:
            # ✅ Final duplicate check before applying
            seen_keys = {}
            for row in range(self.table.rowCount()):
                mod_combo = self.table.cellWidget(row, self.COL_MODIFIER)
                key_combo = self.table.cellWidget(row, self.COL_KEY)
                if mod_combo and key_combo:
                    mod   = mod_combo.currentText().lower()
                    key   = key_combo.currentText().upper()
                    combo = (mod, key)
                    if combo in seen_keys:
                        QMessageBox.critical(
                            self, "Cannot Apply",
                            f"❌ Duplicate shortcut detected!\n\n"
                            f"{mod}+{key} is used in both:\n"
                            f"  • Row {seen_keys[combo] + 1}\n"
                            f"  • Row {row + 1}\n\n"
                            f"Please fix duplicates before applying."
                        )
                        return
                    seen_keys[combo] = row

            # Persist shortcuts in canonical table order so reopening the
            # manager always shows the same alphabetical layout.
            self._sort_table_by_modifier()

            shortcuts    = {}
            simple_tools = SIMPLE_SHORTCUT_TOOLS

            for row in range(self.table.rowCount()):
                mod_combo  = self.table.cellWidget(row, self.COL_MODIFIER)
                key_combo  = self.table.cellWidget(row, self.COL_KEY)
                tool_combo = self.table.cellWidget(row, self.COL_TOOL)
                mod  = mod_combo.currentText().lower() if mod_combo else "alt"
                key  = key_combo.currentText().upper() if key_combo else "F1"
                tool = tool_combo.currentText() if tool_combo else None

                if tool == "DisplayMode":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = decode_display_preset(cell.data(Qt.UserRole)) if cell else None

                    # ✅ AUTO-FIX: create default preset if missing
                    if not preset_payload or not preset_payload.get("views"):
                        print(f"⚠️ DisplayMode preset missing at row {row + 1}, auto-creating default")
                        preset_payload = {
                            "border_percent": getattr(self.app_window, "display_border_percent", 0),
                            "views": {
                                0: {
                                    code: {
                                        "show": entry.get("show", True),
                                        "weight": entry.get("weight", 1.0)
                                    }
                                    for code, entry in self.app_window.class_palette.items()
                                }
                            },
                            "force_refresh": True
                        }

                    shortcuts[(mod, key)] = {"tool": "DisplayMode", "preset": preset_payload}
                    continue

                if tool == "LineMode":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = (
                        decode_line_mode_preset(cell.data(Qt.UserRole))
                        if cell else None
                    )
                    shortcuts[(mod, key)] = {
                        "tool": "Line",
                        "preset": line_mode_to_display_visibility_preset(
                            preset_payload
                        ),
                    }
                    continue

                if tool == "Surface":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = decode_surface_preset(cell.data(Qt.UserRole)) if cell else None
                    shortcuts[(mod, key)] = {"tool": "Surface", "preset": preset_payload}
                    continue

                if tool == "ShadingMode":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = decode_shading_preset(cell.data(Qt.UserRole)) if cell else None
                    if not preset_payload or not preset_payload.get("classes"):
                        QMessageBox.warning(
                            self, "ShadingMode preset missing",
                            f"Row {row + 1} uses ShadingMode but has no preset.\n"
                            "Click the Classes cell, configure shading, then click Apply."
                        )
                        return
                    shortcuts[(mod, key)] = {"tool": "ShadingMode", "preset": preset_payload}
                    print(f"   Row {row}: {mod}+{key} → ShadingMode preset saved")
                    continue

                if tool == "DrawSettings":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = decode_draw_preset(cell.data(Qt.UserRole)) if cell else None
                    shortcuts[(mod, key)] = {"tool": "DrawSettings", "preset": preset_payload}
                    print(f"   Row {row}: {mod}+{key} → DrawSettings preset saved")
                    continue

                if tool == "SyncViews":
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = decode_sync_preset(cell.data(Qt.UserRole)) if cell else None
                    if not preset_payload:
                        preset_payload = {"rows": []}
                    shortcuts[(mod, key)] = {"tool": "SyncViews", "preset": preset_payload}
                    print(f"   Row {row}: {mod}+{key} → SyncViews preset saved")
                    continue
                if tool in DISPLAY_VISIBILITY_TOOLS:
                    cell = self.table.item(row, self.COL_CLASSES)
                    preset_payload = (
                        decode_display_visibility_preset(cell.data(Qt.UserRole))
                        if cell else None
                    )
                    shortcuts[(mod, key)] = {
                        "tool": tool,
                        "preset": preset_payload or {"mode": tool, "classes": {}},
                    }
                    continue
                if tool in simple_tools:
                    shortcuts[(mod, key)] = {"tool": tool, "from": None, "to": None}
                    print(f"   Row {row}: {mod}+{key} → {tool}")
                else:
                    cell = self.table.item(row, self.COL_CLASSES)
                    from_cls, to_cls = decode_classes(cell.data(Qt.UserRole)) if cell else (None, None)
                    if isinstance(from_cls, str):
                        if not from_cls or from_cls in ("None", "Any"):
                            from_cls = None
                        elif "," in from_cls:
                            from_cls = [int(c) for c in from_cls.split(",")]
                        else:
                            from_cls = [int(from_cls)]
                    if isinstance(to_cls, str):
                        to_cls = None if to_cls in ("", "None", "Any") else int(to_cls)
                    shortcuts[(mod, key)] = {"tool": tool, "from": from_cls, "to": to_cls}
                    print(f"   Row {row}: {mod}+{key} → {tool}, from={from_cls}, to={to_cls}")

            self.app_window.shortcuts = shortcuts

            if not hasattr(self.app_window, 'view_palettes'):
                self.app_window.view_palettes = {}

            print(f"   ℹ️ Shortcut presets stored (applied on keypress, not now)")

            print("✅ Shortcuts applied to app_window:", self.app_window.shortcuts)
            self.applied.emit(shortcuts)

            self.auto_save_shortcuts()
            self.is_editing_shortcuts = False

            picker = getattr(self.app_window, "class_picker", None)
            if self._qobject_alive(picker):
                try:
                    picker.hide()
                except (RuntimeError, ReferenceError):
                    self.app_window.class_picker = None
            elif picker is not None:
                self.app_window.class_picker = None

            # Table is already canonicalized before serializing/saving.
            self.close()

        finally:
            self._applying_shortcuts = False

    def on_cancel(self):
        self.is_editing_shortcuts = False
        
        self.hide()

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_display_border_sync()

    def changeEvent(self, event):
        """Intercept WindowStateChange so minimize goes to taskbar, not close."""
        from PySide6.QtCore import QEvent as _QEvent
        if event.type() == _QEvent.WindowStateChange:
            if self.isMinimized():
                event.accept()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        # Always hide on close (X button) — never destroy the widget.
        # Real cleanup happens in _do_real_close() called only at app shutdown.
        event.ignore()
        self.hide()

    def _do_real_close(self):
        """Cleanup logic run only when truly closing (app shutdown)."""
        self.is_editing_shortcuts = False
        self._restore_app_focus()

        picker_attrs = [
            "_display_picker", "_shading_picker", "_surface_picker",
            "_draw_picker", "_draw_settings_dialog",
        ]
        picker_attrs.extend(
            f"_{tool.lower()}_visibility_picker"
            for tool in DISPLAY_VISIBILITY_TOOLS
        )
        for attr_name in picker_attrs:
            picker = getattr(self, attr_name, None)
            if picker is None:
                continue
            try:
                picker.close()
            except Exception:
                pass
            try:
                picker.deleteLater()
            except Exception:
                pass
            setattr(self, attr_name, None)

        picker = getattr(self.app_window, "class_picker", None)
        if (
            self._qobject_alive(picker)
            and self._qobject_alive(getattr(picker, "from_list", None))
            and self._qobject_alive(getattr(picker, "to_combo", None))
        ):
            self._safe_disconnect(
                picker.from_list.itemSelectionChanged,
                self.update_classes_from_picker,
            )
            self._safe_disconnect(
                picker.to_combo.currentIndexChanged,
                self.update_classes_from_picker,
            )
        elif picker is not None:
            try:
                self.app_window.class_picker = None
            except Exception:
                pass
        try:
            if getattr(self.app_window, "_shortcut_manager", None) is self:
                self.app_window._shortcut_manager = None
        except Exception:
            pass

    @staticmethod
    def open_manager(app_window):
        inst = ShortcutManager.instance
        needs_new = inst is None

        if not needs_new:
            try:
                _ = inst.isVisible()
            except Exception:
                needs_new = True

        if needs_new:
            inst = ShortcutManager(app_window)
            ShortcutManager.instance = inst

        # Restore from minimized/taskbar state if needed
        inst.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        inst.setWindowFlag(Qt.Tool, False)
        if inst.isMinimized():
            inst.setWindowState(inst.windowState() & ~Qt.WindowMinimized | Qt.WindowActive)
        elif not inst.isVisible():
            inst.show()
        inst.raise_()
        inst.activateWindow()
        try:
            inst.setFocus(Qt.ActiveWindowFocusReason)
        except Exception:
            pass
        return inst

    def auto_load_shortcuts(self):
        """
        ✅ FIXED: Load shortcuts from QSettings into app_window.shortcuts ONLY.
        NEVER touch view_palettes or class_palette here.
        """
        try:
            shortcuts_data = self.settings.value("shortcuts", None)
            if shortcuts_data is None:
                print("ℹ️ No saved shortcuts found")
                return

            if isinstance(shortcuts_data, str):
                shortcuts_list = json.loads(shortcuts_data)
            else:
                shortcuts_list = shortcuts_data

            print(f"📋 Loading {len(shortcuts_list)} shortcuts from QSettings...")

            shortcuts    = {}
            simple_tools = SIMPLE_SHORTCUT_TOOLS

            self._is_loading_shortcuts = True
            try:
                for entry in shortcuts_list:
                    modifier  = entry.get("modifier", "alt")
                    key       = entry.get("key", "F1")
                    tool      = entry.get("tool", "AboveLine")
                    if tool == "LineMode":
                        preset_payload = line_mode_to_display_visibility_preset(
                            entry.get("line_mode_preset")
                        )
                        shortcuts[(
                            modifier.lower(), key.upper()
                        )] = {
                            "tool": "Line",
                            "preset": preset_payload,
                        }
                        continue
                    mod       = modifier.lower()
                    key_upper = key.upper()

                    if tool == "DisplayMode":
                        preset_payload = entry.get("display_preset")
                        if preset_payload:
                            shortcuts[(mod, key_upper)] = {
                                "tool": "DisplayMode", "preset": preset_payload
                            }
                            views = preset_payload.get("views", {})
                            print(f"   ✅ {mod}+{key_upper} → DisplayMode "
                                  f"(views: {list(views.keys())}) [stored, NOT applied]")
                        continue

                    if tool == "Surface":
                        preset_payload = entry.get("surface_preset")
                        shortcuts[(mod, key_upper)] = {
                            "tool": "Surface", "preset": preset_payload
                        }
                        print(f"   âœ… {mod}+{key_upper} â†’ Surface [stored, NOT applied]")
                        continue

                    if tool == "ShadingMode":
                        preset_payload = entry.get("shading_preset")
                        if preset_payload:
                            shortcuts[(mod, key_upper)] = {
                                "tool": "ShadingMode", "preset": preset_payload
                            }
                            print(f"   ✅ {mod}+{key_upper} → ShadingMode [stored, NOT applied]")
                        continue

                    if tool == "DrawSettings":
                        preset_payload = entry.get("draw_preset")
                        if preset_payload:
                            shortcuts[(mod, key_upper)] = {
                                "tool": "DrawSettings", "preset": preset_payload
                            }
                            print(f"   ✅ {mod}+{key_upper} → DrawSettings [stored, NOT applied]")
                        continue

                    if tool == "SyncViews":
                        preset_payload = entry.get("sync_preset")
                        if not preset_payload:
                            preset_payload = {"rows": []}
                        shortcuts[(mod, key_upper)] = {
                            "tool": "SyncViews", "preset": preset_payload
                        }
                        print(f"   ✅ {mod}+{key_upper} → SyncViews [stored, NOT applied]")
                        continue

                    if tool in DISPLAY_VISIBILITY_TOOLS:
                        preset_payload = entry.get("visibility_preset")
                        shortcuts[(mod, key_upper)] = {
                            "tool": tool,
                            "preset": preset_payload or {"mode": tool, "classes": {}},
                        }
                        continue

                    if tool in simple_tools:
                        shortcuts[(mod, key_upper)] = {
                            "tool": tool, "from": None, "to": None
                        }
                        print(f"   ✅ {mod}+{key_upper} → {tool}")
                    else:
                        from_cls = entry.get("from_classes")
                        to_cls   = entry.get("to_class")
                        shortcuts[(mod, key_upper)] = {
                            "tool": tool, "from": from_cls, "to": to_cls
                        }
                        print(f"   ✅ {mod}+{key_upper} → {tool} [{from_cls} → {to_cls}]")
            finally:
                self._is_loading_shortcuts = False

            self.app_window.shortcuts = shortcuts
            print(f"✅ {len(shortcuts)} shortcuts loaded into app_window.shortcuts")
            print("   ⚠️  view_palettes / class_palette NOT touched — will apply on keypress only")

            # ── Rebuild table UI ──────────────────────────────────────
            self._is_loading_shortcuts = True
            self._selected_rows.clear()
            try:
                self.table.setRowCount(0)
                for entry in shortcuts_list:
                    row_tool = entry.get("tool", "AboveLine")
                    if row_tool == "LineMode":
                        row_tool = "Line"
                    row = self.table.rowCount()
                    self.table.insertRow(row)
                    self._install_row_widgets(
                        row,
                        modifier=entry.get("modifier", "alt"),
                        key=entry.get("key", "F1"),
                        tool=row_tool
                    )
                    tool = row_tool

                    if tool == "DisplayMode":
                        preset_payload = entry.get("display_preset")
                        if preset_payload:
                            views      = preset_payload.get("views", {})
                            border_pct = preset_payload.get("border_percent", 0)
                            first_view = int(list(views.keys())[0]) if views else 0
                            view_name  = (["Main", "V1", "V2", "V3", "V4", "Cut"][first_view]
                                          if first_view < 6 else f"V{first_view}")
                            vis_cnt = sum(
                                sum(1 for c in cls.values() if c.get("show"))
                                for cls in views.values()
                            )
                            # ✅ Include weight info in summary (restored from old code)
                            all_weights = [c.get("weight", 1.0)
                                           for cls in views.values() for c in cls.values()]
                            if all_weights:
                                uw = sorted(set(all_weights))
                                weight_info = (f"W={uw[0]:.1f}" if len(uw) == 1
                                               else f"W={min(all_weights):.1f}-{max(all_weights):.1f}")
                            else:
                                weight_info = "W=1.0"
                            summary = f"{view_name}: {vis_cnt}vis, B={border_pct:.1f}%, {weight_info}"
                            item = QTableWidgetItem(summary)
                            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                            item.setData(Qt.UserRole, encode_display_preset(preset_payload))
                        else:
                            item = QTableWidgetItem("Click to configure")
                            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool == "Surface":
                        preset_payload = entry.get("surface_preset")
                        if preset_payload:
                            item = QTableWidgetItem(summarize_surface_preset(preset_payload))
                            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                            item.setData(Qt.UserRole, encode_surface_preset(preset_payload))
                        else:
                            item = QTableWidgetItem("Click/Double-click to configure surface preset")
                            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                            item.setData(Qt.UserRole, encode_surface_preset({}))
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool == "ShadingMode":
                        preset_payload = entry.get("shading_preset")
                        if preset_payload:
                            az  = preset_payload.get("azimuth", 45)
                            ang = preset_payload.get("angle",   45)
                            quality_label = shading_quality_label(
                                normalize_shading_preset_quality(
                                    preset_payload.get("quality_mode"),
                                    preset_payload.get("speed"),
                                )
                            )
                            visible = [c for c, info in (preset_payload.get("classes", {}) or {}).items()
                                       if info.get("show", True)]
                            item = QTableWidgetItem(
                                f"Shading: {az}°/{ang}°, {len(visible)} visible, Speed={quality_label}"
                            )
                        else:
                            item = QTableWidgetItem("Preset: Not configured yet")
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_shading_preset(preset_payload or {}))
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool == "DrawSettings":
                        preset_payload = entry.get("draw_preset")
                        item = QTableWidgetItem(
                            f"Draw: {len((preset_payload or {}).get('tools', {}))} tools"
                        )
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_draw_preset(preset_payload or {}))
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool == "SyncViews":
                        preset_payload = entry.get("sync_preset")
                        summary = _sync_preset_summary(preset_payload) if preset_payload else "Double-click to configure sync preset"
                        item = QTableWidgetItem(summary)
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_sync_preset(preset_payload or {"rows": []}))
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool == "LineMode":
                        preset_payload = entry.get("line_mode_preset") or {
                            "target_view": 0, "lines": {}
                        }
                        item = QTableWidgetItem(
                            summarize_line_mode_preset(preset_payload)
                        )
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(
                            Qt.UserRole,
                            encode_line_mode_preset(preset_payload),
                        )
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool in DISPLAY_VISIBILITY_TOOLS:
                        preset_payload = entry.get("visibility_preset") or {
                            "mode": tool, "classes": {}
                        }
                        item = QTableWidgetItem(
                            summarize_display_visibility_preset(preset_payload, tool)
                            if preset_payload.get("classes")
                            else f"Click/Double-click to configure {tool} class visibility"
                        )
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(
                            Qt.UserRole,
                            encode_display_visibility_preset(preset_payload),
                        )
                        self.table.setItem(row, self.COL_CLASSES, item)

                    elif tool in SIMPLE_SHORTCUT_TOOLS and tool != "Surface":
                        item = QTableWidgetItem("N/A")
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_classes(None, None))
                        self.table.setItem(row, self.COL_CLASSES, item)

                    else:
                        from_cls = entry.get("from_classes")
                        to_cls   = entry.get("to_class")
                        from_txt = ", ".join(str(c) for c in from_cls) if from_cls else "Any"
                        to_txt   = str(to_cls) if to_cls is not None else "Any"
                        label    = f"{from_txt} → {to_txt}"
                        item = QTableWidgetItem(label)
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setData(Qt.UserRole, encode_classes(from_cls, to_cls))
                        self.table.setItem(row, self.COL_CLASSES, item)

            finally:
                self._is_loading_shortcuts = False

            if self.table.rowCount() > 0:
                self._sort_table_by_modifier()
                self._set_current_row(0)
            print(f"✅ Table rebuilt with {self.table.rowCount()} rows")

        except Exception as e:
            print(f"❌ auto_load_shortcuts failed: {e}")
            import traceback
            traceback.print_exc()

    def auto_save_shortcuts(self):
        """Save shortcut definitions to QSettings — preset data only, no live state."""
        try:
            shortcuts_list = []
            for row in range(self.table.rowCount()):
                mod_combo  = self.table.cellWidget(row, self.COL_MODIFIER)
                key_combo  = self.table.cellWidget(row, self.COL_KEY)
                tool_combo = self.table.cellWidget(row, self.COL_TOOL)
                cell       = self.table.item(row, self.COL_CLASSES)

                if not (mod_combo and key_combo and tool_combo):
                    continue

                modifier = mod_combo.currentText()
                key      = key_combo.currentText()
                tool     = tool_combo.currentText()
                entry    = {"modifier": modifier, "key": key, "tool": tool}

                if tool == "DisplayMode" and cell:
                    preset = decode_display_preset(cell.data(Qt.UserRole))
                    if preset:
                        entry["display_preset"] = preset
                elif tool == "LineMode" and cell:
                    preset = decode_line_mode_preset(cell.data(Qt.UserRole))
                    entry["line_mode_preset"] = (
                        preset or {"target_view": 0, "lines": {}}
                    )
                elif tool == "Surface" and cell:
                    preset = decode_surface_preset(cell.data(Qt.UserRole))
                    entry["surface_preset"] = preset
                elif tool == "ShadingMode" and cell:
                    preset = decode_shading_preset(cell.data(Qt.UserRole))
                    if preset:
                        entry["shading_preset"] = preset
                elif tool == "DrawSettings" and cell:
                    preset = decode_draw_preset(cell.data(Qt.UserRole))
                    if preset:
                        entry["draw_preset"] = preset
                elif tool == "SyncViews" and cell:
                    preset = decode_sync_preset(cell.data(Qt.UserRole))
                    entry["sync_preset"] = preset or {"rows": []}
                elif tool in DISPLAY_VISIBILITY_TOOLS and cell:
                    preset = decode_display_visibility_preset(cell.data(Qt.UserRole))
                    entry["visibility_preset"] = preset or {
                        "mode": tool, "classes": {}
                    }
                elif tool in SIMPLE_SHORTCUT_TOOLS and tool != "Surface":
                    # Simple non-classification tools have no preset payload.
                    pass
                else:
                    if cell:
                        from_cls, to_cls = decode_classes(cell.data(Qt.UserRole))
                        entry["from_classes"] = from_cls
                        entry["to_class"]     = to_cls

                shortcuts_list.append(entry)

            self.settings.setValue("shortcuts", json.dumps(shortcuts_list))
            print(f"Saved {len(shortcuts_list)} shortcuts to QSettings")

        except Exception as e:
            print(f"auto_save_shortcuts failed: {e}")
            import traceback
            traceback.print_exc()

    def _sort_table_by_modifier(self):
        """
        Sort all rows alphabetically by Modifier column.

        Always called via QTimer.singleShot(0, ...) so it runs AFTER the current
        event loop tick — prevents wglMakeCurrent errors from widget destruction
        during synchronous table rebuilds.
        """
        row_count = self.table.rowCount()
        if row_count < 2:
            return

        MODIFIER_ORDER = {
            "alt":             0,
            "alt+shift":       1,
            "ctrl":            2,
            "ctrl+alt":        3,
            "ctrl+alt+shift":  4,
            "ctrl+shift":      5,
            "none":            6,
            "shift":           7,
        }

        def _key_rank(key):
            """Sortable tuple: F-keys numeric, then A-Z, then Space."""
            k = key.upper()
            if len(k) >= 2 and k[0] == "F" and k[1:].isdigit():
                return (0, int(k[1:]), "")
            if k == "SPACE":
                return (2, 0, "SPACE")
            return (1, 0, k)

        def sort_key(r):
            mod = str(r["mod"]).strip().lower()
            key = str(r["key"]).strip().upper()
            return (MODIFIER_ORDER.get(mod, 99), _key_rank(key))

        # ── Snapshot every row before touching the table ──────────────
        rows_data = []
        for row in range(row_count):
            mod_combo  = self.table.cellWidget(row, self.COL_MODIFIER)
            key_combo  = self.table.cellWidget(row, self.COL_KEY)
            tool_combo = self.table.cellWidget(row, self.COL_TOOL)
            cell3      = self.table.item(row, self.COL_CLASSES)
            cb         = self._get_row_checkbox(row)

            rows_data.append({
                "mod":         mod_combo.currentText()  if mod_combo  else "none",
                "key":         key_combo.currentText()  if key_combo  else "F1",
                "tool":        tool_combo.currentText() if tool_combo else "AboveLine",
                "cell3_text":  cell3.text()             if cell3      else "",
                "cell3_data":  cell3.data(Qt.UserRole)  if cell3      else None,
                "cell3_flags": cell3.flags()            if cell3      else Qt.ItemIsEnabled,
                "checked":     cb.isChecked()           if cb         else False,
            })

        rows_data.sort(key=sort_key)

        # ── Rebuild table from sorted snapshot ────────────────────────
        prev_loading = self._is_loading_shortcuts
        self._is_loading_shortcuts = True
        self._selected_rows.clear()
        try:
            self.table.setRowCount(0)
            for row, rd in enumerate(rows_data):
                self.table.insertRow(row)
                self._install_row_widgets(
                    row, modifier=rd["mod"], key=rd["key"], tool=rd["tool"]
                )
                # Restore checkbox state
                cb = self._get_row_checkbox(row)
                if cb and rd["checked"]:
                    cb.blockSignals(True)
                    cb.setChecked(True)
                    cb.blockSignals(False)
                    self._selected_rows.add(row)

                item = QTableWidgetItem(rd["cell3_text"])
                item.setData(Qt.UserRole, rd["cell3_data"])
                item.setFlags(rd["cell3_flags"])
                self.table.setItem(row, self.COL_CLASSES, item)

        finally:
            self._is_loading_shortcuts = prev_loading

        self._refresh_all_row_headers()
        print(f"✅ Table sorted by Modifier: {row_count} rows in alphabetical order")
