# ─────────────────────────────────────────────────────────────────────────────
# raster_properties.py — QGIS-style "Raster Layer Properties" for imported TIFs
#
# Mirrors the core of QGIS's raster Symbology tab and re-renders the VTK texture
# on the existing layer actor (placement / georeference is preserved):
#
#   • Render type     : Multiband color  /  Singleband gray
#   • Band mapping     : assign file bands → Red / Green / Blue  (or Gray)
#   • Contrast stretch : No enhancement  /  Stretch to Min-Max  (+ editable min/max)
#   • Brightness, Contrast, Gamma, Saturation
#   • Opacity
#   • Resampling       : Nearest  /  Bilinear
#
# The processing pipeline is pure NumPy so it is testable without a GL context.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import numpy as np

# Texture pixel budget (matches the importer) to stay memory-safe.
_MAX_TEXTURE_PIXELS = 16_000_000


def default_raster_style(band_count: int) -> dict:
    """A sensible QGIS-like default style for a freshly imported raster."""
    multiband = band_count >= 3
    return {
        "render_type": "multiband" if multiband else "singleband",
        "red_band": 1,
        "green_band": 2 if band_count >= 2 else 1,
        "blue_band": 3 if band_count >= 3 else 1,
        "gray_band": 1,
        "enhancement": "none" if multiband else "minmax",      # "none" | "minmax"
        "min": None,                  # None → auto per-band; or float
        "max": None,
        "brightness": 0,              # -255..255
        "contrast": 0,                # -100..100
        "gamma": 1.0,                 # 0.1..5.0
        "saturation": 0,              # -100..100
        "opacity": 100,               # 0..100 (opaque so list-order stacking works)
        "resampling": "nearest",      # "nearest" | "bilinear" — QGIS-default parity
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROCESSING (pure NumPy) — produces an (H, W, 3) uint8 array
# ─────────────────────────────────────────────────────────────────────────────
def _to_float_band(arr: np.ndarray) -> np.ndarray:
    return arr.astype(np.float32, copy=False)


def _stretch(band: np.ndarray, lo, hi) -> np.ndarray:
    if lo is None or hi is None:
        finite = band[np.isfinite(band)]
        if finite.size:
            lo = float(np.percentile(finite, 2.0))
            hi = float(np.percentile(finite, 98.0))
        else:
            lo, hi = 0.0, 255.0
    if hi <= lo:
        hi = lo + 1.0
    out = (band - lo) / (hi - lo) * 255.0
    return np.clip(out, 0.0, 255.0)


def process_raster_array(src_bands: dict, style: dict) -> np.ndarray:
    """
    src_bands: {band_index(int, 1-based): 2D float ndarray}
    Returns an (H, W, 3) uint8 image after applying the style.
    """
    rt = style.get("render_type", "multiband")
    enh = style.get("enhancement", "minmax")
    mn, mx = style.get("min"), style.get("max")

    def chan(idx):
        a = src_bands.get(int(idx))
        if a is None:
            # fall back to any available band
            a = next(iter(src_bands.values()))
        source_dtype = a.dtype
        a = _to_float_band(a)
        if enh == "minmax":
            lo, hi = style.get("_band_ranges", {}).get(int(idx), (mn, mx))
            a = _stretch(a, lo, hi)
        else:
            # Match the importer's integer conversion instead of clipping
            # 16-bit imagery to white when the first detail window arrives.
            if np.issubdtype(source_dtype, np.integer):
                info = np.iinfo(source_dtype)
                if info.min < 0 or info.max > 255:
                    a = (a - float(info.min)) * (255.0 / (int(info.max) - int(info.min)))
            a = np.clip(a, 0.0, 255.0)
        return a

    if rt == "singleband":
        g = chan(style.get("gray_band", 1))
        rgb = np.stack([g, g, g], axis=-1)
    else:
        r = chan(style.get("red_band", 1))
        g = chan(style.get("green_band", 2))
        b = chan(style.get("blue_band", 3))
        rgb = np.stack([r, g, b], axis=-1)

    # ── Gamma (QGIS: >1 brightens) ───────────────────────────────────────────
    gamma = float(style.get("gamma", 1.0) or 1.0)
    if abs(gamma - 1.0) > 1e-3 and gamma > 0:
        rgb = 255.0 * np.power(np.clip(rgb / 255.0, 0.0, 1.0), 1.0 / gamma)

    # ── Brightness ───────────────────────────────────────────────────────────
    brightness = float(style.get("brightness", 0) or 0)
    if brightness:
        rgb = rgb + brightness

    # ── Contrast ─────────────────────────────────────────────────────────────
    contrast = float(style.get("contrast", 0) or 0)
    if contrast:
        c = max(-255.0, min(255.0, contrast * 2.55))
        factor = (259.0 * (c + 255.0)) / (255.0 * (259.0 - c))
        rgb = factor * (rgb - 128.0) + 128.0

    # ── Saturation ───────────────────────────────────────────────────────────
    sat = float(style.get("saturation", 0) or 0)
    if sat:
        lum = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2])
        lum = lum[..., None]
        factor = 1.0 + sat / 100.0
        rgb = lum + (rgb - lum) * factor

    return np.clip(rgb, 0.0, 255.0).astype(np.uint8)


