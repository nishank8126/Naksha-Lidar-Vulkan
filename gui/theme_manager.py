# """
# gui/theme_manager.py
# ─────────────────────
# Centralized theme manager for NakshaAI-Lidar.
# Supports Dark and Light themes with runtime switching.
# Inspired by the Ring app's dual-theme system (themes.py).
# """

# import os
# from PySide6.QtCore import QEvent, QObject, QSettings, Qt

# try:
#     from shiboken6 import isValid as _qt_object_is_valid
# except ImportError:
#     def _qt_object_is_valid(obj):
#         return obj is not None


# # ═══════════════════════════════════════════════════════════════════════════
# #  THEME COLOR PALETTES
# # ═══════════════════════════════════════════════════════════════════════════

# class ThemeColors:
#     """Theme-aware color palette for inline style usage."""

#     # ── Dark palette ────────────────────────────────────────────────
#     DARK = {
#         # Base
#         "bg_primary":       "#121212",
#         "bg_secondary":     "#1a1a1a",
#         "bg_tertiary":      "#202020",
#         "bg_input":         "#1e1e1e",
#         "bg_button":        "#2a2a2a",
#         "bg_button_hover":  "#3c3c3c",
#         "bg_active":        "#004d40",
#         "bg_active_border": "#1b5e20",

#         # Text
#         "text_primary":     "#e0e0e0",
#         "text_secondary":   "#aaaaaa",
#         "text_muted":       "#888888",
#         "text_on_active":   "#ffffff",

#         # Global icon fallback
#         "icon_primary":     "#d3d1c7",

#         # Per-ribbon / per-group icon colors
#         "icon_file":        "#4FC3F7",
#         "icon_edit":        "#FFB74D",
#         "icon_view":        "#81C784",
#         "icon_tools":       "#BA68C8",
#         "icon_classify":    "#FFD54F",
#         "icon_measure":     "#4DD0E1",
#         "icon_identify":    "#E57373",
#         "icon_by_class":    "#AED581",
#         "icon_draw":        "#F48FB1",
#         "icon_curve":       "#90CAF9",
#         "icon_ai":          "#FF8A65",
#         "icon_display":     "#4db6ac",
#         "icon_settings":    "#B0BEC5",
#         "icon_project":     "#A5D6A7",
#         "icon_history":     "#FFCC80",
#         "icon_clipboard":   "#80CBC4",
#         "icon_attachments": "#CE93D8",
#         "icon_vectors":     "#80DEEA",
#         "icon_dxf":         "#F48FB1",
#         "icon_dwg":         "#9FA8DA",
#         "icon_snt":         "#A5D6A7",

#         # Individual ribbon icon colors: File
#         "icon_open":          "#42a5f5",
#         "icon_save":          "#26a69a",
#         "icon_save_as":       "#7e57c2",
#         "icon_export":        "#ffb300",
#         "icon_import":        "#ef5350",
#         "icon_attach_dxf":    "#29b6f6",
#         "icon_prj_loader":    "#8d6e63",
#         "icon_verify_labels": "#66bb6a",
#         "icon_attach_dwg":    "#5c6bc0",
#         "icon_attach_snt":    "#26c6da",
#         "icon_clear":         "#ec407a",
#         "icon_clear_data":    "#ff1744",

#         # Accent
#         "accent":            "#007acc",
#         "accent_hover":      "#0098ff",
#         "accent_alt":        "#ffaa00",
#         "dialog_primary_bg": "#30363c",
#         "dialog_primary_hover": "#394148",
#         "dialog_primary_border": "#505c66",
#         "dialog_primary_text": "#f5f7fa",
#         "dialog_selection":  "#2b3138",

#         # Borders
#         "border":            "#2a2a2a",
#         "border_light":      "#3a3a3a",
#         "border_active":     "#007acc",

#         # Status
#         "danger":            "#d32f2f",
#         "danger_hover":      "#f44336",
#         "danger_dark":       "#b71c1c",
#         "warning":           "#ff6f00",
#         "warning_bg":        "#e65100",
#         "success":           "#41613d",

#         # Ribbon
#         "ribbon_bg":         "#1e1e1c",
#         "ribbon_section":    "#171715",
#         "ribbon_button":     "#222220",
#         "ribbon_btn_hover":  "#2a2a28",
#         "ribbon_btn_pressed":"#31312e",
#         "ribbon_btn_border": "#2e2e2b",
#         "ribbon_btn_checked":"#2b3540",
#         "ribbon_btn_checked_border":"#556675",
#         "ribbon_title":      "#4a4a47",

#         # Misc
#         "shadow":            "#000000",

#         # ── Individual icon colors: Edit ─────────────────────────────
#         "icon_undo":                 "#ffb74d",
#         "icon_redo":                 "#ff8a65",
#         "icon_cut":                  "#4fc3f7",
#         "icon_copy":                 "#81c784",
#         "icon_paste":                "#ba68c8",
#         "icon_delete":               "#ef5350",

#         # ── Individual icon colors: View ─────────────────────────────
#         "icon_top":                  "#64b5f6",
#         "icon_front":                "#4db6ac",
#         "icon_side":                 "#9575cd",
#         "icon_3d":                   "#ffd54f",
#         "icon_depth":                "#90a4ae",
#         "icon_rgb":                  "#ff7043",
#         "icon_intensity":            "#4dd0e1",
#         "icon_elevation":            "#aed581",
#         "icon_class":                "#f06292",
#         "icon_shading":              "#a1887f",
#         "icon_fit":                  "#7986cb",

#         # ── Individual icon colors: Tools ────────────────────────────
#         "icon_cross":                "#26a69a",
#         "icon_cut_section":          "#ef5350",
#         "icon_width":                "#5c6bc0",
#         "icon_views":                "#29b6f6",
#         "icon_config":               "#ab47bc",
#         "icon_backup":               "#66bb6a",
#         "icon_preferences":          "#ffa726",

#         # ── Individual icon colors: Classify ─────────────────────────
#         "icon_above":                "#42a5f5",
#         "icon_below":                "#ffca28",
#         "icon_classify_rect":        "#26c6da",
#         "icon_classify_circle":      "#7e57c2",
#         "icon_classify_free":        "#ec407a",
#         "icon_brush":                "#8d6e63",
#         "icon_point":                "#d4e157",

#         # ── Individual icon colors: Display ──────────────────────────
#         "icon_measure_line":         "#26a69a",
#         "icon_measure_path":         "#42a5f5",
#         "icon_measure_clear":        "#ec407a",
#         "icon_area_block":           "#26c6da",

#         # ── Individual icon colors: Identify ─────────────────────────
#         "icon_identify":             "#ffb300",
#         "icon_zoom":                 "#66bb6a",
#         "icon_select":               "#90caf9",

#         # ── Individual icon colors: By Class ─────────────────────────
#         "icon_convert":              "#4fc3f7",
#         "icon_close":                "#9575cd",
#         "icon_height":               "#ffb74d",
#         "icon_fence":                "#81c784",
#         "icon_low_points":           "#ef5350",
#         "icon_isolated":             "#26c6da",
#         "icon_ground":               "#a1887f",
#         "icon_surface":              "#ffd54f",

#         # ── Individual icon colors: Draw ─────────────────────────────
#         "icon_smart_line":           "#26a69a",
#         "icon_line":                 "#64b5f6",
#         "icon_draw_rect":            "#ab47bc",
#         "icon_draw_circle":          "#ffca28",
#         "icon_draw_free":            "#ec407a",
#         "icon_text":                 "#90a4ae",
#         "icon_move_vertex":          "#4dd0e1",
#         "icon_vertex":               "#81c784",
#         "icon_grid":                 "#7986cb",
#         "icon_select_drawing":       "#ffb300",
#         "icon_deselect_all":         "#ef5350",
#         "icon_draw_clear":           "#ff1744",
#         "icon_draw_settings":        "#b0bec5",

#         # ── Individual icon colors: Curve ────────────────────────────
#         "icon_curve_point":          "#f06292",
#         "icon_curve_clear":          "#ec407a",

#         # ── Individual icon colors: AI ───────────────────────────────
#         "icon_start":                "#ff7043",
#         "icon_create_block":         "#00e5ff",
#     }

#     # ── Light palette ──────────────────────────────────────────────
#     LIGHT = {
#         # Base
#         "bg_primary":       "#f0f2f5",
#         "bg_secondary":     "#ffffff",
#         "bg_tertiary":      "#e8ecf0",
#         "bg_input":         "#f7f8fa",
#         "bg_button":        "#eef0f4",
#         "bg_button_hover":  "#e0e4ea",
#         "bg_active":        "#0077b6",
#         "bg_active_border": "#005f8a",

#         # Text
#         "text_primary":     "#1b2838",
#         "text_secondary":   "#607d8b",
#         "text_muted":       "#90a4ae",
#         "text_on_active":   "#1c1c1a",

#         # Global icon fallback
#         "icon_primary":     "#1c1c1a",

#         # Per-ribbon / per-group icon colors
#         "icon_file":        "#1565C0",
#         "icon_edit":        "#EF6C00",
#         "icon_view":        "#2E7D32",
#         "icon_tools":       "#6A1B9A",
#         "icon_classify":    "#F9A825",
#         "icon_measure":     "#00838F",
#         "icon_identify":    "#C62828",
#         "icon_by_class":    "#558B2F",
#         "icon_draw":        "#AD1457",
#         "icon_curve":       "#1976D2",
#         "icon_ai":          "#D84315",
#         "icon_display":     "#00897b",
#         "icon_settings":    "#546E7A",
#         "icon_project":     "#2E7D32",
#         "icon_history":     "#FB8C00",
#         "icon_clipboard":   "#00897B",
#         "icon_attachments": "#8E24AA",
#         "icon_vectors":     "#00ACC1",
#         "icon_dxf":         "#C2185B",
#         "icon_dwg":         "#3949AB",
#         "icon_snt":         "#43A047",

#         # Individual ribbon icon colors: File
#         "icon_open":          "#1976d2",
#         "icon_save":          "#00897b",
#         "icon_save_as":       "#5e35b1",
#         "icon_export":        "#fb8c00",
#         "icon_import":        "#e53935",
#         "icon_attach_dxf":    "#039be5",
#         "icon_prj_loader":    "#795548",
#         "icon_verify_labels": "#43a047",
#         "icon_attach_dwg":    "#3949ab",
#         "icon_attach_snt":    "#00acc1",
#         "icon_clear":         "#d81b60",
#         "icon_clear_data":    "#d50000",

#         # Accent
#         "accent":            "#0077b6",
#         "accent_hover":      "#00b4d8",
#         "accent_alt":        "#0096c7",
#         "dialog_primary_bg": "#e7ebef",
#         "dialog_primary_hover": "#dde3e9",
#         "dialog_primary_border": "#c3ccd5",
#         "dialog_primary_text": "#26313a",
#         "dialog_selection":  "#edf1f4",

#         # Borders
#         "border":            "#d0d5dd",
#         "border_light":      "#e0e4ea",
#         "border_active":     "#00b4d8",

#         # Status
#         "danger":            "#d32f2f",
#         "danger_hover":      "#f44336",
#         "danger_dark":       "#b71c1c",
#         "warning":           "#ff6f00",
#         "warning_bg":        "#fff3e0",
#         "success":           "#2e7d32",

#         # Ribbon
#         "ribbon_bg":         "#f4f2ec",
#         "ribbon_section":    "#f0efea",
#         "ribbon_button":     "#ffffff",
#         "ribbon_btn_hover":  "#fbfaf7",
#         "ribbon_btn_pressed":"#ece8df",
#         "ribbon_btn_border": "#d6d1c7",
#         "ribbon_btn_checked":"#e6edf3",
#         "ribbon_btn_checked_border":"#c0cdda",
#         "ribbon_title":      "#a09f99",

#         # Misc
#         "shadow":            "#b0bec5",

#         # ── Individual icon colors: Edit ─────────────────────────────
#         "icon_undo":                 "#fb8c00",
#         "icon_redo":                 "#f4511e",
#         "icon_cut":                  "#039be5",
#         "icon_copy":                 "#43a047",
#         "icon_paste":                "#8e24aa",
#         "icon_delete":               "#d32f2f",

#         # ── Individual icon colors: View ─────────────────────────────
#         "icon_top":                  "#1e88e5",
#         "icon_front":                "#00897b",
#         "icon_side":                 "#5e35b1",
#         "icon_3d":                   "#f9a825",
#         "icon_depth":                "#607d8b",
#         "icon_rgb":                  "#f4511e",
#         "icon_intensity":            "#00acc1",
#         "icon_elevation":            "#7cb342",
#         "icon_class":                "#d81b60",
#         "icon_shading":              "#8d6e63",
#         "icon_fit":                  "#3949ab",

#         # ── Individual icon colors: Tools ────────────────────────────
#         "icon_cross":                "#00897b",
#         "icon_cut_section":          "#e53935",
#         "icon_width":                "#3949ab",
#         "icon_views":                "#039be5",
#         "icon_config":               "#8e24aa",
#         "icon_backup":               "#43a047",
#         "icon_preferences":          "#fb8c00",

