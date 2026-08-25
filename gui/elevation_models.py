"""Create, preview, and export DTM/DSM elevation GeoTIFFs.

The numerical functions in this module are deliberately independent from Qt so
they can be tested without starting the application.  The small UI layer at the
bottom is loaded only when one of the Edit-ribbon surface-model buttons is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import os
import shutil
import tempfile
import uuid

import numpy as np


NODATA_VALUE = -32767.0
DEFAULT_RESOLUTION = 0.5
DEFAULT_COVERAGE_BUFFER = 1.5
MAX_RASTER_CELLS = 30_000_000
_CHUNK_POINTS = 2_000_000


class ElevationModelError(ValueError):
    """Raised when an elevation model cannot be built from the loaded data."""


class ElevationModelCancelled(RuntimeError):
    """Raised when the user cancels a background model build."""


@dataclass
class ElevationModel:
    """An in-memory north-up elevation grid and its export metadata."""

    surface_type: str
    elevation: np.ndarray
    left: float
    top: float
    resolution: float
    crs: object = None
    nodata: float = NODATA_VALUE
    ground_classes: tuple[int, ...] = ()
    source_point_count: int = 0
    source_token: int | None = None
    classification_revision: int = 0
    preview_path: str | None = None

    @property
    def height(self) -> int:
        return int(self.elevation.shape[0])

    @property
    def width(self) -> int:
        return int(self.elevation.shape[1])

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (
            float(self.left),
            float(self.top - self.height * self.resolution),
            float(self.left + self.width * self.resolution),
            float(self.top),
        )

    @property
    def valid_count(self) -> int:
        return int(np.count_nonzero(np.isfinite(self.elevation)))

    def export_array(self) -> np.ndarray:
        out = np.asarray(self.elevation, dtype=np.float32).copy()
        out[~np.isfinite(out)] = np.float32(self.nodata)
        return out


def _notify(
    callback: Callable[[int, str], None] | None,
    percent: int,
    message: str,
) -> None:
    if callback is not None:
        callback(int(percent), str(message))


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ElevationModelCancelled("Elevation-model creation was cancelled.")


def _parse_surface_type(surface_type: str) -> str:
    kind = str(surface_type or "").strip().upper()
    if kind not in {"DTM", "DSM"}:
        raise ElevationModelError("Surface type must be DTM or DSM.")
    return kind


def _finite_xy_bounds(
    xyz: np.ndarray,
    progress: Callable[[int, str], None] | None,
    cancelled: Callable[[], bool] | None,
) -> tuple[float, float, float, float]:
    xmin = ymin = np.inf
    xmax = ymax = -np.inf
    count = len(xyz)
    finite_points = 0

    for start in range(0, count, _CHUNK_POINTS):
        _check_cancelled(cancelled)
        block = xyz[start : start + _CHUNK_POINTS]
        finite = np.isfinite(block[:, :3]).all(axis=1)
        if np.any(finite):
            xy = block[finite, :2]
            xmin = min(xmin, float(np.min(xy[:, 0])))
            xmax = max(xmax, float(np.max(xy[:, 0])))
            ymin = min(ymin, float(np.min(xy[:, 1])))
            ymax = max(ymax, float(np.max(xy[:, 1])))
            finite_points += int(np.count_nonzero(finite))
        _notify(progress, 5 + int(10 * min(count, start + len(block)) / count),
                "Inspecting point-cloud extent…")

    if finite_points == 0:
        raise ElevationModelError("The loaded point cloud has no finite XYZ points.")
    return xmin, ymin, xmax, ymax


def build_elevation_model(
    xyz,
    classifications=None,
    surface_type: str = "DSM",
    resolution: float = DEFAULT_RESOLUTION,
    ground_classes: Iterable[int] = (2,),
    coverage_buffer: float = DEFAULT_COVERAGE_BUFFER,
    crs=None,
    source_token: int | None = None,
    classification_revision: int = 0,
    max_cells: int = MAX_RASTER_CELLS,
    progress: Callable[[int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ElevationModel:
    """Rasterize a loaded point cloud into a DTM or DSM.

    DSM cells use the highest return.  DTM cells use the mean elevation of
    returns from the selected ground classes (ASPRS class 2 by default).
    Empty DTM cells inside the point-cloud footprint are filled with smooth
    inverse-distance interpolation; cells outside that footprint remain
    NoData.
    """

    kind = _parse_surface_type(surface_type)
    try:
        resolution = float(resolution)
        coverage_buffer = float(coverage_buffer)
    except (TypeError, ValueError) as exc:
        raise ElevationModelError("Resolution and coverage buffer must be numeric.") from exc
    if not np.isfinite(resolution) or resolution <= 0:
        raise ElevationModelError("Resolution must be greater than zero.")
    if not np.isfinite(coverage_buffer) or coverage_buffer < 0:
        raise ElevationModelError("Coverage buffer cannot be negative.")

    points = np.asarray(xyz)
    if points.ndim != 2 or points.shape[1] < 3 or len(points) == 0:
        raise ElevationModelError("Loaded point data must be a non-empty Nx3 array.")

    classes = None if classifications is None else np.asarray(classifications).reshape(-1)
    if kind == "DTM" and (classes is None or len(classes) != len(points)):
        raise ElevationModelError(
            "DTM creation requires one classification value per loaded point."
        )

    parsed_ground = tuple(sorted({int(value) for value in ground_classes}))
    if kind == "DTM" and not parsed_ground:
        raise ElevationModelError("Select at least one ground class for the DTM.")

    _notify(progress, 2, "Preparing elevation model…")
    xmin, ymin, xmax, ymax = _finite_xy_bounds(points, progress, cancelled)
    width = max(1, int(np.floor((xmax - xmin) / resolution)) + 1)
    height = max(1, int(np.floor((ymax - ymin) / resolution)) + 1)
    cell_count = width * height
    if cell_count > int(max_cells):
        suggested = resolution * np.sqrt(cell_count / float(max_cells))
        raise ElevationModelError(
            f"The requested grid is {width:,} × {height:,} "
            f"({cell_count:,} cells), above the {int(max_cells):,}-cell safety limit. "
            f"Use a resolution of at least {suggested:.2f} m."
        )

    _check_cancelled(cancelled)
    coverage = np.zeros(cell_count, dtype=bool)
    if kind == "DSM":
        grid = np.full(cell_count, -np.inf, dtype=np.float32)
        ground_counts = None
    else:
        grid = np.zeros(cell_count, dtype=np.float32)
        ground_counts = np.zeros(cell_count, dtype=np.uint32)
    selected_points = 0
    point_count = len(points)

    _notify(progress, 18, f"Rasterizing {kind} points…")
    for start in range(0, point_count, _CHUNK_POINTS):
        _check_cancelled(cancelled)
        stop = min(point_count, start + _CHUNK_POINTS)
        block = points[start:stop, :3]
        finite = np.isfinite(block).all(axis=1)
        if not np.any(finite):
            continue

        valid_xyz = block[finite]
        columns = np.floor((valid_xyz[:, 0] - xmin) / resolution).astype(np.int64)
        rows = np.floor((ymax - valid_xyz[:, 1]) / resolution).astype(np.int64)
        np.clip(columns, 0, width - 1, out=columns)
        np.clip(rows, 0, height - 1, out=rows)
        flat_cells = rows
        flat_cells *= width
        flat_cells += columns
        coverage[flat_cells] = True

        if kind == "DSM":
            target_cells = flat_cells
            target_z = valid_xyz[:, 2]
            np.maximum.at(grid, target_cells, target_z)
            selected_points += len(target_z)
        else:
            block_classes = classes[start:stop][finite]
            ground_mask = np.isin(block_classes, parsed_ground)
            if np.any(ground_mask):
                target_cells = flat_cells[ground_mask]
                target_z = valid_xyz[ground_mask, 2].astype(np.float32, copy=False)
                np.add.at(grid, target_cells, target_z)
                np.add.at(ground_counts, target_cells, 1)
                selected_points += len(target_z)

        _notify(
            progress,
            18 + int(42 * stop / point_count),
            f"Rasterizing {kind}: {stop:,} / {point_count:,} points…",
        )

    if selected_points == 0:
        if kind == "DTM":
            labels = ", ".join(map(str, parsed_ground))
            raise ElevationModelError(
                f"No points use the selected ground class(es): {labels}. "
                "Classify ground points first or choose the correct class codes."
            )
        raise ElevationModelError("No finite points were available for DSM creation.")

    if kind == "DTM":
        observed_flat = ground_counts > 0
        grid[observed_flat] /= ground_counts[observed_flat]
        grid[~observed_flat] = np.nan
        del observed_flat
        del ground_counts

    grid = grid.reshape(height, width)
    coverage = coverage.reshape(height, width)
    observed = np.isfinite(grid)

    _check_cancelled(cancelled)
    _notify(progress, 64, "Building the point-cloud coverage mask…")
    if coverage_buffer > 0:
        from scipy.ndimage import binary_dilation

        iterations = max(1, int(np.ceil(coverage_buffer / resolution)))
        coverage = binary_dilation(coverage, iterations=iterations)

    missing = coverage & ~observed
    if np.any(missing):
        _check_cancelled(cancelled)
        _notify(progress, 72, "Interpolating empty surface cells…")
        if kind == "DTM":
            # GDAL's fill algorithm blends nearby ground samples by inverse
            # distance.  This avoids the large Voronoi patches produced by a
            # nearest-cell fill and gives a much more natural bare-earth TIN-
            # like surface beneath buildings and vegetation.
            from rasterio.fill import fillnodata
            from rasterio.errors import NotGeoreferencedWarning
            import warnings

            search_cells = min(512, max(8, int(np.ceil(50.0 / resolution))))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", NotGeoreferencedWarning)
                interpolated = fillnodata(
                    grid.copy(),
                    mask=observed.astype(np.uint8),
                    max_search_distance=search_cells,
                    smoothing_iterations=1,
                )
            fillable = missing & np.isfinite(interpolated)
            grid[fillable] = interpolated[fillable]
            del interpolated
            missing = coverage & ~np.isfinite(grid)

        # DSM gaps are normally tiny, and a nearest surface value preserves
        # hard roof/vegetation breaks.  It is also a fallback for unusually
        # large DTM voids beyond the IDW search radius.
        if np.any(missing):
            from scipy.ndimage import distance_transform_edt

            current_observed = np.isfinite(grid)
            nearest = distance_transform_edt(
                ~current_observed,
                return_distances=False,
                return_indices=True,
            )
            grid[missing] = grid[nearest[0][missing], nearest[1][missing]]
            del nearest

    grid[~coverage] = np.nan
    if not np.any(np.isfinite(grid)):
        raise ElevationModelError("The generated surface contains no valid cells.")

    _check_cancelled(cancelled)
    _notify(progress, 88, "Finalizing elevation grid…")
    return ElevationModel(
        surface_type=kind,
        elevation=np.asarray(grid, dtype=np.float32),
        left=xmin,
        top=ymax,
        resolution=resolution,
        crs=crs,
        ground_classes=parsed_ground if kind == "DTM" else (),
        source_point_count=point_count,
        source_token=source_token,
        classification_revision=int(classification_revision or 0),
    )


def render_elevation_relief_rgba(
    elevation,
    valid_mask=None,
    pixel_size: float = 1.0,
    color_ramp=None,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    vertical_exaggeration: float = 2.0,
) -> np.ndarray:
    """Render an elevation grid as rainbow colour relief with hillshade.

    The output is an ``H x W x 4`` uint8 image. NoData pixels are transparent.
    Lighting is applied only where a complete 3x3 elevation neighbourhood
    exists, preventing dark halos around the survey boundary.
    """

    values = np.asarray(elevation, dtype=np.float32)
    if values.ndim != 2:
        raise ElevationModelError("Elevation relief input must be a 2D array.")
    if valid_mask is None:
        valid = np.isfinite(values)
    else:
        valid = np.asarray(valid_mask, dtype=bool) & np.isfinite(values)
        if valid.shape != values.shape:
            raise ElevationModelError("Elevation data and valid mask must have the same shape.")

    rgba = np.zeros((*values.shape, 4), dtype=np.uint8)
    if not np.any(valid):
        return rgba

    z = values[valid].astype(np.float32, copy=False)
    lo = float(np.percentile(z, 1.0))
    hi = float(np.percentile(z, 99.0))
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        lo = float(np.min(z))
        hi = float(np.max(z))
    if hi <= lo:
        hi = lo + 1.0
    normalized = np.clip((z - lo) / (hi - lo), 0.0, 1.0)

    if color_ramp and len(color_ramp) >= 2:
        ramp = sorted(color_ramp, key=lambda item: float(item[0]))
        stops = np.asarray([float(item[0]) for item in ramp], dtype=np.float32)
        ramp_colors = np.asarray([item[1] for item in ramp], dtype=np.float32)
        if np.nanmax(ramp_colors) <= 1.0 + 1e-6:
            ramp_colors *= 255.0
    else:
        stops = np.asarray([0.0, 0.25, 0.50, 0.75, 1.0], dtype=np.float32)
        ramp_colors = np.asarray(
            [[0, 0, 255], [0, 255, 255], [0, 255, 0],
             [255, 255, 0], [255, 0, 0]],
            dtype=np.float32,
        )
    colors = np.column_stack(
        [np.interp(normalized, stops, ramp_colors[:, channel]) for channel in range(3)]
    ).astype(np.float32)

    cell = max(float(pixel_size), 1e-9)
    work = np.where(valid, values, 0.0).astype(np.float32, copy=False)
    south_gradient, east_gradient = np.gradient(work, cell, cell)
    north_gradient = -south_gradient
    exaggeration = max(0.0, float(vertical_exaggeration))
    east_gradient *= exaggeration
    north_gradient *= exaggeration

    azimuth_rad = np.deg2rad(float(azimuth))
    altitude_rad = np.deg2rad(float(altitude))
    light_x = np.sin(azimuth_rad) * np.cos(altitude_rad)
    light_y = np.cos(azimuth_rad) * np.cos(altitude_rad)
    light_z = np.sin(altitude_rad)

    normal_length = np.sqrt(
        east_gradient * east_gradient
        + north_gradient * north_gradient
        + np.float32(1.0)
    )
    illumination = (
        (-east_gradient * light_x)
        + (-north_gradient * light_y)
        + light_z
    ) / normal_length
    del south_gradient
    del east_gradient
    del north_gradient
    del normal_length

    # Flat ground stays near its original brightness. Slopes facing away from
    # the light retain ambient detail instead of collapsing to pure black.
    relief = 0.35 + 0.85 * np.clip((illumination + 0.25) / 1.25, 0.0, 1.0)
    from scipy.ndimage import binary_erosion

    shaded_region = binary_erosion(valid, iterations=1, border_value=0)
    relief[~shaded_region] = 1.0
    colors *= relief[valid, None]
    rgba[valid, :3] = np.clip(colors, 0.0, 255.0).astype(np.uint8)
    rgba[valid, 3] = 255
    return rgba


def write_elevation_geotiff(model: ElevationModel, output_path) -> str:
    """Write a single-band Float32, north-up elevation GeoTIFF."""

    import rasterio
    from rasterio.transform import from_origin

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    crs = model.crs
    if crs:
        try:
            crs = rasterio.crs.CRS.from_user_input(crs)
        except Exception as exc:
            raise ElevationModelError(f"Could not interpret project CRS: {crs}") from exc

    array = model.export_array()
    profile = {
        "driver": "GTiff",
        "height": model.height,
        "width": model.width,
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": from_origin(
            model.left, model.top, model.resolution, model.resolution
        ),
        "nodata": np.float32(model.nodata),
        "compress": "deflate",
        "predictor": 3,
        "BIGTIFF": "IF_SAFER",
    }

    valid = array[array != np.float32(model.nodata)]
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(array, 1)
        dst.set_band_description(1, f"{model.surface_type} elevation")
        dst.update_tags(
            AREA_OR_POINT="Point",
            SURFACE_MODEL=model.surface_type,
            RESOLUTION_METERS=f"{model.resolution:g}",
            GROUND_CLASSES=",".join(map(str, model.ground_classes)),
        )
        if valid.size:
            dst.update_tags(
                1,
                STATISTICS_MINIMUM=f"{float(np.min(valid)):.12g}",
                STATISTICS_MAXIMUM=f"{float(np.max(valid)):.12g}",
                STATISTICS_MEAN=f"{float(np.mean(valid, dtype=np.float64)):.12g}",
                STATISTICS_STDDEV=f"{float(np.std(valid, dtype=np.float64)):.12g}",
                STATISTICS_VALID_PERCENT=f"{100.0 * valid.size / array.size:.6g}",
            )
    return str(path)


def _project_crs(app):
    wkt = getattr(app, "project_crs_wkt", None)
    if wkt:
        return wkt
    epsg = getattr(app, "project_crs_epsg", None)
    return f"EPSG:{int(epsg)}" if epsg else None


def _loaded_model_data(app):
    data = getattr(app, "data", None)
    if not isinstance(data, dict) or data.get("xyz") is None:
        raise ElevationModelError("Load a LAS/LAZ point cloud before creating a DTM or DSM.")
    return data["xyz"], data.get("classification")


def _default_export_path(app, kind: str) -> str:
    source = getattr(app, "loaded_file", None) or getattr(app, "last_save_path", None)
    if source:
        source_path = Path(source)
        return str(source_path.with_name(f"{source_path.stem}_{kind}.tif"))
    return str(Path.home() / f"elevation_{kind}.tif")


def _cache_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "NakshaAI" / "elevation_models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _show_model_dialog(app, kind: str):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import (
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFormLayout,
        QLabel,
        QLineEdit,
        QMessageBox,
        QVBoxLayout,
    )

    xyz, classifications = _loaded_model_data(app)
    dialog = QDialog(app)
    dialog.setWindowTitle(f"Create {kind}")
    dialog.setMinimumWidth(460)
    root = QVBoxLayout(dialog)
    description = (
        "Creates a bare-earth terrain grid from classified ground points."
        if kind == "DTM"
        else "Creates a top-surface grid using the highest LiDAR return in each cell."
    )
    label = QLabel(description)
    label.setWordWrap(True)
    root.addWidget(label)

    settings = QSettings("NakshaAI", "LidarApp")
    form = QFormLayout()
    resolution = QDoubleSpinBox(dialog)
    resolution.setRange(0.05, 100.0)
    resolution.setDecimals(2)
    resolution.setSingleStep(0.1)
    resolution.setSuffix(" m")
    resolution.setValue(
        float(settings.value("elevation_models/resolution", DEFAULT_RESOLUTION))
    )
    form.addRow("Grid resolution:", resolution)

    buffer_spin = QDoubleSpinBox(dialog)
    buffer_spin.setRange(0.0, 100.0)
    buffer_spin.setDecimals(2)
    buffer_spin.setSingleStep(0.5)
    buffer_spin.setSuffix(" m")
    buffer_spin.setValue(
        float(settings.value("elevation_models/coverage_buffer",
                             DEFAULT_COVERAGE_BUFFER))
    )
    buffer_spin.setToolTip(
        "Expands the point-coverage mask while keeping areas outside the survey as NoData."
    )
    form.addRow("Coverage buffer:", buffer_spin)

    ground_edit = None
    if kind == "DTM":
        ground_edit = QLineEdit(
            str(settings.value("elevation_models/ground_classes", "2")), dialog
        )
        ground_edit.setPlaceholderText("2")
        ground_edit.setToolTip("Comma-separated LAS class codes; ASPRS Ground is class 2.")
        form.addRow("Ground class(es):", ground_edit)

    add_preview = QCheckBox("Add the generated color elevation layer to the map", dialog)
    add_preview.setChecked(
        settings.value("elevation_models/add_preview", True, type=bool)
    )
    form.addRow("", add_preview)
    root.addLayout(form)

    estimate = QLabel(dialog)
    estimate.setWordWrap(True)
    root.addWidget(estimate)
    xyz_array = np.asarray(xyz)
    # A sampled extent is sufficient for the dialog estimate and avoids a
    # multi-hundred-megabyte boolean/copy spike on very large clouds.
    stride = max(1, len(xyz_array) // 1_000_000)
    estimate_xy = xyz_array[::stride, :2]
    finite = np.isfinite(estimate_xy).all(axis=1)
    if np.any(finite):
        xy = estimate_xy[finite]
        span_x = float(np.max(xy[:, 0]) - np.min(xy[:, 0]))
        span_y = float(np.max(xy[:, 1]) - np.min(xy[:, 1]))

        def update_estimate():
            cell = resolution.value()
            width = int(np.floor(span_x / cell)) + 1
            height = int(np.floor(span_y / cell)) + 1
            estimate.setText(
                f"Output: approximately {width:,} × {height:,} cells "
                f"({width * height:,} total)."
            )

        resolution.valueChanged.connect(update_estimate)
        update_estimate()

    buttons = QDialogButtonBox(
        QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=dialog
    )
    buttons.button(QDialogButtonBox.Ok).setText(f"Create {kind}")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    root.addWidget(buttons)

    if dialog.exec() != QDialog.Accepted:
        return None

    ground_classes = (2,)
    if ground_edit is not None:
        try:
            ground_classes = tuple(
                int(part.strip())
                for part in ground_edit.text().replace(";", ",").split(",")
                if part.strip()
            )
        except ValueError:
            QMessageBox.warning(
                app, "Invalid Ground Classes",
                "Ground classes must be comma-separated integer LAS class codes.",
            )
            return None
        if not ground_classes:
            QMessageBox.warning(
                app, "Invalid Ground Classes", "Enter at least one ground class."
            )
            return None

    settings.setValue("elevation_models/resolution", resolution.value())
    settings.setValue("elevation_models/coverage_buffer", buffer_spin.value())
    settings.setValue("elevation_models/add_preview", add_preview.isChecked())
    if ground_edit is not None:
        settings.setValue("elevation_models/ground_classes", ground_edit.text())

    return {
        "xyz": xyz,
        "classifications": classifications,
        "resolution": resolution.value(),
        "coverage_buffer": buffer_spin.value(),
        "ground_classes": ground_classes,
        "add_preview": add_preview.isChecked(),
    }


def create_elevation_model(app, surface_type: str) -> None:
    """Open settings and build a cached DTM/DSM without blocking the GUI."""

    from PySide6.QtCore import QThread, Qt, Signal
    from PySide6.QtWidgets import QMessageBox, QProgressDialog

    kind = _parse_surface_type(surface_type)
    try:
        options = _show_model_dialog(app, kind)
    except ElevationModelError as exc:
        QMessageBox.information(app, f"Create {kind}", str(exc))
        return
    if options is None:
        return

    jobs = getattr(app, "_elevation_model_jobs", None)
    if not isinstance(jobs, dict):
        jobs = {}
        app._elevation_model_jobs = jobs
    if kind in jobs:
        QMessageBox.information(app, f"Create {kind}", f"A {kind} build is already running.")
        return

    preview_path = _cache_dir() / f"{kind}_{uuid.uuid4().hex}.tif"
    progress_dialog = QProgressDialog(
        f"Creating {kind}…", "Cancel", 0, 100, app
    )
    progress_dialog.setWindowTitle(f"Create {kind}")
    progress_dialog.setWindowModality(Qt.WindowModal)
    progress_dialog.setMinimumDuration(0)
    progress_dialog.setValue(0)

    class Worker(QThread):
        progressed = Signal(int, str)
        succeeded = Signal(object)
        failed = Signal(str)

        def __init__(self):
            super().__init__(app)
            self.setObjectName(f"Naksha{kind}Builder")
            self.cancel_requested = False

        def run(self):
            try:
                model = build_elevation_model(
                    options["xyz"],
                    options["classifications"],
                    surface_type=kind,
                    resolution=options["resolution"],
                    ground_classes=options["ground_classes"],
                    coverage_buffer=options["coverage_buffer"],
                    crs=_project_crs(app),
                    source_token=id(options["xyz"]),
                    classification_revision=getattr(app, "classification_revision", 0),
                    progress=self.progressed.emit,
                    cancelled=lambda: self.cancel_requested,
                )
                self.progressed.emit(92, f"Writing {kind} preview GeoTIFF…")
                model.preview_path = write_elevation_geotiff(model, preview_path)
                self.progressed.emit(100, f"{kind} complete")
                self.succeeded.emit(model)
            except ElevationModelCancelled:
                self.failed.emit("Cancelled")
            except Exception as exc:
                self.failed.emit(str(exc))

        def cancel(self):
            self.cancel_requested = True

    worker = Worker()
    jobs[kind] = {
        "worker": worker,
        "progress": progress_dialog,
    }

    def update_progress(percent, text):
        progress_dialog.setLabelText(text)
        progress_dialog.setValue(percent)

    def finish_success(model):
        cache = getattr(app, "elevation_models", None)
        if not isinstance(cache, dict):
            cache = {}
            app.elevation_models = cache
        cache[kind] = model

        progress_dialog.close()
        if options["add_preview"]:
            try:
                from gui.gis.gis_layers import _import_raster_layer

                _import_raster_layer(
                    app,
                    model.preview_path,
                    f"{kind} {model.resolution:g} m",
                )
            except Exception as exc:
                QMessageBox.warning(
                    app,
                    f"{kind} Created",
                    f"The {kind} was created, but its preview could not be added:\n{exc}",
                )
        left, bottom, right, top = model.bounds
        QMessageBox.information(
            app,
            f"{kind} Created",
            f"{kind} created successfully.\n\n"
            f"Grid: {model.width:,} × {model.height:,} at "
            f"{model.resolution:g} m\n"
            f"Valid cells: {model.valid_count:,}\n"
            f"Bounds: {left:.3f}, {bottom:.3f}, {right:.3f}, {top:.3f}\n\n"
            f"Use Edit → Surface Models → Export {kind} to save a copy.",
        )

    def finish_failure(message):
        progress_dialog.close()
        if message != "Cancelled":
            QMessageBox.critical(app, f"{kind} Creation Failed", message)

    def cleanup():
        jobs.pop(kind, None)

    worker.progressed.connect(update_progress)
    worker.succeeded.connect(finish_success)
    worker.failed.connect(finish_failure)
    progress_dialog.canceled.connect(worker.cancel)
    worker.finished.connect(cleanup)
    worker.finished.connect(worker.deleteLater)
    progress_dialog.show()
    worker.start()


def export_elevation_model(app, surface_type: str) -> None:
    """Export the latest cached DTM/DSM to a user-selected GeoTIFF."""

    from PySide6.QtWidgets import QFileDialog, QMessageBox

    kind = _parse_surface_type(surface_type)
    cache = getattr(app, "elevation_models", None)
    model = cache.get(kind) if isinstance(cache, dict) else None
    if model is None or not getattr(model, "preview_path", None):
        answer = QMessageBox.question(
            app,
            f"Export {kind}",
            f"No {kind} has been created for the current point cloud.\n"
            f"Create it now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if answer == QMessageBox.Yes:
            create_elevation_model(app, kind)
        return

    try:
        xyz, _ = _loaded_model_data(app)
    except ElevationModelError as exc:
        QMessageBox.information(app, f"Export {kind}", str(exc))
        return
    stale = (
        model.source_token != id(xyz)
        or (
            kind == "DTM"
            and model.classification_revision
            != int(getattr(app, "classification_revision", 0) or 0)
        )
    )
    if stale:
        answer = QMessageBox.question(
            app,
            f"{kind} May Be Out of Date",
            f"The point cloud or its classifications changed after this {kind} "
            f"was created.\n\nExport the cached {kind} anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

    output_path, _ = QFileDialog.getSaveFileName(
        app,
        f"Export {kind} GeoTIFF",
        _default_export_path(app, kind),
        "GeoTIFF (*.tif *.tiff)",
    )
    if not output_path:
        return
    if Path(output_path).suffix.lower() not in {".tif", ".tiff"}:
        output_path += ".tif"

    try:
        source = Path(model.preview_path)
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if os.path.normcase(os.path.abspath(source)) != os.path.normcase(
            os.path.abspath(destination)
        ):
            shutil.copy2(source, destination)
        model.last_export_path = str(destination)
        QMessageBox.information(
            app, f"{kind} Exported", f"{kind} GeoTIFF exported to:\n{destination}"
        )
        status = getattr(app, "statusBar", None)
        if callable(status):
            status().showMessage(f"{kind} exported to {destination}", 5000)
    except Exception as exc:
        QMessageBox.critical(app, f"{kind} Export Failed", str(exc))
