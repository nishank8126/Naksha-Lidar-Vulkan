# ─────────────────────────────────────────────────────────────────────────────
# georeference.py — GCP (Ground Control Point) georeferencing for rasters
#
# The QGIS-Georeferencer approach: the user supplies point pairs
#     (image pixel col,row)  ↔  (true map X,Y)
# and we solve a transform that places the image at its real coordinates,
# including ROTATION (which axis-aligned "extent" placement can't do).
#
#   • 2 GCPs  → similarity (Helmert): translation + uniform scale + rotation
#   • 3+ GCPs → full affine (least-squares), also corrects shear
#
# The solved transform is converted to the image's three world corners
# (bottom-left, bottom-right, top-left) and handed to the importer's
# `gcp_corners=` placement, which uses an arbitrary parallelogram plane.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import numpy as np


def solve_transform(gcps):
    """
    gcps: list of (pixel_col, pixel_row, map_x, map_y).
    Returns affine coefficients (a, b, c, d, e, f) where
        X = a*col + b*row + c
        Y = d*col + e*row + f
    """
    n = len(gcps)
    if n < 2:
        raise ValueError("Need at least 2 control points")

    col = np.array([g[0] for g in gcps], dtype=float)
    row = np.array([g[1] for g in gcps], dtype=float)
    X = np.array([g[2] for g in gcps], dtype=float)
    Y = np.array([g[3] for g in gcps], dtype=float)

    if n >= 3:
        # Full affine, least squares.
        A = np.column_stack([col, row, np.ones(n)])
        (a, b, c), *_ = np.linalg.lstsq(A, X, rcond=None)
        (d, e, f), *_ = np.linalg.lstsq(A, Y, rcond=None)
        return (float(a), float(b), float(c), float(d), float(e), float(f))

    # n == 2 → similarity (Helmert): X = a*col - b*row + c ; Y = b*col + a*row + d
    M, v = [], []
    for i in range(n):
        M.append([col[i], -row[i], 1.0, 0.0]); v.append(X[i])
        M.append([row[i],  col[i], 0.0, 1.0]); v.append(Y[i])
    sol, *_ = np.linalg.lstsq(np.array(M, float), np.array(v, float), rcond=None)
    a, b, c, d = (float(x) for x in sol)
    # Map to the generic affine form.
    return (a, -b, c, b, a, d)


def transform_pixel(T, col, row):
    a, b, c, d, e, f = T
    return (a * col + b * row + c, d * col + e * row + f)


def corners_from_transform(T, width, height):
    """Return (bottom_left, bottom_right, top_left) world points for the image."""
    bl = transform_pixel(T, 0.0, float(height))
    br = transform_pixel(T, float(width), float(height))
    tl = transform_pixel(T, 0.0, 0.0)
    return (bl, br, tl)


def rms_error(T, gcps):
    if not gcps:
        return 0.0
    errs = []
    for col, row, X, Y in gcps:
        tx, ty = transform_pixel(T, col, row)
        errs.append(((tx - X) ** 2 + (ty - Y) ** 2) ** 0.5)
    return float(np.mean(errs))