#         # ── Individual icon colors: Classify ─────────────────────────
#         "icon_above":                "#1e88e5",
#         "icon_below":                "#fbc02d",
#         "icon_classify_rect":        "#00acc1",
#         "icon_classify_circle":      "#5e35b1",
#         "icon_classify_free":        "#d81b60",
#         "icon_brush":                "#6d4c41",
#         "icon_point":                "#9e9d24",

#         # ── Individual icon colors: Display / Measure ────────────────
#         "icon_measure_line":         "#00897b",
#         "icon_measure_path":         "#1e88e5",
#         "icon_measure_clear":        "#d81b60",
#         "icon_area_block":           "#00acc1",

#         # ── Individual icon colors: Identify ─────────────────────────
#         "icon_identify":             "#fb8c00",
#         "icon_zoom":                 "#43a047",
#         "icon_select":               "#1e88e5",

#         # ── Individual icon colors: By Class ─────────────────────────
#         "icon_convert":              "#039be5",
#         "icon_close":                "#5e35b1",
#         "icon_height":               "#fb8c00",
#         "icon_fence":                "#43a047",
#         "icon_low_points":           "#e53935",
#         "icon_isolated":             "#00acc1",
#         "icon_ground":               "#8d6e63",
#         "icon_surface":              "#f9a825",

#         # ── Individual icon colors: Draw ─────────────────────────────
#         "icon_smart_line":           "#00897b",
#         "icon_line":                 "#1e88e5",
#         "icon_draw_rect":            "#8e24aa",
#         "icon_draw_circle":          "#fbc02d",
#         "icon_draw_free":            "#d81b60",
#         "icon_text":                 "#78909c",
#         "icon_move_vertex":          "#00acc1",
#         "icon_vertex":               "#43a047",
#         "icon_grid":                 "#3949ab",
#         "icon_select_drawing":       "#fb8c00",
#         "icon_deselect_all":         "#e53935",
#         "icon_draw_clear":           "#d50000",
#         "icon_draw_settings":        "#546e7a",

#         # ── Individual icon colors: Curve ────────────────────────────
#         "icon_curve_point":          "#d81b60",
#         "icon_curve_clear":          "#d81b60",

#         # ── Individual icon colors: AI ───────────────────────────────
#         "icon_start":                "#f4511e",
#         "icon_create_block":         "#00838f",
#     }

#     _current = "dark"

#     @classmethod
#     def set_theme(cls, name: str):
#         cls._current = name.lower()

#     @classmethod
#     def get(cls, key: str, default: str = None) -> str:
#         palette = cls.LIGHT if cls._current == "light" else cls.DARK
#         return palette.get(key, default if default is not None else "#ff00ff")

#     @classmethod
#     def current_theme(cls) -> str:
#         return cls._current

#     @classmethod
#     def is_light(cls) -> bool:
#         return cls._current == "light"


# # ═══════════════════════════════════════════════════════════════════════════
# #  RIBBON BUTTON STYLE HELPERS
# # ═══════════════════════════════════════════════════════════════════════════

# def get_active_button_style() -> str:
#     """Return inline style for an ACTIVE (toggled-on) ribbon button."""
#     c = ThemeColors
#     return f"""
#     QPushButton {{
#         background-color: {c.get('ribbon_btn_checked')};
#         color: {c.get('text_primary')};
#         font-weight: 600;
#         font-size: 10px;
#         border: 1px solid {c.get('ribbon_btn_checked_border')};
#         border-radius: 10px;
#         padding: 4px 6px 8px 6px;
#         text-align: center;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('ribbon_btn_checked')};
#     }}
#     """


# def get_inactive_button_style() -> str:
#     """Return inline style for an INACTIVE ribbon button."""
#     c = ThemeColors
#     return f"""
#     QPushButton {{
#         background-color: {c.get('ribbon_button')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('ribbon_btn_border')};
#         border-radius: 10px;
#         font-size: 10px;
#         font-weight: 500;
#         padding: 4px 6px 8px 6px;
#         text-align: center;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('ribbon_btn_hover')};
#         border: 1px solid {c.get('ribbon_btn_border')};
#     }}
#     """


# def get_ribbon_button_text_style(active: bool = False) -> str:
#     """Return inline style for ribbon button text under icons."""
#     c = ThemeColors
#     bg = c.get("ribbon_btn_checked") if active else c.get("ribbon_button")
#     border = c.get("ribbon_btn_checked_border") if active else c.get("ribbon_btn_border")

#     return f"""
#     QPushButton {{
#         background-color: {bg};
#         color: {c.get('text_primary')};
#         border: 1px solid {border};
#         border-radius: 10px;
#         font-size: 10px;
#         font-weight: 500;
#         padding: 4px 6px 8px 6px;
#         text-align: center;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('ribbon_btn_checked') if active else c.get('ribbon_btn_hover')};
#         border: 1px solid {border};
#     }}
#     """


# def get_status_label_style() -> str:
#     """Return style for identification / info status labels."""
#     c = ThemeColors
#     return f"""
#     QLabel {{
#         color: {c.get('text_secondary')};
#         font-size: 11px;
#         padding: 4px;
#         background-color: {c.get('ribbon_button')};
#         border-radius: 3px;
#     }}
#     """


# def get_status_label_active_style(color_key="warning") -> str:
#     """Return style for an active status label (e.g. select mode)."""
#     c = ThemeColors
#     if color_key == "warning":
#         return f"""
#         QLabel {{
#             color: #ffb74d;
#             font-size: 11px;
#             padding: 4px;
#             background-color: {c.get('warning_bg')};
#             border-radius: 3px;
#         }}
#         """
#     return get_status_label_style()


# def get_sharpness_btn_style(variant="minus") -> str:
#     """Return style for sharpness +/- buttons."""
#     c = ThemeColors
#     if variant == "minus":
#         return f"""
#         QPushButton {{
#             background-color: {c.get('bg_input')};
#             color: {c.get('text_primary')};
#             border: 2px solid {c.get('border_light')};
#             border-radius: 4px;
#             font-size: 20px;
#             font-weight: bold;
#         }}
#         QPushButton:hover {{
#             background-color: {c.get('bg_button_hover')};
#             border: 2px solid {c.get('accent')};
#         }}
#         QPushButton:pressed {{
#             background-color: {c.get('accent')};
#             color: {c.get('text_on_active')};
#             border: 2px solid {c.get('accent')};
#         }}
#         """
#     else:  # plus
#         return f"""
#         QPushButton {{
#             background-color: {c.get('bg_button')};
#             color: {c.get('accent')};
#             border: 2px solid {c.get('border_light')};
#             border-radius: 5px;
#             font-size: 18px;
#             font-weight: bold;
#         }}
#         QPushButton:hover {{
#             background-color: {c.get('bg_button_hover')};
#             border: 2px solid {c.get('accent')};
#         }}
#         QPushButton:pressed {{
#             background-color: {c.get('accent')};
#             color: {c.get('text_on_active')};
#         }}
#         """


# def get_sharpness_value_style() -> str:
#     """Return style for the sharpness value display label."""
#     c = ThemeColors
#     return f"""
#     QLabel {{
#         color: {c.get('text_primary')};
#         font-size: 12px;
#         font-weight: 700;
#         background-color: {c.get('bg_input')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 12px;
#         padding: 0px 10px;
#     }}
#     """


# def get_reset_btn_style() -> str:
#     """Return style for the amplifier reset button."""
#     c = ThemeColors
#     return f"""
#     QPushButton {{
#         background-color: {c.get('success')};
#         color: {c.get('text_on_active')};
#         border: 1px solid {c.get('border')};
#         border-radius: 3px;
#         font-size: 11px;
#         font-weight: bold;
#         padding: 2px;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {c.get('accent_alt')};
#         color: {c.get('text_primary')};
#     }}
#     QPushButton:pressed {{
#         background-color: {c.get('accent_alt')};
#         color: {c.get('text_primary')};
#     }}
#     """


# def get_delete_btn_style() -> str:
#     """Return style for the delete selected points button."""
#     c = ThemeColors
#     return f"""
#     QPushButton {{
#         background-color: {c.get('danger')};
#         color: white;
#         font-size: 11px;
#         font-weight: bold;
#         border: 1px solid {c.get('danger_dark')};
#         border-radius: 4px;
#         padding: 4px;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('danger_hover')};
#         border: 1px solid {c.get('danger')};
#     }}
#     QPushButton:pressed {{
#         background-color: {c.get('danger_dark')};
#     }}
#     """


# def get_select_active_btn_style() -> str:
#     """Return style for active select/tool buttons."""
#     c = ThemeColors
#     return f"""
#     QPushButton {{
#         background-color: {c.get('warning')};
#         color: white;
#         font-weight: bold;
#         border: 1px solid {c.get('warning_bg')};
#         border-radius: 6px;
#     }}
#     """


# def get_sharp_label_style() -> str:
#     """Style for the 'Sharp:' label in ViewRibbon."""
#     c = ThemeColors
#     return f"color: {c.get('text_primary')}; font-size: 12px; font-weight: bold;"


# class _ThemeWindowEventFilter(QObject):
#     """Applies the active theme when top-level widgets are shown."""

#     def eventFilter(self, obj, event):
#         try:
#             if event.type() != QEvent.Show:
#                 return False

#             from PySide6.QtWidgets import QWidget

#             if not isinstance(obj, QWidget):
#                 return False
#             if not _qt_object_is_valid(obj):
#                 return False
#             if not obj.isWindow():
#                 return False
#             if not obj.isVisible():
#                 return False
#             if not obj.testAttribute(Qt.WA_WState_Created):
#                 return False

#             ThemeManager._refresh_widget_theme(obj)
#         except Exception:
#             pass
#         return False


# # ═══════════════════════════════════════════════════════════════════════════
# #  THEME MANAGER
# # ═══════════════════════════════════════════════════════════════════════════

# class ThemeManager:
#     """Manages loading and switching themes."""

#     _current_theme = "dark"
#     _window_event_filter = None

#     @classmethod
#     def apply_theme(cls, app_window, theme_name: str = "dark"):
#         """Apply a theme to the main application window."""
#         theme_name = theme_name.lower()
#         cls._current_theme = theme_name
#         ThemeColors.set_theme(theme_name)
#         cls._ensure_window_event_filter()

#         gui_dir = os.path.dirname(__file__)

#         if theme_name == "light":
#             qss_path = os.path.join(gui_dir, "theme_light.qss")
#         else:
#             qss_path = os.path.join(gui_dir, "theme.qss")

#         try:
#             with open(qss_path, "r", encoding="utf-8") as f:
#                 qss = f.read()

#             app_window.setStyleSheet(qss)

#             try:
#                 from PySide6.QtWidgets import QApplication
#                 app = QApplication.instance()
#                 if app:
#                     app.setStyleSheet(qss)
#             except Exception as e:
#                 print(f"⚠️ Could not apply theme to QApplication globally: {e}")

#             print(f"🎨 {theme_name.capitalize()} theme loaded successfully")
#         except Exception as e:
#             print(f"⚠️ Failed to apply {theme_name} theme: {e}")

#         try:
#             from PySide6.QtWidgets import QApplication
#             app = QApplication.instance()
#             if app:
#                 for widget in app.topLevelWidgets():
#                     cls._refresh_widget_theme(widget)
#         except Exception as e:
#             print(f"⚠️ Failed to refresh themed dialogs: {e}")

#         cls._refresh_open_windows()
#         cls.refresh_runtime_surfaces(app_window)
#         cls._refresh_ribbon_styles(app_window)

#         if hasattr(app_window, "_refresh_activity_bar_theme"):
#             try:
#                 app_window._refresh_activity_bar_theme()
#             except Exception as e:
#                 print(f"⚠️ Failed to refresh activity bar theme: {e}")

#         settings = QSettings("NakshaAI", "LidarApp")
#         settings.setValue("ui_theme", theme_name)

#     @classmethod
#     def toggle_theme(cls, app_window):
#         """Toggle between light and dark themes."""
#         new_theme = "light" if cls._current_theme == "dark" else "dark"
#         cls.apply_theme(app_window, new_theme)

#     @classmethod
#     def current(cls) -> str:
#         return cls._current_theme

#     @classmethod
#     def load_saved_theme(cls) -> str:
#         """Load the saved theme preference, default to dark."""
#         settings = QSettings("NakshaAI", "LidarApp")
#         return settings.value("ui_theme", "dark")

#     @classmethod
#     def canvas_background_for_theme(cls, theme_name: str = None) -> str:
#         """Return the preferred canvas background for a theme."""
#         settings = QSettings("NakshaAI", "LidarApp")
#         background = settings.value("ui_canvas_background", None)
#         if background:
#             return str(background).lower()

#         theme_name = (theme_name or cls.current() or cls.load_saved_theme()).lower()
#         if theme_name == "light":
#             return str(settings.value("ui_canvas_light", "white")).lower()
#         return str(settings.value("ui_canvas_dark", "black")).lower()

#     @classmethod
#     def canvas_background_rgb(cls, theme_name: str = None):
#         """Return normalized RGB for the preferred canvas background."""
#         bg_name = cls.canvas_background_for_theme(theme_name)
#         if bg_name == "black":
#             return (0.0, 0.0, 0.0)
#         return (1.0, 1.0, 1.0)

#     @classmethod
#     def _ensure_window_event_filter(cls):
#         try:
#             from PySide6.QtWidgets import QApplication