def _read_source_bands(path: str, style: dict, app=None, window=None, out_shape=None):
    """
    Read just the bands the style needs, with memory-safe downsampling.

    window: optional rasterio Window to read a sub-region of the file instead
        of the whole raster (used by raster_lod.py's zoom-based refresh).
    out_shape: optional explicit (out_h, out_w) to read at - overrides the
        budget-derived downsample (raster_lod.py already matches this to the
        screen). Ignored when None, in which case the whole-raster budget
        logic below picks the size (the "re-style whole file" path).
    """
    import rasterio
    from rasterio.enums import Resampling

    max_pixels = _MAX_TEXTURE_PIXELS
    if app is not None and out_shape is None:
        try:
            from gui.vector_export import _geotiff_texture_pixel_budget
            max_pixels = _geotiff_texture_pixel_budget(app, hard_cap=_MAX_TEXTURE_PIXELS)
        except Exception:
            pass

    rt = style.get("render_type", "multiband")
    if rt == "singleband":
        wanted = [int(style.get("gray_band", 1))]
    else:
        wanted = [int(style.get("red_band", 1)),
                  int(style.get("green_band", 2)),
                  int(style.get("blue_band", 3))]

    with rasterio.open(path) as src:
        count = src.count
        wanted = [max(1, min(count, b)) for b in wanted]
        read_kwargs = {}
        if window is not None:
            read_kwargs["window"] = window
        if out_shape is not None:
            oh, ow = out_shape
            read_kwargs["out_shape"] = (oh, ow)
            read_kwargs["resampling"] = (Resampling.nearest if style.get("resampling", "nearest") == "nearest" else Resampling.bilinear)
        else:
            w = window.width if window is not None else src.width
            h = window.height if window is not None else src.height
            total = w * h
            if total > max_pixels:
                scale = (max_pixels / float(total)) ** 0.5
                ow, oh = max(1, int(w * scale)), max(1, int(h * scale))
                read_kwargs["out_shape"] = (oh, ow)
                read_kwargs["resampling"] = (Resampling.nearest if style.get("resampling", "nearest") == "nearest" else Resampling.bilinear)
        indexes = sorted(set(wanted))
        data = src.read(indexes, **read_kwargs)
        return dict(zip(indexes, data)), count


