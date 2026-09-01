"""NakshaAI plugin for DGN/DXF/SNT conversion.

Conversion routes:
    DGN -> SNT  (bundled nakshaapp_dgn_converter 1.3.4)
    DGN -> DXF  (bundled nakshaapp_dgn_converter 1.3.4)
    DXF -> SNT  (snt_core)

The known-good DGN backend is bundled privately with the plugin. DXF -> SNT
continues to use the app's snt_core package.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
import time
from typing import Callable, Optional

from PySide6.QtCore import QRect, QSettings, QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
)

from gui.naksha_plugin_api import NakshaPlugin


PLUGIN_VERSION = "1.1.2"
_DGN_FORMATS = ("snt", "dxf")
_DXF_FORMATS = ("snt",)


# Single source of truth for the dialog's visual language. Light theme;
# intentionally tuned to feel "in-app" rather than like a system dialog.
_DIALOG_QSS = """
QDialog#NakshaConverterDialog {
    background: #f6f8fb;
}
QLabel#NCDTitle {
    color: #0f1f33;
    font-size: 18px;
    font-weight: 600;
    background: transparent;
}
QLabel#NCDSubtitle {
    color: #4a5a70;
    font-size: 12px;
    background: transparent;
}
QLabel#NCDLabel {
    color: #5b6878;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.4px;
    background: transparent;
}
QLabel#NCDBrandTitle {
    color: #0f1f33;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}
QLabel#NCDAccentText {
    color: #2bb6e0;
    font-size: 17px;
    font-weight: 700;
    background: transparent;
}
QLabel#NCDVersion {
    color: #8895a6;
    font-size: 11px;
    background: transparent;
}
QLabel#NCDInfoText {
    color: #4a5a70;
    font-size: 12px;
    background: transparent;
}
QLabel#NCDRouteLabel {
    color: #0f1f33;
    font-size: 12px;
    font-weight: 500;
    background: transparent;
}
QLabel#NCDRouteHint {
    color: #8895a6;
    font-size: 11px;
    background: transparent;
}
QLabel#NCDDropPrompt {
    color: #4a5a70;
    font-size: 13px;
    font-weight: 500;
    background: transparent;
}
QLabel#NCDDropHint {
    color: #8895a6;
    font-size: 11px;
    background: transparent;
}
QFrame#NCDInfoCard {
    background: #ffffff;
    border: 1px solid #e1e6ee;
    border-radius: 14px;
}
QFrame#NCDRouteRow {
    background: transparent;
    border: none;
}
QFrame#NCDDropZone {
    background: #f9fbfd;
    border: 1.5px dashed #c0c9d6;
    border-radius: 10px;
}
QFrame#NCDDropZone[dragActive="true"] {
    background: #eef4ff;
    border: 2px solid #2f6fed;
    border-radius: 10px;
}
QFrame#NCDFileCard, QFrame#NCDStatusCard, QFrame#NCDFolderCard,
QFrame#NCDOverwriteCard {
    background: #ffffff;
    border: 1px solid #e1e6ee;
    border-radius: 10px;
}
QFrame#NCDOverwriteCard {
    background: #fff7e6;
    border: 1px solid #f3d790;
}
QFrame#NCDStatusCard {
    border-radius: 10px;
}
QLabel#NCDFileName {
    color: #0f1f33;
    font-size: 14px;
    font-weight: 600;
    background: transparent;
}
QLabel#NCDFileMeta {
    color: #6a7787;
    font-size: 11px;
    background: transparent;
}
QLabel#NCDFilePath {
    color: #8895a6;
    font-size: 11px;
    background: transparent;
}
QLabel#NCDFolderPath {
    color: #0f1f33;
    font-size: 12px;
    background: #f3f5f9;
    border: 1px solid #e1e6ee;
    border-radius: 8px;
    padding: 8px 12px;
}
QLabel#NCDStatusTitle {
    color: #0f1f33;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}
QLabel#NCDStatusBody {
    color: #4a5a70;
    font-size: 12px;
    background: transparent;
}
QLabel#NCDOverwriteText {
    color: #6b4a06;
    font-size: 12px;
    background: transparent;
}
QPushButton {
    color: #0f1f33;
    background: #ffffff;
    border: 1px solid #d6dce6;
    border-radius: 8px;
    padding: 8px 14px;
    font-size: 12px;
}
QPushButton:hover {
    background: #f3f5f9;
    border-color: #c0c9d6;
}
QPushButton:disabled {
    color: #9aa5b3;
    background: #f3f5f9;
    border-color: #e1e6ee;
}
QPushButton#NCDConvertButton {
    color: #ffffff;
    background: #2f6fed;
    border: 1px solid #2f6fed;
    border-radius: 10px;
    padding: 12px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton#NCDConvertButton:hover { background: #1d5dd0; }