#             app = QApplication.instance()
#             if app is None or cls._window_event_filter is not None:
#                 return

#             cls._window_event_filter = _ThemeWindowEventFilter()
#             app.installEventFilter(cls._window_event_filter)
#         except Exception as e:
#             print(f"⚠️ Failed to install theme window filter: {e}")

#     @classmethod
#     def _refresh_open_windows(cls):
#         try:
#             from PySide6.QtWidgets import QApplication

#             app = QApplication.instance()
#             if not app:
#                 return

#             for widget in app.topLevelWidgets():
#                 cls._refresh_widget_theme(widget)
#         except Exception as e:
#             print(f"⚠️ Failed to refresh themed windows: {e}")

#     @classmethod
#     def _refresh_widget_theme(cls, widget):
#         try:
#             from PySide6.QtWidgets import QDialog, QDockWidget, QProgressDialog, QWidget

#             if widget is None or not _qt_object_is_valid(widget):
#                 return
#             if not isinstance(widget, QWidget):
#                 return
#             if not widget.testAttribute(Qt.WA_WState_Created):
#                 return
#             if widget.isWindow() and not widget.isVisible():
#                 return

#             if widget.isWindow():
#                 cls.apply_native_window_theme(widget)

#             if isinstance(widget, QProgressDialog):
#                 widget.setStyleSheet(get_progress_dialog_stylesheet())
#             elif isinstance(widget, QDialog) and widget.property("themeStyledDialog"):
#                 refresh_theme = getattr(widget, "refresh_theme", None)
#                 if callable(refresh_theme):
#                     refresh_theme()
#                 else:
#                     widget.setStyleSheet(get_dialog_stylesheet())
#             elif widget.property("themeStyledWindow"):
#                 refresh_theme = getattr(widget, "refresh_theme", None)
#                 if callable(refresh_theme):
#                     refresh_theme()
#                 else:
#                     widget.setStyleSheet(get_dialog_stylesheet())
#             elif isinstance(widget, QDockWidget):
#                 style = widget.style()
#                 if style is not None:
#                     style.unpolish(widget)
#                     style.polish(widget)
#                 widget.update()
#         except Exception as e:
#             print(f"⚠️ Failed to refresh widget theme: {e}")

#     @classmethod
#     def apply_native_window_theme(cls, widget):
#         """Use Windows immersive dark mode for native title bars when available."""
#         if os.name != "nt" or widget is None or not _qt_object_is_valid(widget):
#             return
#         if hasattr(widget, "testAttribute") and not widget.testAttribute(Qt.WA_WState_Created):
#             return
#         if hasattr(widget, "isVisible") and not widget.isVisible():
#             return

#         try:
#             import ctypes

#             hwnd = int(widget.winId())
#             if not hwnd:
#                 return

#             value = ctypes.c_int(1 if cls.current() == "dark" else 0)
#             dwmapi = ctypes.windll.dwmapi

#             for attribute in (20, 19):
#                 result = dwmapi.DwmSetWindowAttribute(
#                     ctypes.c_void_p(hwnd),
#                     ctypes.c_uint(attribute),
#                     ctypes.byref(value),
#                     ctypes.sizeof(value),
#                 )
#                 if result == 0:
#                     break
#         except Exception:
#             pass

#     @classmethod
#     def refresh_runtime_surfaces(cls, app_window):
#         """Refresh all currently open VTK canvases to match the active theme."""
#         if app_window is None:
#             return

#         for vtk_widget in cls._iter_vtk_widgets(app_window):
#             cls._apply_vtk_widget_theme(vtk_widget)

#     @classmethod
#     def _iter_vtk_widgets(cls, app_window):
#         candidates = []

#         for attr_name in ("vtk_widget", "sec_vtk"):
#             widget = getattr(app_window, attr_name, None)
#             if widget is not None:
#                 candidates.append(widget)

#         section_vtks = getattr(app_window, "section_vtks", None)
#         if isinstance(section_vtks, dict):
#             candidates.extend(section_vtks.values())

#         cut_controller = getattr(app_window, "cut_section_controller", None)
#         if cut_controller is not None:
#             cut_vtk = getattr(cut_controller, "cut_vtk", None)
#             if cut_vtk is not None:
#                 candidates.append(cut_vtk)

#         seen = set()
#         for widget in candidates:
#             if widget is None or id(widget) in seen:
#                 continue
#             seen.add(id(widget))
#             yield widget

#     @classmethod
#     def _apply_vtk_widget_theme(cls, vtk_widget):
#         bg_color = cls.canvas_background_for_theme()
#         rgb = cls.canvas_background_rgb()

#         try:
#             vtk_widget.set_background(bg_color)
#         except Exception:
#             pass

#         renderer = getattr(vtk_widget, "renderer", None)
#         if renderer is not None:
#             try:
#                 renderer.SetBackground(*rgb)
#                 renderer.SetBackground2(*rgb)
#                 renderer.GradientBackgroundOff()
#             except Exception:
#                 pass

#         try:
#             vtk_widget.render()
#         except Exception:
#             pass

#     @classmethod
#     def _refresh_ribbon_styles(cls, app_window):
#         """Re-apply theme-aware inline styles and icon colors on ribbon sections & buttons."""
#         try:
#             if not hasattr(app_window, 'ribbon_manager'):
#                 return

#             from gui.menu_sidebar_system import RibbonSection
#             from gui.icon_provider import get_button_icon
#             from PySide6.QtWidgets import QPushButton
#             from PySide6.QtCore import QSize

#             for name, ribbon in app_window.ribbon_manager.ribbons.items():
#                 for section in ribbon.findChildren(RibbonSection):
#                     if section.active_button:
#                         section.active_button.setStyleSheet(get_active_button_style())

#                     for btn in section.findChildren(QPushButton):
#                         btn_text = btn.property("ribbonText")
#                         section_title = btn.property("ribbonSection") or getattr(section, "section_title", None)
#                         ribbon_scope = btn.property("ribbonScope") or getattr(section, "ribbon_scope", None)

#                         if not btn_text:
#                             tooltip = btn.toolTip()
#                             if tooltip.startswith("<b>") and tooltip.endswith("</b>"):
#                                 btn_text = tooltip[3:-4]

#                         try:
#                             if section.active_button is not None and btn is section.active_button:
#                                 btn.setStyleSheet(get_active_button_style())
#                             else:
#                                 btn.setStyleSheet(get_inactive_button_style())
#                         except Exception:
#                             pass

#                         if btn_text:
#                             icon = get_button_icon(
#                                 btn_text,
#                                 section_title=section_title,
#                                 ribbon_scope=ribbon_scope,
#                                 color=None,
#                                 size=24,
#                             )
#                             if not icon.isNull():
#                                 btn.setIcon(icon)
#                                 btn.setIconSize(QSize(24, 24))

#                 if name == "view":
#                     _refresh_view_ribbon_styles(ribbon)

#                 if name == "identify":
#                     _refresh_identify_ribbon_styles(ribbon)

#         except Exception as e:
#             print(f"Failed to refresh ribbon styles: {e}")


