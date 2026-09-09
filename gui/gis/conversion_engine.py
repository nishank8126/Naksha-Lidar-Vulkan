"""Loss-minimizing GIS vector conversion for Naksha.

Unlike the legacy digitizer-export path, this module copies native OGR
geometries and fields directly.  It is therefore the correct backend for
SHP/GeoJSON/GPKG/FileGDB/KML/GPX -> FileGDB/GPKG conversions.
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

try:
    from osgeo import gdal, ogr, osr
except Exception:
    gdal = None
    ogr = None
    osr = None

from .capabilities import get_driver_capability


@dataclass
class SourceLayer:
    path: str
    layer_name: Optional[str] = None
    output_name: Optional[str] = None
    feature_dataset: Optional[str] = None


@dataclass
class LayerTransferResult:
    source_path: str
    source_layer: str
    output_layer: str
    success: bool
    feature_count: int = 0
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None


class ConversionCancelled(RuntimeError):
    """Raised when the user cancels an in-progress native GIS conversion."""


@dataclass
class ConversionReport:
    output_path: str
    output_driver: str
    success: bool = False
    layers: List[LayerTransferResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["layers"] = [asdict(x) for x in self.layers]
        return data


def _require_gdal():
    if gdal is None or ogr is None:
        raise RuntimeError("GDAL Python bindings (osgeo.gdal/osgeo.ogr) are required.")


def _driver_for_output(path: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return explicit
    low = str(path).lower()
    if low.endswith(".gdb"):
        return "OpenFileGDB"
    if low.endswith(".gpkg"):
        return "GPKG"
    if low.endswith(".shp"):
        return "ESRI Shapefile"
    if low.endswith(".geojson") or low.endswith(".json"):
        return "GeoJSON"
    if low.endswith(".kml") or low.endswith(".kmz"):
        return "LIBKML" if gdal.GetDriverByName("LIBKML") else "KML"
    raise ValueError(f"Cannot infer output driver from: {path}")


def _create_vector_dataset(path: str, driver_name: str, overwrite: bool = False):
    _require_gdal()
    drv = gdal.GetDriverByName(driver_name)
    if drv is None and driver_name == "OpenFileGDB":
        drv = gdal.GetDriverByName("FileGDB")
        driver_name = "FileGDB" if drv is not None else driver_name
    if drv is None:
        raise RuntimeError(f"GDAL driver '{driver_name}' is not installed.")

    if os.path.exists(path):
        if not overwrite:
            try:
                ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_UPDATE)
            except Exception:
                ds = None
            if ds is None:
                raise RuntimeError(f"Output exists but cannot be opened for update: {path}")
            return ds, driver_name, False
        try:
            rc = drv.Delete(path)
            if rc not in (None, 0):
                raise RuntimeError(f"GDAL could not delete existing output: {path}")
        except Exception:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=False)
            else:
                os.remove(path)

    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    ds = None
    if hasattr(drv, "CreateVector"):
        try:
            ds = drv.CreateVector(path)
        except Exception:
            ds = None
    if ds is None:
        try:
            ds = drv.Create(path, 0, 0, 0, gdal.GDT_Unknown)
        except Exception:
            ds = None
    if ds is None:
        raise RuntimeError(f"GDAL driver '{driver_name}' could not create: {path}")
    return ds, driver_name, True


def _open_vector_source(path: str):
    _require_gdal()
    drivers = None
    if str(path).lower().endswith(".gdb"):
        drivers = [x for x in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(x)]
    try:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY,
                         allowed_drivers=drivers or None)
    except TypeError:
        ds = gdal.OpenEx(path, gdal.OF_VECTOR | gdal.OF_READONLY)
    if ds is None:
        raise RuntimeError(f"Cannot open vector source: {path}")
    return ds


def _sanitize_layer_name(name: str, driver_name: str) -> str:
    raw = str(name or "layer").strip() or "layer"
    if driver_name in {"OpenFileGDB", "FileGDB"}:
        clean = re.sub(r"[^A-Za-z0-9_]", "_", raw)
        clean = re.sub(r"_+", "_", clean).strip("_") or "layer"
        if clean[0].isdigit():
            clean = "L_" + clean
        return clean[:160]
    if driver_name == "ESRI Shapefile":
        return raw[:255]
    return raw


def _unique_layer_name(ds, desired: str, driver_name: str) -> str:
    base = _sanitize_layer_name(desired, driver_name)
    existing = set()
    try:
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            if lyr is not None:
                existing.add(str(lyr.GetName()).casefold())
    except Exception:
        pass
    if base.casefold() not in existing:
        return base
    for i in range(2, 10000):
        suffix = f"_{i}"
        candidate = (base[: max(1, 160 - len(suffix))] + suffix) if driver_name in {"OpenFileGDB", "FileGDB"} else base + suffix
        if candidate.casefold() not in existing:
            return candidate
    return f"{base}_{uuid.uuid4().hex[:8]}"


def _traditional_order(srs):
    # GDAL 3+ defaults new SpatialReference objects to OAMS_AUTHORITY_COMPLIANT,
    # which is (lat, lon) for geographic CRS like EPSG:4326. Every other CRS-aware
    # path in this app (reader.py's import_gdb_layer, crs_manager.py's pyproj
    # transforms with always_xy=True) uses traditional (lon, lat) / (easting,
    # northing) GIS order. Without this, reprojecting to a geographic target here
    # silently swaps X/Y relative to the rest of the app (and to GeoJSON's RFC 7946
    # mandated [lon, lat] order).
    if srs is not None:
        try:
            srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
    return srs


def _srs_from_user(value):
    if value is None or osr is None:
        return None
    if hasattr(value, "ExportToWkt"):
        try:
            return _traditional_order(value.Clone())
        except Exception:
            return _traditional_order(value)
    srs = osr.SpatialReference()
    try:
        if isinstance(value, int) or (isinstance(value, str) and value.strip().isdigit()):
            if srs.ImportFromEPSG(int(value)) == 0:
                return _traditional_order(srs)
        text = str(value).strip()
        if text.upper().startswith("EPSG:"):
            if srs.ImportFromEPSG(int(text.split(":", 1)[1])) == 0:
                return _traditional_order(srs)
        if hasattr(srs, "SetFromUserInput") and srs.SetFromUserInput(text) == 0:
            return _traditional_order(srs)
        if srs.ImportFromWkt(text) == 0:
            return _traditional_order(srs)
    except Exception:
        pass
    raise ValueError(f"Invalid target CRS: {value}")


def _clone_field_defn(src_fd):
    try:
        return src_fd.Clone()
    except Exception:
        fd = ogr.FieldDefn(src_fd.GetNameRef(), src_fd.GetType())
        for setter, getter in (
            ("SetWidth", "GetWidth"),
            ("SetPrecision", "GetPrecision"),
            ("SetSubType", "GetSubType"),
            ("SetNullable", "IsNullable"),
            ("SetUnique", "IsUnique"),
            ("SetDefault", "GetDefault"),
            ("SetDomainName", "GetDomainName"),
            ("SetAlternativeName", "GetAlternativeNameRef"),
        ):
            try:
                getattr(fd, setter)(getattr(src_fd, getter)())
            except Exception:
                pass
        return fd


def _copy_field_domains(src_ds, dst_ds, report_warnings: List[str]):
    if not hasattr(src_ds, "GetFieldDomainNames") or not hasattr(dst_ds, "AddFieldDomain"):
        return
    try:
        names = list(src_ds.GetFieldDomainNames() or [])
    except Exception:
        return
    for name in names:
        try:
            if hasattr(dst_ds, "GetFieldDomain") and dst_ds.GetFieldDomain(name) is not None:
                continue
            dom = src_ds.GetFieldDomain(name)
            if dom is not None and not dst_ds.AddFieldDomain(dom):
                report_warnings.append(f"Field domain '{name}' could not be copied.")
        except Exception as exc:
            report_warnings.append(f"Field domain '{name}' copy warning: {exc}")


def _copy_relationships(src_ds, dst_ds, report_warnings: List[str], layer_name_map=None):
    """Copy relationships only when referenced destination names are unchanged."""
    if not hasattr(src_ds, "GetRelationshipNames") or not hasattr(dst_ds, "AddRelationship"):
        return
    layer_name_map = dict(layer_name_map or {})
    try:
        names = list(src_ds.GetRelationshipNames() or [])
    except Exception:
        return
    for name in names:
        try:
            rel = src_ds.GetRelationship(name)
            if rel is None:
                continue
            left = rel.GetLeftTableName() if hasattr(rel, "GetLeftTableName") else None
            right = rel.GetRightTableName() if hasattr(rel, "GetRightTableName") else None
            renamed = [(n, layer_name_map[n]) for n in (left, right)
                       if n and n in layer_name_map and layer_name_map[n] != n]
            if renamed:
                report_warnings.append(
                    f"Relationship '{name}' skipped because endpoint names changed: " +
                    ", ".join(f"{a} -> {b}" for a, b in renamed)
                )
                continue
            missing = [n for n in (left, right) if n and dst_ds.GetLayerByName(n) is None]
            if missing:
                report_warnings.append(
                    f"Relationship '{name}' skipped because destination table(s) are missing: " + ", ".join(missing)
                )
                continue
            if hasattr(dst_ds, "GetRelationship") and dst_ds.GetRelationship(name) is not None:
                continue
            if not dst_ds.AddRelationship(rel):
                report_warnings.append(f"Relationship '{name}' could not be copied.")
        except Exception as exc:
            report_warnings.append(f"Relationship '{name}' copy warning: {exc}")


def _layer_creation_options(driver_name: str, *, feature_dataset: Optional[str],
                            alias: Optional[str], target_arcgis_version: str,
                            create_shape_fields: bool) -> List[str]:
    opts: List[str] = []
    if driver_name in {"OpenFileGDB", "FileGDB"}:
        if feature_dataset:
            try:
                from gui.gis.gdb.writer import sanitize_name
                feature_dataset = sanitize_name(feature_dataset, 160)
            except Exception:
                feature_dataset = str(feature_dataset).strip()
            opts.append(f"FEATURE_DATASET={feature_dataset}")
        if alias:
            opts.append(f"LAYER_ALIAS={alias}")
        if target_arcgis_version:
            opts.append(f"TARGET_ARCGIS_VERSION={target_arcgis_version}")
        if create_shape_fields:
            opts.append("CREATE_SHAPE_AREA_AND_LENGTH_FIELDS=YES")
    elif driver_name == "GPKG":
        opts.append("SPATIAL_INDEX=YES")
        if alias:
            opts.append(f"IDENTIFIER={alias}")
    return opts


def _create_destination_layer(dst_ds, src_layer, output_name: str, driver_name: str,
                              *, feature_dataset: Optional[str], target_srs,
                              target_arcgis_version: str,
                              create_shape_fields: bool,
                              overwrite_layer: bool,
                              report_warnings: List[str]):
    source_defn = src_layer.GetLayerDefn()
    geom_type = source_defn.GetGeomType()
    src_srs = src_layer.GetSpatialRef()
    if src_srs is not None:
        src_srs = _traditional_order(src_srs.Clone())
    dst_srs = target_srs or (src_srs.Clone() if src_srs is not None else None)

    desired = _sanitize_layer_name(output_name, driver_name)
    existing = dst_ds.GetLayerByName(desired)
    if existing is not None:
        if not overwrite_layer:
            desired = _unique_layer_name(dst_ds, desired, driver_name)
        else:
            try:
                dst_ds.DeleteLayer(desired)
            except Exception:
                try:
                    for i in range(dst_ds.GetLayerCount()):
                        lyr = dst_ds.GetLayerByIndex(i)
                        if lyr and lyr.GetName() == desired:
                            dst_ds.DeleteLayer(i)
                            break
                except Exception as exc:
                    raise RuntimeError(f"Could not replace existing layer '{desired}': {exc}")

    alias = None
    try:
        alias = src_layer.GetMetadataItem("ALIAS_NAME") or None
    except Exception:
        alias = None
    opts = _layer_creation_options(
        driver_name,
        feature_dataset=feature_dataset,
        alias=alias,
        target_arcgis_version=target_arcgis_version,
        create_shape_fields=create_shape_fields,
    )
    dst_layer = dst_ds.CreateLayer(desired, srs=dst_srs, geom_type=geom_type, options=opts)
    if dst_layer is None:
        raise RuntimeError(f"Could not create destination layer '{desired}'.")

    # Field domains are already added to the dataset before CreateField so the
    # copied FieldDefn domain references have a valid destination target.
    # Track source-field index -> actual destination-field index because drivers
    # such as Shapefile may launder or truncate names.
    field_map: Dict[int, int] = {}
    for i in range(source_defn.GetFieldCount()):
        src_fd = source_defn.GetFieldDefn(i)
        before_count = dst_layer.GetLayerDefn().GetFieldCount()
        fd = _clone_field_defn(src_fd)
        rc = dst_layer.CreateField(fd)
        if rc != 0:
            fallback = ogr.FieldDefn(src_fd.GetNameRef(), src_fd.GetType())
            try:
                fallback.SetWidth(src_fd.GetWidth())
                fallback.SetPrecision(src_fd.GetPrecision())
            except Exception:
                pass
            rc = dst_layer.CreateField(fallback)
        if rc != 0:
            report_warnings.append(f"Field '{src_fd.GetNameRef()}' could not be created.")
            continue

        dst_defn_now = dst_layer.GetLayerDefn()
        dst_idx = dst_defn_now.GetFieldIndex(src_fd.GetNameRef())
        if dst_idx < 0 and dst_defn_now.GetFieldCount() > before_count:
            dst_idx = dst_defn_now.GetFieldCount() - 1
            try:
                actual_name = dst_defn_now.GetFieldDefn(dst_idx).GetNameRef()
                if actual_name != src_fd.GetNameRef():
                    report_warnings.append(
                        f"Field '{src_fd.GetNameRef()}' was written as '{actual_name}' by {driver_name}."
                    )
            except Exception:
                pass
        if dst_idx >= 0:
            field_map[i] = int(dst_idx)
    return dst_layer, desired, src_srs, dst_srs, field_map



def _copy_feature(src_feature, src_layer, dst_layer, field_map=None,
                  transformer=None, preserve_fid: bool = False):
    dst_defn = dst_layer.GetLayerDefn()
    out = ogr.Feature(dst_defn)
    src_defn = src_layer.GetLayerDefn()
    field_map = field_map or {}
    for i in range(src_defn.GetFieldCount()):
        dst_idx = field_map.get(i, -1)
        if dst_idx < 0:
            src_name = src_defn.GetFieldDefn(i).GetNameRef()
            dst_idx = dst_defn.GetFieldIndex(src_name)
        if dst_idx < 0:
            continue
        try:
            if src_feature.IsFieldSetAndNotNull(i):
                out.SetField(dst_idx, src_feature.GetField(i))
            elif hasattr(out, "SetFieldNull"):
                out.SetFieldNull(dst_idx)
        except Exception:
            try:
                out.SetField(dst_idx, src_feature.GetField(i))
            except Exception:
                pass

    geom = src_feature.GetGeometryRef()
    if geom is not None:
        g = geom.Clone()
        if transformer is not None:
            rc = g.Transform(transformer)
            if rc != 0:
                raise RuntimeError("Geometry reprojection failed.")
        out.SetGeometryDirectly(g)
    if preserve_fid:
        try:
            fid = src_feature.GetFID()
            if fid is not None and int(fid) >= 0:
                out.SetFID(int(fid))
        except Exception:
            pass
    rc = dst_layer.CreateFeature(out)
    out = None
    return rc == 0


def transfer_layer(src_ds, src_layer, dst_ds, output_driver: str, *,
                   output_name: Optional[str] = None,
                   feature_dataset: Optional[str] = None,
                   target_crs=None,
                   target_arcgis_version: str = "ALL",
                   create_shape_fields: bool = True,
                   overwrite_layer: bool = False,
                   preserve_fid: bool = False,
                   progress: Optional[Callable[[int, int, str], None]] = None,
                   cancel_check: Optional[Callable[[], bool]] = None) -> LayerTransferResult:
    src_name = str(src_layer.GetName())
    desired = output_name or src_name
    result = LayerTransferResult("", src_name, desired, False)
    warnings = result.warnings

    _copy_field_domains(src_ds, dst_ds, warnings)
    target_srs = _srs_from_user(target_crs) if target_crs is not None else None

    # CRS safety is a PRE-FLIGHT check.  Do it before creating any destination
    # layer so a bad/unknown source CRS cannot leave behind an empty layer that
    # is incorrectly labelled with the target CRS.
    try:
        preflight_src_srs = src_layer.GetSpatialRef()
        if preflight_src_srs is not None:
            preflight_src_srs = _traditional_order(preflight_src_srs.Clone())
    except Exception:
        preflight_src_srs = None

    transformer = None
    if target_srs is not None and preflight_src_srs is None:
        raise RuntimeError(
            f"Source layer '{src_name}' has no declared CRS. Naksha will not assign the target CRS "
            "without transforming coordinates. Define/repair the source CRS first, then convert."
        )
    if target_srs is not None and preflight_src_srs is not None:
        try:
            same = bool(preflight_src_srs.IsSame(target_srs))
        except Exception:
            same = False
        if not same:
            try:
                transformer = osr.CoordinateTransformation(preflight_src_srs, target_srs)
            except Exception as exc:
                raise RuntimeError(
                    f"Could not create coordinate transformation for layer '{src_name}': {exc}"
                ) from exc
            if transformer is None:
                raise RuntimeError(f"Could not create coordinate transformation for layer '{src_name}'.")

    dst_layer, actual_name, src_srs, dst_srs, field_map = _create_destination_layer(
        dst_ds, src_layer, desired, output_driver,
        feature_dataset=feature_dataset,
        target_srs=target_srs,
        target_arcgis_version=target_arcgis_version,
        create_shape_fields=create_shape_fields,
        overwrite_layer=overwrite_layer,
        report_warnings=warnings,
    )
    result.output_layer = actual_name

    try:
        total = int(src_layer.GetFeatureCount(0))
    except Exception:
        total = -1
    src_layer.ResetReading()
    copied = 0
    skipped = 0
    processed = 0
    while True:
        if cancel_check is not None and cancel_check():
            raise ConversionCancelled("Canceled by user.")
        feat = src_layer.GetNextFeature()
        if feat is None:
            break
        processed += 1
        try:
            if _copy_feature(
                feat, src_layer, dst_layer, field_map,
                transformer=transformer, preserve_fid=preserve_fid,
            ):
                copied += 1
            else:
                skipped += 1
        except Exception as exc:
            skipped += 1
            if len(warnings) < 20:
                warnings.append(f"Feature {feat.GetFID()} skipped: {exc}")
        finally:
            feat = None
        if progress is not None and processed % 250 == 0:
            progress(processed, total, actual_name)
            if cancel_check is not None and cancel_check():
                raise ConversionCancelled("Canceled by user.")

    try:
        dst_layer.SyncToDisk()
    except Exception:
        pass
    result.feature_count = copied
    result.skipped = skipped
    result.success = copied > 0 or total == 0
    if skipped:
        warnings.append(f"{skipped} feature(s) could not be written.")

    if result.success and feature_dataset and output_driver in {"OpenFileGDB", "FileGDB"}:
        # The first successful class materializes an OpenFileGDB Feature Dataset
        # that may previously have existed only as a session-pending descriptor.
        try:
            from gui.gis.gdb.pending import clear_pending
            clear_pending(dst_ds.GetDescription(), feature_dataset)
        except Exception:
            pass
    return result


def normalize_sources(sources: Sequence[Union[str, SourceLayer, dict]]) -> List[SourceLayer]:
    out: List[SourceLayer] = []
    for item in sources:
        if isinstance(item, SourceLayer):
            out.append(item)
        elif isinstance(item, str):
            out.append(SourceLayer(path=item))
        elif isinstance(item, dict):
            out.append(SourceLayer(
                path=str(item.get("path") or ""),
                layer_name=item.get("layer_name") or item.get("layer"),
                output_name=item.get("output_name") or item.get("name"),
                feature_dataset=item.get("feature_dataset"),
            ))
    return [x for x in out if x.path]


def convert_vector_sources(sources: Sequence[Union[str, SourceLayer, dict]], output_path: str,
                           *, output_driver: Optional[str] = None,
                           overwrite_dataset: bool = False,
                           overwrite_layers: bool = False,
                           feature_dataset: Optional[str] = None,
                           target_crs=None,
                           target_arcgis_version: str = "ALL",
                           create_shape_fields: bool = True,
                           preserve_fid: bool = False,
                           copy_relationships: bool = True,
                           progress: Optional[Callable[[int, int, str], None]] = None,
                           cancel_check: Optional[Callable[[], bool]] = None) -> ConversionReport:
    """Convert one or many vector layers into a FileGDB/GPKG/etc. target.

    A source path with no layer name means all public layers in a multi-layer
    datasource.  For single-layer formats (SHP/GeoJSON/KML) that naturally means
    the single layer.
    """
    _require_gdal()
    src_specs = normalize_sources(sources)
    driver_name = _driver_for_output(output_path, output_driver)
    report = ConversionReport(output_path=os.path.abspath(output_path), output_driver=driver_name)
    if not src_specs:
        report.error = "No input vector sources were provided."
        return report

    cap = get_driver_capability(driver_name)
    if not cap.installed:
        report.error = f"GDAL output driver '{driver_name}' is not installed."
        return report
    if not cap.create and not os.path.exists(output_path):
        report.error = f"Driver '{driver_name}' cannot create a new dataset in this GDAL build."
        return report

    dst_ds = None
    source_handles = []
    transaction_started = False
    try:
        dst_ds, actual_driver, _created = _create_vector_dataset(output_path, driver_name, overwrite_dataset)
        report.output_driver = actual_driver
        driver_name = actual_driver
        try:
            if hasattr(dst_ds, "StartTransaction"):
                rc = dst_ds.StartTransaction(force=True)
                transaction_started = (rc == 0)
        except Exception:
            transaction_started = False

        relationship_sources = []
        layer_name_maps: Dict[int, Dict[str, str]] = {}
        for spec in src_specs:
            src_ds = _open_vector_source(spec.path)
            source_handles.append(src_ds)
            relationship_sources.append(src_ds)
            layer_name_maps.setdefault(id(src_ds), {})
            selected = []
            if spec.layer_name:
                lyr = src_ds.GetLayerByName(spec.layer_name)
                if lyr is None:
                    report.layers.append(LayerTransferResult(spec.path, spec.layer_name, spec.output_name or spec.layer_name, False, error="Layer not found"))
                    continue
                selected = [lyr]
            else:
                selected = [src_ds.GetLayerByIndex(i) for i in range(src_ds.GetLayerCount())]
                selected = [x for x in selected if x is not None]

            for src_layer in selected:
                src_name = str(src_layer.GetName())
                output_name = spec.output_name if len(selected) == 1 and spec.output_name else src_name
                try:
                    layer_result = transfer_layer(
                        src_ds, src_layer, dst_ds, driver_name,
                        output_name=output_name,
                        feature_dataset=spec.feature_dataset or feature_dataset,
                        target_crs=target_crs,
                        target_arcgis_version=target_arcgis_version,
                        create_shape_fields=create_shape_fields,
                        overwrite_layer=overwrite_layers,
                        preserve_fid=preserve_fid,
                        progress=progress,
                        cancel_check=cancel_check,
                    )
                    layer_result.source_path = spec.path
                    report.layers.append(layer_result)
                    if layer_result.success:
                        layer_name_maps[id(src_ds)][src_name] = layer_result.output_layer
                except ConversionCancelled:
                    raise
                except Exception as exc:
                    report.layers.append(LayerTransferResult(
                        spec.path, src_name, output_name, False, error=str(exc)))

        if copy_relationships:
            for src_ds in relationship_sources:
                _copy_relationships(
                    src_ds, dst_ds, report.warnings,
                    layer_name_map=layer_name_maps.get(id(src_ds), {}),
                )

        failed = [x for x in report.layers if not x.success]
        succeeded = [x for x in report.layers if x.success]
        if failed and transaction_started and not succeeded:
            dst_ds.RollbackTransaction()
            transaction_started = False
        elif transaction_started:
            rc = dst_ds.CommitTransaction()
            transaction_started = False
            if rc != 0:
                raise RuntimeError("Destination transaction could not be committed.")

        try:
            dst_ds.FlushCache()
        except Exception:
            pass
        report.success = bool(succeeded) and not (failed and not succeeded)
        if failed:
            report.warnings.append(f"{len(failed)} layer(s) failed; {len(succeeded)} succeeded.")
    except ConversionCancelled as exc:
        report.error = str(exc) or "Canceled by user."
        report.warnings.append("Conversion canceled; active transaction rolled back where supported.")
        if dst_ds is not None and transaction_started:
            try:
                dst_ds.RollbackTransaction()
            except Exception:
                pass
            transaction_started = False
        report.success = False
    except Exception as exc:
        report.error = str(exc)
        if dst_ds is not None and transaction_started:
            try:
                dst_ds.RollbackTransaction()
            except Exception:
                pass
        report.success = False
    finally:
        source_handles.clear()
        dst_ds = None
    return report
