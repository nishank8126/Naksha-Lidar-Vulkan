"""
Authoritative canvas CRS management for NakshaAI.

ARCHITECTURE
============

    SOURCE COORDINATES  +  SOURCE CRS
                |
                v
            TRANSFORM  (always_xy=True)
                |
                v
            CANVAS CRS                 <-- ONE authoritative CRS for VTK world XY
                |
                v
               VTK

    BASEMAP (EPSG:3857 / WGS84)
                |
                v
            TRANSFORM -> CANVAS CRS -> VTK

Rules enforced here
-------------------
1. There is exactly ONE authoritative canvas CRS: ``app.canvas_crs``
   (a ``pyproj.CRS``), with provenance in ``app.canvas_crs_info``.
   ``app.project_crs_epsg`` / ``app.project_crs_wkt`` / ``app.crs`` are kept in
   sync for backwards compatibility but are NOT authoritative.

2. The FIRST georeferenced dataset with a *trustworthy* CRS establishes the
   canvas CRS. Later layers are reprojected INTO it - they never silently
   replace it.

3. **NO EPSG:3857 FALLBACK** when a geospatial dataset (LAZ/SNT/GIS) is
   present. If a dataset has no machine-readable CRS it is marked UNRESOLVED
   and basemap alignment is disabled for it. A standalone basemap with an
   empty canvas may still use EPSG:3857 as its own temporary canvas CRS - that
   is the only allowed case.

4. CRS is never inferred from coordinate magnitude, never guessed, never
   hardcoded to a location.

5. All transforms use ``always_xy=True`` and float64.
"""

from __future__ import annotations

import math
import os
import re
import struct

__all__ = [
    "CANVAS_CRS_ATTR",
    "CANVAS_CRS_INFO_ATTR",
    "get_canvas_crs",
    "get_canvas_crs_info",
    "set_canvas_crs",
    "ensure_canvas_crs",
    "clear_canvas_crs",
    "has_geospatial_data",
    "transform_xy_to_canvas",
    "transform_xy_from_canvas",
    "log_dataset_crs",
    "notify_plugins_crs_changed",
    "resolve_laz_crs",
    "resolve_prj_crs",
    "resolve_snt_crs",
    "resolve_gis_crs",
]

CANVAS_CRS_ATTR = "canvas_crs"
CANVAS_CRS_INFO_ATTR = "canvas_crs_info"


# WKT1 / WKT2 keyword stems. NOTE: WKT2 uses "PROJCRS"/"GEOGCRS" (with an R)
# while WKT1 uses "PROJCS"/"GEOGCS". "PROJCS" is NOT a substring of "PROJCRS",
# so both spellings must be checked explicitly.
_WKT_KEYWORDS = (
    "PROJCRS", "PROJCS",
    "GEOGCRS", "GEOGCS", "BASEGEOGCRS",
    "GEOCCS", "COMPOUNDCRS", "COMPD_CS",
    "VERTCRS", "VERT_CS", "LOCAL_CS",
    "ENGINEERINGCRS", "PARAMETRICCRS", "BOUNDCRS", "TIMECRS",
)


def _looks_like_wkt(text):
    """True if *text* starts with, or clearly contains, a WKT1/WKT2 CRS node."""
    if not text:
        return False
    upper = text.upper()
    if upper.lstrip().startswith(_WKT_KEYWORDS):
        return True
    # Tolerate leading junk (e.g. a 32-byte LAS 1.4 WKT VLR preamble of NULs)
    return any(k in upper for k in _WKT_KEYWORDS)

# EPSG:3857 is allowed ONLY as a standalone-basemap canvas CRS on an empty
# project. It is never used as a fallback for real geospatial data.
WEB_MERCATOR = 3857

_GEO_EXT = (".laz", ".las", ".snt", ".dgn", ".dxf", ".shp", ".geojson",
            ".json", ".gpkg", ".kml", ".gml")


# --------------------------------------------------------------------------
# basic accessors
# --------------------------------------------------------------------------
def get_canvas_crs(app):
    """Return the authoritative canvas CRS (pyproj.CRS) or None."""
    if app is None:
        return None
    crs = getattr(app, CANVAS_CRS_ATTR, None)
    if crs is not None:
        return crs
    # Backwards compatibility: adopt an already-known project CRS once.
    crs = getattr(app, "crs", None)
    if crs is not None:
        try:
            setattr(app, CANVAS_CRS_ATTR, crs)
        except Exception:
            pass
        return crs
    return None