# def get_dialog_stylesheet() -> str:
#     """
#     Return a comprehensive theme-aware stylesheet for QDialog and its child widgets.
#     Use this in all popup dialogs to ensure consistent theming.
#     """
#     c = ThemeColors
#     primary_bg = c.get("dialog_primary_bg")
#     primary_hover = c.get("dialog_primary_hover")
#     primary_border = c.get("dialog_primary_border")
#     primary_text = c.get("dialog_primary_text")
#     selection_bg = c.get("dialog_selection")
#     selection_text = c.get("text_primary") if c.is_light() else c.get("text_on_active")
#     progress_start = c.get("accent") if c.is_light() else primary_border
#     progress_end = c.get("accent_hover") if c.is_light() else c.get("accent")
#     checkbox_checked = c.get("accent") if c.is_light() else primary_border
#     checkbox_checked_border = c.get("bg_active_border") if c.is_light() else primary_border
#     return f"""
#     QDialog, QMessageBox {{
#         background-color: {c.get('bg_primary')};
#         color: {c.get('text_primary')};
#         font-family: "SF Pro Text", "Helvetica Neue", "Segoe UI", Arial;
#     }}
#     QLabel {{
#         color: {c.get('text_primary')};
#         background: transparent;
#     }}
#     QLabel#dialogSectionLabel {{
#         color: {c.get('text_primary')};
#         font-weight: 600;
#     }}
#     QLabel#dialogTitle {{
#         color: {c.get('text_primary')};
#         font-size: 16pt;
#         font-weight: 700;
#     }}
#     QLabel#dialogSubtitle {{
#         color: {c.get('text_secondary')};
#         font-size: 9.5pt;
#     }}
#     QLabel#dialogCaption {{
#         color: {c.get('text_secondary')};
#         font-size: 8.5pt;
#     }}
#     QLabel#dialogHintLabel {{
#         color: {c.get('text_secondary')};
#         font-size: 9pt;
#         padding: 8px;
#     }}
#     QLabel#dialogInlineNote {{
#         color: {c.get('text_secondary')};
#         font-size: 9pt;
#     }}
#     QLabel#displayBorderLabel {{
#         color: {c.get('text_secondary')};
#         font-weight: 600;
#         font-size: 8.8pt;
#     }}
#     QLabel#displayBorderValuePill {{
#         background: transparent;
#         color: {c.get('text_primary')};
#         border: none;
#         padding: 0px 2px;
#         font-weight: 600;
#         font-size: 9pt;
#     }}
#     QLabel#valuePill {{
#         background-color: {c.get('bg_input')};
 
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 8px;
#         padding: 5px 10px;
#         font-weight: 600;
#     }}
#     QWidget#dialogHero, QFrame#dialogHero,
#     QWidget#dialogCard, QFrame#dialogCard,
#     QWidget#dialogInfoStrip, QFrame#dialogInfoStrip {{
#         background-color: {c.get('bg_secondary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 10px;
#     }}
#     QFrame#displayControlsCard, QFrame#displayTableCard {{
#         background-color: {c.get('bg_secondary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 12px;
#     }}
#     QFrame#displayBorderStrip {{
#         background-color: {c.get('bg_secondary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 8px;
#     }}
#     QPushButton#displayBorderButton {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 6px;
#         font-size: 9pt;
#         font-weight: 600;
#         min-height: 0px;
#     }}
#     QPushButton#displayBorderButton:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#displayBorderButton:pressed {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QToolButton#displayFileButton {{
#         background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 20px 4px 10px;
#         font-weight: 500;
#     }}
#     QToolButton#displayFileButton:hover,
#     QToolButton#displayFileButton:focus {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QToolButton#displayFileButton:pressed {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QToolButton#displayFileButton::menu-indicator {{
#         subcontrol-origin: padding;
#         subcontrol-position: right center;
#         right: 6px;
#     }}
#     QPushButton#displayApplyBtn {{
#         background-color: {primary_bg};
#         color: {primary_text};
#         border: 1px solid {primary_border};
#         border-radius: 6px;
#         padding: 4px 14px;
#         font-weight: 600;
#         min-height: 0px;
#     }}
#     QPushButton#displayApplyBtn:hover {{
#         background-color: {primary_hover};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#displayApplyBtn:pressed {{
#         background-color: {primary_hover};
#     }}
#     QPushButton#displayCloseBtn {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 14px;
#         font-weight: 600;
#         min-height: 0px;
#     }}
#     QPushButton#displayCloseBtn:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#displayCloseBtn:pressed {{
#         background-color: {c.get('bg_button_hover')};
#     }}
#     QFrame#displayActionRail {{
#         background: transparent;
#         border: none;
#     }}
#     QPushButton#displayActionButton {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 10px;
#         font-weight: 500;
#         min-height: 0px;
#         text-align: center;
#     }}
#     QPushButton#displayActionButton:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#displayActionButton:pressed {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#add_btn {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         border-radius: 10px;
#         padding: 8px 12px;
#         font-weight: 600;
#         text-align: center;
#     }}
#     QPushButton#add_btn:hover {{
#         background-color: #1a3d1a;
#         border: 1px solid #39ff14;
#         color: #39ff14;
#     }}
#     QPushButton#add_btn:pressed {{
#         background-color: #0d200d;
#     }}
#     QPushButton#del_btn {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         border-radius: 10px;
#         padding: 8px 12px;
#         font-weight: 600;
#         text-align: center;
#     }}
#     QPushButton#del_btn:hover {{
#         background-color: #3d1a1a;
#         border: 1px solid #ff4444;
#         color: #ff4444;
#     }}
#     QPushButton#del_btn:pressed {{
#         background-color: #200d0d;
#     }}
#     QMenuBar#displayMenuBar {{
#         background: transparent;
#         color: {c.get('text_primary')};
#         border: none;
#         padding: 0px;
#     }}
#     QMenuBar#displayMenuBar::item {{
#         background: transparent;
#         padding: 5px 8px;
#         margin: 0px 2px 0px 0px;
#         border-radius: 4px;
#     }}
#     QMenuBar#displayMenuBar::item:selected {{
#         background-color: {c.get('bg_button_hover')};
#         color: {c.get('text_primary')};
#     }}
#     QMenuBar#displayMenuBar::item:pressed {{
#         background-color: {selection_bg};
#         color: {selection_text};
#     }}
#     QFrame#progressHero {{
#         background-color: {c.get('bg_secondary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 12px;
#     }}
#     QWidget#dialogInfoStrip, QFrame#dialogInfoStrip {{
#         background-color: {c.get('bg_input')};
#     }}
#     QLabel#progressStatus {{
#         color: {c.get('text_secondary')};
#         font-size: 9pt;
#     }}
#     QLabel#progressMetric {{
#         color: {c.get('accent')};
#         font-size: 9.5pt;
#         font-weight: 700;
#     }}
#     QProgressBar#loadingProgressBar {{
#         border: 1px solid {c.get('border_light')};
#         border-radius: 11px;
#         background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         min-height: 22px;
#         text-align: center;
#         font-weight: 700;
#     }}
#     QProgressBar#loadingProgressBar::chunk {{
#         border-radius: 10px;
#         background: qlineargradient(
#             x1:0, y1:0, x2:1, y2:0,
#             stop:0 {progress_start},
#             stop:1 {progress_end}
#         );
#     }}
#     QGroupBox {{
#         border: 1px solid {c.get('border_light')};
#         border-radius: 8px;
#         margin-top: 12px;
#         padding: 12px 10px 10px 10px;
#         color: {c.get('text_primary')};
#         font-weight: 700;
#     }}
#     QGroupBox::title {{
#         subcontrol-origin: margin;
#         left: 10px;
#         padding: 0 5px;
#     }}
#     QPushButton {{
#         background-color: {c.get('bg_button')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         border-radius: 8px;
#         padding: 7px 16px;
#         font-weight: 600;
#         min-height: 32px;
#         outline: none;
#     }}
#     QPushButton:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton:pressed {{
#         background-color: {c.get('bg_button_hover')};
#         color: {c.get('text_primary')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton:focus, QPushButton:default {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {c.get('border_active')};
#         outline: none;
#     }}
#     QToolButton:focus {{
#         outline: none;
#         border: 1px solid {primary_border};
#     }}
#     QToolButton {{
#         background-color: {c.get('bg_button')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         border-radius: 8px;
#         padding: 6px 10px;
#         outline: none;
#     }}
#     QToolButton:hover {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QToolButton:pressed {{
#         background-color: {c.get('bg_button_hover')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#primaryBtn {{
#         background-color: {primary_bg};
#         color: {primary_text};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#primaryBtn:hover {{
#         background-color: {primary_hover};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#apply_btn {{
#         background-color: {primary_bg};
#         color: {primary_text};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#apply_btn:hover {{
#         background-color: {primary_hover};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#secondaryBtn {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#     }}
#     QPushButton#secondaryBtn:hover {{
#         background-color: {c.get('bg_button')};
#         border: 1px solid {primary_border};
#     }}
#     QPushButton#dangerBtn {{
#         background-color: {c.get('danger')};
#         color: white;
#         border: 1px solid {c.get('danger_dark')};
#     }}
#     QPushButton#dangerBtn:hover {{
#         background-color: {c.get('danger_hover')};
#     }}
#     QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
#         background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 8px;
#         outline: none;
#     }}
#     QLineEdit[readOnly="true"], QTextEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_secondary')};
#     }}
#     QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
#         border: 1px solid {primary_border};
#     }}
#     QComboBox {{
#         background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 6px;
#         padding: 4px 10px;
#         outline: none;
#     }}
#     QComboBox:hover, QComboBox:focus {{
#         border: 1px solid {primary_border};
#     }}
#     QComboBox::drop-down {{
#         border: none;
#         width: 22px;
#     }}
#     QComboBox QAbstractItemView {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         selection-background-color: {selection_bg};
#         selection-color: {selection_text};
#         border: 1px solid {c.get('border')};
#         outline: none;
#     }}
#     QMenuBar {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         border-radius: 6px;
#         padding: 3px 4px;
#     }}
#     QMenuBar::item {{
#         background: transparent;
#         padding: 6px 10px;
#         margin: 1px;
#         border-radius: 4px;
#     }}
#     QMenuBar::item:selected {{
#         background-color: {c.get('bg_button_hover')};
#     }}
#     QMenu {{
#         background-color: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         padding: 4px;
#     }}
#     QMenu::item {{
#         padding: 6px 24px 6px 12px;
#         border-radius: 4px;
#     }}
#     QMenu::item:selected {{
#         background-color: {selection_bg};
#         color: {selection_text};
#     }}
#     QListWidget, QTreeWidget, QTableWidget, QTableView {{
#         background-color: {c.get('bg_secondary')};
#         alternate-background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         gridline-color: {c.get('border')};
#         border: 1px solid {c.get('border')};
#         border-radius: 6px;
#         outline: none;
#     }}
#     QListWidget::item {{
#         padding: 6px;
#         border-bottom: 1px solid {c.get('border')};
#     }}
#     QListWidget::item:selected,
#     QTreeWidget::item:selected,
#     QTableWidget::item:selected,
#     QTableView::item:selected {{
#         background-color: {selection_bg};
#         color: {selection_text};
#     }}
#     QListWidget::item:hover,
#     QTreeWidget::item:hover,
#     QTableWidget::item:hover,
#     QTableView::item:hover {{
#         background-color: {c.get('bg_button_hover')};
#     }}
#     QHeaderView::section {{
#         background-color: {c.get('bg_button')};
#         color: {c.get('text_primary')};
#         border: 1px solid {c.get('border')};
#         padding: 6px 8px;
#         font-weight: 600;
#     }}
#     QTableCornerButton::section {{
#         background-color: {c.get('bg_button')};
#         border: 1px solid {c.get('border')};
#     }}
#     QTableWidget#displayClassTable {{
#         background-color: {c.get('bg_secondary')};
#         alternate-background-color: {c.get('bg_input')};
#         border: 1px solid {c.get('border_light')};
#         border-radius: 10px;
#         gridline-color: transparent;
#         selection-background-color: {selection_bg};
#     }}
#     QTableWidget#displayClassTable::item {{
#         padding: 6px 10px;
#         border: none;
#         border-bottom: 1px solid {c.get('border_light')};
#     }}
#     QTableWidget#displayClassTable::item:selected {{
#         background-color: {selection_bg};
#         color: {selection_text};
#     }}
#     QTableWidget#displayClassTable::item:hover {{
#         background-color: {c.get('bg_button_hover')};
#     }}
#     QTableWidget#displayClassTable QHeaderView::section {{
#         background-color: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         border: none;
#         border-bottom: 1px solid {c.get('border')};
#         border-right: 1px solid {c.get('border_light')};
#         padding: 8px 10px;
#         font-weight: 700;
#     }}
#     QTableWidget#displayClassTable QTableCornerButton::section {{
#         background-color: {c.get('bg_input')};
#         border: none;
#         border-bottom: 1px solid {c.get('border')};
#         border-right: 1px solid {c.get('border_light')};
#     }}
#     QSlider::groove:horizontal {{
#         height: 4px;
#         background: {c.get('border_light')};
#         border-radius: 2px;
#     }}
#     QSlider::handle:horizontal {{
#         background: {primary_border};
#         width: 14px;
#         height: 14px;
#         margin: -5px 0;
#         border-radius: 7px;
#     }}
#     QCheckBox {{
#         color: {c.get('text_primary')};
#         spacing: 8px;
#     }}
#     QCheckBox::indicator {{
#         width: 16px;
#         height: 16px;
#         border-radius: 3px;
#         border: 1px solid {c.get('border_light')};
#         background-color: {c.get('bg_input')};
#     }}
#     QCheckBox::indicator:checked {{
#         background-color: {checkbox_checked};
#         border: 1px solid {checkbox_checked_border};
#     }}
#     QRadioButton {{
#         color: {c.get('text_primary')};
#         spacing: 8px;
#         padding: 4px;
#     }}
#     QRadioButton::indicator {{
#         width: 14px;
#         height: 14px;
#         border-radius: 7px;
#         border: 2px solid {c.get('border_light')};
#         background-color: {c.get('bg_input')};
#     }}
#     QRadioButton::indicator:checked {{
#         background-color: {primary_border};
#         border: 2px solid {primary_border};
#     }}
#     QScrollArea {{
#         border: 1px solid {c.get('border')};
#         border-radius: 6px;
#         background: {c.get('bg_primary')};
#     }}
#     QScrollBar:vertical {{
#         width: 10px;
#         background: {c.get('bg_primary')};
#     }}
#     QScrollBar::handle:vertical {{
#         background: {c.get('border_light')};
#         border-radius: 4px;
#         min-height: 20px;
#     }}
#     QScrollBar::handle:vertical:hover {{
#         background: {c.get('accent')};
#     }}
#     QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
#         height: 0;
#     }}
#     QScrollBar:horizontal {{
#         height: 10px;
#         background: {c.get('bg_primary')};
#     }}
#     QScrollBar::handle:horizontal {{
#         background: {c.get('border_light')};
#         border-radius: 4px;
#         min-width: 20px;
#     }}
#     QScrollBar::handle:horizontal:hover {{
#         background: {c.get('accent')};
#     }}
#     QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
#         width: 0;
#     }}
#     QTabWidget::pane {{
#         border: 1px solid {c.get('border')};
#         border-radius: 6px;
#         background: {c.get('bg_secondary')};
#     }}
#     QTabBar::tab {{
#         background: {c.get('bg_button')};
#         color: {c.get('text_secondary')};
#         padding: 7px 14px;
#         margin-right: 2px;
#         border-top-left-radius: 5px;
#         border-top-right-radius: 5px;
#     }}
#     QTabBar::tab:selected {{
#         background: {primary_bg};
#         color: {primary_text};
#     }}
#     QTabBar::tab:hover:!selected {{
#         background: {c.get('bg_button_hover')};
#         color: {c.get('text_primary')};
#     }}
#     """


# def get_progress_dialog_stylesheet() -> str:
#     """Theme-aware stylesheet for QProgressDialog widgets."""
#     c = ThemeColors
#     primary_bg = c.get("dialog_primary_bg")
#     primary_border = c.get("dialog_primary_border")
#     progress_start = c.get("accent") if c.is_light() else primary_border
#     progress_end = c.get("accent_hover") if c.is_light() else primary_bg
#     return f"""
#     QProgressDialog {{
#         background: {c.get('bg_secondary')};
#         color: {c.get('text_primary')};
#         min-width: 430px;
#         border: 1px solid {c.get('border_light')};
#         border-radius: 12px;
#     }}
#     QLabel {{
#         color: {c.get('text_primary')};
#         font-size: 10pt;
#         font-weight: 600;
#         padding: 10px 12px 6px 12px;
#     }}
#     QProgressBar {{
#         border: 1px solid {c.get('border_light')};
#         border-radius: 10px;
#         text-align: center;
#         background: {c.get('bg_input')};
#         color: {c.get('text_primary')};
#         min-height: 20px;
#         font-weight: 600;
#     }}
#     QProgressBar::chunk {{
#         background: qlineargradient(
#             x1:0, y1:0, x2:1, y2:0,
#             stop:0 {progress_start},
#             stop:1 {progress_end}
#         );
#         border-radius: 9px;
#     }}
#     QPushButton {{
#         background: {c.get('bg_button')};
#         color: {c.get('text_primary')};
#         padding: 7px 14px;
#         border-radius: 6px;
#         border: 1px solid {c.get('border')};
#         font-weight: 600;
#     }}
#     QPushButton:hover {{
#         background: {c.get('bg_button_hover')};
#         border-color: {primary_border};
#     }}
#     """


# def get_title_banner_style() -> str:
#     """Theme-aware style for dialog title banners."""
#     c = ThemeColors
#     return (
#         f"font-size:15px; font-weight:700; color:{c.get('text_primary')}; "
#         f"padding:4px 2px 8px 2px; background:transparent; border:none;"
#     )


# def get_file_item_row_style() -> str:
#     """Theme-aware style for file list item widget rows."""
#     c = ThemeColors
#     return (
#         f"QWidget{{background:{c.get('bg_secondary')}; "
#         f"border:1px solid {c.get('border_light')}; border-radius:10px;}}"
#         f"QWidget:hover{{background:{c.get('bg_input')}; "
#         f"border:1px solid {c.get('dialog_primary_border')};}}"
#     )


# def get_badge_style(variant: str = "success") -> str:
#     """Theme-aware badge style. Variants: 'success', 'warning', 'info'."""
#     c = ThemeColors
#     if variant == "success":
#         return (
#             f"color:{c.get('text_on_active')}; background:{c.get('success')}; "
#             f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
#         )
#     elif variant == "warning":
#         return (
#             f"color:{c.get('text_on_active')}; background:{c.get('warning')}; "
#             f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
#         )
#     else:
#         return (
#             f"color:{c.get('text_on_active')}; background:{c.get('accent')}; "
#             f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
#         )


# def get_notice_banner_style(variant: str = "info") -> str:
#     """Theme-aware notice/callout style for helper messages inside dialogs."""
#     c = ThemeColors
#     if variant == "warning":
#         bg = c.get('warning_bg')
#         fg = c.get('warning')
#         border = c.get('warning')
#     elif variant == "danger":
#         bg = c.get('danger_dark') if not ThemeColors.is_light() else "#fdecea"
#         fg = "#ffffff" if not ThemeColors.is_light() else c.get('danger')
#         border = c.get('danger')
#     elif variant == "success":
#         bg = c.get('success')
#         fg = c.get('text_on_active')
#         border = c.get('success')
#     else:
#         bg = c.get('bg_input')
#         fg = c.get('text_secondary')
#         border = c.get('border_light')

