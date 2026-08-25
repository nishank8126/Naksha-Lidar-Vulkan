# ─────────────────────────────────────────────────────────────────────────────
# gui/gis/gdb/engine.py — QGIS-free GDB schema engine
#
# Adapted from the QGIS plugin gdb_engine.py. Original reads GDB_Items XML
# + GDAL's OGR layer introspection to expose:
#
#   - all_domains          : domain_name -> {code: label}            (CodedValue)
#   - all_domains_meta     : list of rich metadata dicts (CodedValue/Range)
#   - get_cascade_data()   : per-layer subtype + dependent field domains
#   - get_layer_crs_wkt()  : GDB-native CRS as WKT (handles "()" QGIS bug)
#   - get_spatial_domain() : valid OGR XY coordinate range per layer
#   - get_non_nullable_defaults() : safe placeholder values for writes
#   - get_field_types()    : esriFieldType* per field for type-aware defaults
#   - get_extent_xml()     : bounding box from the GDB XML extent
#
# All QGIS coupling is removed — callers pass paths/layer names directly.
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from osgeo import ogr, gdal

log = logging.getLogger("GDBEngine")


# Per-process cache: gdb_path -> GDBEngine
_ENGINE_CACHE: dict = {}
_ENGINE_MTIME: dict = {}   # gdb_path -> float mtime at parse time


# ─────────────────────────────────────────────────────────────────────────────
#  CRS helpers
# ─────────────────────────────────────────────────────────────────────────────

def crs_wkt_root(wkt: str) -> str:
    if not wkt:
        return ''
    match = re.match(r'\s*([A-Za-z0-9_]+)\[', wkt)
    return match.group(1).upper() if match else ''


def crs_wkt_quality(wkt: str) -> int:
    """Rank CRS WKTs by completeness / trustworthiness.

    Higher scores win. The goal is to avoid replacing a projected/compound CRS
    with a weaker geographic-only fragment from feature-class XML.
    """
    if not wkt:
        return -1_000

    text = wkt.strip()
    upper = text.upper()
    root = crs_wkt_root(text)
    score = 0

    if root in ('COMPD_CS', 'COMPOUNDCRS'):
        score += 90
    elif root in ('PROJCS', 'PROJCRS', 'PROJECTEDCRS'):
        score += 65
    elif root in ('GEOGCS', 'GEOGCRS', 'GEOGRAPHICCRS'):
        score += 35
    elif root in ('VERTCS', 'VERT_CS', 'VERTCRS', 'VERTICALCRS'):
        score += 20

    if any(token in upper for token in ('VERTCS[', 'VERT_CS[', 'VERTCRS[', 'VERTICALCRS[')):
        score += 10
    if 'AUTHORITY[' in upper or 'ID[' in upper:
        score += 3

    is_geo_root = root in ('GEOGCS', 'GEOGCRS', 'GEOGRAPHICCRS')
    has_vertical = any(token in upper for token in ('VERTCS[', 'VERT_CS[', 'VERTCRS[', 'VERTICALCRS['))
    has_compound = any(token in upper for token in ('COMPD_CS[', 'COMPOUNDCRS['))
    has_projected = any(token in upper for token in ('PROJCS[', 'PROJCRS[', 'PROJECTEDCRS['))

    # ArcGIS XML sometimes stores degraded fragments like
    # GEOGCS[..., ...],VERTCS[...] for layers whose real CRS is compound UTM.
    if is_geo_root and has_vertical and not has_compound:
        score -= 45
    if is_geo_root and has_projected:
        score -= 45

    return score


# ─────────────────────────────────────────────────────────────────────────────
#  Driver / file helpers
# ─────────────────────────────────────────────────────────────────────────────

def check_gdal_version() -> tuple:
    """Return (ok, version_string). ok=True for GDAL >= 3.6.

    GDAL 3.3 can read domains; 3.6 is the practical minimum for the current
    create/update workflow with OpenFileGDB. Newer features such as BigInteger
    metadata and 64-bit OBJECTID support still benefit from GDAL 3.9/3.10+.
    """
    ver = gdal.VersionInfo("RELEASE_NAME")
    try:
        parts = ver.split('.')
        major, minor = int(parts[0]), int(parts[1])
        return (major > 3 or (major == 3 and minor >= 6)), ver
    except (ValueError, IndexError):
        return False, ver


def normalize_gdb_path(path: str) -> str | None:
    if not path:
        return None
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def has_filegdb_driver() -> bool:
    """True if the ESRI FileGDB SDK is linked (full read+write)."""
    return gdal.GetDriverByName('FileGDB') is not None


def has_openfilegdb_driver() -> bool:
    """True if the open-source OpenFileGDB driver is available (read; write varies)."""
    return gdal.GetDriverByName('OpenFileGDB') is not None


def open_gdb(path: str, update: bool = False):
    """Open a FileGDB using explicit vector flags and driver fallback."""
    flags = gdal.OF_VECTOR | (gdal.OF_UPDATE if update else gdal.OF_READONLY)
    drivers = [d for d in ("OpenFileGDB", "FileGDB") if gdal.GetDriverByName(d)]
    try:
        ds = gdal.OpenEx(path, flags, allowed_drivers=drivers or None)
    except TypeError:
        ds = gdal.OpenEx(path, flags)
    if ds is not None:
        return ds
    return ogr.Open(path, 1 if update else 0)


def list_layer_names(path: str) -> list[str]:
    """Return a list of feature class / table names inside the GDB."""
    ds = open_gdb(path)
    if not ds:
        return []
    try:
        names = []
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            name = lyr.GetName()
            if name and name not in names:
                names.append(name)
        return names
    finally:
        ds = None
        import gc
        gc.collect()


# ─────────────────────────────────────────────────────────────────────────────
#  Project-level domain override persistence (no QGIS, just JSON file)
# ─────────────────────────────────────────────────────────────────────────────

def _project_override_path(gdb_path: str, base_dir: str | None = None) -> str:
    """Where to store per-GDB domain overrides (next to the GDB by default)."""
    base = base_dir or os.path.dirname(gdb_path) or os.getcwd()
    norm = normalize_gdb_path(gdb_path) or ''
    digest = hashlib.sha1(norm.encode('utf-8')).hexdigest()
    return os.path.join(base, f".gdb_engine_overrides_{digest}.json")