def get_canvas_crs_info(app):
    """Return provenance dict for the canvas CRS (may be empty)."""
    if app is None:
        return {}
    info = getattr(app, CANVAS_CRS_INFO_ATTR, None)
    return dict(info) if isinstance(info, dict) else {}


def _sync_legacy_fields(app, crs):
    """Keep project_crs_* fields in sync for older code paths."""
    if app is None or crs is None:
        return
    try:
        setattr(app, "crs", crs)
    except Exception:
        pass
    try:
        setattr(app, "project_crs_wkt", crs.to_wkt())
    except Exception:
        pass
    try:
        epsg = crs.to_epsg()
        setattr(app, "project_crs_epsg", int(epsg) if epsg else None)
    except Exception:
        pass


def set_canvas_crs(app, crs, source="unknown", dataset=None, notify=True,
                   force=False):
    """Set (or replace) the authoritative canvas CRS.

    ``force=False`` (default) means: if a canvas CRS already exists, keep it.
    Replacing an existing canvas CRS would require reprojecting every already
    rendered actor, so it must be explicit.
    """
    if app is None or crs is None:
        return None
    existing = getattr(app, CANVAS_CRS_ATTR, None)
    if existing is not None and not force:
        return existing
    try:
        setattr(app, CANVAS_CRS_ATTR, crs)
    except Exception:
        return None
    info = {
        "source": source,
        "dataset": dataset,
        "epsg": crs.to_epsg(),
        "name": getattr(crs, "name", None),
    }
    try:
        setattr(app, CANVAS_CRS_INFO_ATTR, info)
    except Exception:
        pass
    _sync_legacy_fields(app, crs)
    print(f"[CRS] canvas CRS set: EPSG:{info['epsg']} ({info['name']}) "
          f"from {source}" + (f" [{os.path.basename(str(dataset))}]" if dataset else ""))
    if notify:
        notify_plugins_crs_changed(app)
    if hasattr(app, "update_epsg_display"):
        try:
            app.update_epsg_display()
        except Exception:
            pass
    return crs


def ensure_canvas_crs(app, crs, source="unknown", dataset=None):
    """Establish the canvas CRS only if not already set (first trustworthy wins)."""
    return set_canvas_crs(app, crs, source=source, dataset=dataset,
                          notify=True, force=False)


def clear_canvas_crs(app):
    """Reset all canvas/project CRS state (used by Clear Project)."""
    if app is None:
        return
    for attr in (CANVAS_CRS_ATTR, CANVAS_CRS_INFO_ATTR,
                 "crs", "project_crs_epsg", "project_crs_wkt"):
        try:
            setattr(app, attr, None)
        except Exception:
            pass
    try:
        setattr(app, CANVAS_CRS_INFO_ATTR, {})
    except Exception:
        pass
    print("[CRS] canvas CRS cleared (project reset)")


def has_geospatial_data(app):
    """True when any geospatial dataset is loaded.

    Used to decide whether an EPSG:3857 basemap-only canvas CRS is allowed.
    """
    if app is None:
        return False
    try:
        d = getattr(app, "data", None)
        if d is not None and d.get("xyz") is not None and len(d["xyz"]) > 0:
            return True
    except Exception:
        pass
    for attr in ("snt_actors", "dxf_actors", "gis_layers"):
        try:
            if getattr(app, attr, None):
                return True
        except Exception:
            pass
    return False