def apply_raster_style(app, entry: dict, style: dict) -> bool:
    """
    Re-render the layer's VTK texture using `style` and update the actor.
    Keeps the existing plane (placement / georeference) intact.
    """
    try:
        import vtk
        from vtk.util import numpy_support
    except Exception as exc:
        print(f"   ❌ VTK unavailable: {exc}")
        return False

    path = entry.get("path")
    actors = entry.get("actors") or []
    if not path or not actors:
        return False
    actor = actors[0]
    meta = getattr(actor, "_raster_lod_meta", None)
    if meta and meta.get("eligible"):
        # Restyle the visible window asynchronously; a whole-file texture on
        # the cropped LOD plane would distort the image as well as block UI.
        entry["style"] = dict(style)
        entry["opacity"] = max(0.0, min(1.0, style.get("opacity", 100) / 100.0))
        actor.GetProperty().SetOpacity(entry["opacity"])
        meta.pop("_last_window", None)
        from gui.gis.raster_lod import kick
        kick(app)
        app.vtk_widget.render()
        return True

    try:
        bands, _count = _read_source_bands(path, style, app=app)
    except Exception as exc:
        print(f"   ❌ Could not read raster bands: {exc}")
        return False

    rgb = process_raster_array(bands, style)              # (H, W, 3) uint8
    h, w = rgb.shape[0], rgb.shape[1]

    # Flip rows so VTK's bottom-left origin matches the image, like the importer.
    rgb_flipped = np.ascontiguousarray(rgb[::-1, :, :]).reshape(-1, 3)
    vtk_colors = numpy_support.numpy_to_vtk(
        rgb_flipped, deep=True, array_type=vtk.VTK_UNSIGNED_CHAR)
    vtk_colors.SetNumberOfComponents(3)
    vtk_colors.SetName("Colors")

    vtk_image = vtk.vtkImageData()
    vtk_image.SetDimensions(w, h, 1)
    vtk_image.GetPointData().SetScalars(vtk_colors)

    try:
        tex = actor.GetTexture()
        if tex is None:
            tex = vtk.vtkTexture()
            actor.SetTexture(tex)
        tex.SetInputData(vtk_image)
        if style.get("resampling", "bilinear") == "nearest":
            tex.InterpolateOff()
        else:
            tex.InterpolateOn()
        tex.Modified()
        actor.GetProperty().SetOpacity(max(0.0, min(1.0, style.get("opacity", 90) / 100.0)))
        actor.Modified()
    except Exception as exc:
        print(f"   ❌ Could not update texture: {exc}")
        return False

    # Persist the style on the layer + sync the panel opacity value.
    entry["style"] = dict(style)
    entry["opacity"] = max(0.0, min(1.0, style.get("opacity", 90) / 100.0))

    try:
        app.vtk_widget.render()
    except Exception:
        try:
            app.vtk_widget.GetRenderWindow().Render()
        except Exception:
            pass
    print(f"   🎨 Raster style applied: {style.get('render_type')} "
          f"bands(R{style.get('red_band')},G{style.get('green_band')},"
          f"B{style.get('blue_band')}) opacity={style.get('opacity')}%")
    return True


