"""Native OGR -> VTK vector display pipeline for Naksha GIS.

This module is deliberately separate from the Digitizer.  Imported GIS layers
remain native datasource-backed layers: source CRS/schema/FID identity stays on
the registry entry, while a transformed VTK representation is built only for
rendering in the current Project CRS.

The Digitizer remains an editing/creation tool.  It is not used as the canonical
storage model for SHP/GeoJSON/GPKG/KML/GML/FGB/SQLite/CSV imports.
"""
from __future__ import annotations

import os
from pathlib import Path


def _open_vector(path: str):
    from osgeo import gdal
    flags = gdal.OF_VECTOR | gdal.OF_READONLY
    drivers = None
    if str(path).lower().endswith(".gdb"):
        drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    try:
        return gdal.OpenEx(path, flags, allowed_drivers=drivers or None)
    except TypeError:
        return gdal.OpenEx(path, flags)


def _get_layer(ds, layer_name=None):
    if ds is None:
        return None
    if layer_name:
        try:
            lyr = ds.GetLayerByName(layer_name)
        except Exception:
            lyr = None
        if lyr is not None:
            return lyr
        wanted = str(layer_name).casefold()
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            if lyr is not None and str(lyr.GetName()).casefold() == wanted:
                return lyr
        return None
    return ds.GetLayerByIndex(0) if ds.GetLayerCount() else None


def list_public_layers(path: str) -> list[dict]:
    """Return lightweight public-layer descriptors for a multi-layer source."""
    out = []
    ds = _open_vector(path)
    if ds is None:
        return out
    try:
        from gui.gis.catalog_inspector import _geom_kind, _geom_name
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            if lyr is None:
                continue
            name = str(lyr.GetName() or "")
            low = name.casefold()
            if low.startswith(("gpkg_", "sqlite_", "gdb_")):
                continue
            try:
                geom_type = int(lyr.GetLayerDefn().GetGeomType())
            except Exception:
                geom_type = 0
            try:
                count = int(lyr.GetFeatureCount(0))
                if count < 0:
                    count = int(lyr.GetFeatureCount(1))
            except Exception:
                count = -1
            out.append({
                "name": name,
                "kind": _geom_kind(geom_type),
                "geometry_type": _geom_name(geom_type),
                "feature_count": count,
            })
    finally:
        ds = None
    return out


def _source_crs(lyr):
    src_srs = None
    src_crs = None
    try:
        src_srs = lyr.GetSpatialRef()
    except Exception:
        src_srs = None
    if src_srs is None:
        return None, None
    try:
        src_srs = src_srs.Clone()
    except Exception:
        pass
    try:
        from osgeo import osr
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    try:
        src_srs.AutoIdentifyEPSG()
    except Exception:
        pass
    try:
        from pyproj import CRS
        code = src_srs.GetAuthorityCode(None)
        src_crs = CRS.from_epsg(int(code)) if code else CRS.from_wkt(src_srs.ExportToWkt())
    except Exception:
        try:
            from pyproj import CRS
            src_crs = CRS.from_wkt(src_srs.ExportToWkt())
        except Exception:
            src_crs = None
    return src_srs, src_crs


def _show_error(app, title: str, text: str):
    try:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.critical(app, title, text)
    except Exception:
        print(f"{title}: {text}")


def _build_transform(app, src_srs, src_crs, dataset_label: str):
    """Return (canvas_crs, transformer).  Fail closed on unsafe placement."""
    from gui.crs_manager import ensure_canvas_crs, get_canvas_crs

    if src_crs is not None:
        ensure_canvas_crs(
            app,
            src_crs,
            source="GIS layer CRS",
            dataset=dataset_label,
        )
    canvas_crs = get_canvas_crs(app)

    # An unknown source can only be displayed safely while the project itself
    # also has no declared CRS.  Once a Project CRS exists, raw unknown numbers
    # must never be silently placed into that world coordinate system.
    if canvas_crs is not None and src_srs is None:
        raise RuntimeError(
            "This GIS layer has no declared coordinate reference system, while "
            "the Naksha project already has a Project CRS.\n\n"
            "Naksha will not place unknown raw coordinates into the Project CRS. "
            "Define/repair the source CRS first, or use Assign CRS explicitly."
        )

    if canvas_crs is None or src_srs is None or src_crs is None:
        return canvas_crs, None

    try:
        same = bool(src_crs.equals(canvas_crs))
    except Exception:
        same = False
    if same:
        return canvas_crs, None

    from osgeo import osr
    tgt = osr.SpatialReference()
    rc = tgt.ImportFromWkt(canvas_crs.to_wkt())
    if rc not in (None, 0):
        raise RuntimeError("Could not construct the Project CRS spatial reference.")
    try:
        tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        src_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    try:
        ct = osr.CreateCoordinateTransformation(src_srs, tgt)
    except Exception as exc:
        raise RuntimeError(f"Could not create source -> Project CRS transformation: {exc}") from exc
    if ct is None:
        raise RuntimeError("Could not create source -> Project CRS transformation.")
    return canvas_crs, ct