# ─────────────────────────────────────────────────────────────────────────────
# DIALOG
# ─────────────────────────────────────────────────────────────────────────────
def open_gcp_georeferencer(app, entry: dict):
    """GCP table dialog → solves the transform and re-places the raster."""
    if entry is None or entry.get("kind") != "raster":
        return
    path = entry.get("path")
    if not path:
        return

    # Image pixel size (needed to map corners).
    width = height = None
    try:
        import rasterio
        with rasterio.open(path) as src:
            width, height = int(src.width), int(src.height)
    except Exception as exc:
        print(f"   ⚠️ Could not read raster size: {exc}")

    from PySide6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
        QPushButton, QLabel, QDialogButtonBox, QMessageBox, QHeaderView,
    )
    from PySide6.QtCore import Qt
    from gui.gis.gis_style import apply_gis_dialog_style, compact_layout

    # Non-modal so the user can click the 3D scene while the dialog stays open.
    dlg = QDialog(app)
    dlg.setWindowTitle(f"GCP Georeferencer — {entry.get('name', '')}")
    dlg.setModal(False)
    apply_gis_dialog_style(dlg)
    dlg.setMinimumWidth(480)
    app._gcp_dialog = dlg  # keep a reference so it isn't garbage-collected
    v = QVBoxLayout(dlg)
    compact_layout(v)

    info = QLabel(
        "Click-to-pick (recommended): press “Pick pair”, click a feature on the "
        "IMAGE, then click the SAME feature on your parcels. Repeat 2–3 times → "
        "Apply.\nOr type pixel (col,row) ↔ map (X,Y) directly.\n"
        f"Image size: {width or '?'} × {height or '?'} px (pixel 0,0 = top-left).")
    info.setWordWrap(True)
    v.addWidget(info)

    table = QTableWidget(0, 4)
    table.setHorizontalHeaderLabels(["Pixel col", "Pixel row", "Map X", "Map Y"])
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    v.addWidget(table)

    def _add_row(vals=("", "", "", "")):
        r = table.rowCount()
        table.insertRow(r)
        for col, val in enumerate(vals):
            table.setItem(r, col, QTableWidgetItem(str(val)))

    row_btns = QHBoxLayout()
    pick_btn = QPushButton("🖱 Pick pair on screen")
    add_btn = QPushButton("➕ Add row")
    del_btn = QPushButton("➖ Remove selected")
    row_btns.addWidget(pick_btn); row_btns.addWidget(add_btn)
    row_btns.addWidget(del_btn); row_btns.addStretch()
    v.addLayout(row_btns)

    status = QLabel("Tip: pick well-spread features (corners, junctions) for accuracy.")
    status.setWordWrap(True)
    v.addWidget(status)
    rms_lbl = QLabel("RMS error: —")
    v.addWidget(rms_lbl)

    add_btn.clicked.connect(lambda: _add_row())

    def _del():
        rows = sorted({i.row() for i in table.selectedIndexes()}, reverse=True)
        for r in rows:
            table.removeRow(r)
    del_btn.clicked.connect(_del)

    # ── current raster entry (Apply replaces it, so re-find by path) ─────────
    def _raster_entry():
        try:
            from gui.gis.gis_layers import _registry
            for e in _registry(app):
                if e.get("kind") == "raster" and e.get("path") == path:
                    return e
        except Exception:
            pass
        return entry

    def _world_to_pixel(wx, wy):
        e = _raster_entry()
        acts = e.get("actors") or []
        if not acts or not width or not height:
            return None
        try:
            b = acts[0].GetBounds()  # xmin,xmax,ymin,ymax,zmin,zmax
        except Exception:
            return None
        xmin, xmax, ymin, ymax = b[0], b[1], b[2], b[3]
        if xmax <= xmin or ymax <= ymin:
            return None
        col = (wx - xmin) / (xmax - xmin) * width
        row = (1.0 - (wy - ymin) / (ymax - ymin)) * height  # row 0 = top
        return (col, row)

    # ── one-shot world-point picker on the main scene ────────────────────────
    def _pick_once(callback):
        try:
            import vtk
            iren = app.vtk_widget.interactor
            ren = app.vtk_widget.renderer
        except Exception as exc:
            status.setText(f"⚠️ Cannot access the 3D view to pick: {exc}")
            return
        picker = vtk.vtkPropPicker()
        wpicker = vtk.vtkWorldPointPicker()
        state = {}

        def on_click(caller, _ev):
            try:
                x, y = iren.GetEventPosition()
                if picker.Pick(x, y, 0, ren):
                    wx, wy, _wz = picker.GetPickPosition()
                else:
                    wpicker.Pick(x, y, 0, ren)
                    wx, wy, _wz = wpicker.GetPickPosition()
            finally:
                try:
                    iren.RemoveObserver(state["id"])
                except Exception:
                    pass
            caller.AbortFlagOn()       # don't pan/rotate on this click
            callback(wx, wy)

        state["id"] = iren.AddObserver("LeftButtonPressEvent", on_click, 100.0)

    def _start_pick():
        status.setText("① Click a feature on the IMAGE (e.g. a building/road corner)…")
        dlg.lower()

        def got_img(wx, wy):
            px = _world_to_pixel(wx, wy)
            if px is None:
                status.setText("⚠️ Click landed off the image — try again.")
                dlg.raise_()
                return
            status.setText("② Now click the SAME feature on your PARCELS (vectors)…")

            def got_map(mx, my):
                _add_row((round(px[0], 1), round(px[1], 1),
                          round(mx, 4), round(my, 4)))
                status.setText(f"✅ Added point #{table.rowCount()}. "
                               f"Pick more (≥2) then press Apply.")
                dlg.raise_()
            _pick_once(got_map)
        _pick_once(got_img)

    pick_btn.clicked.connect(_start_pick)

    def _collect_gcps():
        gcps = []
        for r in range(table.rowCount()):
            try:
                vals = [table.item(r, c) for c in range(4)]
                if any(val is None or not val.text().strip() for val in vals):
                    continue
                gcps.append((float(vals[0].text()), float(vals[1].text()),
                             float(vals[2].text()), float(vals[3].text())))
            except (ValueError, AttributeError):
                continue
        return gcps

    def _apply(close):
        gcps = _collect_gcps()
        if len(gcps) < 2:
            QMessageBox.warning(dlg, "GCP", "Add at least 2 complete control points.")
            return
        if not width or not height:
            QMessageBox.warning(dlg, "GCP", "Could not read the image size.")
            return
        try:
            T = solve_transform(gcps)
        except Exception as exc:
            QMessageBox.warning(dlg, "GCP", f"Could not solve transform:\n{exc}")
            return
        rms = rms_error(T, gcps)
        rms_lbl.setText(f"RMS error: {rms:.3f} map units  ({len(gcps)} GCPs)")
        corners = corners_from_transform(T, width, height)

        from gui.gis.gis_layers import _import_raster_layer, _remove_layer, _DOCK_ATTR
        e = _raster_entry()
        name = e.get("name", "raster")
        _remove_layer(app, e)
        _import_raster_layer(app, path, name, gcp_corners=corners)
        dock = getattr(app, _DOCK_ATTR, None)
        if dock is not None:
            try:
                dock.refresh()
            except Exception:
                pass
        if close:
            dlg.close()

    btns = QDialogButtonBox(
        QDialogButtonBox.Apply | QDialogButtonBox.Ok | QDialogButtonBox.Close)
    btns.button(QDialogButtonBox.Apply).clicked.connect(lambda: _apply(False))
    btns.button(QDialogButtonBox.Ok).clicked.connect(lambda: _apply(True))
    btns.button(QDialogButtonBox.Close).clicked.connect(dlg.close)
    v.addWidget(btns)

    dlg.show()
    dlg.raise_()