# --------------------------------------------------------------------------
# transforms
# --------------------------------------------------------------------------
def _transformer(src_crs, dst_crs):
    from pyproj import Transformer
    return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def transform_xy_to_canvas(app, xs, ys, source_crs):
    """Transform source XY arrays -> canvas CRS XY. Returns (new_xs, new_ys).

    Uses float64 and ``always_xy=True``. If no canvas CRS or no source CRS is
    known, the coordinates are returned UNCHANGED (never silently reinterpreted
    as another CRS).
    """
    src = source_crs
    dst = get_canvas_crs(app)
    if src is None or dst is None:
        return xs, ys
    try:
        if src.equals(dst):
            return xs, ys
    except Exception:
        pass
    try:
        import numpy as np
        ax = np.asarray(xs, dtype=np.float64)
        ay = np.asarray(ys, dtype=np.float64)
        t = _transformer(src, dst)
        nx, ny = t.transform(ax, ay)
        return nx, ny
    except Exception:
        # Fall back to a scalar loop; still float64.
        try:
            t = _transformer(src, dst)
            nx = []
            ny = []
            for x, y in zip(xs, ys):
                a, b = t.transform(float(x), float(y))
                nx.append(a)
                ny.append(b)
            return nx, ny
        except Exception:
            return xs, ys


def transform_xy_from_canvas(app, xs, ys, target_crs):
    """Inverse: canvas CRS XY -> target CRS XY (for write/export boundaries)."""
    src = get_canvas_crs(app)
    if src is None or target_crs is None:
        return xs, ys
    try:
        if src.equals(target_crs):
            return xs, ys
    except Exception:
        pass
    try:
        import numpy as np
        ax = np.asarray(xs, dtype=np.float64)
        ay = np.asarray(ys, dtype=np.float64)
        t = _transformer(src, target_crs)
        nx, ny = t.transform(ax, ay)
        return nx, ny
    except Exception:
        return xs, ys


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
def log_dataset_crs(filename, layer_type, source_crs, crs_source,
                    raw_bounds=None, canvas_crs=None, render_bounds=None,
                    extra=None):
    """Print the standardised [CRS] diagnostic block."""
    print("[CRS] " + "-" * 58)
    print(f"[CRS] File         : {filename}")
    print(f"[CRS] Layer type   : {layer_type}")
    if source_crs is None:
        print("[CRS] Source CRS   : UNRESOLVED")
        print("[CRS] CRS source   : (none)")
        print("[CRS] Basemap align: DISABLED (no machine-readable CRS)")
    else:
        try:
            epsg = source_crs.to_epsg()
            label = f"EPSG:{epsg}" if epsg else "custom"
        except Exception:
            label = "custom"
        print(f"[CRS] Source CRS   : {label}")
        print(f"[CRS] CRS source   : {crs_source}")

    if raw_bounds:
        x0, y0, x1, y1 = raw_bounds[:4]
        print(f"[CRS] Raw bounds   : {x0:.3f} {y0:.3f} {x1:.3f} {y1:.3f}")
        print(f"[CRS] Raw center   : {(x0 + x1) / 2.0:.3f} {(y0 + y1) / 2.0:.3f}")

    if canvas_crs is not None:
        try:
            epsg = canvas_crs.to_epsg()
            print(f"[CRS] Canvas CRS   : EPSG:{epsg}")
        except Exception:
            print("[CRS] Canvas CRS   : custom")
    else:
        print("[CRS] Canvas CRS   : (not established)")

    if render_bounds:
        x0, y0, x1, y1 = render_bounds[:4]
        print(f"[CRS] Render center: {(x0 + x1) / 2.0:.3f} {(y0 + y1) / 2.0:.3f}")

    # WGS84 centre when we know a CRS
    ref = source_crs if source_crs is not None else canvas_crs
    if ref is not None and raw_bounds:
        try:
            from pyproj import CRS as _CRS
            t = _transformer(ref, _CRS.from_epsg(4326))
            lon, lat = t.transform((raw_bounds[0] + raw_bounds[2]) / 2.0,
                                   (raw_bounds[1] + raw_bounds[3]) / 2.0)
            if all(math.isfinite(v) for v in (lon, lat)):
                print(f"[CRS] WGS84 center : lon={lon:.6f} lat={lat:.6f}")
        except Exception:
            pass

    if source_crs is not None and canvas_crs is not None:
        try:
            s = source_crs.to_epsg() or "custom"
            d = canvas_crs.to_epsg() or "custom"
            print(f"[CRS] Transf.      : EPSG:{s} -> EPSG:{d}")
        except Exception:
            pass
    if extra:
        for k, v in extra.items():
            print(f"[CRS] {k:<15}: {v}")
    print("[CRS] " + "-" * 58)