#     return (
#         f"background:{bg}; color:{fg}; border:1px solid {border}; "
#         f"border-radius:8px; padding:8px 10px; font-size:9px;"
#     )


# def get_icon_button_style(role: str = "default") -> str:
#     """Theme-aware style for small round icon buttons. Roles: default, danger, settings."""
#     c = ThemeColors
#     if role == "danger":
#         bg = c.get('danger')
#         hover = c.get('danger_hover')
#         border = c.get('danger_dark')
#     elif role == "settings":
#         bg = c.get('bg_button')
#         hover = c.get('bg_button_hover')
#         border = c.get('border_light')
#     else:
#         bg = c.get('bg_button')
#         hover = c.get('bg_button_hover')
#         border = c.get('border_light')
#     return (
#         f"QPushButton{{background:{bg}; color:{c.get('text_on_active')}; "
#         f"border:1px solid {border}; border-radius:13px; font-size:13px; outline:none;}}"
#         f"QPushButton:hover{{background:{hover}; border:1px solid {border};}}"
#     )

# def _refresh_view_ribbon_styles(ribbon):
#     """Re-apply theme styles to ViewRibbon sharpness/amplifier controls."""
#     try:
#         if hasattr(ribbon, 'sharp_value_label'):
#             ribbon.sharp_value_label.setStyleSheet("")
#     except Exception:
#         pass

# def _refresh_identify_ribbon_styles(ribbon):
#     """Re-apply theme styles to IdentificationRibbon."""
#     try:
#         if hasattr(ribbon, 'status_label'):
#             ribbon.status_label.setStyleSheet(get_status_label_style())
#         if hasattr(ribbon, 'delete_btn'):
#             ribbon.delete_btn.setStyleSheet(get_delete_btn_style())
#     except Exception:
#         pass


"""
gui/theme_manager.py
─────────────────────
Centralized theme manager for NakshaAI-Lidar.
Supports Dark and Light themes with runtime switching.
Inspired by the Ring app's dual-theme system (themes.py).
"""

import os
from PySide6.QtCore import QEvent, QObject, QSettings, Qt

try:
    from shiboken6 import isValid as _qt_object_is_valid
except ImportError:
    def _qt_object_is_valid(obj):
        return obj is not None


# ═══════════════════════════════════════════════════════════════════════════
#  THEME COLOR PALETTES
# ═══════════════════════════════════════════════════════════════════════════

class ThemeColors:
    """Theme-aware color palette for inline style usage."""

    # ── Dark palette ────────────────────────────────────────────────
    DARK = {
        # Base
        "bg_primary":       "#121212",
        "bg_secondary":     "#1a1a1a",
        "bg_tertiary":      "#202020",
        "bg_input":         "#1e1e1e",
        "bg_button":        "#2a2a2a",
        "bg_button_hover":  "#3c3c3c",
        "bg_active":        "#004d40",
        "bg_active_border": "#1b5e20",

        # Text
        "text_primary":     "#e0e0e0",
        "text_secondary":   "#aaaaaa",
        "text_muted":       "#888888",
        "text_on_active":   "#ffffff",

        # Global icon fallback
        "icon_primary":     "#d3d1c7",

        # Per-ribbon / per-group icon colors
        "icon_file":        "#4FC3F7",
        "icon_edit":        "#FFB74D",
        "icon_view":        "#81C784",
        "icon_tools":       "#BA68C8",
        "icon_classify":    "#FFD54F",
        "icon_measure":     "#4DD0E1",
        "icon_identify":    "#E57373",
        "icon_by_class":    "#AED581",
        "icon_draw":        "#F48FB1",
        "icon_curve":       "#90CAF9",
        "icon_ai":          "#FF8A65",
        "icon_display":     "#4db6ac",
        "icon_settings":    "#B0BEC5",
        "icon_project":     "#A5D6A7",
        "icon_history":     "#FFCC80",
        "icon_clipboard":   "#80CBC4",
        "icon_attachments": "#CE93D8",
        "icon_vectors":     "#80DEEA",
        "icon_dxf":         "#F48FB1",
        "icon_dwg":         "#9FA8DA",
        "icon_snt":         "#A5D6A7",

        # Individual ribbon icon colors: File
        "icon_open":          "#42a5f5",
        "icon_save":          "#26a69a",
        "icon_save_as":       "#7e57c2",
        "icon_export":        "#ffb300",
        "icon_import":        "#ef5350",
        "icon_attach_dxf":    "#29b6f6",
        "icon_prj_loader":    "#8d6e63",
        "icon_verify_labels": "#66bb6a",
        "icon_attach_dwg":    "#5c6bc0",
        "icon_attach_snt":    "#26c6da",
        "icon_clear":         "#ec407a",
        "icon_clear_data":    "#ff1744",

        # Accent
        "accent":            "#007acc",
        "accent_hover":      "#0098ff",
        "accent_alt":        "#ffaa00",
        "dialog_primary_bg": "#30363c",
        "dialog_primary_hover": "#394148",
        "dialog_primary_border": "#505c66",
        "dialog_primary_text": "#f5f7fa",
        "dialog_selection":  "#2b3138",

        # Borders
        "border":            "#2a2a2a",
        "border_light":      "#3a3a3a",
        "border_active":     "#007acc",

        # Status
        "danger":            "#d32f2f",
        "danger_hover":      "#f44336",
        "danger_dark":       "#b71c1c",
        "warning":           "#ff6f00",
        "warning_bg":        "#e65100",
        "success":           "#41613d",

        # Ribbon
        "ribbon_bg":         "#1e1e1c",
        "ribbon_section":    "#171715",
        "ribbon_button":     "#222220",
        "ribbon_btn_hover":  "#2a2a28",
        "ribbon_btn_pressed":"#31312e",
        "ribbon_btn_border": "#2e2e2b",
        "ribbon_btn_checked":"#2b3540",
        "ribbon_btn_checked_border":"#556675",
        "ribbon_title":      "#4a4a47",

        # Misc
        "shadow":            "#000000",

        # ── Individual icon colors: Edit ─────────────────────────────
        "icon_undo":                 "#ffb74d",
        "icon_redo":                 "#ff8a65",
        "icon_cut":                  "#4fc3f7",
        "icon_copy":                 "#81c784",
        "icon_paste":                "#ba68c8",
        "icon_delete":               "#ef5350",

        # ── Individual icon colors: View ─────────────────────────────
        "icon_top":                  "#64b5f6",
        "icon_front":                "#4db6ac",
        "icon_side":                 "#9575cd",
        "icon_3d":                   "#ffd54f",
        "icon_depth":                "#90a4ae",
        "icon_rgb":                  "#ff7043",
        "icon_intensity":            "#4dd0e1",
        "icon_elevation":            "#aed581",
        "icon_class":                "#f06292",
        "icon_shading":              "#a1887f",
        "icon_fit":                  "#7986cb",

        # ── Individual icon colors: Tools ────────────────────────────
        "icon_cross":                "#26a69a",
        "icon_cut_section":          "#ef5350",
        "icon_width":                "#5c6bc0",
        "icon_views":                "#29b6f6",
        "icon_config":               "#ab47bc",
        "icon_backup":               "#66bb6a",
        "icon_preferences":          "#ffa726",

        # ── Individual icon colors: Classify ─────────────────────────
        "icon_above":                "#42a5f5",
        "icon_below":                "#ffca28",
        "icon_classify_rect":        "#26c6da",
        "icon_classify_circle":      "#7e57c2",
        "icon_classify_free":        "#ec407a",
        "icon_brush":                "#8d6e63",
        "icon_point":                "#d4e157",

        # ── Individual icon colors: Display ──────────────────────────
        "icon_measure_line":         "#26a69a",
        "icon_measure_path":         "#42a5f5",
        "icon_measure_clear":        "#ec407a",
        "icon_area_block":           "#26c6da",

        # ── Individual icon colors: Identify ─────────────────────────
        "icon_identify":             "#ffb300",
        "icon_zoom":                 "#66bb6a",
        "icon_select":               "#90caf9",

        # ── Individual icon colors: By Class ─────────────────────────
        "icon_convert":              "#4fc3f7",
        "icon_close":                "#9575cd",
        "icon_height":               "#ffb74d",
        "icon_fence":                "#81c784",
        "icon_low_points":           "#ef5350",
        "icon_isolated":             "#26c6da",
        "icon_ground":               "#a1887f",
        "icon_surface":              "#ffd54f",

        # ── Individual icon colors: Draw ─────────────────────────────
        "icon_smart_line":           "#26a69a",
        "icon_line":                 "#64b5f6",
        "icon_draw_rect":            "#ab47bc",
        "icon_draw_circle":          "#ffca28",
        "icon_draw_free":            "#ec407a",
        "icon_text":                 "#90a4ae",
        "icon_move_vertex":          "#4dd0e1",
        "icon_vertex":               "#81c784",
        "icon_grid":                 "#7986cb",
        "icon_select_drawing":       "#ffb300",
        "icon_deselect_all":         "#ef5350",
        "icon_draw_clear":           "#ff1744",
        "icon_draw_settings":        "#b0bec5",

        # ── Individual icon colors: Curve ────────────────────────────
        "icon_curve_point":          "#f06292",
        "icon_curve_clear":          "#ec407a",

        # ── Individual icon colors: AI ───────────────────────────────
        "icon_start":                "#ff7043",
        "icon_create_block":         "#00e5ff",
    }

    # ── Light palette ──────────────────────────────────────────────
    LIGHT = {
        # Base
        "bg_primary":       "#f0f2f5",
        "bg_secondary":     "#ffffff",
        "bg_tertiary":      "#e8ecf0",
        "bg_input":         "#f7f8fa",
        "bg_button":        "#eef0f4",
        "bg_button_hover":  "#e0e4ea",
        "bg_active":        "#0077b6",
        "bg_active_border": "#005f8a",

        # Text
        "text_primary":     "#1b2838",
        "text_secondary":   "#607d8b",
        "text_muted":       "#90a4ae",
        "text_on_active":   "#1c1c1a",

        # Global icon fallback
        "icon_primary":     "#1c1c1a",

        # Per-ribbon / per-group icon colors
        "icon_file":        "#1565C0",
        "icon_edit":        "#EF6C00",
        "icon_view":        "#2E7D32",
        "icon_tools":       "#6A1B9A",
        "icon_classify":    "#F9A825",
        "icon_measure":     "#00838F",
        "icon_identify":    "#C62828",
        "icon_by_class":    "#558B2F",
        "icon_draw":        "#AD1457",
        "icon_curve":       "#1976D2",
        "icon_ai":          "#D84315",
        "icon_display":     "#00897b",
        "icon_settings":    "#546E7A",
        "icon_project":     "#2E7D32",
        "icon_history":     "#FB8C00",
        "icon_clipboard":   "#00897B",
        "icon_attachments": "#8E24AA",
        "icon_vectors":     "#00ACC1",
        "icon_dxf":         "#C2185B",
        "icon_dwg":         "#3949AB",
        "icon_snt":         "#43A047",

        # Individual ribbon icon colors: File
        "icon_open":          "#1976d2",
        "icon_save":          "#00897b",
        "icon_save_as":       "#5e35b1",
        "icon_export":        "#fb8c00",
        "icon_import":        "#e53935",
        "icon_attach_dxf":    "#039be5",
        "icon_prj_loader":    "#795548",
        "icon_verify_labels": "#43a047",
        "icon_attach_dwg":    "#3949ab",
        "icon_attach_snt":    "#00acc1",
        "icon_clear":         "#d81b60",
        "icon_clear_data":    "#d50000",

        # Accent
        "accent":            "#0077b6",
        "accent_hover":      "#00b4d8",
        "accent_alt":        "#0096c7",
        "dialog_primary_bg": "#e7ebef",
        "dialog_primary_hover": "#dde3e9",
        "dialog_primary_border": "#c3ccd5",
        "dialog_primary_text": "#26313a",
        "dialog_selection":  "#edf1f4",

        # Borders
        "border":            "#d0d5dd",
        "border_light":      "#e0e4ea",
        "border_active":     "#00b4d8",

        # Status
        "danger":            "#d32f2f",
        "danger_hover":      "#f44336",
        "danger_dark":       "#b71c1c",
        "warning":           "#ff6f00",
        "warning_bg":        "#fff3e0",
        "success":           "#2e7d32",

        # Ribbon
        "ribbon_bg":         "#f4f2ec",
        "ribbon_section":    "#f0efea",
        "ribbon_button":     "#ffffff",
        "ribbon_btn_hover":  "#fbfaf7",
        "ribbon_btn_pressed":"#ece8df",
        "ribbon_btn_border": "#d6d1c7",
        "ribbon_btn_checked":"#e6edf3",
        "ribbon_btn_checked_border":"#c0cdda",
        "ribbon_title":      "#a09f99",

        # Misc
        "shadow":            "#b0bec5",

        # ── Individual icon colors: Edit ─────────────────────────────
        "icon_undo":                 "#fb8c00",
        "icon_redo":                 "#f4511e",
        "icon_cut":                  "#039be5",
        "icon_copy":                 "#43a047",
        "icon_paste":                "#8e24aa",
        "icon_delete":               "#d32f2f",

        # ── Individual icon colors: View ─────────────────────────────
        "icon_top":                  "#1e88e5",
        "icon_front":                "#00897b",
        "icon_side":                 "#5e35b1",
        "icon_3d":                   "#f9a825",
        "icon_depth":                "#607d8b",
        "icon_rgb":                  "#f4511e",
        "icon_intensity":            "#00acc1",
        "icon_elevation":            "#7cb342",
        "icon_class":                "#d81b60",
        "icon_shading":              "#8d6e63",
        "icon_fit":                  "#3949ab",

        # ── Individual icon colors: Tools ────────────────────────────
        "icon_cross":                "#00897b",
        "icon_cut_section":          "#e53935",
        "icon_width":                "#3949ab",
        "icon_views":                "#039be5",
        "icon_config":               "#8e24aa",
        "icon_backup":               "#43a047",
        "icon_preferences":          "#fb8c00",

        # ── Individual icon colors: Classify ─────────────────────────
        "icon_above":                "#1e88e5",
        "icon_below":                "#fbc02d",
        "icon_classify_rect":        "#00acc1",
        "icon_classify_circle":      "#5e35b1",
        "icon_classify_free":        "#d81b60",
        "icon_brush":                "#6d4c41",
        "icon_point":                "#9e9d24",

        # ── Individual icon colors: Display / Measure ────────────────
        "icon_measure_line":         "#00897b",
        "icon_measure_path":         "#1e88e5",
        "icon_measure_clear":        "#d81b60",
        "icon_area_block":           "#00acc1",

        # ── Individual icon colors: Identify ─────────────────────────
        "icon_identify":             "#fb8c00",
        "icon_zoom":                 "#43a047",
        "icon_select":               "#1e88e5",

        # ── Individual icon colors: By Class ─────────────────────────
        "icon_convert":              "#039be5",
        "icon_close":                "#5e35b1",
        "icon_height":               "#fb8c00",
        "icon_fence":                "#43a047",
        "icon_low_points":           "#e53935",
        "icon_isolated":             "#00acc1",
        "icon_ground":               "#8d6e63",
        "icon_surface":              "#f9a825",

        # ── Individual icon colors: Draw ─────────────────────────────
        "icon_smart_line":           "#00897b",
        "icon_line":                 "#1e88e5",
        "icon_draw_rect":            "#8e24aa",
        "icon_draw_circle":          "#fbc02d",
        "icon_draw_free":            "#d81b60",
        "icon_text":                 "#78909c",
        "icon_move_vertex":          "#00acc1",
        "icon_vertex":               "#43a047",
        "icon_grid":                 "#3949ab",
        "icon_select_drawing":       "#fb8c00",
        "icon_deselect_all":         "#e53935",
        "icon_draw_clear":           "#d50000",
        "icon_draw_settings":        "#546e7a",

        # ── Individual icon colors: Curve ────────────────────────────
        "icon_curve_point":          "#d81b60",
        "icon_curve_clear":          "#d81b60",

        # ── Individual icon colors: AI ───────────────────────────────
        "icon_start":                "#f4511e",
        "icon_create_block":         "#00838f",
    }

    _current = "dark"

    @classmethod
    def set_theme(cls, name: str):
        cls._current = name.lower()

    @classmethod
    def get(cls, key: str, default: str = None) -> str:
        palette = cls.LIGHT if cls._current == "light" else cls.DARK
        return palette.get(key, default if default is not None else "#ff00ff")

    @classmethod
    def current_theme(cls) -> str:
        return cls._current

    @classmethod
    def is_light(cls) -> bool:
        return cls._current == "light"


