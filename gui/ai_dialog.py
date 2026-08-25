from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QProgressBar, QPushButton, QMessageBox,
    QSpinBox, QDoubleSpinBox, QCheckBox,
    QGroupBox, QFrame, QTabWidget, QWidget,
    QFormLayout, QFileDialog, QSizePolicy,
    QListWidget, QListWidgetItem, QAbstractItemView,
    QRadioButton, QScrollArea
)
from PySide6.QtCore import Qt, QEvent
from PySide6.QtGui import QFont
from pathlib import Path
import numpy as np

from gui.ai_inference import (
    InferenceConfig,
    DEFAULT_POWER_MAPPING,
)

# ═══════════════════════════════════════════════════════════════
# TerraScan MAC PARSER
# ═══════════════════════════════════════════════════════════════

def _split_mac_args(args_str: str) -> list:
    parts = []; current = ''; in_quote = False
    for ch in args_str:
        if ch == '"':
            in_quote = not in_quote
        elif ch == ',' and not in_quote:
            parts.append(current.strip()); current = ''
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    return parts


def parse_mac_file(path: str) -> dict:
    """
    Parse TerraScan .mac → extract pipeline-relevant params.
    Returns dict with keys matching advanced_config.
    Only successfully parsed keys are included.
    """
    result = {}
    path   = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"MAC file not found: {path}")
    with open(path, encoding='utf-8', errors='ignore') as f:
        lines = f.readlines()
    if '[TerraScan macro]' not in ''.join(lines):
        raise ValueError("Not a valid TerraScan .mac file.")

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith('[') or '(' not in line:
            continue
        fn_name  = line[:line.index('(')]
        args_str = line[line.index('(') + 1: line.rindex(')')]
        args     = [a.strip().strip('"') for a in args_str.split(',')]

        # FnScanClassifyGround → CSF cloth_resolution + rigidness
        if fn_name == 'FnScanClassifyGround' and len(args) >= 10:
            try:
                max_angle = float(args[4])
                iter_dist = float(args[9])
                result['csf_cloth_resolution'] = round(max(0.1, min(5.0, iter_dist)), 2)
                result['csf_rigidness'] = (1 if max_angle > 60
                                           else 2 if max_angle > 30 else 3)
            except (ValueError, IndexError):
                pass

        # FnScanClassifyHgtGrd → HAG vegetation boundaries
        elif fn_name == 'FnScanClassifyHgtGrd' and len(args) >= 6:
            try:
                from_cls = int(args[2]); to_cls = int(args[3])
                min_hgt  = float(args[4]); max_hgt = float(args[5])
                if from_cls not in (0, 1):
                    continue
                if to_cls == 2:
                    if min_hgt >= 0:   result['lowveg_min'] = round(max(0.0, min_hgt), 3)
                    if max_hgt < 500:  result['lowveg_max'] = round(max_hgt, 3)
                elif to_cls == 3:
                    if max_hgt < 500:  result['midveg_max'] = round(max_hgt, 3)
                elif to_cls == 4:
                    if min_hgt >= 0:   result['highveg_min'] = round(min_hgt, 3)
            except (ValueError, IndexError):
                pass

        # FnScanFindWires → wire geometry
        elif fn_name == 'FnScanFindWires':
            try:
                raw_args = _split_mac_args(args_str)
                if len(raw_args) >= 10:
                    result['wire_chain_radius'] = round(max(0.5, float(raw_args[5])), 2)
                    result['wire_hag_max']       = round(max(5.0, float(raw_args[9])), 1)
                    if len(raw_args) >= 12:
                        result['wire_hag_min']   = round(max(0.0, float(raw_args[11])), 1)
            except (ValueError, IndexError):
                pass

    return result


# ═══════════════════════════════════════════════════════════════
# DEFAULT VALUES (mirrors InferenceConfig — single source of truth)
# ═══════════════════════════════════════════════════════════════

_DEFAULTS = {
    'csf_cloth_resolution':  InferenceConfig.CSF_CLOTH_RESOLUTION,
    'csf_rigidness':         InferenceConfig.CSF_RIGIDNESS,
    'csf_class_threshold':   InferenceConfig.CSF_CLASS_THRESHOLD,
    'lowveg_min':            InferenceConfig.LOWVEG_HAG_MIN,
    'lowveg_max':            InferenceConfig.LOWVEG_HAG_MAX,
    'midveg_max':            InferenceConfig.MIDVEG_HAG_MAX,
    'highveg_min':           InferenceConfig.HIGHVEG_HAG_MIN,
    'wire_hag_min':          InferenceConfig.WIRE_HAG_MIN,
    'wire_hag_max':          InferenceConfig.WIRE_HAG_MAX,
    'wire_chain_radius':     InferenceConfig.WIRE_CHAIN_RADIUS,
    'wire_density_max':      InferenceConfig.WIRE_DENSITY_MAX,
    'wire_min_segment_pts':  InferenceConfig.WIRE_MIN_SEGMENT_PTS,
    'wire_linearity_min':    InferenceConfig.WIRE_LINEARITY_MIN,
    'power_corridor_width':  8.0,
}


# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# AI FENCE + MODEL SELECTION POPUP
# Shows digitizer fences, supports hover highlight, multi-select,
# Basic / Advanced model choice, and Basic-only power-line option.
# ═══════════════════════════════════════════════════════════════