# --------------------------------------------------------------------------
# plugin notification
# --------------------------------------------------------------------------
def notify_plugins_crs_changed(app):
    """Tell installed plugins that the canvas CRS changed.

    Reaches plugins through the REAL owner: ``app.plugin_manager.loaded_plugins``
    whose values are dicts like {"instance":..., "manifest":..., "dir":...}.
    Calls exactly ONE refresh entry point per plugin (``refresh()`` preferred,
    else ``_schedule_refresh()``) so we never trigger duplicate tile downloads.
    """
    if app is None:
        return 0
    manager = getattr(app, "plugin_manager", None)
    loaded = getattr(manager, "loaded_plugins", None) if manager is not None else None
    if not loaded:
        # Tolerantly support the legacy shapes if ever present, but the
        # authoritative path is plugin_manager.loaded_plugins.
        for fallback_attr in ("loaded_plugins", "plugins"):
            loaded = getattr(app, fallback_attr, None)
            if loaded:
                break
    if not loaded:
        return 0

    notified = 0
    try:
        items = loaded.values() if hasattr(loaded, "values") else loaded
    except Exception:
        return 0
    for info in items:
        instance = None
        if isinstance(info, dict):
            instance = info.get("instance")
        elif info is not None:
            instance = info
        if instance is None:
            continue
        try:
            refresh = getattr(instance, "on_canvas_crs_changed", None)
            if callable(refresh):
                refresh()
                notified += 1
                continue
            refresh = getattr(instance, "refresh", None)
            if callable(refresh):
                refresh()
                notified += 1
                continue
            schedule = getattr(instance, "_schedule_refresh", None)
            if callable(schedule):
                schedule()
                notified += 1
        except Exception as exc:
            print(f"[CRS] plugin notify failed: {exc}")
    if notified:
        print(f"[CRS] notified {notified} plugin(s) of canvas CRS change")
    return notified