# ═══════════════════════════════════════════════════════════════════════════
#  RIBBON BUTTON STYLE HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_active_button_style() -> str:
    """Return inline style for an ACTIVE (toggled-on) ribbon button."""
    c = ThemeColors
    return f"""
    QPushButton {{
        background-color: {c.get('ribbon_btn_checked')};
        color: {c.get('text_primary')};
        font-weight: 600;
        font-size: 10px;
        border: 1px solid {c.get('ribbon_btn_checked_border')};
        border-radius: 10px;
        padding: 4px 6px 8px 6px;
        text-align: center;
    }}
    QPushButton:hover {{
        background-color: {c.get('ribbon_btn_checked')};
    }}
    """


def get_inactive_button_style() -> str:
    """Return inline style for an INACTIVE ribbon button."""
    c = ThemeColors
    return f"""
    QPushButton {{
        background-color: {c.get('ribbon_button')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('ribbon_btn_border')};
        border-radius: 10px;
        font-size: 10px;
        font-weight: 500;
        padding: 4px 6px 8px 6px;
        text-align: center;
    }}
    QPushButton:hover {{
        background-color: {c.get('ribbon_btn_hover')};
        border: 1px solid {c.get('ribbon_btn_border')};
    }}
    """


def get_ribbon_button_text_style(active: bool = False) -> str:
    """Return inline style for ribbon button text under icons."""
    c = ThemeColors
    bg = c.get("ribbon_btn_checked") if active else c.get("ribbon_button")
    border = c.get("ribbon_btn_checked_border") if active else c.get("ribbon_btn_border")

    return f"""
    QPushButton {{
        background-color: {bg};
        color: {c.get('text_primary')};
        border: 1px solid {border};
        border-radius: 10px;
        font-size: 10px;
        font-weight: 500;
        padding: 4px 6px 8px 6px;
        text-align: center;
    }}
    QPushButton:hover {{
        background-color: {c.get('ribbon_btn_checked') if active else c.get('ribbon_btn_hover')};
        border: 1px solid {border};
    }}
    """


def get_status_label_style() -> str:
    """Return style for identification / info status labels."""
    c = ThemeColors
    return f"""
    QLabel {{
        color: {c.get('text_secondary')};
        font-size: 11px;
        padding: 4px;
        background-color: {c.get('ribbon_button')};
        border-radius: 3px;
    }}
    """


def get_status_label_active_style(color_key="warning") -> str:
    """Return style for an active status label (e.g. select mode)."""
    c = ThemeColors
    if color_key == "warning":
        return f"""
        QLabel {{
            color: #ffb74d;
            font-size: 11px;
            padding: 4px;
            background-color: {c.get('warning_bg')};
            border-radius: 3px;
        }}
        """
    return get_status_label_style()


def get_sharpness_btn_style(variant="minus") -> str:
    """Return style for sharpness +/- buttons."""
    c = ThemeColors
    if variant == "minus":
        return f"""
        QPushButton {{
            background-color: {c.get('bg_input')};
            color: {c.get('text_primary')};
            border: 2px solid {c.get('border_light')};
            border-radius: 4px;
            font-size: 20px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: {c.get('bg_button_hover')};
            border: 2px solid {c.get('accent')};
        }}
        QPushButton:pressed {{
            background-color: {c.get('accent')};
            color: {c.get('text_on_active')};
            border: 2px solid {c.get('accent')};
        }}
        """
    else:  # plus
        return f"""
        QPushButton {{
            background-color: {c.get('bg_button')};
            color: {c.get('accent')};
            border: 2px solid {c.get('border_light')};
            border-radius: 5px;
            font-size: 18px;
            font-weight: bold;
        }}
        QPushButton:hover {{
            background-color: {c.get('bg_button_hover')};
            border: 2px solid {c.get('accent')};
        }}
        QPushButton:pressed {{
            background-color: {c.get('accent')};
            color: {c.get('text_on_active')};
        }}
        """


def get_sharpness_value_style() -> str:
    """Return style for the sharpness value display label."""
    c = ThemeColors
    return f"""
    QLabel {{
        color: {c.get('text_primary')};
        font-size: 12px;
        font-weight: 700;
        background-color: {c.get('bg_input')};
        border: 1px solid {c.get('border_light')};
        border-radius: 12px;
        padding: 0px 10px;
    }}
    """


def get_reset_btn_style() -> str:
    """Return style for the amplifier reset button."""
    c = ThemeColors
    return f"""
    QPushButton {{
        background-color: {c.get('success')};
        color: {c.get('text_on_active')};
        border: 1px solid {c.get('border')};
        border-radius: 3px;
        font-size: 11px;
        font-weight: bold;
        padding: 2px;
    }}
    QPushButton:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {c.get('accent_alt')};
        color: {c.get('text_primary')};
    }}
    QPushButton:pressed {{
        background-color: {c.get('accent_alt')};
        color: {c.get('text_primary')};
    }}
    """


def get_delete_btn_style() -> str:
    """Return style for the delete selected points button."""
    c = ThemeColors
    return f"""
    QPushButton {{
        background-color: {c.get('danger')};
        color: white;
        font-size: 11px;
        font-weight: bold;
        border: 1px solid {c.get('danger_dark')};
        border-radius: 4px;
        padding: 4px;
    }}
    QPushButton:hover {{
        background-color: {c.get('danger_hover')};
        border: 1px solid {c.get('danger')};
    }}
    QPushButton:pressed {{
        background-color: {c.get('danger_dark')};
    }}
    """


def get_select_active_btn_style() -> str:
    """Return style for active select/tool buttons."""
    c = ThemeColors
    return f"""
    QPushButton {{
        background-color: {c.get('warning')};
        color: white;
        font-weight: bold;
        border: 1px solid {c.get('warning_bg')};
        border-radius: 6px;
    }}
    """


def get_sharp_label_style() -> str:
    """Style for the 'Sharp:' label in ViewRibbon."""
    c = ThemeColors
    return f"color: {c.get('text_primary')}; font-size: 12px; font-weight: bold;"


class _ThemeWindowEventFilter(QObject):
    """Applies the active theme when top-level widgets are shown."""

    def eventFilter(self, obj, event):
        try:
            if event.type() != QEvent.Show:
                return False

            from PySide6.QtWidgets import QWidget

            if not isinstance(obj, QWidget):
                return False
            if not _qt_object_is_valid(obj):
                return False
            if not obj.isWindow():
                return False
            if not obj.isVisible():
                return False
            if not obj.testAttribute(Qt.WA_WState_Created):
                return False

            ThemeManager._refresh_widget_theme(obj)
        except Exception:
            pass
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  THEME MANAGER
# ═══════════════════════════════════════════════════════════════════════════