class AIFenceRunDialog(QDialog):
    ALLOWED_TYPES = {
        "rectangle",
        "polygon",
        "freehand",
        "polyline",
        "orthopolygon",
        "circle",
    }

    HOVER_COLOR = (1.0, 0.85, 0.05)      # yellow hover
    SELECT_COLOR = (0.0, 0.45, 1.0)      # blue selected

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app

        self.target_indices = None
        self.selected_fence_count = 0
        self.selected_fence_drawings = []
        self.ai_mode = "basic"
        self.enable_power_lines = False
        self.advanced_target_classes = {4}
        self._adv_target_checks = {}
        self._adv_all_cb = None
        self._adv_target_box = None

        self._hover_actor = None
        self._selected_actors = []
        self._item_update_lock = False

        self.setWindowTitle("AI Fence Selection")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(520, 420)
        self.setMaximumSize(16777215, 16777215)
        self.setSizeGripEnabled(True)
        self.resize(780, 900)
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)

        self._build_ui()
        self.refresh_fences()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # Scroll area prevents clipping when Advanced AI class options are visible.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        content = QWidget()
        self._content_widget = content

        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(10)

        title = QLabel("Select fence(s) and AI model")
        title.setObjectName("sectionTitle")
        title.setStyleSheet("font-size: 13pt; font-weight: 700;")
        title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        content_layout.addWidget(title)

        info = QLabel(
            "Select one or more drawn fences. Move cursor over a fence name to preview it. "
            "Checked fences will stay highlighted. AI will update only points inside selected fence(s)."
        )
        info.setWordWrap(True)
        info.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        content_layout.addWidget(info)

        self._model_box = QGroupBox("AI Model")
        self._model_box.setMinimumHeight(170)
        self._model_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        model_layout = QVBoxLayout(self._model_box)
        model_layout.setContentsMargins(22, 22, 22, 16)
        model_layout.setSpacing(8)

        self.basic_radio = QRadioButton("Basic AI")
        self.basic_radio.setChecked(True)
        self.basic_radio.setMinimumHeight(26)

        self.advanced_radio = QRadioButton("Advanced AI")
        self.advanced_radio.setMinimumHeight(26)

        self.power_cb = QCheckBox("Enable Power Line Detection")
        self.power_cb.setChecked(False)
        self.power_cb.setMinimumHeight(26)
        self.power_cb.setToolTip(
            "Power line detection runs only with Basic AI.\n"
            "Advanced AI will keep this disabled."
        )

        self.basic_radio.toggled.connect(self._sync_model_state)
        self.advanced_radio.toggled.connect(self._sync_model_state)

        model_layout.addWidget(self.basic_radio)
        model_layout.addWidget(self.advanced_radio)
        model_layout.addWidget(self.power_cb)

        # Advanced AI target class filter
        # Advanced AI target class filter
        # Advanced AI target class filter
        self._adv_target_box = QGroupBox("Advanced AI Target Classes")
        self._adv_target_box.setMinimumHeight(255)
        self._adv_target_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        adv_target_layout = QVBoxLayout(self._adv_target_box)
        adv_target_layout.setContentsMargins(36, 28, 18, 14)
        adv_target_layout.setSpacing(5)

        self._adv_all_cb = QCheckBox("All Classes")
        self._adv_all_cb.setChecked(False)
        self._adv_all_cb.setMinimumHeight(24)
        self._adv_all_cb.toggled.connect(self._on_adv_all_toggled)
        adv_target_layout.addWidget(self._adv_all_cb)

        self._adv_target_checks = {}

        for cls_id, cls_name in [
            (0, "Ground"),
            (1, "Low Vegetation"),
            (2, "Medium Vegetation"),
            (3, "High Vegetation"),
            (4, "Building"),
        ]:
            cb = QCheckBox(cls_name)
            cb.setChecked(cls_id == 4)
            cb.setMinimumHeight(24)
            cb.toggled.connect(self._on_adv_class_toggled)
            self._adv_target_checks[cls_id] = cb
            adv_target_layout.addWidget(cb)

        hint = QLabel(
            "Only selected Advanced AI classes will be applied. "
            "Unselected classes remain unchanged."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777; font-size:10px; padding-top:6px;")
        hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        adv_target_layout.addWidget(hint)

        model_layout.addWidget(self._adv_target_box)

        content_layout.addWidget(self._model_box)

        fence_box = QGroupBox("Fence Selection")
        fence_layout = QVBoxLayout(fence_box)
        fence_box.setMinimumHeight(300)
        fence_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fence_layout.setContentsMargins(20, 20, 20, 14)
        fence_layout.setSpacing(8)

        self.fence_list = QListWidget()
        self.fence_list.setMinimumHeight(180)
        self.fence_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.fence_list.setMouseTracking(True)
        self.fence_list.setAlternatingRowColors(True)
        self.fence_list.itemEntered.connect(self._on_item_hovered)
        self.fence_list.itemChanged.connect(self._on_item_changed)
        self.fence_list.viewport().installEventFilter(self)

        fence_layout.addWidget(self.fence_list)

        self.summary_label = QLabel("No fence selected.")
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("padding: 6px; font-weight: 600;")
        self.summary_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        fence_layout.addWidget(self.summary_label)

        content_layout.addWidget(fence_box)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        btn_row_1 = QHBoxLayout()

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_fences)
        btn_row_1.addWidget(refresh_btn)

        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(self.select_all)
        btn_row_1.addWidget(select_all_btn)

        clear_btn = QPushButton("Clear All")
        clear_btn.clicked.connect(self.clear_selection)
        btn_row_1.addWidget(clear_btn)

        btn_row_1.addStretch()
        root.addLayout(btn_row_1)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        root.addWidget(sep)

        btn_row_2 = QHBoxLayout()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row_2.addWidget(cancel_btn)

        btn_row_2.addStretch()

        full_btn = QPushButton("Run Full File")
        full_btn.clicked.connect(self._accept_full_file)
        btn_row_2.addWidget(full_btn)

        selected_btn = QPushButton("Run Selected Fence(s)")
        selected_btn.setDefault(True)
        selected_btn.clicked.connect(self._accept_selected_fences)
        btn_row_2.addWidget(selected_btn)

        root.addLayout(btn_row_2)

        self._sync_model_state()

    def _sync_model_state(self):
        if self.advanced_radio.isChecked():
            self.ai_mode = "advanced"
            self.power_cb.setChecked(False)
            self.power_cb.setEnabled(False)

            if self._adv_target_box is not None:
                self._adv_target_box.setVisible(True)

            if hasattr(self, "_model_box") and self._model_box is not None:
                self._model_box.setMinimumHeight(440)
                self._model_box.setMaximumHeight(440)

            self.advanced_target_classes = self._get_selected_advanced_classes()

        else:
            self.ai_mode = "basic"
            self.power_cb.setEnabled(True)

            if self._adv_target_box is not None:
                self._adv_target_box.setVisible(False)

            if hasattr(self, "_model_box") and self._model_box is not None:
                self._model_box.setMinimumHeight(170)
                self._model_box.setMaximumHeight(190)

            self.advanced_target_classes = {4}

        try:
            if hasattr(self, "_content_widget") and self._content_widget is not None:
                self._content_widget.adjustSize()
            self.updateGeometry()
        except Exception:
            pass

    def _on_adv_all_toggled(self, checked):
        if not self._adv_target_checks:
            return

        for cb in self._adv_target_checks.values():
            cb.blockSignals(True)
            cb.setChecked(bool(checked))
            cb.blockSignals(False)

        self.advanced_target_classes = self._get_selected_advanced_classes()


    def _on_adv_class_toggled(self, checked):
        if not self._adv_target_checks or self._adv_all_cb is None:
            return

        all_checked = all(cb.isChecked() for cb in self._adv_target_checks.values())

        self._adv_all_cb.blockSignals(True)
        self._adv_all_cb.setChecked(all_checked)
        self._adv_all_cb.blockSignals(False)

        self.advanced_target_classes = self._get_selected_advanced_classes()


    def _get_selected_advanced_classes(self):
        selected = set()

        for cls_id, cb in self._adv_target_checks.items():
            if cb.isChecked():
                selected.add(int(cls_id))

        return selected
    def set_advanced_target_classes(self, selected_classes):
        """
        Copy Advanced AI target-class selection from the first AI popup
        into the fence popup.

        Example:
        first popup selected Building only -> fence popup also shows Building checked.
        """
        selected = set()

        for cls_id in selected_classes or []:
            try:
                cls_id = int(cls_id)
            except Exception:
                continue

            if cls_id in self._adv_target_checks:
                selected.add(cls_id)

        for cls_id, cb in self._adv_target_checks.items():
            cb.blockSignals(True)
            cb.setChecked(cls_id in selected)
            cb.blockSignals(False)

        if self._adv_all_cb is not None:
            all_checked = selected == set(self._adv_target_checks.keys())

            self._adv_all_cb.blockSignals(True)
            self._adv_all_cb.setChecked(all_checked)
            self._adv_all_cb.blockSignals(False)

        self.advanced_target_classes = selected

    def _validate_advanced_target_classes(self):
        if self.ai_mode != "advanced":
            return True

        selected = self._get_selected_advanced_classes()

        if not selected:
            QMessageBox.warning(
                self,
                "No Advanced Classes Selected",
                "Select at least one Advanced AI class.\n\n"
                "Example: select Building if you want only building points classified."
            )
            return False

        self.advanced_target_classes = selected
        return True

    def _get_drawings(self):
        digitizer = getattr(self.app, "digitizer", None)
        if digitizer is None:
            return []
        drawings = getattr(digitizer, "drawings", None)
        if drawings is None:
            return []
        return list(drawings)

    def _is_valid_fence(self, drawing):
        if not isinstance(drawing, dict):
            return False

        # Fence mode must only use user-digitized fence drawings.
        # When SNT/DXF overlays are loaded, helper geometry can appear with
        # polyline-like structures and accidentally become AI scope.
        source = str(drawing.get("source", "") or "").strip().lower()
        source_layer = str(drawing.get("source_layer", "") or "").strip().lower()
        name = str(drawing.get("name", "") or "").strip().lower()
        layer = str(drawing.get("layer", "") or "").strip().lower()
        label = str(drawing.get("label", "") or "").strip().lower()
        tag_text = f"{source} {source_layer} {name} {layer} {label}"

        if source and source not in {"digitizer", "ai_fence"}:
            return False

        blocked_tokens = (
            ".snt", ".dxf", "snt_", "dxf_",
            "bbox", "bounding", "block", "blocks",
            "vector", "overlay", "attachment",
            "route", "cl", "center line",
        )
        if any(tok in tag_text for tok in blocked_tokens):
            return False

        dtype = str(drawing.get("type", "")).lower()
        if dtype not in self.ALLOWED_TYPES:
            return False

        if dtype == "circle":
            if drawing.get("center") is not None and drawing.get("radius") is not None:
                return True

        coords = drawing.get("coords", None)
        if coords is None:
            return False

        try:
            arr = np.asarray(coords, dtype=np.float64)
            return arr.ndim == 2 and arr.shape[0] >= 3 and arr.shape[1] >= 2
        except Exception:
            return False

    def _coords_from_fence(self, drawing):
        dtype = str(drawing.get("type", "")).lower()

        if dtype == "circle":
            center = drawing.get("center", None)
            radius = drawing.get("radius", None)

            if center is not None and radius is not None:
                center = np.asarray(center, dtype=np.float64)
                radius = float(radius)
                coords = []
                for a in np.linspace(0.0, 2.0 * np.pi, 96, endpoint=False):
                    coords.append((
                        float(center[0] + radius * np.cos(a)),
                        float(center[1] + radius * np.sin(a)),
                        float(center[2]) if center.shape[0] > 2 else 0.0,
                    ))
                coords.append(coords[0])
                return coords

        coords = drawing.get("coords", [])
        clean = []
        for c in coords:
            if len(c) >= 3:
                clean.append((float(c[0]), float(c[1]), float(c[2])))
            elif len(c) >= 2:
                clean.append((float(c[0]), float(c[1]), 0.0))

        if len(clean) >= 3 and clean[0][:2] != clean[-1][:2]:
            clean.append(clean[0])

        return clean

    def _fence_text(self, drawing, fence_no):
        dtype = str(drawing.get("type", "unknown")).lower()
        coords = self._coords_from_fence(drawing)

        try:
            arr = np.asarray(coords, dtype=np.float64)
            min_x, min_y = np.min(arr[:, :2], axis=0)
            max_x, max_y = np.max(arr[:, :2], axis=0)
            width = max_x - min_x
            height = max_y - min_y

            return (
                f"#{fence_no}: {dtype.title()}  |  "
                f"{len(coords)} pts  |  "
                f"{width:.1f}x{height:.1f}m  |  Digitizer"
            )
        except Exception:
            return f"#{fence_no}: {dtype.title()}  |  Digitizer"

    def refresh_fences(self):
        self._item_update_lock = True
        self._clear_hover_actor()
        self._clear_selected_actors()

        self.fence_list.clear()

        drawings = self._get_drawings()

        fences = []
        for d in drawings:
            if not self._is_valid_fence(d):
                continue

            source = str(d.get("source", "") or "").strip().lower()
            source_layer = str(d.get("source_layer", "") or "").strip().lower()
            layer = str(d.get("layer", "") or "").strip().lower()
            name = str(d.get("name", "") or "").strip().lower()
            label = str(d.get("label", "") or "").strip().lower()

            tag_text = f"{source} {source_layer} {layer} {name} {label}"

            if any(tok in tag_text for tok in (
                "snt", "dxf", "block", "blocks", "bbox", "bounding",
                "grid", "overlay", "vector", "attachment", "cl", "center line",
            )):
                continue

            if source not in ("", "digitizer", "ai_fence"):
                continue

            # if d.get("classified_fence", False):
            #     continue

            fences.append(d)

        for i, fence in enumerate(fences, start=1):
            item = QListWidgetItem(self._fence_text(fence, i))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)

            if fence.get("classified_fence", False) or fence.get("ai_auto_select", False):
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)

            item.setData(Qt.UserRole, fence)
            self.fence_list.addItem(item)

        self._item_update_lock = False

        if fences:
            self._update_selected_highlights()

            auto_count = sum(
                1 for i in range(self.fence_list.count())
                if self.fence_list.item(i).checkState() == Qt.Checked
            )

            if auto_count:
                self.summary_label.setText(
                    f"{auto_count} converted fence(s) pre-selected. AI will update only points inside selected fence(s)."
                )
            else:
                self.summary_label.setText(
                    f"{len(fences)} fence(s) found. Check one or more fence(s), or run full file."
                )
        else:
            self.summary_label.setText(
                "No valid fences found. Draw Rectangle / Polygon / Freehand / Polyline / Circle first."
            )

    def has_fences(self):
        return self.fence_list.count() > 0

    def eventFilter(self, obj, event):
        if obj == self.fence_list.viewport() and event.type() == QEvent.Leave:
            self._clear_hover_actor()
        return super().eventFilter(obj, event)

    def _selected_fences(self):
        selected = []
        for i in range(self.fence_list.count()):
            item = self.fence_list.item(i)
            if item.checkState() == Qt.Checked:
                fence = item.data(Qt.UserRole)
                if self._is_valid_fence(fence):
                    source = str(fence.get("source", "") or "").strip().lower()
                    source_layer = str(fence.get("source_layer", "") or "").strip().lower()
                    layer = str(fence.get("layer", "") or "").strip().lower()
                    name = str(fence.get("name", "") or "").strip().lower()
                    label = str(fence.get("label", "") or "").strip().lower()
                    tag_text = f"{source} {source_layer} {layer} {name} {label}"

                    if source in ("", "digitizer", "ai_fence") and not any(tok in tag_text for tok in (
                        "snt", "dxf", "block", "blocks", "bbox", "bounding",
                        "grid", "overlay", "vector", "attachment", "cl", "center line",
                    )):
                        selected.append(fence)
        return selected

    def select_all(self):
        self._item_update_lock = True
        for i in range(self.fence_list.count()):
            self.fence_list.item(i).setCheckState(Qt.Checked)
        self._item_update_lock = False
        self._update_selected_highlights()

    def clear_selection(self):
        self._item_update_lock = True
        for i in range(self.fence_list.count()):
            self.fence_list.item(i).setCheckState(Qt.Unchecked)
        self._item_update_lock = False
        self._update_selected_highlights()

    def _on_item_changed(self, item):
        if self._item_update_lock:
            return
        self._update_selected_highlights()

    def _on_item_hovered(self, item):
        self._clear_hover_actor()

        if item is None:
            return

        fence = item.data(Qt.UserRole)
        if not self._is_valid_fence(fence):
            return

        coords = self._coords_from_fence(fence)
        actor = self._make_highlight_actor(
            coords,
            color=self.HOVER_COLOR,
            width=7.0,
            opacity=1.0
        )

        if actor is not None:
            self._hover_actor = actor
            self._add_actor(actor)
            self._render()

    def _update_selected_highlights(self):
        self._clear_selected_actors()

        selected = self._selected_fences()

        for fence in selected:
            coords = self._coords_from_fence(fence)
            actor = self._make_highlight_actor(
                coords,
                color=self.SELECT_COLOR,
                width=5.0,
                opacity=1.0
            )
            if actor is not None:
                self._selected_actors.append(actor)
                self._add_actor(actor)

        if selected:
            self.summary_label.setText(
                f"{len(selected)} fence(s) selected. AI will update only points inside selected fence(s)."
            )
        else:
            self.summary_label.setText("No fence selected. Select fence(s), or run full file.")

        self._render()

    def _make_highlight_actor(self, coords, color=(1, 1, 0), width=6.0, opacity=1.0):
        if not coords or len(coords) < 2:
            return None

        try:
            import vtk

            digitizer = getattr(self.app, "digitizer", None)

            if digitizer is not None and hasattr(digitizer, "_build_styled_polydata_world"):
                poly = digitizer._build_styled_polydata_world(coords, line_style="solid")
                origin = getattr(poly, "_world_origin", np.zeros(3, dtype=np.float64))
            else:
                origin = np.asarray(coords[0], dtype=np.float64)

                poly = vtk.vtkPolyData()
                pts = vtk.vtkPoints()
                pts.SetDataTypeToDouble()
                lines = vtk.vtkCellArray()

                for p in coords:
                    pts.InsertNextPoint(
                        float(p[0] - origin[0]),
                        float(p[1] - origin[1]),
                        float((p[2] if len(p) > 2 else 0.0) - origin[2])
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
            actor.GetProperty().SetColor(float(color[0]), float(color[1]), float(color[2]))
            actor.GetProperty().SetLineWidth(float(width))
            actor.GetProperty().SetOpacity(float(opacity))
            actor.PickableOff()

            try:
                actor.GetProperty().SetDepthTestingEnabled(False)
            except Exception:
                pass

            return actor

        except Exception as e:
            print(f"Failed to create AI fence highlight actor: {e}")
            return None

    def _get_overlay_renderer(self):
        digitizer = getattr(self.app, "digitizer", None)

        if digitizer is not None:
            if hasattr(digitizer, "overlay_renderer"):
                return digitizer.overlay_renderer
            if hasattr(digitizer, "_ensure_overlay_renderer"):
                try:
                    return digitizer._ensure_overlay_renderer()
                except Exception:
                    pass

        if hasattr(self.app, "vtk_widget"):
            try:
                if hasattr(self.app.vtk_widget, "renderer"):
                    return self.app.vtk_widget.renderer
            except Exception:
                pass

        return getattr(self.app, "renderer", None)

    def _add_actor(self, actor):
        ren = self._get_overlay_renderer()
        if ren is None or actor is None:
            return
        try:
            ren.AddActor(actor)
        except Exception:
            pass

    def _remove_actor(self, actor):
        if actor is None:
            return

        ren = self._get_overlay_renderer()
        if ren is not None:
            try:
                ren.RemoveActor(actor)
            except Exception:
                pass
            try:
                ren.RemoveViewProp(actor)
            except Exception:
                pass

    def _clear_hover_actor(self):
        if self._hover_actor is not None:
            self._remove_actor(self._hover_actor)
            self._hover_actor = None
            self._render()

    def _clear_selected_actors(self):
        for actor in list(self._selected_actors):
            self._remove_actor(actor)
        self._selected_actors = []
        self._render()

    def _render(self):
        try:
            if hasattr(self.app, "vtk_widget"):
                if hasattr(self.app.vtk_widget, "render"):
                    self.app.vtk_widget.render()
                else:
                    self.app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass

    def _points_inside_polygon(self, xyz, coords):
        poly = np.asarray(coords, dtype=np.float64)

        if poly.ndim != 2 or poly.shape[0] < 3 or poly.shape[1] < 2:
            return np.zeros(len(xyz), dtype=bool)

        poly_xy = poly[:, :2]

        if not np.allclose(poly_xy[0], poly_xy[-1]):
            poly_xy = np.vstack([poly_xy, poly_xy[0]])

        pts_xy = xyz[:, :2]

        min_x, min_y = np.min(poly_xy, axis=0)
        max_x, max_y = np.max(poly_xy, axis=0)

        bbox_mask = (
            (pts_xy[:, 0] >= min_x) &
            (pts_xy[:, 0] <= max_x) &
            (pts_xy[:, 1] >= min_y) &
            (pts_xy[:, 1] <= max_y)
        )

        inside = np.zeros(len(xyz), dtype=bool)
        cand_idx = np.where(bbox_mask)[0]

        if len(cand_idx) == 0:
            return inside

        x = pts_xy[cand_idx, 0]
        y = pts_xy[cand_idx, 1]

        px = poly_xy[:, 0]
        py = poly_xy[:, 1]

        cand_inside = np.zeros(len(cand_idx), dtype=bool)
        j = len(poly_xy) - 1

        for i in range(len(poly_xy)):
            yi = py[i]
            yj = py[j]
            xi = px[i]
            xj = px[j]

            intersects = (
                ((yi > y) != (yj > y)) &
                (x < ((xj - xi) * (y - yi) / ((yj - yi) + 1e-12) + xi))
            )

            cand_inside ^= intersects
            j = i

        inside[cand_idx] = cand_inside
        return inside

    def _points_inside_circle(self, xyz, drawing):
        center = drawing.get("center", None)
        radius = drawing.get("radius", None)

        if center is None or radius is None:
            coords = self._coords_from_fence(drawing)
            return self._points_inside_polygon(xyz, coords)

        center = np.asarray(center, dtype=np.float64)
        radius = float(radius)

        dx = xyz[:, 0] - center[0]
        dy = xyz[:, 1] - center[1]

        return (dx * dx + dy * dy) <= (radius * radius)

    def _calculate_target_indices(self, fences):
        if not hasattr(self.app, "data") or self.app.data is None:
            raise RuntimeError("No point cloud data loaded.")

        xyz = self.app.data.get("xyz")
        if xyz is None:
            raise RuntimeError("XYZ data missing from app.data.")

        xyz = np.asarray(xyz)
        combined = np.zeros(len(xyz), dtype=bool)

        for fence in fences:
            dtype = str(fence.get("type", "")).lower()

            if dtype == "circle":
                inside = self._points_inside_circle(xyz, fence)
            else:
                coords = self._coords_from_fence(fence)
                inside = self._points_inside_polygon(xyz, coords)

            combined |= inside

        return np.where(combined)[0].astype(np.int64)

    def _accept_full_file(self):
        self._sync_model_state()

        if not self._validate_advanced_target_classes():
            return

        self.enable_power_lines = bool(self.power_cb.isChecked()) if self.ai_mode == "basic" else False
        self.target_indices = None
        self.selected_fence_count = 0
        self.selected_fence_drawings = []
        self._clear_hover_actor()
        self._clear_selected_actors()
        self.accept()

    def _accept_selected_fences(self):
        self._sync_model_state()

        if not self._validate_advanced_target_classes():
            return

        self.enable_power_lines = bool(self.power_cb.isChecked()) if self.ai_mode == "basic" else False
        fences = self._selected_fences()

        if not fences:
            QMessageBox.warning(
                self,
                "No Fence Selected",
                "Select at least one fence, or click Run Full File."
            )
            return

        self.summary_label.setText("Calculating points inside selected fence(s)...")
        self.repaint()

        try:
            target_indices = self._calculate_target_indices(fences)
        except Exception as e:
            QMessageBox.critical(
                self,
                "Fence Calculation Failed",
                f"Could not calculate fence points:\n\n{e}"
            )
            return

        if target_indices is None or len(target_indices) == 0:
            QMessageBox.warning(
                self,
                "Empty Fence",
                "Selected fence(s) contain zero points.\n\n"
                "Select a bigger fence or another fence."
            )
            return

        self.target_indices = target_indices
        self.selected_fence_count = len(fences)
        self.selected_fence_drawings = list(fences)

        self._clear_hover_actor()
        self._clear_selected_actors()
        self.accept()

    def accept(self):
        # Ensure temporary fence preview actors never leak into main view.
        self._clear_hover_actor()
        self._clear_selected_actors()
        super().accept()

    def reject(self):
        self._clear_hover_actor()
        self._clear_selected_actors()
        super().reject()

    def closeEvent(self, event):
        # Safety cleanup for title-bar close / external close paths.
        self._clear_hover_actor()
        self._clear_selected_actors()
        super().closeEvent(event)
    
class ClassMappingDialog(QDialog):

    _CLASSES = [
        (0,      "Ground",            1),
        (1,      "Low Vegetation",    2),
        (2,      "Medium Vegetation", 3),
        (3,      "High Vegetation",   4),
        (4,      "Building",          5),
        ('wire', "Power Line Wire",  14),
        ('pole', "Power Line Pole",  15),
    ]

    def __init__(self, parent=None, existing_classes=None, app=None):
        super().__init__(parent)
        self._app                       = app if app is not None else parent
        self.existing_classes           = existing_classes or set()
        self.accepted_class_mapping     = None
        self.accepted_power_mapping     = None
        self.accepted_advanced          = None
        self.accepted_target_indices    = None
        self.accepted_enable_power_lines = False
        self.accepted_ai_mode           = "basic"
        self.accepted_advanced_target_classes = {4}
        self._adv_target_checks        = {}
        self._adv_all_cb               = None
        self._adv_target_box           = None
        self._code_spins                = {}
        self._code_labels               = {}
        self._adv                       = {}
        self._enable_power_cb           = None
        self._advanced_model_cb         = None

        # CL corridor option in first AI popup
        self._use_cl_corridor_cb        = None
        self._cl_status_label           = None

        self._fence_sel                 = None

        # Used when CL corridor is selected from first popup
        self.accepted_selected_fence_count = 0
        self.accepted_selected_fence_drawings = []

        self._setup_ui()

    # ── UI ────────────────────────────────────────────────────

    def _setup_ui(self):
        self.setWindowTitle("AI Classification")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(320, 260)
        # self.setMinimumSize(635, 590)
        self.setMaximumSize(16777215, 16777215)
        self.setSizeGripEnabled(True)
        self.resize(780, 860)
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

        self.tabs = QTabWidget()
        self.tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tabs.addTab(self._build_mapping_tab(), "AI Model")
        self.tabs.addTab(self._build_advanced_tab(), "MAC Value Extractor")
        root.addWidget(self.tabs, 1)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        root.addWidget(sep)

        btn_row = QHBoxLayout()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(80)
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()

        start_btn = QPushButton("Start Classification")
        start_btn.setDefault(True)
        start_btn.setMinimumWidth(150)
        f = QFont(); f.setBold(True)
        start_btn.setFont(f)
        start_btn.clicked.connect(self._validate_and_accept)
        btn_row.addWidget(start_btn)
        root.addLayout(btn_row)

    # ── TAB 1: CLASS MAPPING ──────────────────────────────────

    def _build_mapping_tab(self):
        page   = QWidget()
        page.setMinimumWidth(0)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        grp        = QGroupBox("Class → Output Code Mapping")
        grp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        grp_layout = QVBoxLayout(grp)
        grp_layout.setContentsMargins(12, 18, 12, 12)
        grp_layout.setSpacing(4)

        # Header row
        hdr = QHBoxLayout()
        hdr.setContentsMargins(6, 2, 6, 4)
        lbl_cls  = QLabel("Class")
        lbl_cls.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        lbl_code = QLabel("Output Code")
        lbl_code.setAlignment(Qt.AlignCenter)
        lbl_code.setMinimumWidth(70)
        lbl_code.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        hdr_font = QFont(); hdr_font.setBold(True)
        lbl_cls.setFont(hdr_font); lbl_code.setFont(hdr_font)
        hdr.addWidget(lbl_cls, 3)
        hdr.addWidget(lbl_code, 2)
        grp_layout.addLayout(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        grp_layout.addWidget(sep)

        for idx, (key, name, default_code) in enumerate(self._CLASSES):
            if idx == 5:
                # ── Divider + power-line toggle BEFORE the power rows ──
                div = QFrame()
                div.setFrameShape(QFrame.HLine)
                div.setFrameShadow(QFrame.Sunken)
                grp_layout.addWidget(div)

                cb_row = QHBoxLayout()
                cb_row.setContentsMargins(6, 8, 6, 4)
                self._enable_power_cb = QCheckBox(
                    "Enable Power Line Detection (Wire & Pole)"
                )
                self._enable_power_cb.setChecked(False)
                self._enable_power_cb.setToolTip(
                    "OFF (default): pipeline runs in 5-class mode "
                    "(Ground / Low / Mid / High Veg / Building).\n"
                    "Building detection is preserved exactly.\n\n"
                    "ON: an additional post-pass attempts to identify "
                    "wires and poles. Some building walls may be "
                    "reclassified as poles when this is enabled."
                )
                cb_font = QFont(); cb_font.setBold(True)
                self._enable_power_cb.setFont(cb_font)
                self._enable_power_cb.toggled.connect(self._on_power_toggle)
                cb_row.addWidget(self._enable_power_cb)
                cb_row.addStretch()
                grp_layout.addLayout(cb_row)

                # ── CL corridor option shown in FIRST popup ──
                cl_row = QHBoxLayout()
                cl_row.setContentsMargins(30, 2, 6, 2)

                self._use_cl_corridor_cb = QCheckBox("Use CL Center Line as corridor")
                self._use_cl_corridor_cb.setChecked(False)
                self._use_cl_corridor_cb.setEnabled(False)
                self._use_cl_corridor_cb.setToolTip(
                    "When enabled, Basic AI still classifies all normal classes.\n"
                    "The CL center line is used only as a corridor guide for Wire/Pole detection."
                )
                self._use_cl_corridor_cb.toggled.connect(self._on_cl_corridor_toggle)

                cl_row.addWidget(self._use_cl_corridor_cb)
                cl_row.addStretch()
                grp_layout.addLayout(cl_row)

                width_row = QHBoxLayout()
                width_row.setContentsMargins(54, 0, 6, 2)

                width_lbl = QLabel("CL Corridor Width (m):")
                width_lbl.setMinimumWidth(130)

                self._adv['power_corridor_width'] = QDoubleSpinBox()
                self._adv['power_corridor_width'].setRange(1.0, 50.0)
                self._adv['power_corridor_width'].setDecimals(1)
                self._adv['power_corridor_width'].setSingleStep(1.0)
                self._adv['power_corridor_width'].setValue(
                    float(_DEFAULTS.get('power_corridor_width', 8.0))
                )
                self._adv['power_corridor_width'].setMinimumWidth(70)
                self._adv['power_corridor_width'].setSizePolicy(
                    QSizePolicy.Expanding,
                    QSizePolicy.Fixed,
                )
                self._adv['power_corridor_width'].setEnabled(False)

                width_row.addWidget(width_lbl)
                width_row.addWidget(self._adv['power_corridor_width'], 1)
                width_row.addStretch()
                grp_layout.addLayout(width_row)

                self._cl_status_label = QLabel("CL corridor: enable Power Line Detection first.")
                self._cl_status_label.setWordWrap(True)
                self._cl_status_label.setStyleSheet("color:#777; font-size:10px; padding-left:56px;")
                grp_layout.addWidget(self._cl_status_label)

            row = QHBoxLayout()
            row.setContentsMargins(6, 4, 6, 4)

            name_lbl = QLabel(name)
            name_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

            spin = QSpinBox()
            spin.setRange(0, 255)
            spin.setValue(default_code)
            spin.setAlignment(Qt.AlignCenter)
            spin.setMinimumWidth(70)
            spin.setMinimumHeight(28)
            spin.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            spin.setButtonSymbols(QSpinBox.NoButtons)
            self._code_spins[key] = spin

            # Track power rows so we can grey them out when checkbox is off
            if key in ('wire', 'pole'):
                self._code_labels[key] = name_lbl
                spin.setEnabled(False)
                name_lbl.setEnabled(False)

            row.addWidget(name_lbl, 3)
            row.addWidget(spin, 2)
            grp_layout.addLayout(row)

        layout.addWidget(grp)

        # ── AI engine selector moved here from MAC/Advanced tab ──
        self._engine_box = QGroupBox("AI Engine")
        self._engine_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        engine_layout = QVBoxLayout(self._engine_box)
        engine_layout.setContentsMargins(18, 22, 18, 16)
        engine_layout.setSpacing(10)

        self._advanced_model_cb = QCheckBox("Use Advanced AI Model")
        self._advanced_model_cb.setChecked(False)
        self._advanced_model_cb.toggled.connect(self._on_advanced_ai_toggle)
        self._advanced_model_cb.setToolTip(
            "OFF: use current stable Basic AI model.\n"
            "ON: use Advanced AI model from Advance_Model folder with 68 features.\n\n"
            "Note: Advanced AI supports only 5 classes, so Power Line Detection "
            "will be disabled automatically."
        )

        engine_layout.addWidget(self._advanced_model_cb)

        # Advanced AI target class filter
        # Advanced AI target class filter
        # Advanced AI target class filter
        self._adv_target_box = QGroupBox("Advanced AI Target Classes")
        self._adv_target_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        adv_target_layout = QVBoxLayout(self._adv_target_box)
        adv_target_layout.setContentsMargins(28, 24, 18, 14)
        adv_target_layout.setSpacing(6)

        self._adv_all_cb = QCheckBox("All Classes")
        self._adv_all_cb.setChecked(False)
        self._adv_all_cb.setMinimumHeight(24)
        self._adv_all_cb.toggled.connect(self._on_adv_all_toggled)
        adv_target_layout.addWidget(self._adv_all_cb)

        self._adv_target_checks = {}

        for cls_id, cls_name in [
            (0, "Ground"),
            (1, "Low Vegetation"),
            (2, "Medium Vegetation"),
            (3, "High Vegetation"),
            (4, "Building"),
        ]:
            cb = QCheckBox(cls_name)
            cb.setChecked(cls_id == 4)
            cb.setMinimumHeight(24)
            cb.toggled.connect(self._on_adv_class_toggled)
            self._adv_target_checks[cls_id] = cb
            adv_target_layout.addWidget(cb)

        hint = QLabel(
            "Only selected Advanced AI classes will be applied. "
            "Unselected classes remain unchanged."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#777; font-size:10px; padding-top:6px;")
        adv_target_layout.addWidget(hint)

        engine_layout.addWidget(self._adv_target_box)
        self._sync_adv_target_box_visible()

        layout.addWidget(self._engine_box)

        # Presets
        preset_row = QHBoxLayout()
        preset_row.setSpacing(10)
        asprs_btn  = QPushButton("ASPRS Preset")
        asprs_btn.setToolTip(
            "Ground=1  LowVeg=2  MidVeg=3  HighVeg=4  Building=5  Wire=14  Pole=15"
        )
        asprs_btn.clicked.connect(self._apply_asprs)
        preset_row.addWidget(asprs_btn, 1)

        zero_btn = QPushButton("Zero-Based Preset")
        zero_btn.setToolTip(
            "Ground=0  LowVeg=1  MidVeg=2  HighVeg=3  Building=4  Wire=14  Pole=15"
        )
        zero_btn.clicked.connect(self._apply_zero)
        preset_row.addWidget(zero_btn, 1)

        layout.addLayout(preset_row)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setMinimumWidth(0)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(page)
        return scroll

    # ── POWER TOGGLE HANDLER ─────────────────────────────────

    def _on_power_toggle(self, checked: bool):
        """Enable / disable Wire, Pole, and CL corridor option."""
        for key in ('wire', 'pole'):
            if key in self._code_spins:
                self._code_spins[key].setEnabled(checked)
            if key in self._code_labels:
                self._code_labels[key].setEnabled(checked)

        if self._use_cl_corridor_cb is not None:
            self._use_cl_corridor_cb.setEnabled(bool(checked))

            if not checked:
                self._use_cl_corridor_cb.setChecked(False)

        if 'power_corridor_width' in self._adv:
            self._adv['power_corridor_width'].setEnabled(bool(checked))

        self._update_cl_corridor_status()

    def _on_cl_corridor_toggle(self, checked: bool):
        self._update_cl_corridor_status()

    def _get_power_corridor_width_value(self):
        try:
            if 'power_corridor_width' in self._adv:
                return float(self._adv['power_corridor_width'].value())
        except Exception:
            pass

        return float(_DEFAULTS.get('power_corridor_width', 8.0))

    def _update_cl_corridor_status(self):
        if self._cl_status_label is None:
            return

        if self._enable_power_cb is None or not self._enable_power_cb.isChecked():
            self._cl_status_label.setText("CL corridor: enable Power Line Detection first.")
            return

        if self._use_cl_corridor_cb is not None and self._use_cl_corridor_cb.isChecked():
            self._cl_status_label.setText(
                "CL corridor selected. Basic AI will classify all classes; CL will guide Wire/Pole detection."
            )
        else:
            self._cl_status_label.setText(
                "CL corridor ready. Tick this option to guide Wire/Pole detection near CL."
            )

    def _get_loaded_xyz(self):
        app = self._app

        if app is None or not hasattr(app, "data") or app.data is None:
            raise RuntimeError("No point cloud data loaded.")

        xyz = app.data.get("xyz")
        if xyz is None:
            raise RuntimeError("XYZ data missing from app.data.")

        xyz = np.asarray(xyz, dtype=np.float64)

        if xyz.ndim != 2 or xyz.shape[1] < 2 or len(xyz) == 0:
            raise RuntimeError("Invalid XYZ point cloud data.")

        return xyz

    def _polyline_length_xy(self, coords):
        pts = np.asarray(coords, dtype=np.float64)

        if pts.ndim != 2 or pts.shape[0] < 2:
            return 0.0

        d = np.diff(pts[:, :2], axis=0)
        return float(np.sqrt((d * d).sum(axis=1)).sum())

    def _is_cl_name(self, name):
        text = str(name or "").lower()
        return (
            text == "cl"
            or "cl " in text
            or " cl" in text
            or "center line" in text
            or "centre line" in text
            or "centerline" in text
            or "centreline" in text
        )

    def _is_white_color(self, color):
        """Return True for white/light CL-like vector color."""
        try:
            if color is None:
                return False

            rgb = np.asarray(color[:3], dtype=np.float64)
            return (
                rgb.size >= 3
                and float(rgb.min()) >= 0.72
                and float(rgb.max() - rgb.min()) <= 0.28
            )
        except Exception:
            return False

    def _extract_actor_polyline(self, actor):
        """
        Best-effort VTK actor polyline extraction.
        Works for many DXF/SNT line actors.
        """
        try:
            mapper = actor.GetMapper()
            if mapper is None:
                return None

            poly = mapper.GetInput()
            if poly is None or poly.GetNumberOfPoints() < 2:
                return None

            origin = np.asarray(actor.GetPosition(), dtype=np.float64)

            points = []
            for i in range(poly.GetNumberOfPoints()):
                p = np.asarray(poly.GetPoint(i), dtype=np.float64)
                points.append(tuple((p + origin).tolist()))

            if len(points) < 2:
                return None

            return points

        except Exception:
            return None

    def _collect_cl_candidates(self):
        """
        Collect possible CL center-line candidates from loaded app vectors/actors.
        The final route is selected by number of nearby point-cloud points.
        """
        app = self._app
        candidates = []

        def add_candidate(coords, name="", source="", color=None):
            try:
                arr = np.asarray(coords, dtype=np.float64)
                if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
                    return

                length = self._polyline_length_xy(arr)
                if length < 10.0:
                    return

                candidates.append({
                    "coords": arr[:, :3] if arr.shape[1] >= 3 else arr[:, :2],
                    "name": str(name or ""),
                    "source": str(source or ""),
                    "color": color,
                    "length": length,
                    "is_cl": self._is_cl_name(name),
                    "is_white": self._is_white_color(color),
                })
            except Exception:
                return

        # 1) Digitizer drawings, if CL was loaded/stored there
        try:
            digitizer = getattr(app, "digitizer", None)
            drawings = getattr(digitizer, "drawings", []) if digitizer is not None else []

            for d in drawings or []:
                if not isinstance(d, dict):
                    continue

                dtype = str(d.get("type", "")).lower()
                if dtype not in ("polyline", "line", "freehand"):
                    continue

                name = (
                    d.get("name")
                    or d.get("label")
                    or d.get("layer")
                    or d.get("source_layer")
                    or dtype
                )
                coords = d.get("coords")
                if coords is not None:
                    add_candidate(coords, name=name, source="digitizer")
        except Exception:
            pass

        # 2) SNT / DXF actors
        try:
            for attr in ("dxf_actors", "snt_actors", "vector_actors"):
                actor_groups = getattr(app, attr, None)
                if not actor_groups:
                    continue

                for group in actor_groups:
                    if isinstance(group, dict):
                        actors = group.get("actors", [])
                        group_name = (
                            group.get("layer")
                            or group.get("name")
                            or group.get("filename")
                            or attr
                        )
                    else:
                        actors = [group]
                        group_name = attr

                    for actor in actors:
                        coords = self._extract_actor_polyline(actor)
                        if coords is None:
                            continue

                        color = None
                        try:
                            color = actor.GetProperty().GetColor()
                        except Exception:
                            pass

                        actor_name = (
                            getattr(actor, "layer_name", None)
                            or getattr(actor, "name", None)
                            or getattr(actor, "grid_name", None)
                            or group_name
                        )

                        add_candidate(
                            coords,
                            name=actor_name,
                            source=attr,
                            color=color,
                        )
        except Exception:
            pass

        # 3) Generic app vectors/layers if available
        try:
            for attr in ("snt_attachments", "loaded_vectors", "vector_layers"):
                items = getattr(app, attr, None)
                if not items:
                    continue

                for item in items:
                    if not isinstance(item, dict):
                        continue

                    item_name = item.get("name") or item.get("layer") or item.get("filename") or attr

                    for key in ("coords", "points", "polyline", "line"):
                        coords = item.get(key)
                        if coords is not None:
                            add_candidate(coords, name=item_name, source=attr)

                    for list_key in ("entities", "lines", "polylines", "line_groups"):
                        values = item.get(list_key)
                        if not values:
                            continue

                        for ent in values:
                            if isinstance(ent, dict):
                                coords = ent.get("coords") or ent.get("points")
                                name = ent.get("name") or ent.get("layer") or item_name
                                if coords is not None:
                                    add_candidate(coords, name=name, source=attr)
        except Exception:
            pass

        return candidates
        # ── CL CORRIDOR SAFETY CONSTANTS ──────────────────────────
    _CL_CORRIDOR_MAX_CLOUD_RATIO = 0.30
    _CL_CORRIDOR_SCAN_LIMIT = 12
    _CL_CORRIDOR_MAX_ROUTE_LENGTH_M = 5000.0
    _CL_CORRIDOR_CHUNK_SIZE = 750_000

    def _cl_clean_text(self, value):
        return str(value or "").strip().upper()

    def _cl_polyline_length_2d_safe(self, coords):
        try:
            arr = np.asarray(coords, dtype=np.float64)

            if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
                return 0.0

            d = np.diff(arr[:, :2], axis=0)
            return float(np.sum(np.linalg.norm(d, axis=1)))

        except Exception:
            return 0.0

    def _is_true_cl_corridor_candidate(self, cand):
        """
        Strict CL filter.

        Accept only real CL layer/name.
        Reject bbox, BLOCKS, full SNT/DXF actors, and route helper geometry.
        """
        if not isinstance(cand, dict):
            return False

        name = self._cl_clean_text(cand.get("name"))
        layer = self._cl_clean_text(cand.get("layer"))
        source = str(cand.get("source", "") or "").strip().lower()

        bad_names = {
            "BBOX",
            "BLOCKS",
            "BOUNDINGBOX",
            "BOUNDING_BOX",
        }

        if name in bad_names or layer in bad_names:
            return False

        if name.endswith(".SNT") or name.endswith(".DXF"):
            return False

        if layer.endswith(".SNT") or layer.endswith(".DXF"):
            return False

        if source in {"dxf_actors", "snt_actors"}:
            return False

        return name == "CL" or layer == "CL"

    def _prepare_cl_corridor_candidates(self, candidates):
        """
        Keep only true CL candidates and remove duplicates.
        """
        prepared = []
        seen = set()

        for cand in candidates or []:
            if not self._is_true_cl_corridor_candidate(cand):
                continue

            coords = cand.get("coords")

            try:
                arr = np.asarray(coords, dtype=np.float64)
            except Exception:
                continue

            if arr.ndim != 2 or arr.shape[0] < 2 or arr.shape[1] < 2:
                continue

            length = float(cand.get("length", 0.0) or 0.0)

            if length <= 0.0:
                length = self._cl_polyline_length_2d_safe(arr)

            if length <= 0.0:
                continue

            if length > self._CL_CORRIDOR_MAX_ROUTE_LENGTH_M:
                print(
                    "  REJECT CL candidate: route too long "
                    f"name='{cand.get('name', '')}' "
                    f"source={cand.get('source', '')} "
                    f"length={length:.1f}m"
                )
                continue

            key = (
                self._cl_clean_text(cand.get("name")),
                self._cl_clean_text(cand.get("layer")),
                str(cand.get("source", "")),
                int(arr.shape[0]),
                round(float(arr[0, 0]), 2),
                round(float(arr[0, 1]), 2),
                round(float(arr[-1, 0]), 2),
                round(float(arr[-1, 1]), 2),
            )

            if key in seen:
                continue

            seen.add(key)

            fixed = dict(cand)
            fixed["coords"] = arr
            fixed["length"] = float(length)
            prepared.append(fixed)

        prepared.sort(key=lambda c: -float(c.get("length", 0.0)))
        return prepared

    def _polyline_chunk_mask_2d(self, chunk_xy, line_xy, radius):
        """
        Check one point chunk against one polyline.
        This prevents repeated full-cloud np.where memory pressure.
        """
        n = len(chunk_xy)
        inside = np.zeros(n, dtype=bool)

        if n == 0:
            return inside

        radius = float(radius)

        if radius <= 0.0:
            radius = 1.0

        r2 = radius * radius
        x = chunk_xy[:, 0]
        y = chunk_xy[:, 1]

        for i in range(len(line_xy) - 1):
            a = line_xy[i]
            b = line_xy[i + 1]
            ab = b - a
            ab2 = float(np.dot(ab, ab))

            if ab2 <= 1e-12:
                continue

            min_x = min(a[0], b[0]) - radius
            max_x = max(a[0], b[0]) + radius
            min_y = min(a[1], b[1]) - radius
            max_y = max(a[1], b[1]) + radius

            cand = np.flatnonzero(
                (~inside)
                & (x >= min_x)
                & (x <= max_x)
                & (y >= min_y)
                & (y <= max_y)
            )

            if cand.size == 0:
                continue

            p = chunk_xy[cand]

            t = (
                ((p[:, 0] - a[0]) * ab[0])
                + ((p[:, 1] - a[1]) * ab[1])
            ) / ab2

            t = np.clip(t, 0.0, 1.0)

            proj_x = a[0] + t * ab[0]
            proj_y = a[1] + t * ab[1]

            dx = p[:, 0] - proj_x
            dy = p[:, 1] - proj_y

            keep = (dx * dx + dy * dy) <= r2

            if np.any(keep):
                inside[cand[keep]] = True

        return inside

    def _count_points_near_polyline(self, xyz, coords, radius, stop_after=None):
        """
        Count corridor points safely without creating a full final mask.

        Used first so wrong CL candidates can be rejected early.
        """
        pts = np.asarray(coords, dtype=np.float64)

        if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 2:
            return 0

        xyz_arr = np.asarray(xyz)
        xy = xyz_arr[:, :2]
        line = pts[:, :2]

        total = 0
        chunk_size = int(self._CL_CORRIDOR_CHUNK_SIZE)

        for start in range(0, len(xy), chunk_size):
            end = min(start + chunk_size, len(xy))
            chunk_xy = np.asarray(xy[start:end], dtype=np.float64)

            inside = self._polyline_chunk_mask_2d(
                chunk_xy,
                line,
                radius,
            )

            total += int(inside.sum())

            if stop_after is not None and total > int(stop_after):
                return int(total)

        return int(total)

    def _points_near_polyline(self, xyz, coords, radius):
        """
        Same output as old function, but chunk-safe.
        Returns bool mask of len(xyz).
        """
        pts = np.asarray(coords, dtype=np.float64)

        if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 2:
            return np.zeros(len(xyz), dtype=bool)

        xyz_arr = np.asarray(xyz)
        xy = xyz_arr[:, :2]
        line = pts[:, :2]

        result = np.zeros(len(xy), dtype=bool)
        chunk_size = int(self._CL_CORRIDOR_CHUNK_SIZE)

        for start in range(0, len(xy), chunk_size):
            end = min(start + chunk_size, len(xy))
            chunk_xy = np.asarray(xy[start:end], dtype=np.float64)

            result[start:end] = self._polyline_chunk_mask_2d(
                chunk_xy,
                line,
                radius,
            )

        return result

    def _calculate_cl_corridor_target_indices(self):
        xyz = self._get_loaded_xyz()
        raw_candidates = self._collect_cl_candidates()

        if not raw_candidates:
            raise RuntimeError(
                "CL Center Line not found. Make sure CL/SNT/DXF vector line is loaded and visible."
            )

        radius = self._get_power_corridor_width_value()
        total_points = int(len(xyz))
        max_allowed = int(total_points * self._CL_CORRIDOR_MAX_CLOUD_RATIO)

        candidates = self._prepare_cl_corridor_candidates(raw_candidates)

        print("=" * 60)
        print("CL CORRIDOR SEARCH")
        print(f"  Raw candidates       : {len(raw_candidates)}")
        print(f"  Strict CL candidates : {len(candidates)}")
        print(f"  Radius               : ±{radius:.1f} m")
        print(f"  Cloud points         : {total_points:,}")
        print(
            f"  Max corridor allowed : {max_allowed:,} "
            f"({self._CL_CORRIDOR_MAX_CLOUD_RATIO * 100:.0f}%)"
        )

        if not candidates:
            raise RuntimeError(
                "No valid CL candidate found.\n\n"
                "Accepted only exact name/layer 'CL'.\n"
                "Rejected bbox, BLOCKS, full SNT/DXF geometry, dxf_actors, and snt_actors."
            )

        selected = None
        selected_count = 0

        for i, cand in enumerate(candidates[:self._CL_CORRIDOR_SCAN_LIMIT], start=1):
            coords = cand.get("coords")

            count = self._count_points_near_polyline(
                xyz,
                coords,
                radius,
                stop_after=max_allowed,
            )

            name = cand.get("name", "")
            layer = cand.get("layer", "")
            source = cand.get("source", "")
            length = float(cand.get("length", 0.0) or 0.0)

            print(
                f"  Candidate {i:02d}: "
                f"name='{name}' "
                f"layer='{layer}' "
                f"source={source} "
                f"length={length:.1f}m "
                f"corridor_points={count:,}"
            )

            if count <= 0:
                print("    REJECT: corridor contains zero points")
                continue

            if count > max_allowed:
                print(
                    "    REJECT: corridor too large "
                    f"({count:,}/{total_points:,} points > "
                    f"{self._CL_CORRIDOR_MAX_CLOUD_RATIO * 100:.0f}%)"
                )
                continue

            selected = cand
            selected_count = int(count)
            print("    ACCEPT: first safe real CL candidate")
            break

        if selected is None:
            raise RuntimeError(
                "No safe CL corridor found.\n\n"
                "All strict CL candidates were empty or larger than 30% of the cloud.\n"
                "This prevents selecting full SNT/DXF/BLOCKS/bbox geometry and avoids RAM blow-up."
            )

        best_mask = self._points_near_polyline(
            xyz,
            selected.get("coords"),
            radius,
        )

        target_indices = np.where(best_mask)[0].astype(np.int64)

        if target_indices is None or len(target_indices) == 0:
            raise RuntimeError(
                "CL corridor contains zero points. Increase Corridor Width or check CL coordinate alignment."
            )

        if len(target_indices) > max_allowed:
            raise RuntimeError(
                "Rejected CL corridor because it covers too much of the cloud.\n\n"
                f"Corridor points: {len(target_indices):,}\n"
                f"Cloud points: {total_points:,}\n"
                f"Limit: {max_allowed:,} points"
            )

        print("CL CORRIDOR SELECTED")
        print(f"  Name          : {selected.get('name', '')}")
        print(f"  Layer         : {selected.get('layer', '')}")
        print(f"  Source        : {selected.get('source', '')}")
        print(f"  Length        : {float(selected.get('length', 0.0) or 0.0):.1f} m")
        print(f"  Target points : {len(target_indices):,}")
        print("=" * 60)

        return target_indices
    
    def _sync_adv_target_box_visible(self):
        if self._adv_target_box is None:
            return

        is_advanced = (
            self._advanced_model_cb is not None
            and self._advanced_model_cb.isChecked()
        )

        self._adv_target_box.setVisible(is_advanced)

        if hasattr(self, "_engine_box") and self._engine_box is not None:
            if is_advanced:
                self._engine_box.setMinimumHeight(360)
                self._engine_box.setMaximumHeight(16777215)
            else:
                self._engine_box.setMinimumHeight(95)
                self._engine_box.setMaximumHeight(125)

        try:
            self.updateGeometry()
        except Exception:
            pass

    def _on_adv_all_toggled(self, checked):
        if not self._adv_target_checks:
            return

        for cb in self._adv_target_checks.values():
            cb.blockSignals(True)
            cb.setChecked(bool(checked))
            cb.blockSignals(False)


    def _on_adv_class_toggled(self, checked):
        if not self._adv_target_checks or self._adv_all_cb is None:
            return

        all_checked = all(cb.isChecked() for cb in self._adv_target_checks.values())

        self._adv_all_cb.blockSignals(True)
        self._adv_all_cb.setChecked(all_checked)
        self._adv_all_cb.blockSignals(False)


    def _get_selected_advanced_classes(self):
        selected = set()

        for cls_id, cb in self._adv_target_checks.items():
            if cb.isChecked():
                selected.add(int(cls_id))

        return selected


    def _on_advanced_ai_toggle(self, checked: bool):
        if checked:
            if self._enable_power_cb is not None:
                self._enable_power_cb.setChecked(False)
                self._enable_power_cb.setEnabled(False)

            if self._use_cl_corridor_cb is not None:
                self._use_cl_corridor_cb.setChecked(False)
                self._use_cl_corridor_cb.setEnabled(False)

            if 'power_corridor_width' in self._adv:
                self._adv['power_corridor_width'].setEnabled(False)

            for key in ('wire', 'pole'):
                if key in self._code_spins:
                    self._code_spins[key].setEnabled(False)
                if key in self._code_labels:
                    self._code_labels[key].setEnabled(False)
        else:
            if self._enable_power_cb is not None:
                self._enable_power_cb.setEnabled(True)
                self._on_power_toggle(self._enable_power_cb.isChecked())

        self._sync_adv_target_box_visible()

    # ── TAB 2: ADVANCED (2-column grid) ──────────────────────

    # def _build_advanced_tab(self):
    #     page   = QWidget()
    #     layout = QVBoxLayout(page)
    #     layout.setContentsMargins(10, 10, 10, 10)
    #     layout.setSpacing(8)

    #     # ── MAC loader row ──
    #     mac_row = QHBoxLayout()
    #     mac_row.setSpacing(8)

    #     self._mac_status = QLabel("No .mac file loaded")
    #     self._mac_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    #     browse_btn = QPushButton("Browse .mac…")
    #     browse_btn.setFixedWidth(120)
    #     browse_btn.clicked.connect(self._load_mac_file)

    #     reset_btn = QPushButton("Reset Defaults")
    #     reset_btn.setFixedWidth(120)
    #     reset_btn.setToolTip("Restore all Advanced parameters to their default values")
    #     reset_btn.clicked.connect(self._reset_defaults)

    #     mac_row.addWidget(self._mac_status)
    #     mac_row.addWidget(browse_btn)
    #     mac_row.addWidget(reset_btn)
    #     layout.addLayout(mac_row)

    #     # # ── AI engine selector ──
    #     # engine_box = QGroupBox("AI Engine")
    #     # engine_layout = QVBoxLayout(engine_box)

    #     # self._advanced_model_cb = QCheckBox("Use Advanced AI Model")
    #     # self._advanced_model_cb.setChecked(False)
    #     # self._advanced_model_cb.toggled.connect(self._on_advanced_ai_toggle)
    #     # self._advanced_model_cb.setToolTip(
    #     #     "OFF: use current stable 5-class AI model.\n"
    #     #     "ON: use advanced model from Advance_Model folder with 68 features."
    #     # )

    #     # engine_layout.addWidget(self._advanced_model_cb)
    #     # layout.addWidget(engine_box)

    #     sep = QFrame()
    #     sep.setFrameShape(QFrame.HLine)
    #     sep.setFrameShadow(QFrame.Sunken)
    #     layout.addWidget(sep)

    #     grid = QGridLayout()
    #     grid.setSpacing(8)

    #     grid.addWidget(self._build_csf_group(),  0, 0)
    #     grid.addWidget(self._build_hag_group(),  0, 1)
    #     grid.addWidget(self._build_wire_group(), 1, 0, 1, 2)

    #     layout.addLayout(grid)
    #     layout.addStretch()
    #     return page

    def _build_advanced_tab(self):
        page   = QWidget()
        page.setMinimumWidth(0)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ── MAC loader row ──
        mac_row = QHBoxLayout()
        mac_row.setSpacing(10)

        self._mac_status = QLabel("No .mac file loaded")
        self._mac_status.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        browse_btn = QPushButton("Browse .mac…")
        browse_btn.setMinimumWidth(80)
        browse_btn.clicked.connect(self._load_mac_file)

        reset_btn = QPushButton("Reset Defaults")
        reset_btn.setMinimumWidth(90)
        reset_btn.setToolTip("Restore all MAC extracted parameters to their default values")
        reset_btn.clicked.connect(self._reset_defaults)

        mac_row.addWidget(self._mac_status)
        mac_row.addWidget(browse_btn)
        mac_row.addWidget(reset_btn)
        layout.addLayout(mac_row)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setFrameShadow(QFrame.Sunken)
        layout.addWidget(sep)

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(12)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 80)
        grid.setColumnMinimumWidth(1, 80)

        grid.addWidget(self._build_csf_group(),  0, 0)
        grid.addWidget(self._build_hag_group(),  0, 1)
        grid.addWidget(self._build_wire_group(), 1, 0, 1, 2)

        layout.addLayout(grid)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setMinimumWidth(0)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        scroll.setWidget(page)
        return scroll
    
    def _build_fence_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        info = QLabel(
            "Optional: Select fence(s) to classify only a specific region.\n"
            "If no fence is selected, AI runs on the full loaded point cloud.\n\n"
            "Important: Fence mode is supported for Advanced AI."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        # try:
        #     self._fence_sel = AIFenceSelectorWidget(self._app, parent=page)
        #     layout.addWidget(self._fence_sel)
        # except Exception as e:
        #     self._fence_sel = None
        #     warn = QLabel(
        #         "Fence selector failed to initialize.\n"
        #         "AI will run on full point cloud.\n"
        #         f"Details: {e}"
        #     )
        #     warn.setWordWrap(True)
        #     warn.setStyleSheet("color:#ff9800;")
        #     layout.addWidget(warn)

        self._fence_sel = None

        note = QLabel(
            "Fence selection is handled by the AI Fence Selection popup.\n"
            "Use Start Classification, then select Run Full File or Run Selected Fence(s)."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#777;")
        layout.addWidget(note)

        layout.addStretch()
        return page

    # ── GROUP BUILDERS ────────────────────────────────────────

    def _build_csf_group(self):
        grp  = QGroupBox("CSF Ground Extraction")
        grp.setMinimumWidth(0)
        grp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        form = QFormLayout(grp)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        form.setContentsMargins(14, 20, 14, 12)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._adv['csf_cloth_resolution'] = self._dspin(
            0.1, 5.0, _DEFAULTS['csf_cloth_resolution'], 0.1, 2,
            "Cloth grid resolution. Larger = smoother ground model."
        )
        self._adv['csf_rigidness'] = self._ispin(
            1, 3, _DEFAULTS['csf_rigidness'],
            "1 = flat/gentle    2 = moderate    3 = steep/rough terrain"
        )
        self._adv['csf_class_threshold'] = self._dspin(
            0.05, 2.0, _DEFAULTS['csf_class_threshold'], 0.05, 2,
            "Max height above cloth surface to be considered ground."
        )
        form.addRow("Cloth Resolution (m):", self._adv['csf_cloth_resolution'])
        form.addRow("Rigidness (1–3):",       self._adv['csf_rigidness'])
        form.addRow("Class Threshold (m):",   self._adv['csf_class_threshold'])
        return grp

    def _build_hag_group(self):
        grp  = QGroupBox("Vegetation Height Boundaries")
        grp.setMinimumWidth(0)
        grp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        form = QFormLayout(grp)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setLabelAlignment(Qt.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        form.setContentsMargins(14, 20, 14, 12)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._adv['lowveg_min']  = self._dspin(
            0.0, 1.0,  _DEFAULTS['lowveg_min'],  0.05, 2,
            "Points below this HAG are forced to Ground."
        )
        self._adv['lowveg_max']  = self._dspin(
            0.1, 2.0,  _DEFAULTS['lowveg_max'],  0.05, 2,
            "Low Vegetation upper height boundary."
        )
        self._adv['midveg_max']  = self._dspin(
            0.5, 15.0, _DEFAULTS['midveg_max'],  0.25, 2,
            "Medium Vegetation upper height boundary."
        )
        self._adv['highveg_min'] = self._dspin(
            0.5, 15.0, _DEFAULTS['highveg_min'], 0.25, 2,
            "High Vegetation lower boundary."
        )
        form.addRow("Low Veg min (m):",  self._adv['lowveg_min'])
        form.addRow("Low Veg max (m):",  self._adv['lowveg_max'])
        form.addRow("Mid Veg max (m):",  self._adv['midveg_max'])
        form.addRow("High Veg min (m):", self._adv['highveg_min'])
        return grp

    def _build_wire_group(self):
        grp  = QGroupBox("Wire Detection Geometry")
        grp.setToolTip("Used only when Power Line Detection is enabled.")
        grp.setMinimumWidth(0)
        grp.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        grid = QGridLayout(grp)
        grid.setContentsMargins(14, 20, 14, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnMinimumWidth(1, 70)
        grid.setColumnMinimumWidth(3, 70)

        self._adv['wire_hag_min'] = self._dspin(
            0.0, 30.0, _DEFAULTS['wire_hag_min'], 0.5, 1,
            "Wire points below this height are ignored."
        )
        self._adv['wire_hag_max'] = self._dspin(
            5.0, 200.0, _DEFAULTS['wire_hag_max'], 5.0, 1,
            "Wire points above this height are ignored."
        )
        self._adv['wire_chain_radius'] = self._dspin(
            0.5, 20.0, _DEFAULTS['wire_chain_radius'], 0.5, 1,
            "Max gap between wire points to connect."
        )
        self._adv['wire_density_max'] = self._ispin(
            1, 200, _DEFAULTS['wire_density_max'],
            "Max neighbours in 0.5 m radius — filters dense non-wire clusters."
        )
        self._adv['wire_min_segment_pts'] = self._ispin(
            5, 500, _DEFAULTS['wire_min_segment_pts'],
            "Minimum connected points to keep a wire segment."
        )
        self._adv['wire_linearity_min'] = self._dspin(
            0.30, 0.99, _DEFAULTS['wire_linearity_min'], 0.01, 2,
            "Minimum linearity score (0–1). Higher = stricter."
        )

        params_grid = [
            ("HAG Min (m)",         'wire_hag_min',        0, 0),
            ("HAG Max (m)",         'wire_hag_max',        0, 2),
            ("Chain Radius (m)",    'wire_chain_radius',   1, 0),
            ("Density Max",         'wire_density_max',    1, 2),
            ("Min Segment Pts",     'wire_min_segment_pts',2, 0),
            ("Linearity Min",       'wire_linearity_min',  2, 2),
        ]
        for label_text, key, row, col in params_grid:
            lbl = QLabel(label_text + ":")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lbl,                   row, col)
            grid.addWidget(self._adv[key],        row, col + 1)

        return grp

    # ── MAC LOADER ────────────────────────────────────────────

    def _load_mac_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load TerraScan Macro", "",
            "TerraScan Macro (*.mac);;All Files (*)"
        )
        if not path:
            return

        try:
            params = parse_mac_file(path)
        except Exception as e:
            QMessageBox.warning(self, "MAC Parse Error",
                f"Could not parse the file:\n\n{str(e)}")
            self._mac_status.setText("⚠  Parse failed")
            return

        if not params:
            self._mac_status.setText("⚠  No params found")
            return

        _type_map = {
            'csf_cloth_resolution': float, 'csf_rigidness':        int,
            'csf_class_threshold':  float, 'lowveg_min':           float,
            'lowveg_max':           float, 'midveg_max':           float,
            'highveg_min':          float, 'wire_hag_min':         float,
            'wire_hag_max':         float, 'wire_chain_radius':    float,
            'wire_density_max':     int,   'wire_min_segment_pts': int,
            'wire_linearity_min':   float,
        }
        n_filled = 0
        for key, typ in _type_map.items():
            if key in params and key in self._adv:
                self._adv[key].setValue(typ(params[key]))
                n_filled += 1

        self._mac_status.setText(
            f"✓  {Path(path).name}  ({n_filled} params loaded)"
        )
        print(f"\n  MAC loaded: {path}  ({n_filled} params)")
        for k in params:
            print(f"    {k}: {params[k]}")

    # ── RESET ─────────────────────────────────────────────────

    def _reset_defaults(self):
        for key, val in _DEFAULTS.items():
            if key in self._adv:
                self._adv[key].setValue(val)
        self._mac_status.setText("No .mac file loaded")

    # ── WIDGET FACTORIES ─────────────────────────────────────

    def _dspin(self, lo, hi, val, step, dec, tip=""):
        w = QDoubleSpinBox()
        w.setRange(lo, hi); w.setValue(val)
        w.setSingleStep(step); w.setDecimals(dec)
        w.setAlignment(Qt.AlignRight)
        w.setMinimumWidth(45)
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if tip: w.setToolTip(tip)
        return w

    def _ispin(self, lo, hi, val, tip=""):
        w = QSpinBox()
        w.setRange(lo, hi); w.setValue(val)
        w.setAlignment(Qt.AlignRight)
        w.setMinimumWidth(45)
        w.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if tip: w.setToolTip(tip)
        return w

    # ── PRESETS ───────────────────────────────────────────────

    def _apply_asprs(self):
        for k, v in {0:1, 1:2, 2:3, 3:4, 4:5, 'wire':14, 'pole':15}.items():
            self._code_spins[k].setValue(v)

    def _apply_zero(self):
        for k, v in {0:0, 1:1, 2:2, 3:3, 4:4, 'wire':14, 'pole':15}.items():
            self._code_spins[k].setValue(v)



    def _calculate_cl_corridor_prior(self):
        """
        Find the real CL center line and return it as a power-line prior.

        IMPORTANT:
        This does NOT become target_indices.
        Basic AI still classifies the full/fence cloud.
        This only guides Wire/Pole detection near CL.
        """
        xyz = self._get_loaded_xyz()
        candidates = self._collect_cl_candidates()

        if not candidates:
            raise RuntimeError(
                "CL Center Line not found. Make sure CL/SNT/DXF vector line is loaded and visible."
            )

        radius = self._get_power_corridor_width_value()
        total_points = int(len(xyz))
        max_allowed_points = int(total_points * 0.30)

        valid_cl_candidates = []

        for cand in candidates:
            name = str(cand.get("name", "") or "").strip()
            layer = str(cand.get("layer", "") or "").strip()
            source = str(cand.get("source", "") or "").lower()
            coords = cand.get("coords")

            if coords is None:
                continue

            lname = name.lower()
            llayer = layer.lower()

            # Only accept true CL candidates.
            is_true_cl = (
                cand.get("is_cl")
                or lname == "cl"
                or llayer == "cl"
            )

            if not is_true_cl:
                continue

            # Reject bbox/block/full geometry style candidates.
            bad_text = f"{lname} {llayer} {source}"
            if (
                "bbox" in bad_text
                or "block" in bad_text
                or "blocks" in bad_text
                or "boundary" in bad_text
                or "grid" in bad_text
            ):
                continue

            try:
                pts = np.asarray(coords, dtype=np.float64)
                if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 2:
                    continue
            except Exception:
                continue

            length = float(cand.get("length", 0.0) or 0.0)

            valid_cl_candidates.append({
                **cand,
                "name": name,
                "layer": layer,
                "source": source,
                "length": length,
                "coords": pts,
            })

        if not valid_cl_candidates:
            raise RuntimeError(
                "No valid CL candidate found.\n\n"
                "Only real CL layer/name is allowed. BBOX/BLOCK/full SNT geometry is rejected."
            )

        valid_cl_candidates = sorted(
            valid_cl_candidates,
            key=lambda c: float(c.get("length", 0.0) or 0.0),
            reverse=True,
        )

        print("=" * 60)
        print("CL POWER-LINE PRIOR SEARCH")
        print(f"  Valid CL candidates : {len(valid_cl_candidates)}")
        print(f"  Radius              : ±{radius:.1f} m")
        print(f"  Max allowed points  : {max_allowed_points:,} / {total_points:,}")

        rejected_large = []

        for i, cand in enumerate(valid_cl_candidates[:30], start=1):
            coords = cand.get("coords")
            mask = self._points_near_polyline(xyz, coords, radius)
            count = int(mask.sum())

            print(
                f"  Candidate {i:02d}: "
                f"name='{cand.get('name', '')}' "
                f"source={cand.get('source', '')} "
                f"length={cand.get('length', 0.0):.1f}m "
                f"corridor_points={count:,}"
            )

            if count <= 0:
                continue

            if count > max_allowed_points:
                rejected_large.append((cand, count))
                print(
                    f"    REJECTED: corridor too large "
                    f"({count:,} > {max_allowed_points:,}, more than 30% of cloud)"
                )
                continue

            if self._cl_status_label is not None:
                self._cl_status_label.setText(
                    f"CL corridor selected: {count:,} points, "
                    f"length {cand.get('length', 0.0):.1f} m, "
                    f"width ±{radius:.1f} m."
                )

            print("CL POWER-LINE PRIOR SELECTED")
            print(f"  Name            : {cand.get('name', '')}")
            print(f"  Source          : {cand.get('source', '')}")
            print(f"  Length          : {cand.get('length', 0.0):.1f} m")
            print(f"  Corridor points : {count:,}")

            return {
                "coords": np.asarray(coords, dtype=np.float32).tolist(),
                "point_count": count,
                "name": str(cand.get("name", "") or ""),
                "source": str(cand.get("source", "") or ""),
                "length": float(cand.get("length", 0.0) or 0.0),
            }

        if rejected_large:
            biggest = max(rejected_large, key=lambda x: x[1])
            raise RuntimeError(
                "CL candidate found, but corridor is too large.\n\n"
                f"Selected-like corridor points: {biggest[1]:,}\n"
                f"Maximum allowed: {max_allowed_points:,} "
                f"(30% of {total_points:,})\n\n"
                "This is likely not the real CL line, or corridor width is too high."
            )

        raise RuntimeError(
            "CL candidate found, but corridor contains zero points.\n\n"
            "Increase CL Corridor Width or check if the CL line overlaps the loaded point cloud."
        )

    # ── VALIDATE + ACCEPT ────────────────────────────────────

    def _validate_and_accept(self):
        enable_power = self._enable_power_cb.isChecked()
        use_advanced_ai = self._advanced_model_cb.isChecked()
        selected_advanced_classes = {0, 1, 2, 3, 4}

        use_cl_corridor = (
            enable_power
            and self._use_cl_corridor_cb is not None
            and self._use_cl_corridor_cb.isChecked()
        )

        cl_corridor_prior = None

        if use_advanced_ai:
            selected_advanced_classes = self._get_selected_advanced_classes()

            if not selected_advanced_classes:
                self.tabs.setCurrentIndex(0)
                QMessageBox.warning(
                    self,
                    "No Advanced Classes Selected",
                    "Select at least one Advanced AI class.\n\n"
                    "Example: select Building if you want only building points classified."
                )
                return

        if use_advanced_ai and enable_power:
            self.tabs.setCurrentIndex(0)
            QMessageBox.warning(
                self,
                "Advanced AI Limitation",
                "Advanced AI supports only 5 classes:\n\n"
                "Ground / Low Vegetation / Medium Vegetation / High Vegetation / Building\n\n"
                "Disable Power Line Detection to use Advanced AI."
            )
            return
        
        if use_cl_corridor:
            if use_advanced_ai:
                self.tabs.setCurrentIndex(0)
                QMessageBox.warning(
                    self,
                    "CL Corridor Limitation",
                    "CL corridor power-line detection must run with Basic AI.\n\n"
                    "Disable Advanced AI and try again."
                )
                return

            try:
                cl_corridor_prior = self._calculate_cl_corridor_prior()
            except Exception as e:
                QMessageBox.warning(
                    self,
                    "CL Corridor Failed",
                    str(e)
                )
                return

            if not cl_corridor_prior or not cl_corridor_prior.get("coords"):
                QMessageBox.warning(
                    self,
                    "Empty CL Corridor",
                    "CL corridor contains zero points.\n\n"
                    "Increase Corridor Width or check CL position."
                )
                return

        model_codes = [self._code_spins[i].value() for i in range(5)]

        # Only validate wire/pole codes when power detection is enabled.
        if enable_power:
            wire_code = self._code_spins['wire'].value()
            pole_code = self._code_spins['pole'].value()
            all_codes = model_codes + [wire_code, pole_code]

            if len(set(all_codes)) != len(all_codes):
                dupes = {c for c in all_codes if all_codes.count(c) > 1}
                QMessageBox.warning(self, "Duplicate Codes",
                    f"All output codes must be unique.\nDuplicates: {dupes}")
                return
            if wire_code == pole_code:
                QMessageBox.warning(self, "Duplicate Power Codes",
                    f"Wire and Pole codes must differ (both = {wire_code}).")
                return
        else:
            # Validate only the 5 base classes
            if len(set(model_codes)) != len(model_codes):
                dupes = {c for c in model_codes if model_codes.count(c) > 1}
                QMessageBox.warning(self, "Duplicate Codes",
                    f"All output codes must be unique.\nDuplicates: {dupes}")
                return
            wire_code = self._code_spins['wire'].value()
            pole_code = self._code_spins['pole'].value()

        lv_min = self._adv['lowveg_min'].value()
        lv_max = self._adv['lowveg_max'].value()
        mv_max = self._adv['midveg_max'].value()
        wh_min = self._adv['wire_hag_min'].value()
        wh_max = self._adv['wire_hag_max'].value()

        if lv_min >= lv_max:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "Invalid HAG Boundaries",
                "Low Veg min must be less than Low Veg max.")
            return
        if lv_max >= mv_max:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "Invalid HAG Boundaries",
                "Low Veg max must be less than Mid Veg max.")
            return
        if enable_power and wh_min >= wh_max:
            self.tabs.setCurrentIndex(1)
            QMessageBox.warning(self, "Invalid Wire HAG Range",
                "Wire HAG Min must be less than Wire HAG Max.")
            return

        self.accepted_enable_power_lines = enable_power
        self.accepted_class_mapping      = {i: self._code_spins[i].value()
                                             for i in range(5)}
        self.accepted_power_mapping      = {
            InferenceConfig.WIRE_INTERNAL_CODE: wire_code,
            InferenceConfig.POLE_INTERNAL_CODE: pole_code,
        }
        self.accepted_ai_mode = (
            "advanced" if self._advanced_model_cb.isChecked() else "basic"
        )
        self.accepted_advanced_target_classes = set(selected_advanced_classes)

        cl_coords = None
        cl_point_count = 0
        cl_name = ""
        cl_source = ""
        cl_length = 0.0

        if cl_corridor_prior:
            cl_coords = cl_corridor_prior.get("coords")
            cl_point_count = int(cl_corridor_prior.get("point_count", 0) or 0)
            cl_name = str(cl_corridor_prior.get("name", "") or "")
            cl_source = str(cl_corridor_prior.get("source", "") or "")
            cl_length = float(cl_corridor_prior.get("length", 0.0) or 0.0)

        self.accepted_advanced = {
            'ai_mode': self.accepted_ai_mode,
            'advanced_target_classes': sorted(self.accepted_advanced_target_classes),
            'csf_cloth_resolution':  self._adv['csf_cloth_resolution'].value(),
            'csf_rigidness':         self._adv['csf_rigidness'].value(),
            'csf_class_threshold':   self._adv['csf_class_threshold'].value(),
            'lowveg_min':  lv_min,  'lowveg_max':  lv_max,
            'midveg_max':  mv_max,
            'highveg_min': self._adv['highveg_min'].value(),
            'wire_hag_min':          wh_min,
            'wire_hag_max':          wh_max,
            'wire_chain_radius':     self._adv['wire_chain_radius'].value(),
            'wire_density_max':      self._adv['wire_density_max'].value(),
            'wire_min_segment_pts':  self._adv['wire_min_segment_pts'].value(),
            'wire_linearity_min':    self._adv['wire_linearity_min'].value(),

            # CL corridor guidance for power-line/pole detection.
            # This is a prior only. It must NOT limit normal 5-class classification.
            'power_corridor_width':       self._get_power_corridor_width_value(),
            'cl_corridor_width':          self._get_power_corridor_width_value(),
            'use_cl_corridor':            bool(use_cl_corridor),
            'use_cl_powerline_prior':     bool(use_cl_corridor and cl_coords),
            'cl_corridor_coords':         cl_coords,
            'cl_corridor_point_count':    cl_point_count,
            'cl_corridor_name':           cl_name,
            'cl_corridor_source':         cl_source,
            'cl_corridor_length':         cl_length,
        }

        # CL is only a power-line guide. It must not become classification scope.
        # Fence selection, if any, is handled by AIFenceRunDialog after this dialog.
        self.accepted_target_indices = None
        self.accepted_selected_fence_count = 0
        self.accepted_selected_fence_drawings = []

        self.accept()

    def get_class_mapping(self):        return self.accepted_class_mapping
    def get_power_mapping(self):        return self.accepted_power_mapping
    def get_advanced_config(self):      return self.accepted_advanced
    def get_enable_power_lines(self):   return self.accepted_enable_power_lines
    def get_ai_mode(self):              return self.accepted_ai_mode
    def get_advanced_target_classes(self):
        return set(self.accepted_advanced_target_classes or {4})
    def get_target_indices(self):       return self.accepted_target_indices

    def get_selected_fence_count(self):
        return int(getattr(self, "accepted_selected_fence_count", 0) or 0)

    def get_selected_fence_drawings(self):
        return list(getattr(self, "accepted_selected_fence_drawings", []) or [])


# ═══════════════════════════════════════════════════════════════
# PROGRESS DIALOG
# ═══════════════════════════════════════════════════════════════

class AIClassificationDialog(QDialog):

    def __init__(self, app, class_mapping, power_mapping,
             advanced_config=None, enable_power_lines=False,
             ai_mode="basic", target_indices=None,
             selected_fence_count=0, advanced_target_classes=None,
             selected_fence_drawings=None,
             parent=None):
        super().__init__(parent)
        self.app                = app
        self.class_mapping      = class_mapping
        self.power_mapping      = power_mapping
        self.advanced_config    = advanced_config or {}
        self.enable_power_lines = bool(enable_power_lines)
        self.ai_mode            = ai_mode
        self.advanced_target_classes = set(
            advanced_target_classes
            if advanced_target_classes is not None
            else self.advanced_config.get("advanced_target_classes", [4])
        )
        self.target_indices     = None

        if target_indices is not None:
            arr = np.asarray(target_indices, dtype=np.int64).ravel()
            if arr.size > 0:
                self.target_indices = arr

        self.selected_fence_count = int(selected_fence_count or 0)
        self.selected_fence_drawings = list(selected_fence_drawings or [])
        self._original_classification_for_fence = None

        # AI undo backup
        # Saved before AI starts, pushed to app undo stack after AI finishes.
        self._ai_undo_before_classes = None

        self.worker             = None
        self._finish_handled    = False
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle("AI Classification")
        self.setWindowFlags(
            self.windowFlags()
            | Qt.Window
            | Qt.WindowMinimizeButtonHint
            | Qt.WindowMaximizeButtonHint
            | Qt.WindowCloseButtonHint
        )
        self.setMinimumSize(320, 260)
        self.setMaximumSize(16777215, 16777215)
        self.setSizeGripEnabled(True)
        self.setModal(False)
        self.setWindowModality(Qt.NonModal)
        layout = QVBoxLayout(self)

        model_txt = "Advanced AI" if self.ai_mode == "advanced" else "Basic AI"
        mode = "with Power Lines" if self.enable_power_lines else "5-class mode"

        if self.ai_mode == "advanced":
            target_names = self._advanced_target_class_names()
            if target_names != "All Classes":
                mode = f"{mode} | Target: {target_names}"

        if self.target_indices is not None:
            fence_txt = (
                f"Fence mode ON - {len(self.target_indices):,} points inside "
                f"{self.selected_fence_count} selected fence(s)."
            )
        else:
            fence_txt = "Full-file mode."

        self.status_label = QLabel(
            f"Initializing {model_txt} pipeline ({mode}).\n{fence_txt}"
        )
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        btn_row = QHBoxLayout()
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_classification)
        btn_row.addWidget(self.cancel_btn)

        self.close_btn = QPushButton("Close")
        self.close_btn.setEnabled(False)
        self.close_btn.clicked.connect(self.accept)
        btn_row.addWidget(self.close_btn)

        layout.addLayout(btn_row)

    def _snapshot_ai_undo_before(self, data_dict):
        """
        Save classification before AI starts.
        """
        self._ai_undo_before_classes = None

        try:
            if data_dict is None:
                return

            cls = data_dict.get("classification")
            xyz = data_dict.get("xyz")

            if cls is None:
                if xyz is None:
                    return
                self._ai_undo_before_classes = np.zeros(len(xyz), dtype=np.uint8)
            else:
                self._ai_undo_before_classes = np.asarray(cls).copy()

            print(
                f"AI undo snapshot saved: "
                f"{len(self._ai_undo_before_classes):,} points"
            )

        except Exception as e:
            print(f"Could not save AI undo snapshot: {e}")
            self._ai_undo_before_classes = None

    def _push_ai_undo_after(self):
        """
        Push AI classification undo step after AI finishes.

        Returns:
            tuple[int, np.ndarray | None]:
                (changed_count, changed_mask)
                changed_mask is the undo-scope boolean mask (or None).
        """
        try:
            changed_mask = self._compute_ai_changed_mask_after(restrict_to_fence=True)
            if changed_mask is None:
                return 0, None

            changed_count = int(changed_mask.sum())
            if changed_count == 0:
                print("AI undo skipped: no classification changes detected.")
                return 0, changed_mask

            before = np.asarray(self._ai_undo_before_classes)
            after = np.asarray(self.app.data.get("classification"))

            undo_step = {
                "type": "ai_classification",
                "mask": changed_mask.copy(),
                "old_classes": before[changed_mask].copy(),
                "oldclasses": before[changed_mask].copy(),
                "new_classes": after[changed_mask].copy(),
                "newclasses": after[changed_mask].copy(),
                "ai_mode": self.ai_mode,
                "fence_mode": self.target_indices is not None,
                "selected_fence_count": int(self.selected_fence_count or 0),
            }

            if hasattr(self.app, "undo_stack"):
                self.app.undo_stack.append(undo_step)
            elif hasattr(self.app, "undostack"):
                self.app.undostack.append(undo_step)
            else:
                print("AI undo skipped: app has no undo stack.")
                return 0, changed_mask

            if hasattr(self.app, "redo_stack"):
                self.app.redo_stack.clear()
            if hasattr(self.app, "redostack"):
                self.app.redostack.clear()

            try:
                from gui.memory_manager import trim_undo_stack
                trim_undo_stack(self.app)
            except Exception:
                pass

            print(
                f"AI undo step added: {changed_count:,} changed points "
                f"| mode={self.ai_mode} "
                f"| fence={self.target_indices is not None}"
            )
            return changed_count, changed_mask

        except Exception as e:
            print(f"AI undo push failed: {e}")
            return 0, None

    def _compute_ai_changed_mask_after(self, restrict_to_fence=False):
        """
        Compare pre-AI snapshot vs current classification and return changed mask.
        """
        try:
            before = self._ai_undo_before_classes
            if before is None:
                return None

            if not hasattr(self.app, "data") or self.app.data is None:
                return None

            after = self.app.data.get("classification")
            if after is None:
                return None

            before = np.asarray(before)
            after = np.asarray(after)

            if len(before) != len(after):
                print(
                    "AI changed-mask skipped: length mismatch "
                    f"before={len(before):,}, after={len(after):,}"
                )
                return None

            changed_mask = (before != after)

            if restrict_to_fence and self.target_indices is not None:
                target = np.asarray(self.target_indices, dtype=np.int64).ravel()
                target = target[(target >= 0) & (target < len(after))]
                fence_mask = np.zeros(len(after), dtype=bool)
                fence_mask[target] = True
                changed_mask &= fence_mask

            return changed_mask
        except Exception as e:
            print(f"AI changed-mask build failed: {e}")
            return None

    def _auto_apply_active_ptc_palette(self):
        """
        Keep active PTC palette/colors/weights bound to AI output.
        """
        try:
            dialog = getattr(self.app, "display_mode_dialog", None)
            if dialog is None:
                return

            ptc_path = getattr(dialog, "current_ptc_path", None)
            if not ptc_path:
                return

            from gui.class_display import clone_palette

            main_palette = None
            if hasattr(dialog, "view_palettes"):
                main_palette = dialog.view_palettes.get(0)

            if main_palette:
                self.app.class_palette = clone_palette(main_palette)
                if not hasattr(self.app, "view_palettes") or self.app.view_palettes is None:
                    self.app.view_palettes = {}
                self.app.view_palettes[0] = clone_palette(main_palette)
                print("✅ AI finish: active PTC palette auto-applied to Main View.")
            elif hasattr(dialog, "load_classes_from_path"):
                dialog.load_classes_from_path(ptc_path, force_colors=False)
                print("✅ AI finish: reloaded active PTC for palette sync.")
        except Exception as e:
            print(f"⚠️ AI finish PTC auto-apply skipped: {e}")

    def _mark_selected_fences_as_classified(self):
        """
        Tag AI-used digitized fences so Draw->Clear shows classified fence picker.
        """
        if not self.selected_fence_drawings:
            return

        marked = 0
        recolored = 0

        for fence in list(self.selected_fence_drawings):
            if not isinstance(fence, dict):
                continue
            try:
                fence["classified_fence"] = True
                marked += 1
            except Exception:
                pass

            actor = fence.get("actor")
            if actor is None:
                continue
            try:
                prop = actor.GetProperty()
                if prop is not None:
                    prop.SetColor(0.0, 1.0, 1.0)
                    prop.SetLineWidth(4.0)
                recolored += 1
            except Exception:
                pass

        if marked > 0:
            print(
                f"✅ AI finish: marked {marked} fence(s) as classified "
                f"(recolored: {recolored})."
            )

    def _refresh_views_after_ai(self, changed_mask):
        """
        Route AI completion through the standard app refresh/sync pipeline.
        Always emit classification_finished so cross-section, cut-section,
        stats, and any cached section buffers refresh after AI.
        """
        self._auto_apply_active_ptc_palette()

        try:
            if hasattr(self.app, "refresh_all_views"):
                self.app.refresh_all_views()
            else:
                from gui.class_display import update_class_mode
                update_class_mode(self.app, force_refresh=True)
        except Exception as e:
            print(f"AI finish main-view refresh failed: {e}")

        try:
            if hasattr(self.app, "classification_finished"):
                self.app.classification_finished.emit(changed_mask)
        except Exception as e:
            print(f"AI finish signal emit failed: {e}")

        try:
            digitizer = getattr(self.app, "digitizer", None)
            if digitizer is not None and hasattr(digitizer, "rebind_drawings"):
                digitizer.rebind_drawings()
        except Exception as e:
            print(f"AI finish drawing rebind skipped: {e}")

    def start_classification(self, data_dict):
        if getattr(self, "_classification_started", False):
            print("AI start ignored: classification is already running for this dialog.")
            return
        self._classification_started = True
        self._finish_handled = False
        self._snapshot_ai_undo_before(data_dict)

        try:
            if self.target_indices is not None:
                cls = data_dict.get("classification")
                if cls is not None:
                    self._original_classification_for_fence = np.asarray(cls).copy()
                    print(
                        f"AI Fence Mode: {len(self.target_indices):,} target points "
                        f"inside {self.selected_fence_count} fence(s)"
                    )
        except Exception as e:
            print(f"Could not snapshot original classification for fence mode: {e}")
            self._original_classification_for_fence = None

        if self.ai_mode == "advanced":
            from gui.advance_ai_inference import AdvancedInferenceWorker

            self.worker = AdvancedInferenceWorker(
                data_dict=data_dict,
                class_mapping=self.class_mapping,
                power_mapping=self.power_mapping,
                advanced_config=self.advanced_config,
                enable_power_lines=False,
                target_indices=self.target_indices,
            )
        else:
            from gui.ai_inference import InferenceWorker

            self.worker = InferenceWorker(
                data_dict,
                self.class_mapping,
                self.power_mapping,
                advanced_config=self.advanced_config,
                enable_power_lines=self.enable_power_lines,
                target_indices=self.target_indices,
            )

        self.worker.progress.connect(self.update_progress)
        self.worker.finished.connect(self.classification_finished)
        self.worker.error.connect(self.classification_error)
        self.worker.start()

    def update_progress(self, percent, message):
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)
    
    def _advanced_target_class_names(self):
        names = {
            0: "Ground",
            1: "Low Veg",
            2: "Medium Veg",
            3: "High Veg",
            4: "Building",
        }

        selected = set(int(x) for x in (self.advanced_target_classes or {4}))

        if selected == {0, 1, 2, 3, 4}:
            return "All Classes"

        return ", ".join(names.get(i, str(i)) for i in sorted(selected))


    def _apply_advanced_target_class_filter(self):
        """
        Advanced AI target class filter.

        Advanced AI predicts all 5 classes normally.
        This filter applies only selected class predictions to the project.
        Unselected class predictions are reverted to previous classification.

        Example:
        selected = Building only
        -> only points predicted as Building are copied
        -> Ground/Vegetation predictions remain unchanged
        """
        if self.ai_mode != "advanced":
            return

        selected = set(int(x) for x in (self.advanced_target_classes or {4}))

        # All Classes means keep the full Advanced AI output inside the fence.
        # No class filter should run in this case.
        if selected == {0, 1, 2, 3, 4}:
            print("Advanced target filter: All Classes selected, no filtering needed.")
            return

        try:
            before = self._ai_undo_before_classes
            current = self.app.data.get("classification")

            if before is None or current is None:
                print("Advanced target filter skipped: missing before/current classification.")
                return

            before = np.asarray(before)
            current = np.asarray(current)

            if len(before) != len(current):
                print(
                    "Advanced target filter skipped: length mismatch "
                    f"before={len(before):,}, current={len(current):,}"
                )
                return

            allowed_output_codes = set()

            for internal_cls in selected:
                if internal_cls in self.class_mapping:
                    allowed_output_codes.add(int(self.class_mapping[internal_cls]))

            if not allowed_output_codes:
                print("Advanced target filter skipped: no allowed output codes.")
                return

            allowed_mask = np.isin(current, list(allowed_output_codes))

            if self.target_indices is not None:
                target = np.asarray(self.target_indices, dtype=np.int64).ravel()
                target = target[(target >= 0) & (target < len(current))]

                fence_mask = np.zeros(len(current), dtype=bool)
                fence_mask[target] = True

                allowed_mask &= fence_mask

            final_cls = before.copy()
            final_cls[allowed_mask] = current[allowed_mask]

            live_cls = self.app.data.get("classification")
            if live_cls is not None:
                live_cls = np.asarray(live_cls)
                if len(live_cls) == len(final_cls):
                    live_cls[:] = final_cls
                else:
                    self.app.data["classification"] = final_cls
            else:
                self.app.data["classification"] = final_cls

            print("=" * 60)
            print("ADVANCED TARGET CLASS FILTER APPLIED")
            print(f"  Selected classes : {self._advanced_target_class_names()}")
            print(f"  Allowed codes    : {sorted(allowed_output_codes)}")
            print(f"  Applied points   : {int(allowed_mask.sum()):,}")
            print("  Unselected predictions reverted to previous classification.")
            print("=" * 60)

        except Exception as e:
            print(f"Advanced target class filter failed: {e}")


    def _apply_power_only_output_filter(self):
        """Deprecated: CL no longer enables power-only output.

        CL is now only a Wire/Pole guidance corridor. Normal Ground/Vegetation/
        Building output must remain from Basic AI unless the user separately
        uses fence mode or Advanced target-class filtering.
        """
        return

    def _apply_fence_output_guard(self):
        """
        Final safety guard:
        selected fence points keep AI output,
        outside fence points remain unchanged.
        """
        if self.target_indices is None:
            return

        if self._original_classification_for_fence is None:
            return

        try:
            current = self.app.data.get("classification")
            if current is None:
                return

            current = np.asarray(current)
            original = np.asarray(self._original_classification_for_fence)

            if len(current) != len(original):
                print("Fence guard skipped: classification length mismatch")
                return

            target = np.asarray(self.target_indices, dtype=np.int64)
            target = target[(target >= 0) & (target < len(current))]

            final_cls = original.copy()
            final_cls[target] = current[target]

            live_cls = self.app.data.get("classification")
            if live_cls is not None:
                live_cls = np.asarray(live_cls)
                if len(live_cls) == len(final_cls):
                    live_cls[:] = final_cls
                else:
                    self.app.data["classification"] = final_cls
            else:
                self.app.data["classification"] = final_cls

            print(
                f"Fence output guard applied: "
                f"{len(target):,} points updated, outside fence unchanged"
            )

        except Exception as e:
            print(f"Fence output guard failed: {e}")

    def classification_finished(self):
        if self._finish_handled:
            return
        self._finish_handled = True

        self.progress_bar.setValue(100)
        self.status_label.setText("Classification complete!")
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)

        self._teardown_worker()

        self._apply_fence_output_guard()
        self._apply_advanced_target_class_filter()
        changed_mask = self._compute_ai_changed_mask_after(restrict_to_fence=False)
        ai_changed_count, _ = self._push_ai_undo_after()
        ai_changed_count = int(ai_changed_count or 0)
        if ai_changed_count > 0 and hasattr(self.app, "mark_ai_classification_completed"):
            try:
                self.app.mark_ai_classification_completed(processed_points=ai_changed_count)
            except Exception as e:
                print(f"AI undo guard arm failed: {e}")

        self._mark_selected_fences_as_classified()

        try:
            self._refresh_views_after_ai(changed_mask)
            summary = self._build_result_summary(processed_points=ai_changed_count)
            QMessageBox.information(self, "AI Classification Complete", summary)
            self.accept()
        except Exception as e:
            import traceback
            QMessageBox.critical(self, "Display Update Failed",
                f"Classification succeeded but display update failed:\n\n"
                f"{str(e)}\n\nData IS stored in memory.")
            print(f"Display error:\n{traceback.format_exc()}")

    def _build_result_summary(self, processed_points=None):
        from gui.ai_inference import InferenceConfig
        model_names = {0:'Ground', 1:'Low Vegetation', 2:'Medium Vegetation',
                       3:'High Vegetation', 4:'Building'}
        code_to_name = {v: model_names[k] for k, v in self.class_mapping.items()}

        wire_out = pole_out = None
        if self.enable_power_lines:
            wire_out = self.power_mapping.get(InferenceConfig.WIRE_INTERNAL_CODE, 14)
            pole_out = self.power_mapping.get(InferenceConfig.POLE_INTERNAL_CODE, 15)
            code_to_name[wire_out] = "Power Line Wire"
            code_to_name[pole_out] = "Power Line Pole/Tower"

        try:
            classification = self.app.data.get("classification")
            if classification is None:
                return "Classification complete!\n\nNo result data available."
            n_total      = len(classification)
            unique, cnts = np.unique(classification, return_counts=True)
            mode = "Power Line mode" if self.enable_power_lines else "5-class mode"
            lines = [f"Classification complete! ({mode})\n",
                     f"Total points: {n_total:,}"]
            if processed_points is not None and int(processed_points) > 0:
                scope_tag = "fence" if self.target_indices is not None else "updated"
                lines.append(f"Processed points ({scope_tag}): {int(processed_points):,}")
            lines.extend(["", "Results:"])
            for cls, cnt in zip(unique, cnts):
                ci   = int(cls)
                name = code_to_name.get(ci, f'Code {ci}')
                pct  = 100 * cnt / n_total
                icon = "⚡" if (self.enable_power_lines and ci in (wire_out, pole_out)) else " "
                lines.append(f"  {icon} {name} (code {ci}): {cnt:,} ({pct:.1f}%)")
            if self.enable_power_lines:
                n_wire = int(np.sum(classification == wire_out))
                n_pole = int(np.sum(classification == pole_out))
                if n_wire > 0 or n_pole > 0:
                    lines.append("\nPower Line Detection:")
                    if n_wire > 0: lines.append(f"  Wire (code {wire_out}): {n_wire:,} pts")
                    if n_pole > 0: lines.append(f"  Pole (code {pole_out}): {n_pole:,} pts")
            return "\n".join(lines)
        except Exception:
            return "Classification complete!\n\nPoint cloud updated."

    def classification_error(self, error_msg):
        self._finish_handled = True
        self.status_label.setText("Classification failed")
        self.cancel_btn.setEnabled(False)
        self.close_btn.setEnabled(True)
        self._teardown_worker()
        QMessageBox.critical(self, "AI Classification Failed",
            f"Error:\n\n{error_msg}")

    def cancel_classification(self):
        if self.worker and self.worker.isRunning():
            self.status_label.setText("Cancelling...")
            self.cancel_btn.setEnabled(False)
            self.worker.cancel()
            if not self.worker.wait(5000):
                QMessageBox.information(
                    self,
                    "Cancelling in progress",
                    "Worker is still shutting down. Please wait a few seconds "
                    "before closing this dialog."
                )
                self.status_label.setText("Cancelling... please wait")
                self.close_btn.setEnabled(False)
                return
            self.status_label.setText("Classification cancelled")
            self.close_btn.setEnabled(True)

    def _teardown_worker(self):
        if self.worker is None:
            return
        if self.worker.isRunning():
            # Never destroy a live QThread; caller must wait/handle deferred close.
            return
        try:
            self.worker.progress.disconnect()
            self.worker.finished.disconnect()
            self.worker.error.disconnect()
        except RuntimeError:
            pass
        self.worker.deleteLater()
        self.worker = None

    def closeEvent(self, event):
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(self, "In Progress",
                "Classification is still running. Cancel and close?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self.worker.cancel()
                if self.worker.wait(5000):
                    self._teardown_worker()
                    event.accept()
                else:
                    QMessageBox.information(
                        self,
                        "Please wait",
                        "Cancellation is still in progress. The dialog will stay open "
                        "until the worker fully stops."
                    )
                    event.ignore()
            else:
                event.ignore()
        else:
            self._teardown_worker()
            event.accept()


# ═══════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def _find_source_file_from_app(app):
    if hasattr(app, 'data') and app.data is not None:
        for key in ['file_path','filepath','source_file','path','filename',
                    'las_path','laz_path','input_path','current_file',
                    'loaded_file','source_path','file','las_file','laz_file']:
            if key in app.data:
                val = app.data[key]
                if val is not None:
                    p = Path(str(val))
                    if p.exists() and p.suffix.lower() in ('.laz', '.las'):
                        return p
        for key, val in app.data.items():
            if isinstance(val, (str, Path)):
                p = Path(str(val))
                if p.exists() and p.suffix.lower() in ('.laz', '.las'):
                    return p
    for attr in ['current_file','file_path','filepath','loaded_file',
                 'source_file','current_path','las_path','laz_path',
                 'input_file','filename']:
        if hasattr(app, attr):
            val = getattr(app, attr)
            if val is not None:
                p = Path(str(val))
                if p.exists() and p.suffix.lower() in ('.laz', '.las'):
                    return p
    return None


def _activate_existing_ai_dialog(app):
    """
    If an AI workflow dialog already exists, restore/focus it.
    If it was minimized to taskbar, bring the SAME existing dialog back.
    """
    for attr in ("_ai_progress_dialog", "_ai_fence_dialog", "_ai_mapping_dialog"):
        dialog = getattr(app, attr, None)
        if dialog is None:
            continue
        try:
            if dialog.isVisible() or dialog.isMinimized():
                if dialog.isMinimized():
                    dialog.showNormal()
                else:
                    dialog.show()

                dialog.raise_()
                dialog.activateWindow()
                print("AI launch ignored: existing AI classification dialog restored/focused.")
                return True

            setattr(app, attr, None)

        except RuntimeError:
            setattr(app, attr, None)
        except Exception as e:
            print(f"Could not activate existing AI dialog ({attr}): {e}")

    return False


def show_ai_classification_dialog(app):
    from gui.ai_inference import HAS_JAKTERISTICS, HAS_CSF

    try:
        if _activate_existing_ai_dialog(app):
            return

        if not HAS_JAKTERISTICS:
            QMessageBox.critical(
                app,
                "Missing Dependency",
                "jakteristics is required.\n\npip install jakteristics"
            )
            return

        if not HAS_CSF:
            QMessageBox.critical(
                app,
                "Missing Dependency",
                "CSF is required.\n\npip install cloth-simulation-filter"
            )
            return

        if not hasattr(app, "data") or app.data is None:
            QMessageBox.warning(app, "No Point Cloud", "Please load a LAZ/LAS file first.")
            return

        if "xyz" not in app.data or app.data["xyz"] is None:
            QMessageBox.warning(app, "Invalid Data", "Missing XYZ coordinates. Please reload the file.")
            return

        n_points = len(app.data["xyz"])
        if n_points < 100:
            QMessageBox.warning(app, "Too Few Points", f"Only {n_points} points found.")
            return

        print(f"\n{'=' * 60}\nAI CLASSIFICATION — DATA VALIDATION\n{'=' * 60}")
        print(f"  Points: {n_points:,}")

        source_file = _find_source_file_from_app(app)
        if source_file:
            print(f"  Source file: {source_file}")
            app.data["_source_file_path"] = str(source_file)
        else:
            print("  WARNING: Source LAZ/LAS file not found!")

        has_rn = (
            "return_number" in app.data
            and app.data["return_number"] is not None
            and len(app.data["return_number"]) == n_points
            and np.max(app.data["return_number"]) > 0
        )

        has_nr = (
            "number_of_returns" in app.data
            and app.data["number_of_returns"] is not None
            and len(app.data["number_of_returns"]) == n_points
            and np.max(app.data["number_of_returns"]) > 0
        )

        if not has_rn:
            print("  Return number missing in viewer cache — using safe default.")
            app.data["return_number"] = np.ones(n_points, dtype=np.float32)
            has_rn = True

        if not has_nr:
            print("  Number of returns missing in viewer cache — using safe default.")
            app.data["number_of_returns"] = np.ones(n_points, dtype=np.float32)
            has_nr = True

        print("  Return fields ready. Skipping slow UI-thread source-file reread.")

        has_intensity = (
            "intensity" in app.data
            and app.data["intensity"] is not None
            and len(app.data["intensity"]) == n_points
        )
        print(f"  has_returns:   {has_rn and has_nr}")
        print(f"{'=' * 60}\n")

        warnings = []
        if not has_intensity:
            warnings.append("• INTENSITY unavailable — zero-filled")

        if not (has_rn and has_nr):
            warnings.append(
                "• RETURN NUMBER data unavailable.\n"
                "  Building and vegetation separation WILL be degraded."
            )

        if warnings:
            reply = QMessageBox.warning(
                app,
                "Missing LiDAR Attributes",
                "Data warnings:\n\n" + "\n\n".join(warnings) + "\n\nProceed anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.No:
                return

        existing_classes = set()
        if "classification" in app.data and app.data["classification"] is not None:
            existing_classes = set(int(c) for c in np.unique(app.data["classification"]))

        mapping_dialog = ClassMappingDialog(
            parent=None,
            existing_classes=existing_classes,
            app=app,
        )
        mapping_dialog.setModal(False)
        mapping_dialog.setWindowModality(Qt.NonModal)

        # Keep reference, otherwise Python may garbage-collect the modeless dialog.
        app._ai_mapping_dialog = mapping_dialog

        def _cleanup_mapping_dialog():
            try:
                app._ai_mapping_dialog = None
            except Exception:
                pass

        def _launch_progress_dialog(
            class_mapping,
            power_mapping,
            advanced_config,
            enable_power_lines,
            ai_mode,
            target_indices,
            selected_fence_count,
            advanced_target_classes,
            selected_fence_drawings=None,
        ):
            advanced_config = dict(advanced_config or {})

            if ai_mode == "advanced":
                # Advanced AI fence mode must not silently force Building-only.
                # If All Classes is selected, let all 5 predicted classes update inside the fence.
                # If a single class is selected, only that class is filtered later.
                try:
                    advanced_target_classes = set(int(x) for x in (advanced_target_classes or []))
                except Exception:
                    advanced_target_classes = set()

                all_classes = {0, 1, 2, 3, 4}

                if not advanced_target_classes:
                    advanced_target_classes = {4}

                advanced_config["advanced_target_classes"] = sorted(advanced_target_classes)

            if getattr(app, "_ai_run_active", False):
                print("AI launch ignored: another AI classification is already running.")
                return
            app._ai_run_active = True

            progress_dialog = AIClassificationDialog(
                app,
                class_mapping,
                power_mapping,
                advanced_config=advanced_config,
                enable_power_lines=enable_power_lines,
                ai_mode=ai_mode,
                target_indices=target_indices,
                selected_fence_count=selected_fence_count,
                advanced_target_classes=advanced_target_classes,
                selected_fence_drawings=selected_fence_drawings,
                parent=None,
            )

            progress_dialog.setModal(False)
            progress_dialog.setWindowModality(Qt.NonModal)

            app._ai_progress_dialog = progress_dialog

            def _cleanup_progress_dialog(_result=0):
                try:
                    app._ai_progress_dialog = None
                except Exception:
                    pass
                try:
                    app._ai_run_active = False
                except Exception:
                    pass

            progress_dialog.finished.connect(_cleanup_progress_dialog)

            progress_dialog.start_classification(app.data)
            progress_dialog.show()
            progress_dialog.raise_()
            progress_dialog.activateWindow()

        def _after_mapping_accepted():
            try:
                class_mapping = mapping_dialog.get_class_mapping()
                power_mapping = mapping_dialog.get_power_mapping()
                advanced_config = mapping_dialog.get_advanced_config()
                enable_power_lines = mapping_dialog.get_enable_power_lines()
                ai_mode = mapping_dialog.get_ai_mode()
                advanced_target_classes = mapping_dialog.get_advanced_target_classes()

                if class_mapping is None or power_mapping is None:
                    _cleanup_mapping_dialog()
                    return

                target_indices = mapping_dialog.get_target_indices()
                selected_fence_count = mapping_dialog.get_selected_fence_count()
                selected_fence_drawings = mapping_dialog.get_selected_fence_drawings()

                try:
                    fence_dialog = AIFenceRunDialog(app, parent=None)
                    fence_dialog.setModal(False)
                    fence_dialog.setWindowModality(Qt.NonModal)

                    # Carry first-popup AI choice AND Advanced target classes into optional fence popup.
                    try:
                        if ai_mode == "advanced":
                            fence_dialog.advanced_radio.setChecked(True)
                            fence_dialog._sync_model_state()
                            fence_dialog.set_advanced_target_classes(advanced_target_classes)
                        else:
                            fence_dialog.basic_radio.setChecked(True)
                            fence_dialog.power_cb.setChecked(bool(enable_power_lines))
                            fence_dialog._sync_model_state()
                    except Exception as e:
                        print(f"Could not sync AI fence popup settings: {e}")

                    if fence_dialog.has_fences():
                        app._ai_fence_dialog = fence_dialog

                        def _after_fence_finished(result):
                            try:
                                if result != QDialog.Accepted:
                                    print("AI classification cancelled at fence/model selection.")
                                    return

                                final_ai_mode = fence_dialog.ai_mode
                                final_enable_power_lines = fence_dialog.enable_power_lines
                                final_target_indices = fence_dialog.target_indices
                                final_selected_fence_count = fence_dialog.selected_fence_count
                                final_selected_fence_drawings = list(
                                    getattr(fence_dialog, "selected_fence_drawings", []) or []
                                )
                                if final_ai_mode == "advanced":
                                    final_advanced_target_classes = set(
                                        int(x) for x in fence_dialog._get_selected_advanced_classes()
                                    )

                                    # Empty means user did not choose anything; use safe Building-only default.
                                    if not final_advanced_target_classes:
                                        final_advanced_target_classes = {4}
                                        try:
                                            fence_dialog.set_advanced_target_classes(final_advanced_target_classes)
                                        except Exception:
                                            pass
                                else:
                                    final_advanced_target_classes = advanced_target_classes

                                if final_ai_mode == "advanced":
                                    print(f"  Advanced target classes: {sorted(final_advanced_target_classes)}")

                                print("=" * 60)
                                print("AI RUN SETTINGS")
                                print(f"  AI model              : {final_ai_mode.upper()}")
                                print(
                                    f"  Power-line detection  : "
                                    f"{'ENABLED' if final_enable_power_lines else 'DISABLED'}"
                                )

                                if final_target_indices is not None:
                                    print("  Fence mode            : ENABLED")
                                    print(f"  Selected fences       : {final_selected_fence_count}")
                                    print(f"  Target points         : {len(final_target_indices):,}")
                                else:
                                    print("  Fence mode            : DISABLED / FULL FILE")

                                print("=" * 60)

                                _launch_progress_dialog(
                                    class_mapping,
                                    power_mapping,
                                    advanced_config,
                                    final_enable_power_lines,
                                    final_ai_mode,
                                    final_target_indices,
                                    final_selected_fence_count,
                                    final_advanced_target_classes,
                                    final_selected_fence_drawings,
                                )

                            finally:
                                try:
                                    app._ai_fence_dialog = None
                                except Exception:
                                    pass

                        fence_dialog.finished.connect(_after_fence_finished)
                        fence_dialog.show()
                        fence_dialog.raise_()
                        fence_dialog.activateWindow()
                        return

                    else:
                        print("AI Fence popup: no fences found. Using existing AI model settings.")

                except Exception as e:
                    print(f"AI fence/model popup failed, continuing with full file: {e}")
                    target_indices = None
                    selected_fence_count = 0

                if target_indices is not None:
                    print("  Fence mode: ENABLED")
                    print(f"  Fence target points: {len(target_indices):,} / {n_points:,}")
                else:
                    print("  Fence mode: DISABLED")

                _launch_progress_dialog(
                    class_mapping,
                    power_mapping,
                    advanced_config,
                    enable_power_lines,
                    ai_mode,
                    target_indices,
                    selected_fence_count,
                    advanced_target_classes,
                    None,
                )

            except Exception as e:
                import traceback
                QMessageBox.critical(
                    app,
                    "Initialization Failed",
                    f"Failed to start AI classification:\n\n{str(e)}"
                )
                print(f"Dialog init error:\n{traceback.format_exc()}")

            finally:
                _cleanup_mapping_dialog()

        def _after_mapping_finished(result):
            if result != QDialog.Accepted:
                print("  Cancelled by user")
                _cleanup_mapping_dialog()

        mapping_dialog.accepted.connect(_after_mapping_accepted)
        mapping_dialog.finished.connect(_after_mapping_finished)

        mapping_dialog.show()
        mapping_dialog.raise_()
        mapping_dialog.activateWindow()

    except Exception as e:
        import traceback
        QMessageBox.critical(
            app,
            "Initialization Failed",
            f"Failed to start AI classification:\n\n{str(e)}"
        )
        print(f"Dialog init error:\n{traceback.format_exc()}")