QPushButton#NCDConvertButton:disabled {
    color: #ffffff;
    background: #9ab4ed;
    border-color: #9ab4ed;
}
QPushButton#NCDPrimary {
    color: #ffffff;
    background: #2f6fed;
    border: 1px solid #2f6fed;
    border-radius: 8px;
    padding: 8px 18px;
    font-weight: 600;
}
QPushButton#NCDPrimary:hover { background: #1d5dd0; }
QPushButton#NCDSegment {
    color: #0f1f33;
    background: #ffffff;
    border: 1px solid #d6dce6;
    padding: 9px 16px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton#NCDSegment:checked {
    color: #ffffff;
    background: #2f6fed;
    border-color: #2f6fed;
}
QPushButton#NCDSegment:disabled {
    color: #9aa5b3;
    background: #f3f5f9;
}
QPushButton#NCDIconButton {
    background: transparent;
    border: none;
    color: #8895a6;
    font-size: 16px;
}
QPushButton#NCDIconButton:hover { color: #0f1f33; }
QProgressBar {
    background: #e6ebf3;
    border: none;
    border-radius: 4px;
    text-align: center;
    color: #0f1f33;
    font-size: 11px;
    height: 10px;
}
QProgressBar::chunk {
    background: #2f6fed;
    border-radius: 4px;
}
QLabel#NCDProgressLabel {
    color: #4a5a70;
    font-size: 12px;
    background: transparent;
}
QLabel#NCDClose {
    color: #8895a6;
    font-size: 12px;
    background: transparent;
}
"""


class ConversionError(RuntimeError):
    """A controlled conversion, validation, or dependency failure."""


@dataclass(frozen=True)
class ConversionSummary:
    route: str
    input_path: Path
    output_path: Path
    report_path: Optional[Path]
    entity_count: int
    layer_count: int
    output_size: int
    elapsed_seconds: float
    message: str
    warnings: tuple[str, ...] = ()


def formats_for_input(path: str | Path) -> tuple[str, ...]:
    """Return valid output formats for a supported input filename."""
    suffix = Path(path).suffix.lower()
    if suffix == ".dgn":
        return _DGN_FORMATS
    if suffix == ".dxf":
        return _DXF_FORMATS
    return ()


def default_output_path(path: str | Path, output_format: str) -> Path:
    """Return the output beside the input with the requested extension."""
    source = Path(path)
    fmt = output_format.lower().lstrip(".")
    if fmt not in formats_for_input(source):
        raise ConversionError(
            f"Cannot convert {source.suffix or 'this input'} to .{fmt}."
        )
    return source.with_suffix(f".{fmt}")


def dependency_status() -> dict[str, tuple[bool, str]]:
    """Return availability and version/error text for both converter engines."""
    status: dict[str, tuple[bool, str]] = {}
    engines = {
        "dgn_backend": "nakshaapp_dgn_converter",
        "snt_core": "snt_core",
    }
    for engine_name, module_name in engines.items():
        try:
            module = importlib.import_module(module_name)
            version = str(getattr(module, "__version__", "installed"))
            status[engine_name] = (True, version)
        except Exception as exc:
            status[engine_name] = (
                False,
                f"{type(exc).__name__}: {exc}",
            )
    return status


def _load_dependency(module_name: str, purpose: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:
        raise ConversionError(
            f"{purpose} is unavailable because {module_name} could not be loaded: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def _emit(progress: Optional[Callable[[str], None]], message: str) -> None:
    if progress is not None:
        progress(message)


def _validate_job(
    input_path: str | Path,
    output_path: str | Path,
    output_format: str,
    *,
    overwrite: bool,
) -> tuple[Path, Path, str]:
    source = Path(input_path).expanduser()
    target = Path(output_path).expanduser()
    fmt = output_format.lower().lstrip(".")

    if not source.is_file():
        raise ConversionError(f"Input file not found: {source}")
    valid_formats = formats_for_input(source)
    if not valid_formats:
        raise ConversionError("Input must be a .dgn or .dxf file.")
    if fmt not in valid_formats:
        allowed = ", ".join(f".{item}" for item in valid_formats)
        raise ConversionError(
            f"{source.suffix.upper()} input supports only: {allowed}."
        )
    if target.suffix.lower() != f".{fmt}":
        raise ConversionError(f"Output filename must end with .{fmt}.")
    if source.resolve() == target.resolve():
        raise ConversionError("Input and output paths must be different.")
    if target.exists() and not overwrite:
        raise ConversionError(f"Output already exists: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    return source, target, fmt


def convert_file(
    input_path: str | Path,
    output_path: str | Path,
    output_format: str,
    *,
    description: str = "",
    model_selection: str = "default",
    follow_references: bool = False,
    timeout_seconds: float = 1800.0,
    overwrite: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> ConversionSummary:
    """Run one validated conversion using the correct installed engine."""
    source, target, fmt = _validate_job(
        input_path, output_path, output_format, overwrite=overwrite
    )

    if source.suffix.lower() == ".dgn":
        started = time.perf_counter()
        _emit(progress, "Starting bundled DGN conversion engine ...")
        try:
            if fmt == "snt":
                engine = _load_dependency(
                    "nakshaapp_dgn_converter.snt_pipeline",
                    "Bundled DGN conversion",
                )
                _emit(progress, "Reading DGN and encoding SNT ...")
                result = engine._guarded_convert(
                    source,
                    target,
                    description=description,
                    follow_refs=follow_references,
                )
            else:
                engine = _load_dependency(
                    "nakshaapp_dgn_converter.dxf_pipeline",
                    "Bundled DGN conversion",
                )
                _emit(progress, "Reading DGN and encoding DXF ...")
                result = engine._guarded_convert_dxf(source, target)
        except ConversionError:
            raise
        except Exception as exc:
            raise ConversionError(
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not target.is_file():
            raise ConversionError(
                f"Bundled DGN engine did not create: {target}"
            )

        raw_warnings = getattr(result, "warnings", ()) or ()
        if isinstance(raw_warnings, str):
            raw_warnings = (raw_warnings,)
        entity_count = int(
            getattr(result, "entity_count", 0) or 0
        )
        layer_count = int(getattr(result, "layer_count", 0) or 0)
        elapsed = time.perf_counter() - started
        return ConversionSummary(
            route=f"DGN -> {fmt.upper()}",
            input_path=source,
            output_path=target,
            report_path=getattr(result, "report_path", None),
            entity_count=entity_count,
            layer_count=layer_count,
            output_size=int(
                getattr(result, "output_size_bytes", 0)
                or target.stat().st_size
            ),
            elapsed_seconds=elapsed,
            message=(
                f"Converted {entity_count:,} {fmt.upper()} entities"
                if entity_count
                else f"{fmt.upper()} conversion complete"
            ),
            warnings=tuple(str(item) for item in raw_warnings),
        )

    engine = _load_dependency("snt_core", "DXF to SNT conversion")
    _emit(progress, "Reading DXF and encoding SNT ...")
    try:
        result = engine.convert(
            dxf_path=source,
            snt_path=target,
            description=description,
        )
    except Exception as exc:
        raise ConversionError(f"{type(exc).__name__}: {exc}") from exc

    produced = Path(getattr(result, "snt_path", target))
    if not produced.is_file() or not target.is_file():
        raise ConversionError(
            f"snt_core did not create the requested output: {target}"
        )
    warnings = getattr(result, "warnings", ()) or ()
    _emit(progress, "DXF to SNT conversion complete.")
    return ConversionSummary(
        route="DXF -> SNT",
        input_path=source,
        output_path=target,
        report_path=getattr(result, "report_path", None),
        entity_count=int(getattr(result, "entity_count", 0) or 0),
        layer_count=int(getattr(result, "layer_count", 0) or 0),
        output_size=int(getattr(result, "output_size_bytes", 0) or 0),
        elapsed_seconds=0.0,
        message=f"Converted {int(getattr(result, 'entity_count', 0) or 0):,} SNT entities",
        warnings=tuple(str(item) for item in warnings),
    )


def output_paths_for_selection(
    input_path: str | Path,
    output_formats: tuple[str, ...],
    output_dir: str | Path | None = None,
) -> tuple[Path, ...]:
    """Return automatic output paths for one UI target selection."""
    source = Path(input_path)
    destination = (
        Path(output_dir) if output_dir is not None else source.parent
    )
    formats = tuple(
        dict.fromkeys(fmt.lower().lstrip(".") for fmt in output_formats)
    )
    valid = formats_for_input(source)
    if not formats or any(fmt not in valid for fmt in formats):
        raise ConversionError("The selected output format is not supported.")
    return tuple(destination / f"{source.stem}.{fmt}" for fmt in formats)


def convert_files(
    input_path: str | Path,
    output_formats: tuple[str, ...],
    *,
    output_dir: str | Path | None = None,
    description: str = "",
    model_selection: str = "default",
    follow_references: bool = False,
    timeout_seconds: float = 1800.0,
    overwrite: bool = False,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[ConversionSummary, ...]:
    """Convert one imported file to every selected automatic output."""
    source = Path(input_path)
    formats = tuple(
        dict.fromkeys(fmt.lower().lstrip(".") for fmt in output_formats)
    )
    targets = output_paths_for_selection(
        source, formats, output_dir=output_dir
    )

    # Validate every target before starting so a batch cannot fail halfway
    # merely because its second output already exists.
    for target, fmt in zip(targets, formats):
        _validate_job(source, target, fmt, overwrite=overwrite)

    summaries = []
    total = len(formats)
    for index, (target, fmt) in enumerate(
        zip(targets, formats), start=1
    ):
        prefix = f"{fmt.upper()} ({index}/{total})" if total > 1 else fmt.upper()

        def relay(message: str, label=prefix) -> None:
            _emit(progress, f"{label}: {message}")

        summaries.append(
            convert_file(
                source,
                target,
                fmt,
                description=description,
                model_selection=model_selection,
                follow_references=follow_references,
                timeout_seconds=timeout_seconds,
                overwrite=overwrite,
                progress=relay,
            )
        )
    return tuple(summaries)


class _ConversionWorker(QThread):
    progressed = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, job: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("NakshaDrawingConverterWorker")
        self._job = dict(job)

    def _report_progress(self, message: str) -> None:
        formats = tuple(self._job.get("output_formats", ("snt",)))
        total = max(1, len(formats))
        index = 1
        if total > 1 and "(" in message and "/" in message:
            try:
                marker = message.split("(", 1)[1].split(")", 1)[0]
                index = int(marker.split("/", 1)[0])
            except (ValueError, IndexError):
                index = 1

        lowered = message.lower()
        if "complete" in lowered:
            stage = 95
        elif "encoding" in lowered:
            stage = 70
        elif "reading" in lowered or "staging" in lowered:
            stage = 30
        elif "starting" in lowered:
            stage = 10
        else:
            stage = 15
        percent = int(((index - 1) + stage / 100.0) * 100 / total)
        self.progressed.emit(max(0, min(99, percent)), message)

    def run(self) -> None:
        try:
            self.progressed.emit(0, "Preparing conversion...")
            summaries = convert_files(
                **self._job,
                progress=self._report_progress,
            )
            self.progressed.emit(100, "Conversion complete")
            self.succeeded.emit(summaries)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def _file_type_icon(suffix: str, size: int = 36) -> QIcon:
    """Build a small, dependency-free file-type badge icon."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    base_color = (
        QColor("#2f6fed")
        if suffix.lower() == ".dgn"
        else QColor("#7a4cdb")
    )
    painter.setBrush(base_color)
    painter.setPen(Qt.PenStyle.NoPen)
    body = pix.rect().adjusted(4, 4, -4, -4)
    painter.drawRoundedRect(body, 6, 6)

    corner = QRect(
        body.x() + int(body.width() * 0.55),
        body.y(),
        int(body.width() * 0.45),
        int(body.height() * 0.45),
    )
    painter.setBrush(QColor(255, 255, 255, 60))
    painter.drawRoundedRect(corner, 4, 4)

    painter.setPen(QColor("#ffffff"))
    font = QFont()
    font.setBold(True)
    font.setPointSize(11)
    painter.setFont(font)
    label = suffix.lstrip(".").upper() if suffix else "FILE"
    if len(label) > 4:
        label = label[:4]
    painter.drawText(body, Qt.AlignmentFlag.AlignCenter, label)

    painter.end()
    return QIcon(pix)