def load_project_domain_overrides(gdb_path: str, base_dir: str | None = None):
    fp = _project_override_path(gdb_path, base_dir)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else None
    except (OSError, ValueError) as exc:
        log.warning("Invalid stored domain overrides for %s: %s", gdb_path, exc)
        return None


def save_project_domain_overrides(gdb_path: str, domains_meta, base_dir: str | None = None) -> bool:
    fp = _project_override_path(gdb_path, base_dir)
    try:
        payload = json.dumps(domains_meta, ensure_ascii=True, sort_keys=True)
        with open(fp, 'w', encoding='utf-8') as fh:
            fh.write(payload)
        return True
    except OSError as exc:
        log.warning("Could not write project domain overrides for %s: %s", gdb_path, exc)
        return False


def clear_project_domain_overrides(gdb_path: str, base_dir: str | None = None) -> bool:
    fp = _project_override_path(gdb_path, base_dir)
    if not os.path.exists(fp):
        return True
    try:
        os.remove(fp)
        return True
    except OSError as exc:
        log.warning("Could not remove project domain overrides for %s: %s", gdb_path, exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Engine cache
# ─────────────────────────────────────────────────────────────────────────────

def clear_engine_cache(gdb_path: str | None = None):
    if gdb_path is None:
        _ENGINE_CACHE.clear()
        _ENGINE_MTIME.clear()
        return
    key = normalize_gdb_path(gdb_path)
    _ENGINE_CACHE.pop(key, None)
    _ENGINE_MTIME.pop(key, None)


def register_engine(engine):
    key = normalize_gdb_path(engine.gdb_path)
    if key:
        _ENGINE_CACHE[key] = engine
    return engine


def get_engine(gdb_path: str, refresh: bool = False) -> "GDBEngine | None":
    key = normalize_gdb_path(gdb_path)
    if not key:
        return None

    # Auto-refresh when the GDB folder has been modified externally.
    if not refresh and key in _ENGINE_CACHE:
        try:
            current_mtime = os.path.getmtime(key)
            if _ENGINE_MTIME.get(key) != current_mtime:
                log.info(
                    "GDB '%s' changed on disk (mtime %s → %s) — refreshing engine.",
                    key, _ENGINE_MTIME.get(key), current_mtime)
                refresh = True
        except OSError:
            pass

    if refresh or key not in _ENGINE_CACHE:
        engine = GDBEngine(key)
        overrides = load_project_domain_overrides(key)
        if overrides is not None:
            engine.set_domains_meta(overrides)
        _ENGINE_CACHE[key] = engine
        try:
            _ENGINE_MTIME[key] = os.path.getmtime(key)
        except OSError:
            pass

    return _ENGINE_CACHE[key]


# ─────────────────────────────────────────────────────────────────────────────
#  GDBEngine — the schema reader
# ─────────────────────────────────────────────────────────────────────────────

class GDBEngine:
    """Parses an ESRI File Geodatabase and extracts schema metadata.

    Stores:
      - all_domains          : domain_name -> {code: label}
      - all_domains_meta     : list of rich metadata dicts
      - _field_domains       : layer_lower -> {field_name: domain_name}
      - _cascade_layers      : layer_lower -> cascade info dict
      - _layer_crs_wkt       : layer_key -> WKT CRS string
      - _spatial_domains     : layer_key -> {xmin, ymin, xmax, ymax}
      - _layer_extents       : layer_key -> {xmin, ymin, xmax, ymax, wkt}
      - _field_types         : layer_key -> {field_lower: esriFieldType*}
      - _non_nullable        : layer_key -> set of field names
    """

    def __init__(self, gdb_path: str):
        self.gdb_path = normalize_gdb_path(gdb_path) or gdb_path
        self.all_domains: dict = {}
        self.all_domains_meta: list = []
        self._domains_meta_by_name: dict = {}
        self._field_domains: dict = {}
        self._all_layer_fields: dict = {}
        self._non_nullable: dict = {}
        self._field_types: dict = {}
        self._field_defaults: dict = {}
        self._subtype_defaults: dict = {}
        self._layer_feature_types: dict = {}
        self._layer_has_z: dict = {}
        self._layer_has_m: dict = {}
        self._cascade_layers: dict = {}
        self._layer_display_names: dict = {}
        self._layer_groups: dict = {}
        self._layer_catalog_paths: dict = {}
        self._internal_layers: set = set()
        self._spatial_domains: dict = {}
        self._dataset_crs_wkt: dict = {}
        self._layer_crs_wkt: dict = {}
        self._layer_extents: dict = {}
        self._load()

    # ── Loading ────────────────────────────────────────────────────────────

    def _load(self):
        _t0 = time.perf_counter()
        try:
            ds = ogr.Open(self.gdb_path, 0)
        except Exception as e:
            log.warning("OGR failed to open GDB %s: %s", self.gdb_path, e)
            ds = None

        if not ds:
            log.warning("Could not open GDB: %s", self.gdb_path)
            return

        try:
            _t1 = time.perf_counter()
            self._read_basic_layer_info(ds)
            log.warning("GDBEngine._load: _read_basic_layer_info %.3fs", time.perf_counter() - _t1)
            _t1 = time.perf_counter()
            self._read_gdb_items_xml(ds)
            log.warning("GDBEngine._load: _read_gdb_items_xml %.3fs", time.perf_counter() - _t1)
        except Exception as e:
            log.error("Error loading GDB %s: %s", self.gdb_path, e)
        finally:
            ds = None
            import gc
            gc.collect()
        log.warning("GDBEngine._load: TOTAL %.3fs for %s", time.perf_counter() - _t0, self.gdb_path)

    def _read_basic_layer_info(self, ds):
        """Populate layer skeletons + grab CRS WKT from GDAL (most reliable)."""
        self._all_layer_fields = {}
        self._field_domains = {}
        for i in range(ds.GetLayerCount()):
            lyr = ds.GetLayerByIndex(i)
            name = lyr.GetName()
            key = self._layer_key(name)
            self._layer_display_names.setdefault(key, name)
            self._field_domains[key] = {}
            self._all_layer_fields[key] = []
            self._field_types.setdefault(key, {})
            self._field_defaults.setdefault(key, {})
            defn = lyr.GetLayerDefn()
            for j in range(defn.GetFieldCount()):
                fdefn = defn.GetFieldDefn(j)
                fname = fdefn.GetName()
                self._all_layer_fields[key].append(fname.lower())
                dname = fdefn.GetDomainName()
                if dname:
                    self._field_domains[key][fname] = dname
                try:
                    self._field_types[key][fname.lower()] = fdefn.GetTypeName()
                except Exception:
                    pass

            # CRS from GDAL (preferred — GDAL reads the FileGDB SRS correctly)
            srs = lyr.GetSpatialRef()
            if srs is not None:
                try:
                    wkt = srs.ExportToWkt()
                    if wkt:
                        self._layer_crs_wkt[key] = wkt
                        canonical = self._layer_key(name)
                        if canonical and canonical != key:
                            self._layer_crs_wkt[canonical] = wkt
                        log.debug(
                            "CRS WKT stored (GDAL) for '%s': %.60s…", name, wkt)
                except Exception as e:
                    log.debug("CRS export failed for '%s': %s", name, e)

    # ── GDB_Items XML parsing ─────────────────────────────────────────────

    def _read_gdb_items_xml(self, ds):
        try:
            sql = (
                "SELECT Name, Definition FROM GDB_Items "
                "WHERE Definition LIKE '%CodedValueDomain%' "
                "OR Definition LIKE '%RangeDomain%' "
                "OR Definition LIKE '%FeatureClass%' "
                "OR Definition LIKE '%FeatureDataset%' "
                "OR Definition LIKE '%Table%'"
            )
            result = ds.ExecuteSQL(sql)
            if not result:
                return
        except Exception as e:
            log.warning("Cannot query GDB_Items in %s: %s", self.gdb_path, e)
            return

        dataset_xmls = []
        layer_xmls = []
        feat = result.GetNextFeature()
        while feat is not None:
            lname_raw = feat.GetField("Name")
            definition = feat.GetField("Definition")
            if not definition:
                feat = result.GetNextFeature()
                continue
            try:
                root = ET.fromstring(definition)
                if "CodedValueDomain" in root.tag or "RangeDomain" in root.tag:
                    self._parse_domain(root)
                else:
                    if lname_raw:
                        dataset_type = (root.findtext("./DatasetType") or '').strip()
                        if dataset_type == 'esriDTFeatureDataset':
                            dataset_xmls.append((lname_raw, root))
                        else:
                            layer_xmls.append((lname_raw, root))
            except ET.ParseError as e:
                log.warning("Malformed XML for '%s': %s", lname_raw, e)
            except Exception as e:
                log.warning("Error processing GDB_Items row '%s': %s", lname_raw, e)
            feat = result.GetNextFeature()

        for lname_raw, root in dataset_xmls:
            self._parse_feature_dataset(lname_raw, root)

        for lname_raw, root in layer_xmls:
            self._parse_feature_class(lname_raw, root)

    def _parse_domain(self, root):
        dname_node = root.find(".//DomainName")
        if dname_node is None or not dname_node.text:
            return
        dname = dname_node.text.strip()

        desc_node = root.find(".//Description")
        ftype_node = root.find(".//FieldType")
        split_node = root.find(".//SplitPolicy")
        merge_node = root.find(".//MergePolicy")

        ftype = (ftype_node.text.replace('esriFieldType', '')
                 if ftype_node is not None and ftype_node.text else 'String')
        split = (split_node.text.replace('esriSPT', '')
                 if split_node is not None and split_node.text else 'DefaultValue')
        merge = (merge_node.text.replace('esriMPT', '')
                 if merge_node is not None and merge_node.text else 'DefaultValue')

        if "RangeDomain" in root.tag:
            min_node = root.find(".//MinValue")
            max_node = root.find(".//MaxValue")
            self._upsert_domain_meta({
                'name':         dname,
                'description':  desc_node.text if (desc_node is not None and desc_node.text) else dname,
                'field_type':   ftype,
                'domain_type':  'Range',
                'split':        split,
                'merge':        merge,
                'min_value':    min_node.text if (min_node is not None and min_node.text) else None,
                'max_value':    max_node.text if (max_node is not None and max_node.text) else None,
                'coded_values': [],
            })
        else:
            enum = {}
            for cv in root.findall(".//CodedValue"):
                c_name = cv.find("Name")
                c_code = cv.find("Code")
                if c_name is not None and c_code is not None:
                    enum[c_code.text] = c_name.text
            self._upsert_domain_meta({
                'name':         dname,
                'description':  desc_node.text if (desc_node is not None and desc_node.text) else dname,
                'field_type':   ftype,
                'domain_type':  'CodedValue',
                'split':        split,
                'merge':        merge,
                'coded_values': [{'code': k, 'description': v}
                                 for k, v in sorted(enum.items())],
            })

    def _parse_feature_dataset(self, lname_raw, root):
        sr_node = self._find_spatial_reference_node(root)
        wkt = self._extract_wkt_from_sr_node(sr_node)
        if not wkt:
            return

        dataset_name = (root.findtext("./Name") or lname_raw or '').strip()
        key = self._layer_key(dataset_name)
        if key:
            self._dataset_crs_wkt[key] = wkt
            log.debug(
                "Feature-dataset CRS stored for '%s': root=%s score=%s",
                dataset_name, crs_wkt_root(wkt), crs_wkt_quality(wkt),
            )

    def _parse_feature_class(self, lname_raw, root):
        lname_lower = self._resolve_layer_storage_key(lname_raw, root)
        normalized_catalog_path = self._normalize_catalog_path(root)
        group_name = self._catalog_group_name(normalized_catalog_path)
        canonical = self._layer_key(lname_raw)

        if normalized_catalog_path:
            self._layer_catalog_paths[lname_lower] = normalized_catalog_path
            if canonical and canonical != lname_lower:
                self._layer_catalog_paths[canonical] = normalized_catalog_path
        if group_name:
            self._layer_groups[lname_lower] = group_name
            if canonical and canonical != lname_lower:
                self._layer_groups[canonical] = group_name
        if self._looks_internal_layer(lname_raw, normalized_catalog_path):
            self._internal_layers.add(lname_lower)
            if canonical:
                self._internal_layers.add(canonical)

        if lname_lower not in self._field_domains:
            self._field_domains[lname_lower] = {}
        if lname_lower not in self._all_layer_fields:
            self._all_layer_fields[lname_lower] = []
        if lname_lower not in self._field_types:
            self._field_types[lname_lower] = {}
        if lname_lower not in self._field_defaults:
            self._field_defaults[lname_lower] = {}

        feature_type = (root.findtext("./FeatureType") or root.findtext(".//FeatureType") or "").strip()
        if feature_type:
            self._layer_feature_types[lname_lower] = feature_type
            if canonical and canonical != lname_lower:
                self._layer_feature_types[canonical] = feature_type
        for xml_name, target in (("HasZ", self._layer_has_z), ("HasM", self._layer_has_m)):
            val = (root.findtext(f"./{xml_name}") or root.findtext(f".//{xml_name}") or "").strip().lower()
            if val in ("true", "1", "yes"):
                target[lname_lower] = True
                if canonical and canonical != lname_lower:
                    target[canonical] = True
            elif val in ("false", "0", "no"):
                target[lname_lower] = False
                if canonical and canonical != lname_lower:
                    target[canonical] = False

        for field in root.findall(".//GPFieldInfoEx"):
            fname_node = field.find("Name")
            fdomain_node = field.find("DomainName")
            if fname_node is None or not fname_node.text:
                continue
            fname = fname_node.text
            if fname.lower() not in self._all_layer_fields[lname_lower]:
                self._all_layer_fields[lname_lower].append(fname.lower())
            if fdomain_node is not None and fdomain_node.text:
                self._field_domains[lname_lower][fname] = fdomain_node.text.strip()

            nullable_node = field.find("IsNullable")
            if nullable_node is not None and nullable_node.text:
                if nullable_node.text.strip().lower() == 'false':
                    if lname_lower not in self._non_nullable:
                        self._non_nullable[lname_lower] = set()
                    self._non_nullable[lname_lower].add(fname.lower())

            ftype_node = field.find("FieldType")
            if ftype_node is not None and ftype_node.text:
                self._field_types[lname_lower][fname.lower()] = ftype_node.text.strip()

            default_node = field.find("DefaultValue")
            if default_node is not None and default_node.text is not None:
                self._field_defaults[lname_lower][fname.lower()] = default_node.text.strip()

        sr_node = self._find_spatial_reference_node(root)
        selected_wkt, selected_source = self._select_best_layer_crs_wkt(
            lname_raw, lname_lower, root, sr_node)
        if selected_wkt and sr_node is None:
            self._store_layer_crs_wkt(lname_lower, lname_raw, selected_wkt)

        if sr_node is not None:
            wkt_node = sr_node.find("WKT")
            if wkt_node is not None and wkt_node.text:
                wkt = selected_wkt or wkt_node.text.strip()
                for key in (lname_lower, self._layer_key(lname_raw)):
                    if key:
                        existing = self._layer_crs_wkt.get(key)
                        self._layer_crs_wkt[key] = wkt
                        if existing and existing != wkt:
                            log.debug(
                                "CRS WKT updated for '%s' key='%s' via %s",
                                lname_raw, key, selected_source or 'XML',
                            )
                        else:
                            log.debug(
                                "CRS WKT stored (%s) for '%s' key='%s': %.60s…",
                                selected_source or 'XML', lname_raw, key, wkt,
                            )

            # B) XY fixed-point domain → valid coordinate range for OGR
            try:
                xo = sr_node.find("XOrigin")
                yo = sr_node.find("YOrigin")
                xys = sr_node.find("XYScale")
                if (xo is not None and xo.text and
                        yo is not None and yo.text and
                        xys is not None and xys.text):
                    x_origin = float(xo.text)
                    y_origin = float(yo.text)
                    xy_scale = float(xys.text)
                    if xy_scale <= 0:
                        raise ValueError("XYScale must be > 0")
                    max_enc = (1 << 53) - 1
                    x_max = x_origin + max_enc / xy_scale
                    y_max = y_origin + max_enc / xy_scale
                    entry = {
                        'xmin': x_origin, 'ymin': y_origin,
                        'xmax': x_max,   'ymax': y_max,
                    }
                    extent = None
                    try:
                        xmin_node = root.find("./Extent/XMin")
                        ymin_node = root.find("./Extent/YMin")
                        xmax_node = root.find("./Extent/XMax")
                        ymax_node = root.find("./Extent/YMax")
                        if all(node is not None and node.text for node in (
                                xmin_node, ymin_node, xmax_node, ymax_node)):
                            extent = {
                                'xmin': float(xmin_node.text),
                                'ymin': float(ymin_node.text),
                                'xmax': float(xmax_node.text),
                                'ymax': float(ymax_node.text),
                            }
                            if not all(math.isfinite(value) for value in extent.values()):
                                extent = None
                    except ValueError:
                        extent = None

                    if extent is not None:
                        contains_extent = (
                            entry['xmin'] <= extent['xmin'] <= entry['xmax'] and
                            entry['xmin'] <= extent['xmax'] <= entry['xmax'] and
                            entry['ymin'] <= extent['ymin'] <= entry['ymax'] and
                            entry['ymin'] <= extent['ymax'] <= entry['ymax']
                        )
                        if not contains_extent:
                            log.warning(
                                "Ignoring spatial domain for '%s' because it does not "
                                "contain the dataset extent: domain=%s extent=%s",
                                lname_raw, entry, extent,
                            )
                            entry = None

                    if entry is not None:
                        self._spatial_domains[lname_lower] = entry
                        canonical = self._layer_key(lname_raw)
                        if canonical and canonical != lname_lower:
                            self._spatial_domains[canonical] = entry
                        log.debug(
                            "Spatial domain stored for '%s': "
                            "X %.4f–%.4f  Y %.4f–%.4f",
                            lname_raw, x_origin, x_max, y_origin, y_max,
                        )
            except (ValueError, ZeroDivisionError) as e:
                log.debug("XY domain parse failed for '%s': %s", lname_raw, e)

        # C) XML extent metadata
        try:
            xmin_node = root.find("./Extent/XMin")
            ymin_node = root.find("./Extent/YMin")
            xmax_node = root.find("./Extent/XMax")
            ymax_node = root.find("./Extent/YMax")
            extent_sr_node = root.find("./Extent/SpatialReference")
            extent_wkt = ''
            if extent_sr_node is not None:
                extent_wkt = (extent_sr_node.findtext("WKT") or '').strip()
            if all(node is not None and node.text for node in (
                    xmin_node, ymin_node, xmax_node, ymax_node)):
                extent = {
                    'xmin': float(xmin_node.text),
                    'ymin': float(ymin_node.text),
                    'xmax': float(xmax_node.text),
                    'ymax': float(ymax_node.text),
                    'wkt': extent_wkt,
                }
                if all(math.isfinite(extent[k]) for k in ('xmin', 'ymin', 'xmax', 'ymax')):
                    self._layer_extents[lname_lower] = extent
                    canonical = self._layer_key(lname_raw)
                    if canonical and canonical != lname_lower:
                        self._layer_extents[canonical] = dict(extent)
        except ValueError:
            pass

        # ── Subtypes ──────────────────────────────────────────────────────
        subtype_field_node = root.find(".//SubtypeFieldName")
        default_subtype_node = root.find(".//DefaultSubtypeCode")
        subtypes = root.findall(".//Subtype")
        if not subtypes or subtype_field_node is None or not subtype_field_node.text:
            return

        subtype_field = subtype_field_node.text.strip()
        default_subtype_code = None
        if default_subtype_node is not None and default_subtype_node.text:
            default_subtype_code = default_subtype_node.text.strip()
        subtype_domain = {}
        dependent_field_domains = {}
        subtype_default_values = {}

        for st in subtypes:
            st_name = st.find("SubtypeName")
            st_code = st.find("SubtypeCode")
            if not (st_name is not None and st_code is not None
                    and st_name.text and st_code.text):
                continue
            code = st_code.text
            subtype_domain[code] = st_name.text

            for fi in st.findall(".//SubtypeFieldInfo"):
                fi_fname = fi.find("FieldName")
                if fi_fname is None or not fi_fname.text:
                    continue
                dep_field = fi_fname.text.strip()

                fi_default = fi.find("DefaultValue")
                if fi_default is not None and fi_default.text is not None:
                    subtype_default_values.setdefault(code, {})[dep_field.lower()] = fi_default.text.strip()

                fi_domain = fi.find("DomainName")
                if fi_domain is None or not fi_domain.text:
                    continue
                dep_domain_name = fi_domain.text.strip()
                if dep_field.lower() == subtype_field.lower():
                    continue
                if dep_field not in dependent_field_domains:
                    dependent_field_domains[dep_field] = {}
                dependent_field_domains[dep_field][code] = dep_domain_name

        if subtype_domain:
            self._cascade_layers[lname_lower] = {
                'subtype_field':       subtype_field,
                'default_subtype_code': default_subtype_code,
                'subtype_domain':      subtype_domain,
                'dependent_field_domains': dependent_field_domains,
                'subtype_default_values': subtype_default_values,
            }
            self._subtype_defaults[lname_lower] = subtype_default_values

    # ── Public API ────────────────────────────────────────────────────────

    def is_cascaded_layer(self, lname: str) -> bool:
        return self._resolve_key(lname, self._cascade_layers) is not None

    def get_cascade_data(self, lname: str):
        key = self._resolve_key(lname, self._cascade_layers)
        if not key:
            return None
        raw = self._cascade_layers[key]
        dependent_fields = {}
        for field_name, subtype_map in raw.get('dependent_field_domains', {}).items():
            dependent_fields[field_name] = {
                code: dict(self.all_domains.get(domain_name, {}))
                for code, domain_name in subtype_map.items()
            }
        return {
            'subtype_field': raw['subtype_field'],
            'default_subtype_code': raw.get('default_subtype_code'),
            'subtype_domain': dict(raw['subtype_domain']),
            'dependent_fields': dependent_fields,
            'dependent_field_domains': copy.deepcopy(raw.get('dependent_field_domains', {})),
            'subtype_default_values': copy.deepcopy(raw.get('subtype_default_values', {})),
        }

    def get_layer_display_name(self, lname: str) -> str:
        key = self._resolve_key(lname, self._layer_display_names)
        if not key:
            key = self._resolve_key(lname, self._field_domains)
        if not key:
            key = self._layer_key(lname)
        return self._layer_display_names.get(key, lname)

    def get_layer_group(self, lname: str) -> str | None:
        key = self._resolve_key(lname, self._layer_groups)
        if not key:
            return None
        group = self._layer_groups.get(key)
        return group or None

    def get_layer_catalog_path(self, lname: str) -> str | None:
        key = self._resolve_key(lname, self._layer_catalog_paths)
        if not key:
            return None
        value = self._layer_catalog_paths.get(key)
        return value or None

    def is_internal_layer(self, lname: str) -> bool:
        key = self._resolve_key(lname, {k: True for k in self._internal_layers})
        return key in self._internal_layers if key else False

    def get_layer_crs_wkt(self, lname: str) -> str | None:
        """Return WKT CRS string for the layer, or None.

        This is the GDB's NATIVE coordinate system — coordinates must be
        in this CRS when writing back. Reproject geometry before writing
        if your project/canvas CRS differs.
        """
        if not lname:
            return None
        key = self._resolve_key(lname, self._layer_crs_wkt)
        if key:
            return self._layer_crs_wkt.get(key)
        for variant in self._name_variants(lname):
            if variant in self._layer_crs_wkt:
                return self._layer_crs_wkt[variant]
        log.debug(
            "get_layer_crs_wkt: no WKT for '%s'. Available: %s",
            lname, list(self._layer_crs_wkt.keys())[:10],
        )
        return None

    def get_domain_for_field(self, lname: str, ogr_name: str, fname: str) -> str | None:
        fname_lower = fname.lower()
        for candidate in (ogr_name, lname):
            if not candidate:
                continue
            key = self._resolve_key(candidate, self._field_domains)
            if not key:
                continue
            fd = self._field_domains[key]
            if fname in fd:
                return fd[fname]
            for k, v in fd.items():
                if k.lower() == fname_lower:
                    return v
        return None

    def get_spatial_domain(self, lname: str):
        """Return {xmin, ymin, xmax, ymax} valid OGR coordinate range, or None."""
        if not lname:
            return None
        key = self._resolve_key(lname, self._spatial_domains)
        if key:
            return self._spatial_domains.get(key)
        for variant in self._name_variants(lname):
            if variant in self._spatial_domains:
                return self._spatial_domains[variant]
        log.debug(
            "get_spatial_domain: no entry for '%s'. Keys: %s",
            lname, list(self._spatial_domains.keys())[:10],
        )
        return None

    def get_layer_extent(self, lname: str):
        """Return XML-derived extent metadata for a layer, or None."""
        if not lname:
            return None
        key = self._resolve_key(lname, self._layer_extents)
        if key:
            data = self._layer_extents.get(key)
            return dict(data) if data else None
        for variant in self._name_variants(lname):
            if variant in self._layer_extents:
                data = self._layer_extents[variant]
                return dict(data) if data else None
        return None

    def get_non_nullable_fields(self, lname: str) -> set:
        key = self._resolve_key(lname, self._non_nullable)
        if not key:
            return set()
        return set(self._non_nullable.get(key, ()))

    def get_non_nullable_defaults(self, lname: str) -> dict:
        """Return Python-formatted default values (strings) for non-nullable
        fields — safe to use in a `WHERE` clause or quoted SET expression.

        Returns:  {field_name_lower: 'value-as-string'}
        """
        _INT_TYPES = {'esrifieldtypeoid', 'esrifieldtypeinteger', 'esrifieldtypesmallinteger',
                      'esrifieldtypebiginteger'}
        _FLOAT_TYPES = {'esrifieldtypesingle', 'esrifieldtypedouble'}
        _TEXT_TYPES = {'esrifieldtypestring', 'esrifieldtypeglobalid',
                       'esrifieldtypeguid', 'esrifieldtypexml'}
        _SKIP_TYPES = {'esrifieldtypegeometry', 'esrifieldtypeblob',
                       'esrifieldtyperaster', 'esrifieldtypeoid'}

        non_null = self.get_non_nullable_fields(lname)
        if not non_null:
            return {}

        type_key = self._resolve_key(lname, self._field_types)
        ftypes = self._field_types.get(type_key, {}) if type_key else {}

        defaults_key = self._resolve_key(lname, self._field_defaults)
        schema_defaults = self._field_defaults.get(defaults_key, {}) if defaults_key else {}

        result = {}
        for fname_lower in non_null:
            if ('shape' in fname_lower or fname_lower.startswith('st_') or
                    fname_lower in ('objectid', 'globalid', 'fid', 'oid')):
                continue
            esri_type = ftypes.get(fname_lower, '').lower()
            if fname_lower in schema_defaults:
                raw_default = str(schema_defaults[fname_lower])
                if esri_type in _INT_TYPES:
                    try:
                        result[fname_lower] = str(int(float(raw_default)))
                    except (TypeError, ValueError):
                        result[fname_lower] = '0'
                elif esri_type in _FLOAT_TYPES:
                    try:
                        result[fname_lower] = str(float(raw_default))
                    except (TypeError, ValueError):
                        result[fname_lower] = '0'
                else:
                    escaped = raw_default.replace("'", "''")
                    result[fname_lower] = f"'{escaped}'"
                continue

            domain_name = self.get_domain_for_field(lname, lname, fname_lower)
            domain_values = self.all_domains.get(domain_name, {}) if domain_name else {}
            first_code = next(iter(domain_values.keys()), None)
            if esri_type in _SKIP_TYPES:
                continue
            if first_code is not None:
                if esri_type in _INT_TYPES:
                    try:
                        result[fname_lower] = str(int(float(str(first_code))))
                    except (TypeError, ValueError):
                        result[fname_lower] = '0'
                elif esri_type in _FLOAT_TYPES:
                    try:
                        result[fname_lower] = str(float(str(first_code)))
                    except (TypeError, ValueError):
                        result[fname_lower] = '0'
                else:
                    escaped = str(first_code).replace("'", "''")
                    result[fname_lower] = f"'{escaped}'"
            elif esri_type in _INT_TYPES:
                result[fname_lower] = '0'
            elif esri_type in _FLOAT_TYPES:
                result[fname_lower] = '0'
            elif esri_type in _TEXT_TYPES:
                result[fname_lower] = "' '"
            elif 'date' in esri_type:
                continue
            else:
                result[fname_lower] = '0'
        return result

    def get_non_nullable_default_values(self, lname: str) -> dict:
        """Same as `get_non_nullable_defaults` but returns Python values
        (int / float / str) suitable for `feature.SetField()` directly.
        """
        _INT_TYPES = {'esrifieldtypeoid', 'esrifieldtypeinteger', 'esrifieldtypesmallinteger',
                      'esrifieldtypebiginteger'}
        _FLOAT_TYPES = {'esrifieldtypesingle', 'esrifieldtypedouble'}
        _TEXT_TYPES = {'esrifieldtypestring', 'esrifieldtypeglobalid',
                       'esrifieldtypeguid', 'esrifieldtypexml'}
        _SKIP_TYPES = {'esrifieldtypegeometry', 'esrifieldtypeblob',
                       'esrifieldtyperaster', 'esrifieldtypeoid'}

        non_null = self.get_non_nullable_fields(lname)
        if not non_null:
            return {}

        type_key = self._resolve_key(lname, self._field_types)
        ftypes = self._field_types.get(type_key, {}) if type_key else {}

        defaults_key = self._resolve_key(lname, self._field_defaults)
        schema_defaults = self._field_defaults.get(defaults_key, {}) if defaults_key else {}

        result = {}
        for fname_lower in non_null:
            if ('shape' in fname_lower or fname_lower.startswith('st_') or
                    fname_lower in ('objectid', 'globalid', 'fid', 'oid')):
                continue

            esri_type = ftypes.get(fname_lower, '').lower()
            if esri_type in _SKIP_TYPES:
                continue

            if fname_lower in schema_defaults:
                raw_default = schema_defaults[fname_lower]
                if esri_type in _INT_TYPES:
                    try:
                        result[fname_lower] = int(float(str(raw_default)))
                    except (TypeError, ValueError):
                        result[fname_lower] = 0
                elif esri_type in _FLOAT_TYPES:
                    try:
                        result[fname_lower] = float(str(raw_default))
                    except (TypeError, ValueError):
                        result[fname_lower] = 0.0
                else:
                    result[fname_lower] = str(raw_default)
                continue

            if 'date' in esri_type or 'time' in esri_type:
                # Do not invent temporal values. If the schema has no default,
                # the Create Features UI must collect one from the user.
                continue

            domain_name = self.get_domain_for_field(lname, lname, fname_lower)
            domain_values = self.all_domains.get(domain_name, {}) if domain_name else {}
            first_code = next(iter(domain_values.keys()), None)

            if first_code is not None:
                if esri_type in _INT_TYPES:
                    try:
                        result[fname_lower] = int(float(str(first_code)))
                    except (TypeError, ValueError):
                        result[fname_lower] = 0
                elif esri_type in _FLOAT_TYPES:
                    try:
                        result[fname_lower] = float(str(first_code))
                    except (TypeError, ValueError):
                        result[fname_lower] = 0.0
                else:
                    result[fname_lower] = str(first_code)
                continue

            if esri_type in _INT_TYPES:
                result[fname_lower] = 0
            elif esri_type in _FLOAT_TYPES:
                result[fname_lower] = 0.0
            elif esri_type in _TEXT_TYPES:
                result[fname_lower] = ' '
            else:
                result[fname_lower] = 0
        return result

    def get_domain_meta(self, domain_name: str):
        return self._domains_meta_by_name.get(domain_name)

    def export_domains_meta(self) -> list:
        return copy.deepcopy(self.all_domains_meta)

    def set_domains_meta(self, domains_meta: list):
        self.all_domains = {}
        self.all_domains_meta = []
        self._domains_meta_by_name = {}
        for meta in domains_meta:
            clean = self._clean_domain_meta(meta)
            self._upsert_domain_meta(clean)

    def rename_domain_references(self, old_name: str, new_name: str):
        if not old_name or not new_name or old_name == new_name:
            return
        for field_domains in self._field_domains.values():
            for fname, dname in list(field_domains.items()):
                if dname == old_name:
                    field_domains[fname] = new_name
        for cascade in self._cascade_layers.values():
            for subtype_map in cascade.get('dependent_field_domains', {}).values():
                for subtype_code, dname in list(subtype_map.items()):
                    if dname == old_name:
                        subtype_map[subtype_code] = new_name

    def remap_domain_references(self, rename_map: dict):
        if not rename_map:
            return
        for field_domains in self._field_domains.values():
            for fname, dname in list(field_domains.items()):
                if dname in rename_map:
                    field_domains[fname] = rename_map[dname]
        for cascade in self._cascade_layers.values():
            for subtype_map in cascade.get('dependent_field_domains', {}).values():
                for subtype_code, dname in list(subtype_map.items()):
                    if dname in rename_map:
                        subtype_map[subtype_code] = rename_map[dname]

    def remove_domain_references(self, domain_name: str):
        if not domain_name:
            return
        for field_domains in self._field_domains.values():
            for fname, dname in list(field_domains.items()):
                if dname == domain_name:
                    field_domains.pop(fname, None)
        for cascade in self._cascade_layers.values():
            for dep_field, subtype_map in list(
                    cascade.get('dependent_field_domains', {}).items()):
                cleaned = {c: d for c, d in subtype_map.items() if d != domain_name}
                if cleaned:
                    cascade['dependent_field_domains'][dep_field] = cleaned
                else:
                    cascade['dependent_field_domains'].pop(dep_field, None)

    def domain_usage(self, domain_name: str) -> dict:
        usage = {'fields': [], 'subtypes': []}
        if not domain_name:
            return usage
        for layer_key, field_domains in self._field_domains.items():
            display = self._layer_display_names.get(layer_key, layer_key)
            for field_name, dname in field_domains.items():
                if dname == domain_name:
                    usage['fields'].append((display, field_name))
        for layer_key, cascade in self._cascade_layers.items():
            display = self._layer_display_names.get(layer_key, layer_key)
            for field_name, subtype_map in cascade.get('dependent_field_domains', {}).items():
                for subtype_code, dname in subtype_map.items():
                    if dname == domain_name:
                        usage['subtypes'].append((display, field_name, subtype_code))
        return usage

    def get_field_types(self, lname: str) -> dict:
        """Return {field_lower: esriFieldType*} for the layer."""
        key = self._resolve_key(lname, self._field_types)
        if not key:
            return {}
        return dict(self._field_types.get(key, {}))

    def get_layer_feature_type(self, lname: str) -> str | None:
        key = self._resolve_key(lname, self._layer_feature_types)
        return self._layer_feature_types.get(key) if key else None

    def layer_has_z(self, lname: str) -> bool | None:
        key = self._resolve_key(lname, self._layer_has_z)
        return self._layer_has_z.get(key) if key else None

    def layer_has_m(self, lname: str) -> bool | None:
        key = self._resolve_key(lname, self._layer_has_m)
        return self._layer_has_m.get(key) if key else None

    def get_field_defaults(self, lname: str) -> dict:
        key = self._resolve_key(lname, self._field_defaults)
        return dict(self._field_defaults.get(key, {})) if key else {}

    def has_crs(self, lname: str) -> bool:
        return self.get_layer_crs_wkt(lname) is not None

    # ── Internal helpers ──────────────────────────────────────────────────

    @staticmethod
    def _find_spatial_reference_node(root):
        if root is None:
            return None
        sr_node = root.find("./Extent/SpatialReference")
        if sr_node is None:
            sr_node = root.find("./SpatialReference")
        if sr_node is None:
            sr_node = root.find(".//SpatialReference")
        return sr_node

    @staticmethod
    def _extract_wkt_from_sr_node(sr_node):
        if sr_node is None:
            return None
        wkt_node = sr_node.find("WKT")
        if wkt_node is None or not wkt_node.text:
            return None
        wkt = wkt_node.text.strip()
        return wkt or None

    @staticmethod
    def _parent_catalog_name(root):
        if root is None:
            return None
        catalog_path = (root.findtext("./CatalogPath") or '').strip()
        if not catalog_path:
            return None
        normalized = re.sub(r'[\\/]+', '/', catalog_path).strip('/')
        parts = [part.strip() for part in normalized.split('/') if part.strip()]
        if len(parts) < 2:
            return None
        return parts[-2]

    @staticmethod
    def _normalize_catalog_path(root):
        if root is None:
            return ''
        catalog_path = (root.findtext("./CatalogPath") or '').strip()
        if not catalog_path:
            return ''
        return re.sub(r'[\\/]+', '/', catalog_path).strip('/')

    @classmethod
    def _catalog_group_name(cls, normalized_path: str) -> str | None:
        if not normalized_path:
            return None
        parts = [part.strip() for part in normalized_path.split('/') if part.strip()]
        if len(parts) < 2:
            return None
        parent = parts[-2]
        if parent.lower().endswith('.gdb'):
            return None
        return parent or None

    @staticmethod
    def _looks_internal_layer(name: str, normalized_path: str = '') -> bool:
        tokens = [str(name or '').strip().lower()]
        if normalized_path:
            tokens.extend(part.strip().lower() for part in normalized_path.split('/') if part.strip())
        return any(
            token.startswith(('gdb_', 'sde_', 'sqlite_')) or
            token.endswith(('__attach', '__attachrel')) or
            token in {'gdb_items', 'gdb_itemrelationships', 'gdb_itemtypes',
                      'gdb_systemcatalog', 'gdb_spatialrefs'}
            for token in tokens if token
        )

    def _store_layer_crs_wkt(self, lname_lower, lname_raw, wkt):
        if not wkt:
            return
        for key in (lname_lower, self._layer_key(lname_raw)):
            if key:
                self._layer_crs_wkt[key] = wkt

    def _select_best_layer_crs_wkt(self, lname_raw, lname_lower, root, sr_node):
        candidates = []
        seen = set()

        def add_candidate(source, wkt):
            if not wkt:
                return
            normalized = wkt.strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            candidates.append((crs_wkt_quality(normalized), source, normalized))

        existing = None
        for key in (lname_lower, self._layer_key(lname_raw)):
            if key and self._layer_crs_wkt.get(key):
                existing = self._layer_crs_wkt.get(key)
                break
        add_candidate("GDAL", existing)

        parent_name = self._parent_catalog_name(root)
        if parent_name:
            add_candidate(
                f"dataset XML ({parent_name})",
                self._dataset_crs_wkt.get(self._layer_key(parent_name)),
            )

        add_candidate("layer XML", self._extract_wkt_from_sr_node(sr_node))
        if not candidates:
            return None, None

        candidates.sort(key=lambda item: item[0], reverse=True)
        _score, source, wkt = candidates[0]
        return wkt, source

    @staticmethod
    def _resolve_key(lname, dictionary):
        if not lname:
            return None
        low = lname.lower()
        if low in dictionary:
            return low
        canonical = GDBEngine._layer_key(lname)
        if canonical in dictionary:
            return canonical
        variants = GDBEngine._name_variants(lname)
        for variant in variants:
            if variant in dictionary:
                return variant
        best, best_len = None, 0
        for key in dictionary:
            if key in low or (canonical and key in canonical):
                key_len = len(key)
                if key_len > best_len:
                    best, best_len = key, key_len
        return best

    @staticmethod
    def _layer_key(name):
        if not name:
            return ''
        return re.sub(r'[^a-z0-9]+', '', name.lower())

    @staticmethod
    def _name_variants(name):
        raw = (name or '').strip()
        if not raw:
            return []
        variants = {raw.lower(), GDBEngine._layer_key(raw)}
        normalized_path = re.sub(r'[\\/]+', '/', raw).strip('/')
        if normalized_path:
            variants.add(normalized_path.lower())
            variants.add(GDBEngine._layer_key(normalized_path))
            tail = normalized_path.split('/')[-1].strip()
            if tail:
                variants.add(tail.lower())
                variants.add(GDBEngine._layer_key(tail))
        for sep in ('--', '\u2014', '\u2013', '|'):
            if sep in raw:
                tail = raw.split(sep)[-1].strip()
                if tail:
                    variants.add(tail.lower())
                    variants.add(GDBEngine._layer_key(tail))
        return [v for v in variants if v]

    def _resolve_layer_storage_key(self, lname_raw, root=None):
        candidates = []
        if lname_raw:
            candidates.append(lname_raw)
        if root is not None:
            for path in ("./Name", "./AliasName", "./CatalogPath"):
                node = root.find(path)
                if node is None or not node.text:
                    continue
                value = node.text.strip()
                if path == "./CatalogPath":
                    normalized = re.sub(r'[\\/]+', '/', value).strip('/')
                    if normalized:
                        candidates.append(normalized)
                        tail = normalized.split('/')[-1].strip()
                        if tail:
                            candidates.append(tail)
                    continue
                if value:
                    candidates.append(value)
        for candidate in candidates:
            key = self._layer_key(candidate)
            if key in self._field_domains or key in self._all_layer_fields:
                existing_display = self._layer_display_names.get(key)
                if (existing_display and
                        existing_display.lower() != candidate.lower()):
                    log.warning(
                        "Layer name collision: '%s' and '%s' both map to "
                        "canonical key '%s'. Domain lookups may be ambiguous "
                        "for one of these layers.",
                        existing_display, candidate, key)
                self._layer_display_names.setdefault(key, candidate)
                return key
        for candidate in candidates:
            key = self._layer_key(candidate)
            if key:
                self._layer_display_names.setdefault(key, candidate)
                return key
        key = self._layer_key(lname_raw)
        self._layer_display_names.setdefault(key, lname_raw or key)
        return key

    @staticmethod
    def _clean_domain_meta(meta):
        clean = {
            'name': str(meta.get('name', '')).strip(),
            'description': str(meta.get('description', '') or '').strip(),
            'field_type': str(meta.get('field_type', 'String') or 'String'),
            'domain_type': str(meta.get('domain_type', 'CodedValue') or 'CodedValue'),
            'split': str(meta.get('split', 'DefaultValue') or 'DefaultValue'),
            'merge': str(meta.get('merge', 'DefaultValue') or 'DefaultValue'),
            'min_value': meta.get('min_value'),
            'max_value': meta.get('max_value'),
            'coded_values': [],
        }
        if clean['domain_type'] == 'CodedValue':
            seen_codes = {}
            for cv in meta.get('coded_values', []):
                code = str(cv.get('code', '')).strip()
                desc = str(cv.get('description', '') or '').strip()
                if code == '':
                    continue
                seen_codes[code] = desc
            clean['coded_values'] = [
                {'code': code, 'description': desc}
                for code, desc in seen_codes.items()
            ]
            clean['min_value'] = None
            clean['max_value'] = None
        else:
            clean['coded_values'] = []
            clean['min_value'] = (None if clean['min_value'] in ('', None)
                                  else str(clean['min_value']))
            clean['max_value'] = (None if clean['max_value'] in ('', None)
                                  else str(clean['max_value']))
        return clean

    def _upsert_domain_meta(self, meta):
        clean = self._clean_domain_meta(meta)
        name = clean['name']
        if not name:
            return
        if clean['domain_type'] == 'CodedValue':
            self.all_domains[name] = {
                cv['code']: cv['description']
                for cv in clean.get('coded_values', [])
            }
        else:
            self.all_domains[name] = {}
        if name in self._domains_meta_by_name:
            for idx, existing in enumerate(self.all_domains_meta):
                if existing['name'] == name:
                    self.all_domains_meta[idx] = clean
                    break
        else:
            self.all_domains_meta.append(clean)
        self._domains_meta_by_name[name] = clean