# --------------------------------------------------------------------------
# CRS resolution: .prj (OGC WKT or TerraScan)
# --------------------------------------------------------------------------
def resolve_prj_crs(prj_path):
    """Return (crs, source_label) from a .prj file, or (None, None).

    Handles:
      * OGC WKT (PROJCS/GEOGCS/...)            -> parsed as WKT
      * TerraScan project ([TerraScan project] -> ProjectionSystem=<EPSG>)
    A TerraScan *block* file (Block <name> + coords) is geometry, not CRS, and
    yields None.
    """
    try:
        from pyproj import CRS
        if not prj_path or not os.path.isfile(prj_path):
            return None, None
        with open(prj_path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(2048).lstrip()
        if not head:
            return None, None
        upper = head.upper()
        if head[:32].lstrip().upper().startswith("[TERRASCAN"):
            with open(prj_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            m = re.search(r"ProjectionSystem\s*=\s*(\d+)", text, re.IGNORECASE)
            if m:
                epsg = int(m.group(1))
                if epsg > 0:
                    return CRS.from_epsg(epsg), f"TerraScan PRJ ProjectionSystem={epsg}"
            return None, None
        # Reject quickly (e.g. TerraScan block files starting with "Block ...")
        # unless the content really is a WKT1 or WKT2 CRS definition.
        if not _looks_like_wkt(upper):
            return None, None
        with open(prj_path, "r", encoding="utf-8", errors="ignore") as f:
            return CRS.from_wkt(f.read()), "OGC WKT PRJ"
    except Exception:
        return None, None


# --------------------------------------------------------------------------
# CRS resolution: LAZ/LAS (VLRs and EVLRs)
# --------------------------------------------------------------------------
def _parse_vlr_blocks(f, start, end, extended):
    """Yield (user_id, record_id, data_bytes) for VLRs or EVLRs.

    Regular VLR  : 2 reserved + 16 user_id + 2 record_id + 2 record_len = 22 B header
    Extended VLR : 2 reserved + 16 user_id + 2 record_id + 8 record_len + 32 desc = 60 B header
    """
    pos = start
    hdr = 60 if extended else 22
    while pos + hdr <= end:
        f.seek(pos)
        f.read(2)                                       # reserved
        uid = f.read(16).split(b"\x00", 1)[0].decode("ascii", "ignore")
        rid = struct.unpack_from("<H", f.read(2))[0]
        if extended:
            rlen = struct.unpack_from("<Q", f.read(8))[0]
            f.read(32)                                  # description
        else:
            rlen = struct.unpack_from("<H", f.read(2))[0]
        data_start = pos + hdr
        if rlen <= 0 or data_start + rlen > end:
            break
        f.seek(data_start)
        data = f.read(rlen)
        yield uid, rid, data
        pos = data_start + rlen


def resolve_laz_crs(path):
    """Return (crs, source_label) from a LAZ/LAS header, or (None, None).

    Checks BOTH regular VLRs (which live between the fixed header and the point
    data) and LAS 1.4 Extended VLRs (which live at the end of the file).
    Recognises:
      * OGC WKT VLR        - record 2112 (also 2113 legacy)
      * GeoKey Directory   - record 34735, keys 3072 (projected) / 2048 (geographic)
    """
    try:
        from pyproj import CRS
        if not path or not os.path.isfile(path):
            return None, None
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if f.read(4) != b"LASF":
                return None, None
            f.seek(24)
            major = f.read(1)[0]
            minor = f.read(1)[0]
            f.seek(94)
            header_size = struct.unpack_from("<H", f.read(2))[0]
            offset_to_pt = struct.unpack_from("<I", f.read(4))[0]
            n_vlr = struct.unpack_from("<I", f.read(4))[0]

            ranges = []
            if n_vlr and offset_to_pt > header_size:
                ranges.append((header_size, offset_to_pt, False, n_vlr))
            # LAS 1.4 EVLRs
            if major == 1 and minor >= 3:
                try:
                    f.seek(227)
                    start_evlr = struct.unpack_from("<Q", f.read(8))[0]
                    n_evlr = struct.unpack_from("<I", f.read(4))[0]
                    if start_evlr and n_evlr and start_evlr < size:
                        ranges.append((start_evlr, size, True, n_evlr))
                except Exception:
                    pass

            for start, end, extended, count in ranges:
                seen = 0
                for uid, rid, data in _parse_vlr_blocks(f, start, end, extended):
                    seen += 1
                    if seen > count:
                        break
                    if not uid.startswith("LASF_Proj"):
                        continue
                    # OGC WKT (WKT1 or WKT2). LAS 1.4 reserves a 32-byte block
                    # at the start of this VLR, so strip NULs from BOTH ends
                    # before decoding. If the payload is truncated/corrupt,
                    # CRS.from_wkt raises and we fall through to the GeoKey
                    # search instead of using a broken CRS.
                    if rid in (2112, 2113) and data:
                        try:
                            wkt = (data.strip(b"\x00")
                                   .decode("utf-8", "ignore").strip())
                            if wkt and _looks_like_wkt(wkt):
                                return CRS.from_wkt(wkt), f"LAS OGC WKT VLR ({rid})"
                        except Exception:
                            pass
                    # GeoKey Directory
                    if rid == 34735 and len(data) >= 8:
                        try:
                            ns = len(data) // 2
                            shorts = struct.unpack_from("<" + "H" * ns, data)
                            nkeys = shorts[3]
                            for i in range(nkeys):
                                b = 4 + i * 4
                                if b + 3 >= ns:
                                    break
                                kid, tloc, cnt, voff = (shorts[b], shorts[b + 1],
                                                        shorts[b + 2], shorts[b + 3])
                                if kid in (3072, 2048) and tloc == 0 and cnt == 1:
                                    epsg = int(voff)
                                    if epsg > 0:
                                        return (CRS.from_epsg(epsg),
                                                f"LAS GeoKey Directory (key {kid})")
                        except Exception:
                            pass
    except Exception:
        return None, None
    return None, None


# --------------------------------------------------------------------------
# CRS resolution: SNT
# --------------------------------------------------------------------------
def resolve_snt_crs(snt_path):
    """Return (crs, source_label) for an SNT/DGN, or (None, None).

    Chain:
      1. adjacent OGC WKT .prj
      2. adjacent TerraScan .prj -> ProjectionSystem=<EPSG>
      3. LAZ/LAS listed in the SNT's TerraScan block list -> their VLR CRS
      4. a single differently-named .prj in the same folder (delivery convention)
    """
    try:
        from pathlib import Path
        if not snt_path:
            return None, None
        p = Path(str(snt_path))
        candidates = []
        same = p.with_suffix(".prj")
        if same.is_file():
            candidates.append(same)
        try:
            siblings = [q for q in p.parent.glob("*.prj") if q.is_file()]
        except Exception:
            siblings = []
        if len(siblings) == 1 and siblings[0] not in candidates:
            candidates.append(siblings[0])

        terrascans = []
        for prj in candidates:
            crs, label = resolve_prj_crs(str(prj))
            if crs is not None:
                return crs, label
            # remember TerraScan files for the block-list fallback
            try:
                with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                    if f.read(2048).lstrip()[:32].lstrip().upper().startswith("[TERRASCAN"):
                        terrascans.append(prj)
            except Exception:
                pass

        # 3. referenced LAZ/LAS from TerraScan block list
        for prj in terrascans:
            for laz in _terrascan_block_laz(prj):
                crs, label = resolve_laz_crs(laz)
                if crs is not None:
                    return crs, f"{label} (via SNT block {os.path.basename(laz)})"
        return None, None
    except Exception:
        return None, None


def _terrascan_block_laz(prj_path):
    """Return existing .laz/.las paths referenced by a TerraScan block list."""
    out = []
    try:
        from pathlib import Path
        prj = Path(str(prj_path))
        with open(prj, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        seen = set()
        for ln in lines:
            m = re.match(r"\s*Block\s+(.+?)\s*$", ln)
            if not m:
                continue
            base = m.group(1).strip()
            for ext in (".laz", ".las", ".LAZ", ".LAS"):
                if base.lower().endswith(ext.lower()):
                    base = base[: -len(ext)]
                    break
            for ext in (".laz", ".las"):
                cand = prj.parent / f"{base}{ext}"
                if cand.exists() and str(cand).lower() not in seen:
                    seen.add(str(cand).lower())
                    out.append(str(cand))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# CRS resolution: GIS (GDB / SHP / GeoJSON / raster)
# --------------------------------------------------------------------------
def resolve_gis_crs(path, layer_name=None):
    """Return (crs, source_label) for a GIS source, or (None, None).

    Uses GDAL/OGR, which the project already depends on. Works for GDB feature
    classes (per-feature-class spatial reference), SHP (.prj), GeoJSON, GeoTIFF.
    """
    try:
        from osgeo import osr, ogr, gdal
        from pyproj import CRS
    except Exception:
        return None, None
    if not path or not os.path.exists(path):
        return None, None

    lower = str(path).lower()
    srs = None
    label = None
    try:
        if lower.endswith((".tif", ".tiff")):
            ds = gdal.Open(str(path))
            if ds is None:
                return None, None
            wkt = ds.GetProjection()
            ds = None
            if wkt:
                srs = osr.SpatialReference()
                srs.ImportFromWkt(wkt)
                label = "GeoTIFF projection"
        else:
            ds = ogr.Open(str(path), 0)
            if ds is None:
                return None, None
            lyr = None
            if layer_name:
                try:
                    lyr = ds.GetLayerByName(str(layer_name))
                except Exception:
                    lyr = None
                if lyr is None:
                    for i in range(ds.GetLayerCount()):
                        cand = ds.GetLayer(i)
                        if cand and cand.GetName().lower() == str(layer_name).lower():
                            lyr = cand
                            break
            if lyr is None:
                lyr = ds.GetLayer(0)
            if lyr is None:
                ds = None
                return None, None
            srs = lyr.GetSpatialRef()
            label = f"OGR SRS ({os.path.basename(str(path))})"
            ds = None
    except Exception:
        return None, None

    if srs is None:
        return None, None
    try:
        srs.AutoIdentifyEPSG()
    except Exception:
        pass
    try:
        code = srs.GetAuthorityCode(None)
        if code:
            return CRS.from_epsg(int(code)), f"{label} -> EPSG:{code}"
    except Exception:
        pass
    try:
        wkt = srs.ExportToWkt()
        if wkt:
            return CRS.from_wkt(wkt), f"{label} -> WKT"
    except Exception:
        pass
    return None, None