def _brand_mark_icon(size: int = 56) -> QIcon:
    """Build the Naksha Converter brand mark — two soft teardrops."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setPen(Qt.PenStyle.NoPen)

    rect = pix.rect()
    cx, cy = rect.center().x(), rect.center().y()
    r = int(size * 0.30)

    # Large primary droplet (top-left), pointing down-right
    p.setBrush(QColor("#1f55c7"))
    p.drawEllipse(QRect(cx - r - 2, cy - r - 4, r * 2, r * 2))

    # Smaller accent droplet (bottom-right), pointing up-left
    p.setBrush(QColor("#34c5ee"))
    sr = int(r * 0.75)
    p.drawEllipse(QRect(cx + 4, cy + 6, sr * 2, sr * 2))

    # Tiny sparkle dot
    p.setBrush(QColor("#1f55c7"))
    p.drawEllipse(QRect(cx + int(r * 1.5), cy - int(r * 1.4), 4, 4))

    p.end()
    return QIcon(pix)


class _ConverterDialog(QDialog):
    def __init__(self, app_window, parent=None):
        super().__init__(parent or app_window)
        self._app_window = app_window
        self._worker: Optional[_ConversionWorker] = None
        self._source_path: Optional[Path] = None
        self._selected_target = "snt"
        self._pending_overwrite: bool = False
        self._pending_auto_open_snt: Optional[Path] = None
        self._dispose_after_finish = False
        self._settings = QSettings("NakshaTech", "NakshaAI-LiDAR")
        self.setObjectName("NakshaConverterDialog")
        self.setWindowTitle("Naksha Converter")
        self.setMinimumWidth(620)
        self.setAcceptDrops(True)
        self.setStyleSheet(_DIALOG_QSS)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(18)

        # Header ------------------------------------------------------------
        header = QHBoxLayout()
        header.setSpacing(0)
        title_block = QVBoxLayout()
        title_block.setSpacing(4)
        self.title_label = QLabel("Convert Drawing")
        self.title_label.setObjectName("NCDTitle")
        title_block.addWidget(self.title_label)

        self.subtitle_label = QLabel(
            "Import a DGN or DXF and choose a target format. "
            "Output is written beside the imported drawing."
        )
        self.subtitle_label.setObjectName("NCDSubtitle")
        self.subtitle_label.setWordWrap(True)
        title_block.addWidget(self.subtitle_label)
        header.addLayout(title_block, 1)
        header.addStretch(1)
        root.addLayout(header)

        # Stacked body: empty -> loaded -> done ----------------------------
        self.stack = QStackedWidget()
        self._build_empty_state()
        self._build_loaded_state()
        self.stack.addWidget(self.empty_widget)
        self.stack.addWidget(self.loaded_widget)
        self.stack.setCurrentWidget(self.empty_widget)
        root.addWidget(self.stack, 1)

        # Persistent convert button stays at the bottom of the dialog -----
        self.convert_button = QPushButton("Convert to SNT")
        self.convert_button.setObjectName("NCDConvertButton")
        self.convert_button.setMinimumHeight(48)
        self.convert_button.setEnabled(False)
        self.convert_button.hide()
        self.convert_button.clicked.connect(self._start_conversion)
        root.addWidget(self.convert_button)

        self.progress_label = QLabel()
        self.progress_label.setObjectName("NCDProgressLabel")
        self.progress_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_label.hide()
        root.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        root.addWidget(self.progress_bar)

        self.import_button = self.empty_import_button
        self.import_button.clicked.connect(self._browse_input)
        self.change_button.clicked.connect(self._browse_input)

    # -- empty state -----------------------------------------------------
    def _build_empty_state(self) -> None:
        self.empty_widget = QFrame()
        empty_root = QVBoxLayout(self.empty_widget)
        empty_root.setContentsMargins(0, 0, 0, 0)
        empty_root.setSpacing(14)

        # ── Welcome / info card ───────────────────────────────────────
        self.info_card = QFrame()
        self.info_card.setObjectName("NCDInfoCard")
        info_layout = QHBoxLayout(self.info_card)
        info_layout.setContentsMargins(18, 16, 18, 16)
        info_layout.setSpacing(16)

        brand_mark = QLabel()
        brand_mark.setObjectName("NCDBrandMark")
        mark_pixmap = self._load_brand_mark(size=64)
        if mark_pixmap is not None:
            brand_mark.setPixmap(mark_pixmap)
        info_layout.addWidget(
            brand_mark, 0, Qt.AlignmentFlag.AlignVCenter
        )

        brand_text = QVBoxLayout()
        brand_text.setSpacing(2)

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        brand_name = QLabel("Naksha")
        brand_name.setObjectName("NCDBrandTitle")
        title_row.addWidget(brand_name)
        brand_accent = QLabel("Converter")
        brand_accent.setObjectName("NCDAccentText")
        title_row.addWidget(brand_accent)
        title_row.addStretch(1)
        version_chip = QLabel(f"v{PLUGIN_VERSION}")
        version_chip.setObjectName("NCDVersion")
        title_row.addWidget(version_chip)
        brand_text.addLayout(title_row)

        info_text = QLabel(
            "Convert DGN and DXF drawings directly inside "
            "NakshaAI. Drop a file or browse to get started."
        )
        info_text.setObjectName("NCDInfoText")
        info_text.setWordWrap(True)
        brand_text.addWidget(info_text)
        info_layout.addLayout(brand_text, 1)

        empty_root.addWidget(self.info_card)

        # ── Conversion routes ─────────────────────────────────────────
        routes_row = QHBoxLayout()
        routes_row.setSpacing(10)

        def _route_tile(label: str, hint: str) -> QFrame:
            tile = QFrame()
            tile.setObjectName("NCDRouteRow")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(14, 10, 14, 10)
            tile_layout.setSpacing(2)
            lbl = QLabel(label)
            lbl.setObjectName("NCDRouteLabel")
            tile_layout.addWidget(lbl)
            sub = QLabel(hint)
            sub.setObjectName("NCDRouteHint")
            tile_layout.addWidget(sub)
            return tile

        routes_row.addWidget(
            _route_tile("DGN → SNT", "Naksha overlay format")
        )
        routes_row.addWidget(
            _route_tile("DGN → DXF", "AutoCAD exchange format")
        )
        routes_row.addWidget(
            _route_tile("DXF → SNT", "Naksha overlay format")
        )
        empty_root.addLayout(routes_row)

        # ── Drop zone ─────────────────────────────────────────────────
        self.drop_zone = QFrame()
        self.drop_zone.setObjectName("NCDDropZone")
        self.drop_zone.setProperty("dragActive", False)
        self.drop_zone.setMinimumHeight(150)
        drop_layout = QVBoxLayout(self.drop_zone)
        drop_layout.setContentsMargins(20, 22, 20, 22)
        drop_layout.setSpacing(8)
        drop_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icon_box = QLabel()
        icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_pixmap = self._load_brand_mark(size=42)
        if icon_pixmap is None:
            icon_pixmap = _file_type_icon("dgn", 36).pixmap(QSize(36, 36))
        icon_box.setPixmap(icon_pixmap)
        drop_layout.addWidget(icon_box, 0, Qt.AlignmentFlag.AlignCenter)

        drop_title = QLabel("Drag and drop a DGN or DXF file here")
        drop_title.setObjectName("NCDDropPrompt")
        drop_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_layout.addWidget(
            drop_title, 0, Qt.AlignmentFlag.AlignCenter
        )

        self.empty_import_button = QPushButton("Choose File...")
        self.empty_import_button.setObjectName("NCDPrimary")
        self.empty_import_button.setMinimumHeight(36)
        self.empty_import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        drop_layout.addWidget(
            self.empty_import_button, 0, Qt.AlignmentFlag.AlignCenter
        )

        drop_hint = QLabel(
            "Output is written beside the imported drawing. "
            "Drawings stay on this device."
        )
        drop_hint.setObjectName("NCDDropHint")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drop_hint.setWordWrap(True)
        drop_layout.addWidget(
            drop_hint, 0, Qt.AlignmentFlag.AlignCenter
        )
        empty_root.addWidget(self.drop_zone)

        empty_root.addStretch(1)

    @staticmethod
    def _load_brand_mark(size: int = 48) -> Optional[QPixmap]:
        """Load the bundled Naksha brand mark if present."""
        candidates = (
            Path(__file__).resolve().parent / "naksha_brand.png",
            Path(__file__).resolve().parent.parent
            / "naksha_converter"
            / "assets"
            / "logo_light.png",
        )
        for path in candidates:
            try:
                if path.is_file():
                    pix = QPixmap(str(path))
                    if not pix.isNull():
                        return pix.scaled(
                            size,
                            size,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
            except Exception:
                continue
        return None

    # -- loaded state ----------------------------------------------------
    def _build_loaded_state(self) -> None:
        self.loaded_widget = QFrame()
        loaded = QVBoxLayout(self.loaded_widget)
        loaded.setContentsMargins(0, 0, 0, 0)
        loaded.setSpacing(16)

        # File card ------------------------------------------------------
        self.file_panel = QFrame()
        self.file_panel.setObjectName("NCDFileCard")
        file_root = QHBoxLayout(self.file_panel)
        file_root.setContentsMargins(14, 14, 14, 14)
        file_root.setSpacing(14)

        file_icon = QLabel()
        file_icon.setObjectName("NCDFileIcon")
        file_root.addWidget(file_icon, 0, Qt.AlignmentFlag.AlignVCenter)

        file_text = QVBoxLayout()
        file_text.setSpacing(2)
        self.file_name_label = QLabel()
        self.file_name_label.setObjectName("NCDFileName")
        file_text.addWidget(self.file_name_label)
        self.file_meta_label = QLabel()
        self.file_meta_label.setObjectName("NCDFileMeta")
        file_text.addWidget(self.file_meta_label)
        self.file_path_label = QLabel()
        self.file_path_label.setObjectName("NCDFilePath")
        self.file_path_label.setWordWrap(True)
        self.file_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        file_text.addWidget(self.file_path_label)
        file_root.addLayout(file_text, 1)

        self.change_button = QPushButton("Change")
        self.change_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_button.setMinimumHeight(32)
        file_root.addWidget(
            self.change_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        loaded.addWidget(self.file_panel)

        # Target format --------------------------------------------------
        target_block = QVBoxLayout()
        target_block.setSpacing(8)

        target_label = QLabel("TARGET FORMAT")
        target_label.setObjectName("NCDLabel")
        target_block.addWidget(target_label)

        target_row = QHBoxLayout()
        target_row.setSpacing(0)
        self.target_group = QButtonGroup(self)
        self.target_group.setExclusive(True)
        self.snt_button = QPushButton("SNT Overlay")
        self.dxf_button = QPushButton("DXF Exchange")
        self.both_button = QPushButton("SNT + DXF")
        self.target_buttons = {
            "snt": self.snt_button,
            "dxf": self.dxf_button,
            "both": self.both_button,
        }
        for index, (target, button) in enumerate(
            self.target_buttons.items()
        ):
            button.setObjectName("NCDSegment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setMinimumHeight(34)
            button.clicked.connect(
                lambda checked, value=target: (
                    self._select_target(value) if checked else None
                )
            )
            self.target_group.addButton(button)
            target_row.addWidget(button)
        target_block.addLayout(target_row)
        loaded.addLayout(target_block)

        # Output folder row ---------------------------------------------
        folder_label = QLabel("OUTPUT FOLDER")
        folder_label.setObjectName("NCDLabel")
        loaded.addWidget(folder_label)

        self.output_folder_label = QLabel()
        self.output_folder_label.setObjectName("NCDFolderPath")
        self.output_folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.output_folder_label.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        self.output_folder_label.setMinimumHeight(40)
        loaded.addWidget(self.output_folder_label)

        self.auto_open_snt = QCheckBox(
            "Open converted SNT automatically"
        )
        self.auto_open_snt.setObjectName("NCDAutoOpenSNT")
        self.auto_open_snt.setChecked(True)
        self.auto_open_snt.setToolTip(
            "Load the converted SNT into NakshaAI and close this dialog"
        )
        loaded.addWidget(self.auto_open_snt)

        # Inline overwrite confirm (hidden until needed) ---------------
        self.overwrite_card = QFrame()
        self.overwrite_card.setObjectName("NCDOverwriteCard")
        overwrite_layout = QHBoxLayout(self.overwrite_card)
        overwrite_layout.setContentsMargins(14, 12, 14, 12)
        overwrite_layout.setSpacing(10)
        self.overwrite_text = QLabel()
        self.overwrite_text.setObjectName("NCDOverwriteText")
        self.overwrite_text.setWordWrap(True)
        overwrite_layout.addWidget(self.overwrite_text, 1)
        self.overwrite_yes = QPushButton("Replace")
        self.overwrite_yes.setObjectName("NCDPrimary")
        self.overwrite_yes.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overwrite_yes.clicked.connect(self._confirm_overwrite)
        self.overwrite_no = QPushButton("Cancel")
        self.overwrite_no.setCursor(Qt.CursorShape.PointingHandCursor)
        self.overwrite_no.clicked.connect(self._cancel_overwrite)
        overwrite_layout.addWidget(self.overwrite_no)
        overwrite_layout.addWidget(self.overwrite_yes)
        self.overwrite_card.hide()
        loaded.addWidget(self.overwrite_card)

        # Inline completion status (hidden until success) -------------
        self.status_card = QFrame()
        self.status_card.setObjectName("NCDStatusCard")
        self.status_card.setStyleSheet(
            "QFrame#NCDStatusCard { background: #f0f8f1;"
            " border: 1px solid #c8e2cc; border-radius: 10px; }"
        )
        status_layout = QVBoxLayout(self.status_card)
        status_layout.setContentsMargins(14, 12, 14, 12)
        status_layout.setSpacing(6)
        self.status_title = QLabel()
        self.status_title.setObjectName("NCDStatusTitle")
        status_layout.addWidget(self.status_title)
        self.status_body = QLabel()
        self.status_body.setObjectName("NCDStatusBody")
        self.status_body.setWordWrap(True)
        status_layout.addWidget(self.status_body)

        status_actions = QHBoxLayout()
        status_actions.setSpacing(8)
        status_actions.addStretch(1)
        self.status_open_button = QPushButton("Open folder")
        self.status_open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status_open_button.clicked.connect(self._open_output_folder)
        status_actions.addWidget(self.status_open_button)
        self.status_convert_another = QPushButton("Convert another")
        self.status_convert_another.setCursor(Qt.CursorShape.PointingHandCursor)
        self.status_convert_another.clicked.connect(self._convert_another)
        status_actions.addWidget(self.status_convert_another)
        status_layout.addLayout(status_actions)
        self.status_card.hide()
        loaded.addWidget(self.status_card)

        loaded.addStretch(1)

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _friendly_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024.0 or unit == "GB":
                return (
                    f"{int(value)} {unit}"
                    if unit == "B"
                    else f"{value:.1f} {unit}"
                )
            value /= 1024.0
        return f"{size} B"

    def _set_loaded(self, loaded: bool) -> None:
        if loaded:
            self.stack.setCurrentWidget(self.loaded_widget)
            self.convert_button.show()
        else:
            self.stack.setCurrentWidget(self.empty_widget)
            self.convert_button.hide()
            self.progress_bar.hide()
            self.progress_label.hide()
            self.status_card.hide()
            self.overwrite_card.hide()

    # -- event handlers --------------------------------------------------
    def _last_directory(self) -> str:
        stored = self._settings.value(
            "drawing_converter/last_directory", ""
        )
        if stored and Path(str(stored)).is_dir():
            return str(stored)
        return str(Path.home())

    def _browse_input(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Import DGN or DXF",
            self._last_directory(),
            "Drawing files (*.dgn *.dxf);;DGN files (*.dgn);;"
            "DXF files (*.dxf)",
        )
        if selected:
            self._load_source(Path(selected))

    def _load_source(self, source: Path) -> None:
        if not source.is_file() or not formats_for_input(source):
            QMessageBox.warning(
                self,
                "Unsupported file",
                "Select a valid .dgn or .dxf drawing.",
            )
            return

        self._source_path = source
        self._pending_overwrite = False
        self._settings.setValue(
            "drawing_converter/last_directory", str(source.parent)
        )
        self.file_name_label.setText(source.name)
        self.file_meta_label.setText(
            f"{source.suffix[1:].upper()} Format  -  "
            f"{self._friendly_size(source.stat().st_size)}"
        )
        self.file_path_label.setText(str(source))
        self.file_path_label.setToolTip(str(source))
        self.output_folder_label.setText(str(source.parent))
        self.output_folder_label.setToolTip(str(source.parent))

        # Update file card icon
        icon_holder = self.file_panel.findChild(QLabel, "NCDFileIcon")
        if icon_holder is not None:
            icon_holder.setPixmap(
                _file_type_icon(source.suffix, 40).pixmap(QSize(40, 40))
            )

        is_dgn = source.suffix.lower() == ".dgn"
        self.dxf_button.setVisible(is_dgn)
        self.both_button.setVisible(is_dgn)
        self._select_target("snt")

        self.overwrite_card.hide()
        self.status_card.hide()
        self.progress_bar.hide()
        self.progress_label.hide()

        self._set_loaded(True)
        self.convert_button.setEnabled(True)
        self.adjustSize()

    def _select_target(self, target: str) -> None:
        if target not in self.target_buttons:
            return
        if (
            self._source_path is not None
            and self._source_path.suffix.lower() == ".dxf"
            and target != "snt"
        ):
            target = "snt"
        self._selected_target = target
        self.target_buttons[target].setChecked(True)
        if target == "snt":
            self.convert_button.setText("Convert to SNT")
        elif target == "dxf":
            self.convert_button.setText("Convert to DXF")
        else:
            self.convert_button.setText("Convert SNT + DXF")
        has_snt = target in ("snt", "both")
        self.auto_open_snt.setEnabled(has_snt and not self.is_running())
        self.auto_open_snt.setToolTip(
            "Load the converted SNT into NakshaAI and close this dialog"
            if has_snt
            else "Auto-open is available only for SNT output"
        )

    def _selected_formats(self) -> tuple[str, ...]:
        return (
            ("snt", "dxf")
            if self._selected_target == "both"
            else (self._selected_target,)
        )

    def _start_conversion(self) -> None:
        if self.is_running() or self._source_path is None:
            return
        formats = self._selected_formats()
        targets = output_paths_for_selection(self._source_path, formats)
        existing = [target for target in targets if target.exists()]
        if existing:
            names = ", ".join(path.name for path in existing)
            self.overwrite_text.setText(
                f"These output files already exist: {names}. "
                "Replace them with the new conversion?"
            )
            self.overwrite_card.show()
            self._pending_targets = targets
            self._pending_formats = formats
            self.convert_button.setEnabled(False)
            return

        self._launch_conversion(formats, targets, overwrite=False)

    def _confirm_overwrite(self) -> None:
        self.overwrite_card.hide()
        self._launch_conversion(
            self._pending_formats,
            self._pending_targets,
            overwrite=True,
        )

    def _cancel_overwrite(self) -> None:
        self.overwrite_card.hide()
        self.convert_button.setEnabled(True)

    def _launch_conversion(
        self,
        formats: tuple[str, ...],
        targets: tuple[Path, ...],
        *,
        overwrite: bool,
    ) -> None:
        if self._source_path is None:
            return

        dependency = (
            "dgn_backend"
            if self._source_path.suffix.lower() == ".dgn"
            else "snt_core"
        )
        available, detail = dependency_status()[dependency]
        if not available:
            self.overwrite_card.hide()
            QMessageBox.critical(
                self,
                "Converter unavailable",
                f"{dependency} could not be loaded:\n{detail}",
            )
            self.convert_button.setEnabled(True)
            return

        self.status_card.hide()
        self._pending_auto_open_snt = None
        worker = _ConversionWorker(
            {
                "input_path": self._source_path,
                "output_formats": formats,
                "output_dir": self._source_path.parent,
                "description": self._source_path.stem,
                "model_selection": "default",
                "follow_references": False,
                "timeout_seconds": 1800.0,
                "overwrite": overwrite,
            },
            self,
        )
        self._worker = worker
        worker.progressed.connect(self._progressed)
        worker.succeeded.connect(self._succeeded)
        worker.failed.connect(self._failed)
        worker.finished.connect(self._worker_finished)
        self._set_busy(True)
        worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.change_button.setEnabled(not busy)
        self.convert_button.setEnabled(not busy)
        for button in self.target_buttons.values():
            button.setEnabled(not busy)
        self.auto_open_snt.setEnabled(
            not busy and self._selected_target in ("snt", "both")
        )
        if busy:
            self.status_card.hide()
            self.overwrite_card.hide()
            self.progress_label.setText("Preparing conversion...")
            self.progress_label.show()
            self.progress_bar.setValue(0)
            self.progress_bar.show()
            self.convert_button.setText("Converting...")
        else:
            self._select_target(self._selected_target)

    def _progressed(self, percent: int, message: str) -> None:
        self.progress_bar.setValue(percent)
        self.progress_label.setText(message)
        status_bar = getattr(self._app_window, "statusBar", None)
        if callable(status_bar):
            status_bar().showMessage(
                f"Naksha Converter: {percent}% - {message}"
            )

    def _succeeded(
        self, summaries: tuple[ConversionSummary, ...]
    ) -> None:
        self.progress_bar.setValue(100)
        self.progress_label.setText("Conversion complete")
        if self._dispose_after_finish:
            return
        if self.auto_open_snt.isChecked():
            self._pending_auto_open_snt = next(
                (
                    summary.output_path
                    for summary in summaries
                    if summary.output_path.suffix.lower() == ".snt"
                ),
                None,
            )
        lines = []
        for summary in summaries:
            lines.append(f"- {summary.output_path.name}")
        self.status_title.setText(
            f"Converted {len(summaries)} file"
            f"{'s' if len(summaries) != 1 else ''}"
        )
        self.status_body.setText(
            "Saved in "
            f"{summaries[0].output_path.parent}"
            + ("\n" + "\n".join(lines) if lines else "")
        )
        self.status_card.show()
        self.progress_bar.hide()
        self.progress_label.setText("")

    def _failed(self, message: str) -> None:
        self.progress_label.setText("Conversion failed")
        self.progress_label.show()
        if self._dispose_after_finish:
            return
        QMessageBox.critical(self, "Conversion failed", message)

    def _worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if self._dispose_after_finish:
            self.close()
            self.deleteLater()
        else:
            self._set_busy(False)
            self.convert_button.setEnabled(True)
            auto_open_path = self._pending_auto_open_snt
            self._pending_auto_open_snt = None
            if auto_open_path is not None:
                open_snt = getattr(
                    self._app_window,
                    "open_snt_files_from_shell",
                    None,
                )
                if callable(open_snt):
                    open_snt([str(auto_open_path)])
                    self.close()
                else:
                    QMessageBox.warning(
                        self,
                        "SNT auto-open unavailable",
                        "The converted SNT was saved successfully, but this "
                        "NakshaAI build cannot load it automatically.",
                    )

    def _open_output_folder(self) -> None:
        if self._source_path is None:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self._source_path.parent))
        )

    def _convert_another(self) -> None:
        self._source_path = None
        self.status_card.hide()
        self.overwrite_card.hide()
        self.progress_bar.hide()
        self.progress_label.hide()
        self.convert_button.setEnabled(False)
        self._set_loaded(False)
        self._browse_input()

    # -- drag and drop ---------------------------------------------------
    def _drop_targets(self) -> list[Path]:
        urls = getattr(self, "_pending_drop_urls", None) or []
        return [Path(url) for url in urls if url]

    def _set_drop_highlight(self, active: bool) -> None:
        if getattr(self, "drop_zone", None) is None:
            return
        if self.drop_zone.property("dragActive") == active:
            return
        self.drop_zone.setProperty("dragActive", active)
        self.drop_zone.style().unpolish(self.drop_zone)
        self.drop_zone.style().polish(self.drop_zone)

    def _candidate_paths(self, mime) -> list[Path]:
        candidates: list[Path] = []
        if mime is None or not mime.hasUrls():
            return candidates
        for url in mime.urls():
            local = url.toLocalFile()
            if not local:
                continue
            path = Path(local)
            if (
                path.is_file()
                and path.suffix.lower() in (".dgn", ".dxf")
            ):
                candidates.append(path)
        return candidates

    def dragEnterEvent(self, event) -> None:
        if self.is_running():
            event.ignore()
            return
        candidates = self._candidate_paths(event.mimeData())
        if candidates:
            event.acceptProposedAction()
            self._pending_drop_urls = [
                str(p) for p in candidates
            ]
            self._set_drop_highlight(True)
        else:
            event.ignore()
            self._set_drop_highlight(False)

    def dragMoveEvent(self, event) -> None:
        candidates = self._candidate_paths(event.mimeData())
        if candidates:
            event.acceptProposedAction()
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._pending_drop_urls = []
        self._set_drop_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._pending_drop_urls = []
        self._set_drop_highlight(False)
        candidates = self._candidate_paths(event.mimeData())
        if not candidates:
            event.ignore()
            return
        event.acceptProposedAction()
        self._load_source(candidates[0])

    def is_running(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def prepare_for_unload(self) -> None:
        self._dispose_after_finish = True
        self.hide()
        if not self.is_running():
            self.close()
            self.deleteLater()

    def closeEvent(self, event) -> None:
        if self.is_running():
            self.hide()
            event.ignore()
            return
        super().closeEvent(event)


class DrawingConverterPlugin(NakshaPlugin):
    def on_load(self, app_window) -> None:
        super().on_load(app_window)
        self._dialog: Optional[_ConverterDialog] = None

    def get_ribbon_button(self):
        return {
            "label": "Convert",
            "emoji": "⇄",
            "section": "Conversion",
            "callback": self.show_converter,
            "toggleable": False,
        }

    def show_converter(self) -> None:
        if self._dialog is None:
            self._dialog = _ConverterDialog(
                self.app_window, self.app_window
            )
            self._dialog.destroyed.connect(self._dialog_destroyed)
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()

    def _dialog_destroyed(self) -> None:
        self._dialog = None

    def on_unload(self) -> None:
        if self._dialog is not None:
            self._dialog.prepare_for_unload()
            self._dialog = None