class ThemeManager:
    """Manages loading and switching themes."""

    _current_theme = "dark"
    _window_event_filter = None

    @classmethod
    def apply_theme(cls, app_window, theme_name: str = "dark"):
        """Apply a theme to the main application window."""
        theme_name = theme_name.lower()
        cls._current_theme = theme_name
        ThemeColors.set_theme(theme_name)
        cls._ensure_window_event_filter()

        gui_dir = os.path.dirname(__file__)

        if theme_name == "light":
            qss_path = os.path.join(gui_dir, "theme_light.qss")
        else:
            qss_path = os.path.join(gui_dir, "theme.qss")

        try:
            with open(qss_path, "r", encoding="utf-8") as f:
                qss = f.read()

            app_window.setStyleSheet(qss)

            try:
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance()
                if app:
                    app.setStyleSheet(qss)
            except Exception as e:
                print(f"⚠️ Could not apply theme to QApplication globally: {e}")

            print(f"🎨 {theme_name.capitalize()} theme loaded successfully")
        except Exception as e:
            print(f"⚠️ Failed to apply {theme_name} theme: {e}")

        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                for widget in app.topLevelWidgets():
                    cls._refresh_widget_theme(widget)
        except Exception as e:
            print(f"⚠️ Failed to refresh themed dialogs: {e}")

        cls._refresh_open_windows()
        cls.refresh_runtime_surfaces(app_window)
        cls._refresh_ribbon_styles(app_window)

        if hasattr(app_window, "_refresh_activity_bar_theme"):
            try:
                app_window._refresh_activity_bar_theme()
            except Exception as e:
                print(f"⚠️ Failed to refresh activity bar theme: {e}")

        settings = QSettings("NakshaAI", "LidarApp")
        settings.setValue("ui_theme", theme_name)

    @classmethod
    def toggle_theme(cls, app_window):
        """Toggle between light and dark themes."""
        new_theme = "light" if cls._current_theme == "dark" else "dark"
        cls.apply_theme(app_window, new_theme)

    @classmethod
    def current(cls) -> str:
        return cls._current_theme

    @classmethod
    def load_saved_theme(cls) -> str:
        """Load the saved theme preference, default to dark."""
        settings = QSettings("NakshaAI", "LidarApp")
        return settings.value("ui_theme", "dark")

    @classmethod
    def canvas_background_for_theme(cls, theme_name: str = None) -> str:
        """Return the preferred canvas background for a theme."""
        settings = QSettings("NakshaAI", "LidarApp")
        background = settings.value("ui_canvas_background", None)
        if background:
            return str(background).lower()

        theme_name = (theme_name or cls.current() or cls.load_saved_theme()).lower()
        if theme_name == "light":
            return str(settings.value("ui_canvas_light", "white")).lower()
        return str(settings.value("ui_canvas_dark", "black")).lower()

    @classmethod
    def canvas_background_rgb(cls, theme_name: str = None):
        """Return normalized RGB for the preferred canvas background."""
        bg_name = cls.canvas_background_for_theme(theme_name)
        if bg_name == "black":
            return (0.0, 0.0, 0.0)
        return (1.0, 1.0, 1.0)

    @classmethod
    def _ensure_window_event_filter(cls):
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None or cls._window_event_filter is not None:
                return

            cls._window_event_filter = _ThemeWindowEventFilter()
            app.installEventFilter(cls._window_event_filter)
        except Exception as e:
            print(f"⚠️ Failed to install theme window filter: {e}")

    @classmethod
    def _refresh_open_windows(cls):
        try:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
            if not app:
                return

            for widget in app.topLevelWidgets():
                cls._refresh_widget_theme(widget)
        except Exception as e:
            print(f"⚠️ Failed to refresh themed windows: {e}")

    @classmethod
    def _refresh_widget_theme(cls, widget):
        try:
            from PySide6.QtWidgets import QDialog, QDockWidget, QProgressDialog, QWidget

            if widget is None or not _qt_object_is_valid(widget):
                return
            if not isinstance(widget, QWidget):
                return
            if not widget.testAttribute(Qt.WA_WState_Created):
                return
            if widget.isWindow() and not widget.isVisible():
                return

            if widget.isWindow():
                cls.apply_native_window_theme(widget)

            if isinstance(widget, QProgressDialog):
                widget.setStyleSheet(get_progress_dialog_stylesheet())
            elif isinstance(widget, QDialog) and widget.property("themeStyledDialog"):
                refresh_theme = getattr(widget, "refresh_theme", None)
                if callable(refresh_theme):
                    refresh_theme()
                else:
                    widget.setStyleSheet(get_dialog_stylesheet())
            elif widget.property("themeStyledWindow"):
                refresh_theme = getattr(widget, "refresh_theme", None)
                if callable(refresh_theme):
                    refresh_theme()
                else:
                    widget.setStyleSheet(get_dialog_stylesheet())
            elif isinstance(widget, QDockWidget):
                style = widget.style()
                if style is not None:
                    style.unpolish(widget)
                    style.polish(widget)
                widget.update()
        except Exception as e:
            print(f"⚠️ Failed to refresh widget theme: {e}")

    @classmethod
    def apply_native_window_theme(cls, widget):
        """Use Windows immersive dark mode for native title bars when available."""
        if os.name != "nt" or widget is None or not _qt_object_is_valid(widget):
            return
        if hasattr(widget, "testAttribute") and not widget.testAttribute(Qt.WA_WState_Created):
            return
        if hasattr(widget, "isVisible") and not widget.isVisible():
            return

        try:
            import ctypes

            hwnd = int(widget.winId())
            if not hwnd:
                return

            value = ctypes.c_int(1 if cls.current() == "dark" else 0)
            dwmapi = ctypes.windll.dwmapi

            for attribute in (20, 19):
                result = dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_uint(attribute),
                    ctypes.byref(value),
                    ctypes.sizeof(value),
                )
                if result == 0:
                    break
        except Exception:
            pass

    @classmethod
    def refresh_runtime_surfaces(cls, app_window):
        """Refresh all currently open VTK canvases to match the active theme."""
        if app_window is None:
            return

        for vtk_widget in cls._iter_vtk_widgets(app_window):
            cls._apply_vtk_widget_theme(vtk_widget)
        try:
            from gui.scene_render_pipeline import sync_scene_background
            sync_scene_background(app_window)
        except Exception:
            pass

    @classmethod
    def _iter_vtk_widgets(cls, app_window):
        candidates = []

        for attr_name in ("vtk_widget", "sec_vtk"):
            widget = getattr(app_window, attr_name, None)
            if widget is not None:
                candidates.append(widget)

        section_vtks = getattr(app_window, "section_vtks", None)
        if isinstance(section_vtks, dict):
            candidates.extend(section_vtks.values())

        cut_controller = getattr(app_window, "cut_section_controller", None)
        if cut_controller is not None:
            cut_vtk = getattr(cut_controller, "cut_vtk", None)
            if cut_vtk is not None:
                candidates.append(cut_vtk)

        seen = set()
        for widget in candidates:
            if widget is None or id(widget) in seen:
                continue
            seen.add(id(widget))
            yield widget

    @classmethod
    def _apply_vtk_widget_theme(cls, vtk_widget):
        bg_color = cls.canvas_background_for_theme()
        rgb = cls.canvas_background_rgb()

        try:
            vtk_widget.set_background(bg_color)
        except Exception:
            pass

        renderer = getattr(vtk_widget, "renderer", None)
        if renderer is not None:
            try:
                renderer.SetBackground(*rgb)
                renderer.SetBackground2(*rgb)
                renderer.GradientBackgroundOff()
            except Exception:
                pass

        try:
            vtk_widget.render()
        except Exception:
            pass

    @classmethod
    def _refresh_ribbon_styles(cls, app_window):
        """Re-apply theme-aware inline styles and icon colors on ribbon sections & buttons."""
        try:
            if not hasattr(app_window, 'ribbon_manager'):
                return

            from gui.menu_sidebar_system import RibbonSection
            from gui.icon_provider import get_button_icon
            from PySide6.QtWidgets import QPushButton
            from PySide6.QtCore import QSize

            for name, ribbon in app_window.ribbon_manager.ribbons.items():
                for section in ribbon.findChildren(RibbonSection):
                    if section.active_button:
                        section.active_button.setStyleSheet(get_active_button_style())

                    for btn in section.findChildren(QPushButton):
                        btn_text = btn.property("ribbonText")
                        section_title = btn.property("ribbonSection") or getattr(section, "section_title", None)
                        ribbon_scope = btn.property("ribbonScope") or getattr(section, "ribbon_scope", None)

                        if not btn_text:
                            tooltip = btn.toolTip()
                            if tooltip.startswith("<b>") and tooltip.endswith("</b>"):
                                btn_text = tooltip[3:-4]

                        try:
                            if section.active_button is not None and btn is section.active_button:
                                btn.setStyleSheet(get_active_button_style())
                            else:
                                btn.setStyleSheet(get_inactive_button_style())
                        except Exception:
                            pass

                        if btn_text:
                            icon = get_button_icon(
                                btn_text,
                                section_title=section_title,
                                ribbon_scope=ribbon_scope,
                                color=None,
                                size=24,
                            )
                            if not icon.isNull():
                                btn.setIcon(icon)
                                btn.setIconSize(QSize(24, 24))

                if name == "view":
                    _refresh_view_ribbon_styles(ribbon)

                if name == "identify":
                    _refresh_identify_ribbon_styles(ribbon)

        except Exception as e:
            print(f"Failed to refresh ribbon styles: {e}")


