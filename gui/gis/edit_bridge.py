"""Bridge Naksha Draw geometry into datasource-backed GIS feature layers.

The canvas/digitizer works in Project CRS. OGR layers remain the source of
truth and are written in their own source CRS. This module is the only place
where those two models meet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_MANAGED_FIELDS = {"shape_length", "shape_area"}


@dataclass
class FeatureWriteResult:
    success: bool
    fid: int | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def _open_update(path: str):
    from osgeo import gdal
    drivers = None
    if str(path).lower().endswith(".gdb"):
        drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    flags = gdal.OF_VECTOR | gdal.OF_UPDATE
    try:
        return gdal.OpenEx(str(path), flags, allowed_drivers=drivers or None)
    except TypeError:
        return gdal.OpenEx(str(path), flags)


def _get_layer(ds, name: str | None):
    if ds is None:
        return None
    if name:
        try:
            layer = ds.GetLayerByName(str(name))
        except Exception:
            layer = None
        if layer is not None:
            return layer
        wanted = str(name).casefold()
        for index in range(ds.GetLayerCount()):
            candidate = ds.GetLayerByIndex(index)
            if candidate is not None and str(candidate.GetName()).casefold() == wanted:
                return candidate
        return None
    return ds.GetLayerByIndex(0) if ds.GetLayerCount() else None


def _target(entry: dict) -> tuple[str, str]:
    path = str(entry.get("gdb_path") or entry.get("path") or "").strip()
    layer_name = str(entry.get("gdb_layer_name") or entry.get("source_layer") or "").strip()
    return path, layer_name


def _flat_type(geom_type: int) -> int:
    from osgeo import ogr
    try:
        return int(ogr.GT_Flatten(geom_type))
    except Exception:
        return int(geom_type) & 0xFFFFFFFC


def layer_geometry_kind(geom_type: int) -> str:
    from osgeo import ogr
    flat = _flat_type(geom_type)
    if flat in {ogr.wkbPoint, ogr.wkbMultiPoint}:
        return "point"
    if flat in {ogr.wkbLineString, ogr.wkbMultiLineString}:
        return "line"
    if flat in {ogr.wkbPolygon, ogr.wkbMultiPolygon}:
        return "polygon"
    if flat == ogr.wkbNone:
        return "table"
    return "unknown"


def inspect_edit_target(entry: dict) -> dict:
    """Open an entry for update and return its authoritative OGR schema."""
    from osgeo import ogr
    path, layer_name = _target(entry)
    if not path:
        raise RuntimeError("The selected layer has no datasource path.")
    ds = _open_update(path)
    if ds is None:
        raise RuntimeError(
            "The selected datasource could not be opened for update. It may be "
            "read-only, locked, or unsupported by the installed GDAL driver."
        )
    try:
        layer = _get_layer(ds, layer_name)
        if layer is None:
            raise RuntimeError(f"Layer '{layer_name}' was not found in {path}.")
        definition = layer.GetLayerDefn()
        geom_type = int(definition.GetGeomType())
        fields = []
        for index in range(definition.GetFieldCount()):
            field_def = definition.GetFieldDefn(index)
            name = str(field_def.GetNameRef())
            if name.casefold() in _MANAGED_FIELDS:
                continue
            try:
                nullable = bool(field_def.IsNullable())
            except Exception:
                nullable = True
            try:
                default = field_def.GetDefault()
            except Exception:
                default = None
            try:
                domain = field_def.GetDomainName() or ""
            except Exception:
                domain = ""
            fields.append({
                "name": name,
                "type": int(field_def.GetType()),
                "type_name": str(field_def.GetFieldTypeName(field_def.GetType())),
                "nullable": nullable,
                "default": default,
                "domain": str(domain),
            })
        # GeoJSON cannot encode fields while it has zero features. Merge the
        # private Catalog schema so the first inserted feature materializes them.
        try:
            from gui.gis.vector_writer import load_vector_schema
            hints = load_vector_schema(path)
        except Exception:
            hints = {}
        known = {item["name"].casefold() for item in fields}
        type_map = {
            "integer": ogr.OFTInteger, "integer64": ogr.OFTInteger64,
            "real": ogr.OFTReal, "date": ogr.OFTDate,
            "datetime": ogr.OFTDateTime, "string": ogr.OFTString,
        }
        for hint in list(hints.get("fields") or []):
            name = str(hint.get("name") or "").strip()
            if not name or name.casefold() in known or name.casefold() in _MANAGED_FIELDS:
                continue
            ftype = type_map.get(str(hint.get("type") or "string").casefold(), ogr.OFTString)
            fields.append({
                "name": name, "type": ftype, "type_name": ogr.GetFieldTypeName(ftype),
                "nullable": bool(hint.get("nullable", True)), "default": hint.get("default"),
                "domain": str(hint.get("domain") or ""),
                "width": int(hint.get("width") or 0),
                "precision": int(hint.get("precision") or 0),
            })
            known.add(name.casefold())
        try:
            driver = str(ds.GetDriver().GetDescription())
        except Exception:
            driver = ""
        kind = layer_geometry_kind(geom_type)
        if kind == "unknown":
            hinted = str(entry.get("geom") or "").casefold()
            if hinted in {"point", "line", "polygon"}:
                kind = hinted
        return {
            "path": path,
            "layer_name": str(layer.GetName()),
            "geometry_type": geom_type,
            "kind": kind,
            "fields": fields,
            "driver": driver,
        }
    finally:
        ds = None


def _is_closed_ring(coords: list) -> bool:
    if len(coords) < 4:
        return False
    try:
        a, b = coords[0], coords[-1]
        return abs(float(a[0]) - float(b[0])) <= 1e-8 and abs(float(a[1]) - float(b[1])) <= 1e-8
    except Exception:
        return False


# Draw tools emit many type labels ("parallel_line", "ortho_right", "hatcharea",
# ...). Unknown labels fall back to this token scan so a newly added tool still
# reaches the GIS writer instead of being reported as unsupported.
_KIND_TOKENS = (
    ("gispoint", "point"),
    ("hatch", "polygon"),
    ("polygon", "polygon"),
    ("rectangle", "polygon"),
    ("circle", "polygon"),
    ("freehand", "polygon"),
    ("ortho", "polygon"),
    ("polyline", "line"),
    ("centerline", "line"),
    ("smartline", "line"),
    ("parallel", "line"),
    ("line", "line"),
    ("curve", "line"),
)


def drawing_geometry_kind(drawing: dict) -> str:
    """Classify a finalized Draw entry as point / line / polygon / unsupported."""
    dtype = str(drawing.get("type") or "").casefold()
    coords = list(drawing.get("coords") or [])
    if dtype in {"point", "gispoint"} or len(coords) == 1:
        return "point"
    closed = _is_closed_ring(coords)
    if dtype in {"polygon", "rectangle", "circle", "freehand", "hatcharea"}:
        return "polygon"
    if dtype == "polyline" and drawing.get("source") != "accudraw":
        return "polygon" if closed else "line"
    if dtype == "smartline" and closed:
        return "polygon"
    if dtype in {"smartline", "line", "line_segment", "curve", "centerline", "parallel", "polyline"}:
        return "line"
    for token, kind in _KIND_TOKENS:
        if token not in dtype:
            continue
        if kind == "polygon" or not closed or len(coords) < 3:
            return kind
        # A closed ring from a line-family tool (e.g. "parallel_polygon") is a
        # polygon, matching the polyline rule above.
        return "polygon"
    # Unknown tool: infer the kind from the geometry itself.
    if len(coords) >= 3 and closed:
        return "polygon"
    if len(coords) >= 2:
        return "line"
    return "unsupported"


def _clean_coords(drawing: dict) -> list[tuple[float, float, float]]:
    import math
    clean = []
    for raw in list(drawing.get("coords") or []):
        if raw is None or len(raw) < 2:
            continue
        x, y = float(raw[0]), float(raw[1])
        z = float(raw[2]) if len(raw) > 2 and raw[2] is not None else 0.0
        if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
            raise ValueError("Geometry contains a non-finite coordinate.")
        point = (x, y, z)
        if not clean or point != clean[-1]:
            clean.append(point)
    return clean


def drawing_to_ogr_geometry(drawing: dict, target_geom_type: int):
    """Convert a finalized Draw entry in Project CRS to an OGR geometry."""
    from osgeo import ogr
    target_kind = layer_geometry_kind(target_geom_type)
    source_kind = drawing_geometry_kind(drawing)
    if target_kind != source_kind:
        raise ValueError(
            f"The Draw tool produced {source_kind} geometry, but the active GIS layer requires {target_kind}."
        )
    coords = _clean_coords(drawing)
    try:
        has_z = bool(ogr.GT_HasZ(target_geom_type))
    except Exception:
        has_z = False

    def add_point(geometry, point):
        if has_z:
            geometry.AddPoint(float(point[0]), float(point[1]), float(point[2]))
        else:
            geometry.AddPoint_2D(float(point[0]), float(point[1]))

    if target_kind == "point":
        if len(coords) != 1:
            raise ValueError("A point feature requires exactly one coordinate.")
        geom = ogr.Geometry(ogr.wkbPoint25D if has_z else ogr.wkbPoint)
        add_point(geom, coords[0])
    elif target_kind == "line":
        if len(coords) < 2:
            raise ValueError("A line feature requires at least two vertices.")
        geom = ogr.Geometry(ogr.wkbLineString25D if has_z else ogr.wkbLineString)
        for point in coords:
            add_point(geom, point)
    elif target_kind == "polygon":
        if len(coords) >= 2 and coords[0][:2] == coords[-1][:2]:
            coords = coords[:-1]
        if len(coords) < 3:
            raise ValueError("A polygon feature requires at least three vertices.")
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for point in coords:
            add_point(ring, point)
        add_point(ring, coords[0])
        geom = ogr.Geometry(ogr.wkbPolygon25D if has_z else ogr.wkbPolygon)
        geom.AddGeometry(ring)
    else:
        raise ValueError("The active layer is not an editable point, line, or polygon layer.")

    flat_target = _flat_type(target_geom_type)
    multi_types = {
        ogr.wkbMultiPoint: ogr.wkbMultiPoint,
        ogr.wkbMultiLineString: ogr.wkbMultiLineString,
        ogr.wkbMultiPolygon: ogr.wkbMultiPolygon,
    }
    if flat_target in multi_types:
        multi = ogr.Geometry(multi_types[flat_target])
        multi.AddGeometry(geom)
        geom = multi
    return geom


def _project_to_source_transform(app, layer):
    from osgeo import osr
    from gui.crs_manager import get_canvas_crs
    project_crs = get_canvas_crs(app) if app is not None else None
    source_srs = layer.GetSpatialRef()
    if project_crs is None and source_srs is None:
        return None
    if project_crs is None:
        raise RuntimeError("The project has no CRS; coordinates cannot be safely written to this georeferenced layer.")
    if source_srs is None:
        raise RuntimeError("The target layer has no CRS; Project CRS coordinates cannot be safely written to it.")
    project_srs = osr.SpatialReference()
    rc = project_srs.ImportFromWkt(project_crs.to_wkt())
    if rc not in (None, 0):
        raise RuntimeError("Could not construct the Project CRS transformation.")
    source_srs = source_srs.Clone()
    try:
        project_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    except Exception:
        pass
    try:
        if bool(project_srs.IsSame(source_srs)):
            return None
    except Exception:
        pass
    transform = osr.CreateCoordinateTransformation(project_srs, source_srs)
    if transform is None:
        raise RuntimeError("Could not create the Project CRS to layer CRS transformation.")
    return transform


def _set_field(feature, field_info: dict, value: Any):
    from osgeo import ogr
    if value is None or value == "":
        return
    name = field_info["name"]
    ftype = int(field_info["type"])
    if ftype in {ogr.OFTInteger, ogr.OFTInteger64}:
        feature.SetField(name, int(float(value)))
    elif ftype == ogr.OFTReal:
        feature.SetField(name, float(value))
    else:
        feature.SetField(name, str(value))


def insert_drawing_feature(app, entry: dict, drawing: dict, attributes: dict | None = None,
                           subtype_code: Any = None) -> FeatureWriteResult:
    """Transactionally append one Draw geometry to the selected OGR layer."""
    from osgeo import ogr
    path, layer_name = _target(entry)
    ds = None
    layer = None
    transaction = False
    try:
        ds = _open_update(path)
        if ds is None:
            raise RuntimeError("Datasource could not be opened for update; check permissions and locks.")
        layer = _get_layer(ds, layer_name)
        if layer is None:
            raise RuntimeError(f"Layer '{layer_name}' was not found.")
        definition = layer.GetLayerDefn()
        # Materialize pending fields before the first feature in schema-less
        # empty formats such as GeoJSON.
        try:
            from gui.gis.vector_writer import load_vector_schema
            hints = load_vector_schema(path)
        except Exception:
            hints = {}
        existing_fields = {
            str(definition.GetFieldDefn(i).GetNameRef()).casefold()
            for i in range(definition.GetFieldCount())
        }
        type_map = {
            "integer": ogr.OFTInteger, "integer64": ogr.OFTInteger64,
            "real": ogr.OFTReal, "date": ogr.OFTDate,
            "datetime": ogr.OFTDateTime, "string": ogr.OFTString,
        }
        for hint in list(hints.get("fields") or []):
            name = str(hint.get("name") or "").strip()
            if not name or name.casefold() in existing_fields:
                continue
            field_def = ogr.FieldDefn(
                name, type_map.get(str(hint.get("type") or "string").casefold(), ogr.OFTString)
            )
            if hint.get("width"):
                field_def.SetWidth(int(hint["width"]))
            if hint.get("precision"):
                field_def.SetPrecision(int(hint["precision"]))
            if layer.CreateField(field_def) != 0:
                raise RuntimeError(f"Could not create pending field '{name}'.")
            existing_fields.add(name.casefold())
        definition = layer.GetLayerDefn()
        target_geom_type = int(definition.GetGeomType())
        if layer_geometry_kind(target_geom_type) == "unknown":
            source_kind = drawing_geometry_kind(drawing)
            target_geom_type = {
                "point": ogr.wkbPoint,
                "line": ogr.wkbLineString,
                "polygon": ogr.wkbPolygon,
            }.get(source_kind, target_geom_type)
        geom = drawing_to_ogr_geometry(drawing, target_geom_type)
        transform = _project_to_source_transform(app, layer)
        if transform is not None:
            rc = geom.Transform(transform)
            if rc not in (None, 0):
                raise RuntimeError(f"Coordinate transformation returned error code {rc}.")

        values = dict(attributes or {})
        if subtype_code not in (None, "", "default") and entry.get("gdb_path"):
            try:
                from gui.gis.gdb.engine import get_engine
                cascade = get_engine(path).get_cascade_data(layer_name)
                if cascade and cascade.get("subtype_field"):
                    values[cascade["subtype_field"]] = subtype_code
            except Exception:
                pass
        try:
            transaction = layer.StartTransaction() == 0
        except Exception:
            transaction = False
        feature = ogr.Feature(definition)
        feature.SetGeometry(geom)
        for index in range(definition.GetFieldCount()):
            field_def = definition.GetFieldDefn(index)
            name = str(field_def.GetNameRef())
            if name in values:
                _set_field(feature, {"name": name, "type": field_def.GetType()}, values[name])
        rc = layer.CreateFeature(feature)
        if rc != 0:
            raise RuntimeError(f"OGR CreateFeature failed with error code {rc}.")
        fid = int(feature.GetFID()) if feature.GetFID() >= 0 else None
        feature = None
        if transaction and layer.CommitTransaction() != 0:
            raise RuntimeError("The feature transaction could not be committed.")
        transaction = False
        try:
            layer.SyncToDisk()
            ds.FlushCache()
        except Exception:
            pass
        return FeatureWriteResult(True, fid=fid)
    except Exception as exc:
        if transaction and layer is not None:
            try:
                layer.RollbackTransaction()
            except Exception:
                pass
        return FeatureWriteResult(False, error=str(exc))
    finally:
        ds = None


def _domain_values(entry: dict, field_info: dict, subtype_code=None) -> dict:
    path, layer_name = _target(entry)
    if not entry.get("gdb_path"):
        return {}
    try:
        from gui.gis.gdb.engine import get_engine
        engine = get_engine(path)
        cascade = engine.get_cascade_data(layer_name) or {}
        dependent = cascade.get("dependent_fields", {}).get(field_info["name"], {})
        values = dependent.get(str(subtype_code), dependent.get(subtype_code, {}))
        if values:
            return dict(values)
        domain_name = field_info.get("domain") or engine.get_domain_for_field(
            layer_name, layer_name, field_info["name"]
        )
        return dict(engine.all_domains.get(domain_name, {})) if domain_name else {}
    except Exception:
        return {}


def ask_feature_attributes(parent, entry: dict, schema: dict, subtype_code=None):
    """Return (accepted, values), using coded domains where available."""
    fields = list(schema.get("fields") or [])
    if subtype_code not in (None, "", "default") and entry.get("gdb_path"):
        try:
            from gui.gis.gdb.engine import get_engine
            cascade = get_engine(schema["path"]).get_cascade_data(schema["layer_name"]) or {}
            subtype_field = str(cascade.get("subtype_field") or "").casefold()
            fields = [f for f in fields if f["name"].casefold() != subtype_field]
        except Exception:
            pass
    if not fields:
        return True, {}

    from PySide6.QtWidgets import (
        QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
        QLineEdit, QMessageBox, QVBoxLayout,
    )
    try:
        from gui.gis.gis_style import apply_gis_dialog_style, compact_layout
    except Exception:
        apply_gis_dialog_style = lambda _widget: None
        compact_layout = lambda _layout: None
    dialog = QDialog(parent)
    dialog.setWindowTitle(f"New Feature | {schema['layer_name']}")
    dialog.setMinimumWidth(420)
    apply_gis_dialog_style(dialog)
    layout = QVBoxLayout(dialog)
    compact_layout(layout)
    heading = QLabel(f"Enter attributes for the new {schema['kind']} feature")
    heading.setStyleSheet("font-weight:600;")
    layout.addWidget(heading)
    form = QFormLayout()
    widgets = {}
    for info in fields:
        domain_values = _domain_values(entry, info, subtype_code)
        if domain_values:
            widget = QComboBox()
            if info.get("nullable") or info.get("default") not in (None, ""):
                widget.addItem("<Default / NULL>", None)
            for code, label in domain_values.items():
                widget.addItem(f"{label} ({code})", code)
        else:
            widget = QLineEdit()
            default = info.get("default")
            if default not in (None, ""):
                widget.setPlaceholderText(f"Default: {default}")
        label = info["name"] + ("" if info.get("nullable") or info.get("default") not in (None, "") else " *")
        widget.setToolTip(info.get("type_name", ""))
        form.addRow(label, widget)
        widgets[info["name"]] = (widget, info)
    layout.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
    layout.addWidget(buttons)

    def accept():
        missing = []
        for name, (widget, info) in widgets.items():
            value = widget.currentData() if isinstance(widget, QComboBox) else widget.text().strip()
            if value in (None, "") and not info.get("nullable") and info.get("default") in (None, ""):
                missing.append(name)
        if missing:
            QMessageBox.warning(dialog, "Required Attributes", "Enter a value for: " + ", ".join(missing))
            return
        dialog.accept()

    buttons.accepted.connect(accept)
    buttons.rejected.connect(dialog.reject)
    if dialog.exec() != QDialog.Accepted:
        return False, {}
    result = {}
    for name, (widget, _info) in widgets.items():
        value = widget.currentData() if isinstance(widget, QComboBox) else widget.text().strip()
        if value not in (None, ""):
            result[name] = value
    return True, result


class GISDigitizeBridge:
    def __init__(self, app):
        self.app = app
        self.digitizer = getattr(app, "digitizer", None)
        self._busy = False

    def install(self):
        """Subscribe to Draw completion. Safe to call repeatedly."""
        digitizer = getattr(self.app, "digitizer", None) or self.digitizer
        if digitizer is None:
            return False
        self.digitizer = digitizer
        subscribe = getattr(digitizer, "add_drawing_finalized_callback", None)
        if subscribe is None:
            return False
        subscribe(self.on_drawing_finalized)
        return True

    def _message(self, text: str, error: bool = False):
        try:
            self.app.statusBar().showMessage(text, 5000)
        except Exception:
            pass
        if error:
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(self.app, "GIS Feature Editing", text)
            except Exception:
                pass

    def _remove_runtime_parts(self, drawing: dict, keep_geometry: bool):
        digitizer = self.digitizer
        keep = {"actor", "fill_actor", "hatch_actors"} if keep_geometry else set()
        for key in ("actor", "fill_actor", "start_marker", "end_marker", "arrow_actor"):
            if key in keep:
                continue
            value = drawing.get(key)
            values = value if isinstance(value, list) else [value]
            for actor in values:
                if actor is None:
                    continue
                try:
                    digitizer._remove_actor_from_overlay(actor)
                except Exception:
                    try:
                        digitizer.renderer.RemoveViewProp(actor)
                    except Exception:
                        pass
        for marker in list(drawing.get("vertex_markers") or []):
            try:
                digitizer._remove_actor_from_overlay(marker)
            except Exception:
                pass
        if not keep_geometry:
            for actor in list(drawing.get("hatch_actors") or []):
                try:
                    digitizer._remove_actor_from_overlay(actor)
                except Exception:
                    pass
        digitizer.drawings[:] = [item for item in digitizer.drawings if item is not drawing]

    def _adopt_visual(self, entry: dict, drawing: dict, subtype_code=None):
        actors = []
        for key in ("actor", "fill_actor"):
            if drawing.get(key) is not None:
                actors.append(drawing[key])
        actors.extend([a for a in list(drawing.get("hatch_actors") or []) if a is not None])
        color = entry.get("color")
        if subtype_code is not None:
            sub = (entry.get("sub_layers") or {}).get(subtype_code, {})
            color = sub.get("color") or color
            sub_actors = sub.setdefault("actors", [])
            sub_actors.extend(a for a in actors if a not in sub_actors)
            if sub.get("actor") is None and actors:
                sub["actor"] = actors[0]
            sub["feature_count"] = int(sub.get("feature_count") or 0) + 1
        entry_actors = entry.setdefault("actors", [])
        entry_actors.extend(a for a in actors if a not in entry_actors)
        if color:
            try:
                from PySide6.QtGui import QColor
                qc = QColor(str(color))
                rgb = (qc.redF(), qc.greenF(), qc.blueF())
                for actor in actors:
                    if hasattr(actor, "GetProperty"):
                        actor.GetProperty().SetColor(*rgb)
            except Exception:
                pass

    def on_drawing_finalized(self, drawing: dict):
        entry = getattr(self.app, "active_gis_edit_layer", None)
        if self._busy or not isinstance(entry, dict) or entry.get("kind") != "vector":
            return
        if drawing.get("_gis_bridge_handled") or drawing.get("type") == "text":
            return
        self._busy = True
        drawing["_gis_bridge_handled"] = True
        try:
            schema = inspect_edit_target(entry)
            source_kind = drawing_geometry_kind(drawing)
            if schema["kind"] != "unknown" and source_kind != schema["kind"]:
                self._remove_runtime_parts(drawing, keep_geometry=False)
                self._message(
                    f"Feature not saved: this tool creates {source_kind} geometry, but "
                    f"'{schema['layer_name']}' requires {schema['kind']}.", error=True,
                )
                return
            subtype_code = getattr(self.app, "active_gis_edit_subtype", None)
            accepted, values = ask_feature_attributes(self.app, entry, schema, subtype_code)
            if not accepted:
                self._remove_runtime_parts(drawing, keep_geometry=False)
                self._message("New GIS feature cancelled.")
                return
            result = insert_drawing_feature(self.app, entry, drawing, values, subtype_code=subtype_code)
            if not result.success:
                self._remove_runtime_parts(drawing, keep_geometry=False)
                self._message(f"Feature was not saved: {result.error}", error=True)
                return
            drawing.update({
                "_gis_committed": True,
                "_gis_feature_fid": result.fid,
                "_gis_layer_id": entry.get("id"),
            })
            self._adopt_visual(entry, drawing, subtype_code)
            self._remove_runtime_parts(drawing, keep_geometry=True)
            entry["feature_count"] = int(entry.get("feature_count") or 0) + 1
            entry["loaded_feature_count"] = entry["feature_count"]
            entry["rendered_geometry_feature_count"] = int(
                entry.get("rendered_geometry_feature_count") or 0
            ) + 1
            entry["placeholder"] = False
            if entry.get("geom") in (None, "", "unknown"):
                entry["geom"] = source_kind
                entry["geometry_type"] = source_kind.title()
            entry.pop("placeholder_reason", None)
            entry.pop("_cached_count", None)
            model = entry.get("native_model")
            if model is not None:
                model.feature_count = entry["feature_count"]
            try:
                if self.digitizer.undo_stack:
                    self.digitizer.undo_stack.pop()
            except Exception:
                pass
            panel = getattr(self.app, "_gis_layers_panel", None)
            if panel is not None and hasattr(panel, "update_feature_count"):
                panel.update_feature_count(entry.get("id"), subtype_code)
            elif panel is not None and hasattr(panel, "refresh"):
                panel.refresh(select_id=entry.get("id"))
            for dialog in list(getattr(self.app, "_attribute_tables", {}).values()):
                try:
                    if getattr(dialog, "entry", None) is entry:
                        dialog.load_data()
                except Exception:
                    pass
            fid_text = result.fid if result.fid is not None else ""
            self._message(f"Saved feature {fid_text} to {schema['layer_name']}")
        except Exception as exc:
            self._remove_runtime_parts(drawing, keep_geometry=False)
            self._message(f"Feature was not saved: {exc}", error=True)
        finally:
            self._busy = False
            try:
                self.app.vtk_widget.render()
            except Exception:
                pass


def install_gis_edit_bridge(app):
    bridge = getattr(app, "_gis_digitize_bridge", None)
    if bridge is None:
        bridge = GISDigitizeBridge(app)
        app._gis_digitize_bridge = bridge
    # Re-attempt every time: the digitizer may not have existed on the first
    # call, and subscribing twice is a no-op.
    bridge.install()
    return bridge


def set_active_edit_layer(app, entry: dict | None, subtype_code=None):
    candidate = entry if isinstance(entry, dict) and entry.get("kind") == "vector" else None
    if candidate is not None:
        try:
            schema = inspect_edit_target(candidate)
            if schema.get("kind") not in {"point", "line", "polygon"}:
                if not (schema.get("kind") == "unknown" and schema.get("driver", "").casefold() == "geojson"):
                    raise RuntimeError("The selected layer does not have an editable feature geometry type.")
            candidate["_edit_schema"] = schema
            model = candidate.get("native_model")
            if model is not None:
                model.read_only = False
            candidate.pop("edit_error", None)
        except Exception as exc:
            candidate["edit_error"] = str(exc)
            candidate = None
    app.active_gis_edit_layer = candidate
    app.active_gis_edit_subtype = subtype_code if candidate is not None else None
    if candidate is not None:
        install_gis_edit_bridge(app)
    return candidate