# ─────────────────────────────────────────────────────────────────────────────
# DIALOG
# ─────────────────────────────────────────────────────────────────────────────
def open_raster_properties(app, entry: dict):
    """Open the QGIS-style Raster Layer Properties dialog for a raster layer."""
    if entry is None or entry.get("kind") != "raster":
        return
    path = entry.get("path")
    if not path:
        return

    # Discover band count.
    band_count = 3
    try:
        import rasterio
        with rasterio.open(path) as src:
            band_count = int(src.count)
    except Exception as exc:
        print(f"   ⚠️ Could not read band count: {exc}")

    style = dict(entry.get("style") or default_raster_style(band_count))

    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLabel,
        QSlider, QDoubleSpinBox, QLineEdit, QGroupBox, QDialogButtonBox, QWidget,
    )
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QDoubleValidator
    try:
        from gui.theme_manager import get_dialog_stylesheet
    except Exception:
        def get_dialog_stylesheet():
            return ""

    dlg = QDialog(app)
    dlg.setWindowTitle(f"Raster Properties — {entry.get('name', '')}")
    dlg.setModal(True)
    dlg.setStyleSheet(get_dialog_stylesheet())
    dlg.setMinimumWidth(380)
    root = QVBoxLayout(dlg)

    band_items = [f"Band {i}" for i in range(1, band_count + 1)]

    def _band_combo(default_idx):
        cb = QComboBox()
        cb.addItems(band_items)
        cb.setCurrentIndex(max(0, min(band_count - 1, int(default_idx) - 1)))
        return cb

    # ── Band rendering group ─────────────────────────────────────────────────
    band_box = QGroupBox("Band rendering")
    band_form = QFormLayout(band_box)

    render_combo = QComboBox()
    render_combo.addItems(["Multiband color", "Singleband gray"])
    render_combo.setCurrentIndex(0 if style["render_type"] == "multiband" else 1)
    band_form.addRow("Render type", render_combo)

    red_cb = _band_combo(style["red_band"])
    green_cb = _band_combo(style["green_band"])
    blue_cb = _band_combo(style["blue_band"])
    gray_cb = _band_combo(style["gray_band"])
    band_form.addRow("Red band", red_cb)
    band_form.addRow("Green band", green_cb)
    band_form.addRow("Blue band", blue_cb)
    band_form.addRow("Gray band", gray_cb)
    root.addWidget(band_box)

    # ── Contrast enhancement / min-max ───────────────────────────────────────
    minmax_box = QGroupBox("Min / Max value settings")
    mm_form = QFormLayout(minmax_box)
    enh_combo = QComboBox()
    enh_combo.addItems(["Stretch to Min-Max", "No enhancement"])
    enh_combo.setCurrentIndex(0 if style.get("enhancement") == "minmax" else 1)
    mm_form.addRow("Contrast enhancement", enh_combo)
    min_edit = QLineEdit("" if style.get("min") is None else str(style["min"]))
    max_edit = QLineEdit("" if style.get("max") is None else str(style["max"]))
    min_edit.setPlaceholderText("auto (2%)")
    max_edit.setPlaceholderText("auto (98%)")
    min_edit.setValidator(QDoubleValidator())
    max_edit.setValidator(QDoubleValidator())
    mm_form.addRow("Min", min_edit)
    mm_form.addRow("Max", max_edit)
    root.addWidget(minmax_box)

    # ── Brightness / contrast / gamma / saturation ───────────────────────────
    adj_box = QGroupBox("Brightness & contrast")
    adj_form = QFormLayout(adj_box)

    def _slider(lo, hi, val):
        s = QSlider(Qt.Horizontal)
        s.setRange(lo, hi)
        s.setValue(int(val))
        return s

    bright = _slider(-255, 255, style.get("brightness", 0))
    contrast = _slider(-100, 100, style.get("contrast", 0))
    sat = _slider(-100, 100, style.get("saturation", 0))
    gamma_spin = QDoubleSpinBox()
    gamma_spin.setRange(0.1, 5.0)
    gamma_spin.setSingleStep(0.1)
    gamma_spin.setValue(float(style.get("gamma", 1.0)))
    adj_form.addRow("Brightness", bright)
    adj_form.addRow("Contrast", contrast)
    adj_form.addRow("Saturation", sat)
    adj_form.addRow("Gamma", gamma_spin)
    root.addWidget(adj_box)

    # ── Opacity / resampling ─────────────────────────────────────────────────
    misc_box = QGroupBox("Rendering")
    misc_form = QFormLayout(misc_box)
    opacity = _slider(0, 100, style.get("opacity", 90))
    opacity_lbl = QLabel(f"{opacity.value()}%")
    op_row = QHBoxLayout()
    op_w = QWidget(); op_w.setLayout(op_row)
    op_row.addWidget(opacity, 1); op_row.addWidget(opacity_lbl)
    opacity.valueChanged.connect(lambda v: opacity_lbl.setText(f"{v}%"))
    misc_form.addRow("Opacity", op_w)
    resample_combo = QComboBox()
    resample_combo.addItems(["Bilinear", "Nearest neighbour"])
    resample_combo.setCurrentIndex(0 if style.get("resampling") == "bilinear" else 1)
    misc_form.addRow("Resampling", resample_combo)
    root.addWidget(misc_box)

    # ── Enable/disable band rows by render type ──────────────────────────────
    def _sync_band_rows():
        multiband = render_combo.currentIndex() == 0
        for w in (red_cb, green_cb, blue_cb):
            w.setEnabled(multiband)
        gray_cb.setEnabled(not multiband)
    render_combo.currentIndexChanged.connect(_sync_band_rows)
    _sync_band_rows()

    # ── Buttons ──────────────────────────────────────────────────────────────
    btns = QDialogButtonBox(
        QDialogButtonBox.Apply | QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    root.addWidget(btns)

    def _collect_style():
        def _f(le):
            t = le.text().strip()
            try:
                return float(t) if t else None
            except ValueError:
                return None
        return {
            "render_type": "multiband" if render_combo.currentIndex() == 0 else "singleband",
            "red_band": red_cb.currentIndex() + 1,
            "green_band": green_cb.currentIndex() + 1,
            "blue_band": blue_cb.currentIndex() + 1,
            "gray_band": gray_cb.currentIndex() + 1,
            "enhancement": "minmax" if enh_combo.currentIndex() == 0 else "none",
            "min": _f(min_edit),
            "max": _f(max_edit),
            "brightness": bright.value(),
            "contrast": contrast.value(),
            "saturation": sat.value(),
            "gamma": gamma_spin.value(),
            "opacity": opacity.value(),
            "resampling": "bilinear" if resample_combo.currentIndex() == 0 else "nearest",
        }

    def _apply():
        apply_raster_style(app, entry, _collect_style())
        dock = getattr(app, "_gis_layers_dock", None)
        if dock is not None:
            try:
                dock.refresh()
            except Exception:
                pass

    btns.button(QDialogButtonBox.Apply).clicked.connect(_apply)
    btns.accepted.connect(lambda: (_apply(), dlg.accept()))
    btns.rejected.connect(dlg.reject)

    dlg.exec()