def get_dialog_stylesheet() -> str:
    """
    Return a comprehensive theme-aware stylesheet for QDialog and its child widgets.
    Use this in all popup dialogs to ensure consistent theming.
    """
    c = ThemeColors
    primary_bg = c.get("dialog_primary_bg")
    primary_hover = c.get("dialog_primary_hover")
    primary_border = c.get("dialog_primary_border")
    primary_text = c.get("dialog_primary_text")
    selection_bg = c.get("dialog_selection")
    selection_text = c.get("text_primary") if c.is_light() else c.get("text_on_active")
    progress_start = c.get("accent") if c.is_light() else primary_border
    progress_end = c.get("accent_hover") if c.is_light() else c.get("accent")
    checkbox_checked = c.get("accent") if c.is_light() else primary_border
    checkbox_checked_border = c.get("bg_active_border") if c.is_light() else primary_border
    return f"""
    QDialog, QMessageBox {{
        background-color: {c.get('bg_primary')};
        color: {c.get('text_primary')};
        font-family: "SF Pro Text", "Helvetica Neue", "Segoe UI", Arial;
    }}
    QLabel {{
        color: {c.get('text_primary')};
        background: transparent;
    }}
    QLabel#dialogSectionLabel {{
        color: {c.get('text_primary')};
        font-weight: 600;
    }}
    QLabel#dialogTitle {{
        color: {c.get('text_primary')};
        font-size: 16pt;
        font-weight: 700;
    }}
    QLabel#dialogSubtitle {{
        color: {c.get('text_secondary')};
        font-size: 9.5pt;
    }}
    QLabel#dialogCaption {{
        color: {c.get('text_secondary')};
        font-size: 8.5pt;
    }}
    QLabel#dialogHintLabel {{
        color: {c.get('text_secondary')};
        font-size: 9pt;
        padding: 8px;
    }}
    QLabel#dialogInlineNote {{
        color: {c.get('text_secondary')};
        font-size: 9pt;
    }}
    QLabel#displayBorderLabel {{
        color: {c.get('text_secondary')};
        font-weight: 600;
        font-size: 8.8pt;
    }}
    QLabel#displayBorderValuePill {{
        background: transparent;
        color: {c.get('text_primary')};
        border: none;
        padding: 0px 2px;
        font-weight: 600;
        font-size: 9pt;
    }}
    QLabel#valuePill {{
        background-color: {c.get('bg_input')};
 
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 8px;
        padding: 5px 10px;
        font-weight: 600;
    }}
    QWidget#dialogHero, QFrame#dialogHero,
    QWidget#dialogCard, QFrame#dialogCard,
    QWidget#dialogInfoStrip, QFrame#dialogInfoStrip {{
        background-color: {c.get('bg_secondary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 10px;
    }}
    QFrame#displayControlsCard, QFrame#displayTableCard {{
        background-color: {c.get('bg_secondary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 12px;
    }}
    QFrame#displayBorderStrip {{
        background-color: {c.get('bg_secondary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 8px;
    }}
    QPushButton#displayBorderButton {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 6px;
        font-size: 9pt;
        font-weight: 600;
        min-height: 0px;
    }}
    QPushButton#displayBorderButton:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton#displayBorderButton:pressed {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QToolButton#displayFileButton {{
        background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 20px 4px 10px;
        font-weight: 500;
    }}
    QToolButton#displayFileButton:hover,
    QToolButton#displayFileButton:focus {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QToolButton#displayFileButton:pressed {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QToolButton#displayFileButton::menu-indicator {{
        subcontrol-origin: padding;
        subcontrol-position: right center;
        right: 6px;
    }}
    QPushButton#displayApplyBtn {{
        background-color: {primary_bg};
        color: {primary_text};
        border: 1px solid {primary_border};
        border-radius: 6px;
        padding: 4px 14px;
        font-weight: 600;
        min-height: 0px;
    }}
    QPushButton#displayApplyBtn:hover {{
        background-color: {primary_hover};
        border: 1px solid {primary_border};
    }}
    QPushButton#displayApplyBtn:pressed {{
        background-color: {primary_hover};
    }}
    QPushButton#displayCloseBtn {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 14px;
        font-weight: 600;
        min-height: 0px;
    }}
    QPushButton#displayCloseBtn:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton#displayCloseBtn:pressed {{
        background-color: {c.get('bg_button_hover')};
    }}
    QFrame#displayActionRail {{
        background: transparent;
        border: none;
    }}
    QPushButton#displayActionButton {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 10px;
        font-weight: 500;
        min-height: 0px;
        text-align: center;
    }}
    QPushButton#displayActionButton:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton#displayActionButton:pressed {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton#add_btn {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        border-radius: 10px;
        padding: 8px 12px;
        font-weight: 600;
        text-align: center;
    }}
    QPushButton#add_btn:hover {{
        background-color: #1a3d1a;
        border: 1px solid #39ff14;
        color: #39ff14;
    }}
    QPushButton#add_btn:pressed {{
        background-color: #0d200d;
    }}
    QPushButton#del_btn {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        border-radius: 10px;
        padding: 8px 12px;
        font-weight: 600;
        text-align: center;
    }}
    QPushButton#del_btn:hover {{
        background-color: #3d1a1a;
        border: 1px solid #ff4444;
        color: #ff4444;
    }}
    QPushButton#del_btn:pressed {{
        background-color: #200d0d;
    }}
    QMenuBar#displayMenuBar {{
        background: transparent;
        color: {c.get('text_primary')};
        border: none;
        padding: 0px;
    }}
    QMenuBar#displayMenuBar::item {{
        background: transparent;
        padding: 5px 8px;
        margin: 0px 2px 0px 0px;
        border-radius: 4px;
    }}
    QMenuBar#displayMenuBar::item:selected {{
        background-color: {c.get('bg_button_hover')};
        color: {c.get('text_primary')};
    }}
    QMenuBar#displayMenuBar::item:pressed {{
        background-color: {selection_bg};
        color: {selection_text};
    }}
    QFrame#progressHero {{
        background-color: {c.get('bg_secondary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 12px;
    }}
    QWidget#dialogInfoStrip, QFrame#dialogInfoStrip {{
        background-color: {c.get('bg_input')};
    }}
    QLabel#progressStatus {{
        color: {c.get('text_secondary')};
        font-size: 9pt;
    }}
    QLabel#progressMetric {{
        color: {c.get('accent')};
        font-size: 9.5pt;
        font-weight: 700;
    }}
    QProgressBar#loadingProgressBar {{
        border: 1px solid {c.get('border_light')};
        border-radius: 11px;
        background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        min-height: 22px;
        text-align: center;
        font-weight: 700;
    }}
    QProgressBar#loadingProgressBar::chunk {{
        border-radius: 10px;
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:0,
            stop:0 {progress_start},
            stop:1 {progress_end}
        );
    }}
    QGroupBox {{
        border: 1px solid {c.get('border_light')};
        border-radius: 8px;
        margin-top: 12px;
        padding: 12px 10px 10px 10px;
        color: {c.get('text_primary')};
        font-weight: 700;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 5px;
    }}
    QPushButton {{
        background-color: {c.get('bg_button')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        border-radius: 8px;
        padding: 7px 16px;
        font-weight: 600;
        min-height: 32px;
        outline: none;
    }}
    QPushButton:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton:pressed {{
        background-color: {c.get('bg_button_hover')};
        color: {c.get('text_primary')};
        border: 1px solid {primary_border};
    }}
    QPushButton:focus, QPushButton:default {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {c.get('border_active')};
        outline: none;
    }}
    QToolButton:focus {{
        outline: none;
        border: 1px solid {primary_border};
    }}
    QToolButton {{
        background-color: {c.get('bg_button')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        border-radius: 8px;
        padding: 6px 10px;
        outline: none;
    }}
    QToolButton:hover {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QToolButton:pressed {{
        background-color: {c.get('bg_button_hover')};
        border: 1px solid {primary_border};
    }}
    QPushButton#primaryBtn {{
        background-color: {primary_bg};
        color: {primary_text};
        border: 1px solid {primary_border};
    }}
    QPushButton#primaryBtn:hover {{
        background-color: {primary_hover};
        border: 1px solid {primary_border};
    }}
    QPushButton#apply_btn {{
        background-color: {primary_bg};
        color: {primary_text};
        border: 1px solid {primary_border};
    }}
    QPushButton#apply_btn:hover {{
        background-color: #1a2a3d;
        border: 1px solid #4a9eff;
        color: #4a9eff;
    }}
    QPushButton#secondaryBtn {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
    }}
    QPushButton#secondaryBtn:hover {{
        background-color: {c.get('bg_button')};
        border: 1px solid {primary_border};
    }}
    QPushButton#dangerBtn {{
        background-color: {c.get('danger')};
        color: white;
        border: 1px solid {c.get('danger_dark')};
    }}
    QPushButton#dangerBtn:hover {{
        background-color: {c.get('danger_hover')};
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
        background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 8px;
        outline: none;
    }}
    QLineEdit[readOnly="true"], QTextEdit[readOnly="true"], QPlainTextEdit[readOnly="true"] {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_secondary')};
    }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus, QPlainTextEdit:focus {{
        border: 1px solid {primary_border};
    }}
    QComboBox {{
        background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border_light')};
        border-radius: 6px;
        padding: 4px 10px;
        outline: none;
    }}
    QComboBox:hover, QComboBox:focus {{
        border: 1px solid {primary_border};
    }}
    QComboBox::drop-down {{
        border: none;
        width: 22px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        selection-background-color: {selection_bg};
        selection-color: {selection_text};
        border: 1px solid {c.get('border')};
        outline: none;
    }}
    QMenuBar {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        border-radius: 6px;
        padding: 3px 4px;
    }}
    QMenuBar::item {{
        background: transparent;
        padding: 6px 10px;
        margin: 1px;
        border-radius: 4px;
    }}
    QMenuBar::item:selected {{
        background-color: {c.get('bg_button_hover')};
    }}
    QMenu {{
        background-color: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        padding: 4px;
    }}
    QMenu::item {{
        padding: 6px 24px 6px 12px;
        border-radius: 4px;
    }}
    QMenu::item:selected {{
        background-color: {selection_bg};
        color: {selection_text};
    }}
    QListWidget, QTreeWidget, QTableWidget, QTableView {{
        background-color: {c.get('bg_secondary')};
        alternate-background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        gridline-color: {c.get('border')};
        border: 1px solid {c.get('border')};
        border-radius: 6px;
        outline: none;
    }}
    QListWidget::item {{
        padding: 6px;
        border-bottom: 1px solid {c.get('border')};
    }}
    QListWidget::item:selected,
    QTreeWidget::item:selected,
    QTableWidget::item:selected,
    QTableView::item:selected {{
        background-color: {selection_bg};
        color: {selection_text};
    }}
    QListWidget::item:hover,
    QTreeWidget::item:hover,
    QTableWidget::item:hover,
    QTableView::item:hover {{
        background-color: {c.get('bg_button_hover')};
    }}
    QHeaderView::section {{
        background-color: {c.get('bg_button')};
        color: {c.get('text_primary')};
        border: 1px solid {c.get('border')};
        padding: 6px 8px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{
        background-color: {c.get('bg_button')};
        border: 1px solid {c.get('border')};
    }}
    QTableWidget#displayClassTable {{
        background-color: {c.get('bg_secondary')};
        alternate-background-color: {c.get('bg_input')};
        border: 1px solid {c.get('border_light')};
        border-radius: 10px;
        gridline-color: transparent;
        selection-background-color: {selection_bg};
    }}
    QTableWidget#displayClassTable::item {{
        padding: 6px 10px;
        border: none;
        border-bottom: 1px solid {c.get('border_light')};
    }}
    QTableWidget#displayClassTable::item:selected {{
        background-color: {selection_bg};
        color: {selection_text};
    }}
    QTableWidget#displayClassTable::item:hover {{
        background-color: {c.get('bg_button_hover')};
    }}
    QTableWidget#displayClassTable QHeaderView::section {{
        background-color: {c.get('bg_input')};
        color: {c.get('text_primary')};
        border: none;
        border-bottom: 1px solid {c.get('border')};
        border-right: 1px solid {c.get('border_light')};
        padding: 8px 10px;
        font-weight: 700;
    }}
    QTableWidget#displayClassTable QTableCornerButton::section {{
        background-color: {c.get('bg_input')};
        border: none;
        border-bottom: 1px solid {c.get('border')};
        border-right: 1px solid {c.get('border_light')};
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {c.get('border_light')};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {primary_border};
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}
    QCheckBox {{
        color: {c.get('text_primary')};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border-radius: 3px;
        border: 1px solid {c.get('border_light')};
        background-color: {c.get('bg_input')};
    }}
    QCheckBox::indicator:checked {{
        background-color: {checkbox_checked};
        border: 1px solid {checkbox_checked_border};
    }}
    QRadioButton {{
        color: {c.get('text_primary')};
        spacing: 8px;
        padding: 4px;
    }}
    QRadioButton::indicator {{
        width: 14px;
        height: 14px;
        border-radius: 7px;
        border: 2px solid {c.get('border_light')};
        background-color: {c.get('bg_input')};
    }}
    QRadioButton::indicator:checked {{
        background-color: {primary_border};
        border: 2px solid {primary_border};
    }}
    QScrollArea {{
        border: 1px solid {c.get('border')};
        border-radius: 6px;
        background: {c.get('bg_primary')};
    }}
    QScrollBar:vertical {{
        width: 10px;
        background: {c.get('bg_primary')};
    }}
    QScrollBar::handle:vertical {{
        background: {c.get('border_light')};
        border-radius: 4px;
        min-height: 20px;
    }}
    QScrollBar::handle:vertical:hover {{
        background: {c.get('accent')};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QScrollBar:horizontal {{
        height: 10px;
        background: {c.get('bg_primary')};
    }}
    QScrollBar::handle:horizontal {{
        background: {c.get('border_light')};
        border-radius: 4px;
        min-width: 20px;
    }}
    QScrollBar::handle:horizontal:hover {{
        background: {c.get('accent')};
    }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
        width: 0;
    }}
    QTabWidget::pane {{
        border: 1px solid {c.get('border')};
        border-radius: 6px;
        background: {c.get('bg_secondary')};
    }}
    QTabBar::tab {{
        background: {c.get('bg_button')};
        color: {c.get('text_secondary')};
        padding: 7px 14px;
        margin-right: 2px;
        border-top-left-radius: 5px;
        border-top-right-radius: 5px;
    }}
    QTabBar::tab:selected {{
        background: {primary_bg};
        color: {primary_text};
    }}
    QTabBar::tab:hover:!selected {{
        background: {c.get('bg_button_hover')};
        color: {c.get('text_primary')};
    }}
    """


def get_progress_dialog_stylesheet() -> str:
    """Theme-aware stylesheet for QProgressDialog widgets."""
    c = ThemeColors
    primary_bg = c.get("dialog_primary_bg")
    primary_border = c.get("dialog_primary_border")
    progress_start = c.get("accent") if c.is_light() else primary_border
    progress_end = c.get("accent_hover") if c.is_light() else primary_bg
    return f"""
    QProgressDialog {{
        background: {c.get('bg_secondary')};
        color: {c.get('text_primary')};
        min-width: 430px;
        border: 1px solid {c.get('border_light')};
        border-radius: 12px;
    }}
    QLabel {{
        color: {c.get('text_primary')};
        font-size: 10pt;
        font-weight: 600;
        padding: 10px 12px 6px 12px;
    }}
    QProgressBar {{
        border: 1px solid {c.get('border_light')};
        border-radius: 10px;
        text-align: center;
        background: {c.get('bg_input')};
        color: {c.get('text_primary')};
        min-height: 20px;
        font-weight: 600;
    }}
    QProgressBar::chunk {{
        background: qlineargradient(
            x1:0, y1:0, x2:1, y2:0,
            stop:0 {progress_start},
            stop:1 {progress_end}
        );
        border-radius: 9px;
    }}
    QPushButton {{
        background: {c.get('bg_button')};
        color: {c.get('text_primary')};
        padding: 7px 14px;
        border-radius: 6px;
        border: 1px solid {c.get('border')};
        font-weight: 600;
    }}
    QPushButton:hover {{
        background: {c.get('bg_button_hover')};
        border-color: {primary_border};
    }}
    """


def get_title_banner_style() -> str:
    """Theme-aware style for dialog title banners."""
    c = ThemeColors
    return (
        f"font-size:15px; font-weight:700; color:{c.get('text_primary')}; "
        f"padding:4px 2px 8px 2px; background:transparent; border:none;"
    )


def get_file_item_row_style() -> str:
    """Theme-aware style for file list item widget rows."""
    c = ThemeColors
    return (
        f"QWidget{{background:{c.get('bg_secondary')}; "
        f"border:1px solid {c.get('border_light')}; border-radius:10px;}}"
        f"QWidget:hover{{background:{c.get('bg_input')}; "
        f"border:1px solid {c.get('dialog_primary_border')};}}"
    )


def get_badge_style(variant: str = "success") -> str:
    """Theme-aware badge style. Variants: 'success', 'warning', 'info'."""
    c = ThemeColors
    if variant == "success":
        return (
            f"color:{c.get('text_on_active')}; background:{c.get('success')}; "
            f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
        )
    elif variant == "warning":
        return (
            f"color:{c.get('text_on_active')}; background:{c.get('warning')}; "
            f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
        )
    else:
        return (
            f"color:{c.get('text_on_active')}; background:{c.get('accent')}; "
            f"padding:3px 8px; border-radius:999px; font-size:9px; font-weight:700;"
        )


def get_notice_banner_style(variant: str = "info") -> str:
    """Theme-aware notice/callout style for helper messages inside dialogs."""
    c = ThemeColors
    if variant == "warning":
        bg = c.get('warning_bg')
        fg = c.get('warning')
        border = c.get('warning')
    elif variant == "danger":
        bg = c.get('danger_dark') if not ThemeColors.is_light() else "#fdecea"
        fg = "#ffffff" if not ThemeColors.is_light() else c.get('danger')
        border = c.get('danger')
    elif variant == "success":
        bg = c.get('success')
        fg = c.get('text_on_active')
        border = c.get('success')
    else:
        bg = c.get('bg_input')
        fg = c.get('text_secondary')
        border = c.get('border_light')

    return (
        f"background:{bg}; color:{fg}; border:1px solid {border}; "
        f"border-radius:8px; padding:8px 10px; font-size:9px;"
    )


def get_icon_button_style(role: str = "default") -> str:
    """Theme-aware style for small round icon buttons. Roles: default, danger, settings."""
    c = ThemeColors
    if role == "danger":
        bg = c.get('danger')
        hover = c.get('danger_hover')
        border = c.get('danger_dark')
    elif role == "settings":
        bg = c.get('bg_button')
        hover = c.get('bg_button_hover')
        border = c.get('border_light')
    else:
        bg = c.get('bg_button')
        hover = c.get('bg_button_hover')
        border = c.get('border_light')
    return (
        f"QPushButton{{background:{bg}; color:{c.get('text_on_active')}; "
        f"border:1px solid {border}; border-radius:13px; font-size:13px; outline:none;}}"
        f"QPushButton:hover{{background:{hover}; border:1px solid {border};}}"
    )

def _refresh_view_ribbon_styles(ribbon):
    """Re-apply theme styles to ViewRibbon sharpness/amplifier controls."""
    try:
        if hasattr(ribbon, 'sharp_value_label'):
            ribbon.sharp_value_label.setStyleSheet("")
    except Exception:
        pass

def _refresh_identify_ribbon_styles(ribbon):
    """Re-apply theme styles to IdentificationRibbon."""
    try:
        if hasattr(ribbon, 'status_label'):
            ribbon.status_label.setStyleSheet(get_status_label_style())
        if hasattr(ribbon, 'delete_btn'):
            ribbon.delete_btn.setStyleSheet(get_delete_btn_style())
    except Exception:
        pass