def _field_schema(lyr) -> list[dict]:
    out = []
    try:
        defn = lyr.GetLayerDefn()
        for i in range(defn.GetFieldCount()):
            fd = defn.GetFieldDefn(i)
            item = {
                "name": fd.GetNameRef(),
                "type": fd.GetFieldTypeName(fd.GetType()),
                "width": int(fd.GetWidth()),
                "precision": int(fd.GetPrecision()),
            }
            try:
                item["domain"] = fd.GetDomainName() or ""
            except Exception:
                item["domain"] = ""
            out.append(item)
    except Exception:
        pass
    return out


def import_native_vector_layer(app, path: str, *, layer_name=None,
                               display_name=None, fmt=None,
                               max_features: int = 0):
    """Load one OGR layer through the common native GIS display pipeline.

    Returns the registered GIS layer entry on success, otherwise ``None``.
    """
    from osgeo import ogr
    from gui.gis.gdb.reader import (
        geometry_kind, geometry_type_label, _build_point_polydata,
        _build_line_polydata, _build_polygon_polydata, _infer_scene_z,
        _get_overlay_renderer, _make_vtk_actor, _pick_color, _render,
    )
    from gui.gis.gis_layers import (
        register_gis_layer, _registry, find_loaded_layer, focus_loaded_layer,
    )

    ds = _open_vector(path)
    if ds is None:
        _show_error(app, "Import GIS Data", f"GDAL could not open this vector source:\n{path}")
        return None
    try:
        lyr = _get_layer(ds, layer_name)
        if lyr is None:
            _show_error(app, "Import GIS Data", f"Layer '{layer_name}' was not found in:\n{path}")
            return None

        actual_layer_name = str(lyr.GetName() or layer_name or Path(path).stem)

        # Importing a source that is already on the map must reuse the layer
        # that is already there - never build a second actor set for it.
        already = find_loaded_layer(app, path, actual_layer_name)
        if already is not None:
            focus_loaded_layer(app, already)
            return already

        label = display_name or (
            f"{Path(path).name} · {actual_layer_name}"
            if layer_name or ds.GetLayerCount() > 1
            else Path(path).name
        )
        defn = lyr.GetLayerDefn()
        geom_type = int(defn.GetGeomType())
        kind = geometry_kind(geom_type)
        geom_label = geometry_type_label(geom_type)
        try:
            total = int(lyr.GetFeatureCount(0))
            if total < 0:
                total = int(lyr.GetFeatureCount(1))
        except Exception:
            total = -1

        source_fmt = fmt or Path(path).suffix.lower().lstrip(".") or "vector"
        try:
            drv = ds.GetDriver()
            source_driver = drv.GetDescription() if drv is not None else source_fmt
        except Exception:
            source_driver = source_fmt

        src_srs, src_crs = _source_crs(lyr)
        try:
            canvas_crs, transformer = _build_transform(
                app, src_srs, src_crs, f"{Path(path).name}:{actual_layer_name}"
            )
        except Exception as exc:
            _show_error(
                app,
                "Coordinate Transformation Failed",
                f"Layer: {actual_layer_name}\nSource: {path}\n\n{exc}\n\n"
                "The layer was NOT loaded, preventing wrong-world-coordinate display.",
            )
            return None

        try:
            source_wkt = src_srs.ExportToWkt() if src_srs is not None else None
        except Exception:
            source_wkt = src_crs.to_wkt() if src_crs is not None else None
        try:
            project_wkt = canvas_crs.to_wkt() if canvas_crs is not None else source_wkt
        except Exception:
            project_wkt = source_wkt
        try:
            has_z = bool(ogr.GT_HasZ(geom_type))
        except Exception:
            has_z = None
        try:
            has_m = bool(ogr.GT_HasM(geom_type))
        except Exception:
            has_m = None
        field_schema = _field_schema(lyr)
        try:
            from gui.gis.vector_writer import load_vector_schema
            schema_hint = load_vector_schema(path)
        except Exception:
            schema_hint = {}
        if not field_schema and schema_hint.get("fields"):
            field_schema = list(schema_hint["fields"])
        if kind == "unknown" and total == 0:
            hinted_kind = str(schema_hint.get("geometry") or "").replace(" ", "").casefold()
            hinted_kind = {
                "linestring": "line", "polyline": "line", "multilinestring": "line",
                "multipoint": "point", "multipolygon": "polygon",
            }.get(hinted_kind, hinted_kind)
            if hinted_kind in {"point", "line", "polygon"}:
                kind = hinted_kind
                geom_label = hinted_kind.title()

        from gui.gis.layer_model import GISLayerModel, attach_native_model
        native_model = GISLayerModel(
            path=str(path),
            layer_name=actual_layer_name,
            driver=source_driver,
            kind="table" if kind == "none" else "vector",
            geometry_type=geom_label,
            feature_count=max(0, total) if total >= 0 else -1,
            source_crs_wkt=source_wkt,
            project_crs_wkt=project_wkt,
            fields=field_schema,
            has_z=has_z,
            has_m=has_m,
            read_only=True,
        )

        # Non-spatial OGR layers are first-class standalone tables.  They belong
        # in the registry for Attribute Table access, but they have no visual actor.
        if kind == "none":
            entry = register_gis_layer(
                app, label, path, "table", source_fmt, [], allow_empty=True,
                feature_count=max(0, total), source_layer=actual_layer_name,
            )
            if entry is not None:
                entry.update({
                    "geom": "table",
                    "source_crs": src_crs,
                    "source_wkt": source_wkt,
                    "project_crs": canvas_crs,
                })
                attach_native_model(entry, native_model)
            return entry

        if kind == "unknown":
            # Empty GeoJSON has no feature from which GDAL can infer a geometry
            # type. Keep it editable; the first Draw feature establishes it.
            if total == 0 and str(source_driver).casefold() == "geojson":
                entry = register_gis_layer(
                    app, label, path, "vector", source_fmt, [], allow_empty=True,
                    feature_count=0, source_layer=actual_layer_name,
                )
                if entry is not None:
                    entry.update({
                        "source_layer": actual_layer_name,
                        "source_driver": source_driver,
                        "geom": "unknown",
                        "geometry_type": geom_label,
                        "feature_count": 0,
                        "loaded_feature_count": 0,
                        "source_crs": src_crs,
                        "source_wkt": source_wkt,
                        "project_crs": canvas_crs,
                        "placeholder": True,
                        "placeholder_reason": "empty_geometry_schema",
                        "_placement": "ogr-native",
                    })
                    attach_native_model(entry, native_model)
                return entry
            _show_error(
                app, "Unsupported GIS Geometry",
                f"Naksha cannot currently render geometry type '{geom_label}' in layer '{actual_layer_name}'."
            )
            return None
        # Pick a stable unused layer color.
        used_colors = {e.get("color") for e in _registry(app) if e.get("color")}
        color = _pick_color(actual_layer_name, used_colors)

        progress = None
        if total > 1000:
            try:
                from PySide6.QtWidgets import QProgressDialog
                from PySide6.QtCore import Qt, QCoreApplication
                progress = QProgressDialog(
                    f"Loading '{actual_layer_name}'...", "Cancel", 0, total, app
                )
                progress.setWindowTitle("Import GIS Layer")
                progress.setAttribute(Qt.WA_DeleteOnClose, True)
                progress.setWindowModality(Qt.WindowModal)
                progress.setMinimumDuration(150)
                progress.setValue(0)
                QCoreApplication.processEvents()
            except Exception:
                progress = None

        geoms = []
        rows_seen = 0
        geometry_features = 0
        transform_error = None
        canceled = False
        lyr.ResetReading()
        while True:
            feat = lyr.GetNextFeature()
            if feat is None:
                break
            rows_seen += 1
            if progress is not None and rows_seen % 500 == 0:
                try:
                    from PySide6.QtCore import QCoreApplication
                    progress.setValue(min(rows_seen, max(total, rows_seen)))
                    QCoreApplication.processEvents()
                    if progress.wasCanceled():
                        canceled = True
                        break
                except Exception:
                    pass

            g = feat.GetGeometryRef()
            if g is not None and not g.IsEmpty():
                clone = g.Clone()
                if transformer is not None:
                    try:
                        rc = clone.Transform(transformer)
                        if rc not in (None, 0):
                            raise RuntimeError(f"OGR Transform returned error code {rc}")
                    except Exception as exc:
                        transform_error = f"Feature FID {feat.GetFID()}: {exc}"
                        feat = None
                        break
                geoms.append(clone)
                geometry_features += 1
            feat = None
            if max_features and len(geoms) >= max_features:
                break

        if progress is not None:
            try:
                progress.close()
            except Exception:
                pass

        if canceled:
            return None
        if transform_error:
            geoms.clear()
            _show_error(
                app,
                "Coordinate Transformation Failed",
                f"A feature in '{actual_layer_name}' could not be transformed into the Project CRS.\n\n"
                f"{transform_error}\n\nThe layer was NOT loaded.",
            )
            return None

        if not geoms:
            entry = register_gis_layer(
                app, label, path, "vector", source_fmt, [], allow_empty=True,
                feature_count=max(0, total if total >= 0 else rows_seen),
                source_layer=actual_layer_name,
            )
            if entry is not None:
                entry.update({
                    "source_layer": actual_layer_name,
                    "source_driver": source_driver,
                    "geom": kind,
                    "feature_count": max(0, total if total >= 0 else rows_seen),
                    "loaded_feature_count": rows_seen,
                    "source_crs": src_crs,
                    "source_wkt": source_wkt,
                    "project_crs": canvas_crs,
                    "placeholder": True,
                    "placeholder_reason": "empty" if rows_seen == 0 else "no_non_empty_geometry",
                    "_placement": "ogr-native",
                })
                native_model.feature_count = max(0, total if total >= 0 else rows_seen)
                attach_native_model(entry, native_model)
            return entry

        scene_z = _infer_scene_z(app)
        outline_pd = None
        if kind == "point":
            pd = _build_point_polydata(geoms, scene_z)
        elif kind == "line":
            pd = _build_line_polydata(geoms, scene_z)
        else:
            pd, outline_pd = _build_polygon_polydata(geoms, scene_z)
        geoms.clear()

        actors = []
        if pd is not None:
            actor = _make_vtk_actor(pd, kind, color, outline=False)
            if actor is not None:
                actors.append(actor)
        if outline_pd is not None:
            actor = _make_vtk_actor(outline_pd, kind, color, outline=True)
            if actor is not None:
                actors.append(actor)

        if not actors:
            _show_error(
                app, "Import GIS Data",
                f"Layer '{actual_layer_name}' contained {geometry_features} geometry feature(s), "
                "but no renderable VTK representation could be built."
            )
            return None

        overlay = _get_overlay_renderer(app)
        if overlay is not None:
            for actor in actors:
                try:
                    overlay.AddActor(actor)
                except Exception:
                    pass
        _render(app)

        entry = register_gis_layer(
            app, label, path, "vector", source_fmt, actors,
            feature_count=max(0, total if total >= 0 else rows_seen),
            source_layer=actual_layer_name,
        )
        if entry is None:
            return None
        entry.update({
            "source_layer": actual_layer_name,
            "source_driver": source_driver,
            "geom": kind,
            "geometry_type": geom_label,
            "feature_count": max(0, total if total >= 0 else rows_seen),
            "loaded_feature_count": rows_seen,
            "rendered_geometry_feature_count": geometry_features,
            "source_crs": src_crs,
            "source_wkt": source_wkt,
            "project_crs": canvas_crs,
            "_placement": "ogr-native",
        })
        native_model.feature_count = max(0, total if total >= 0 else rows_seen)
        attach_native_model(entry, native_model)
        try:
            from gui.gis.gis_layers import zoom_to_gis_entries
            zoom_to_gis_entries(app, [entry])
        except Exception:
            pass
        return entry
    finally:
        ds = None
