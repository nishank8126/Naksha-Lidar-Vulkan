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
from pyproj import CRS

__all__ = [
    "CANVAS_CRS_ATTR",
    "CANVAS_CRS_INFO_ATTR",
    "get_canvas_crs",
    "get_canvas_crs_info",
    "set_canvas_crs",
    "ensure_canvas_crs",
    "clear_canvas_crs",
    "extract_epsg_code",
    "has_geospatial_data",
    "transform_xy_to_canvas",
    "transform_xy_from_canvas",
    "transform_xyz_to_canvas",
    "transform_xyz_from_canvas",
    "project_points_to_canvas",
    "log_dataset_crs",
    "notify_plugins_crs_changed",
    "resolve_laz_crs",
    "resolve_point_cloud_crs",
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


def extract_epsg_code(crs):
    """Extract a numeric EPSG code from a pyproj.CRS with fuzzy/confidence fallbacks."""
    if crs is None:
        return None
    try:
        code = crs.to_epsg()
        if code:
            return int(code)
    except Exception:
        pass
    try:
        code = crs.to_epsg(min_confidence=25)
        if code:
            return int(code)
    except Exception:
        pass
    try:
        auth = crs.to_authority()
        if auth and str(auth[0]).upper() == "EPSG":
            return int(auth[1])
    except Exception:
        pass
    try:
        for sub in getattr(crs, "sub_crs_list", None) or []:
            sub_code = extract_epsg_code(sub)
            if sub_code:
                return sub_code
    except Exception:
        pass
    try:
        wkt = crs.to_wkt()
        m = re.search(r'(?:AUTHORITY|ID)\["EPSG",\s*"?(\d+)"?\]', wkt, re.IGNORECASE)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


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
        epsg = extract_epsg_code(crs)
        setattr(app, "project_crs_epsg", int(epsg) if epsg else None)
    except Exception:
        pass


def set_canvas_crs(app, crs, source="unknown", dataset=None, notify=True,
                   force=False, persistent=None):
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
        "epsg": extract_epsg_code(crs),
        "name": getattr(crs, "name", None),
        "persistent": (str(source).strip().lower() == "user-selected project crs"
                       if persistent is None else bool(persistent)),
    }
    try:
        setattr(app, CANVAS_CRS_INFO_ATTR, info)
    except Exception:
        pass
    _sync_legacy_fields(app, crs)
    try:
        from gui.projection_engine import get_projection_engine
        get_projection_engine(app)
    except Exception:
        pass
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


def clear_canvas_crs(app, reason="project reset", notify=True):
    """Complete canvas-CRS transition, including UI and one plugin notice."""
    if app is None:
        return
    old = get_canvas_crs(app)
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
    label = None
    try:
        auth = old.to_authority() if old is not None else None
        label = f"{auth[0]}:{auth[1]}" if auth else (old.name if old is not None else "None")
    except Exception:
        label = str(old)
    print(f"[CRS] canvas CRS released: {label} reason={reason}")
    if hasattr(app, "update_epsg_display"):
        try:
            app.update_epsg_display()
        except Exception:
            pass
    if notify:
        notify_plugins_crs_changed(app)


def spatial_content_summary(app):
    """Single authoritative inventory used for CRS lifetime decisions."""
    summary = {"gis": 0, "snt": 0, "pointcloud": 0, "cad": 0,
               "rasters": 0, "drawings": 0}
    if app is None:
        return summary
    try:
        data = getattr(app, "data", None)
        xyz = data.get("xyz") if isinstance(data, dict) else None
        summary["pointcloud"] = int(xyz is not None and len(xyz) > 0)
    except Exception:
        pass
    summary["gis"] = len(getattr(app, "gis_layers", None) or [])
    summary["snt"] = max(len(getattr(app, "snt_attachments", None) or []),
                         len(getattr(app, "snt_actors", None) or []))
    cad_stores = ("dxf_attachments", "dxf_actors", "dwg_attachments",
                  "dwg_actors", "dgn_attachments", "dgn_actors")
    summary["cad"] = max([len(getattr(app, n, None) or []) for n in cad_stores] or [0])
    raster_stores = ("geotiff_actors", "raster_actors", "ecw_actors")
    summary["rasters"] = max([len(getattr(app, n, None) or []) for n in raster_stores] or [0])
    try:
        summary["drawings"] = len(getattr(getattr(app, "digitizer", None), "drawings", None) or [])
    except Exception:
        pass
    return summary


def scene_has_spatial_content(app):
    return any(spatial_content_summary(app).values())


def _capture_geographic_camera_state(app, old_crs):
    """Capture lon/lat and approximate slippy zoom before old-CRS release."""
    try:
        from pyproj import CRS, Transformer
        renderer = app.vtk_widget.renderer
        camera = renderer.GetActiveCamera()
        rw = app.vtk_widget.GetRenderWindow()
        width, height = rw.GetSize()
        aspect = max(1, width) / max(1, height)
        half_h = float(camera.GetParallelScale())
        cx, cy = map(float, camera.GetFocalPoint()[:2])
        inverse = Transformer.from_crs(old_crs, CRS.from_epsg(4326), always_xy=True)
        lon, lat = inverse.transform(cx, cy)
        edge_lon, edge_lat = inverse.transform(cx + half_h * aspect, cy + half_h)
        if not all(math.isfinite(v) for v in (lon, lat, edge_lon, edge_lat)):
            return None
        geographic_width = max(1e-9, 2.0 * abs(edge_lon - lon))
        zoom = int(round(math.log2(360.0 * max(1, width) / (256.0 * geographic_width))))
        return {"lon": lon, "lat": lat, "zoom": max(0, min(22, zoom))}
    except Exception:
        return None


def reconcile_canvas_crs_after_content_change(app, reason=""):
    """Release only an automatic CRS after the final spatial object is gone."""
    old = get_canvas_crs(app)
    info = get_canvas_crs_info(app)
    summary = spatial_content_summary(app)
    released = False
    if old is not None and not any(summary.values()) and not info.get("persistent", False):
        try:
            app._released_canvas_view = _capture_geographic_camera_state(app, old)
        except Exception:
            app._released_canvas_view = None
        clear_canvas_crs(app, reason=reason or "last spatial layer removed", notify=True)
        released = True
    new = get_canvas_crs(app)
    print("CRS_STATE action=layer_removed "
          f"remaining_gis={summary['gis']} remaining_snt={summary['snt']} "
          f"remaining_pointcloud={summary['pointcloud']} remaining_cad={summary['cad']} "
          f"remaining_drawings={summary['drawings']} old_canvas_crs={old} "
          f"new_canvas_crs={new} provenance={info.get('source')} released={released}")
    return released


def has_geospatial_data(app):
    """True when any geospatial dataset is loaded.

    Used to decide whether an EPSG:3857 basemap-only canvas CRS is allowed.
    """
    return scene_has_spatial_content(app)


# --------------------------------------------------------------------------
# transforms
# --------------------------------------------------------------------------
def _transformer(src_crs, dst_crs):
    """Compatibility helper; real selection lives in projection_engine."""
    try:
        from gui.projection_engine import build_transformer
        t, _report = build_transformer(src_crs, dst_crs)
        return t
    except Exception:
        from pyproj import Transformer
        return Transformer.from_crs(src_crs, dst_crs, always_xy=True)


def transform_xy_to_canvas(app, xs, ys, source_crs):
    """Transform source XY arrays -> authoritative project/canvas CRS.

    Coordinates are never re-labelled.  When either CRS is unresolved they are
    returned unchanged and the caller can surface the unresolved state.
    """
    dst = get_canvas_crs(app)
    if source_crs is None or dst is None:
        return xs, ys
    try:
        from gui.projection_engine import transform_xy
        nx, ny, report = transform_xy(xs, ys, source_crs, dst)
        try:
            get_projection_engine = __import__('gui.projection_engine', fromlist=['get_projection_engine']).get_projection_engine
            get_projection_engine(app).last_report = report
        except Exception:
            pass
        return nx, ny
    except Exception:
        try:
            t = _transformer(source_crs, dst)
            if t is None:
                return xs, ys
            return t.transform(xs, ys)
        except Exception:
            return xs, ys


def transform_xy_from_canvas(app, xs, ys, target_crs):
    """Inverse: project/canvas CRS XY -> target/source CRS XY."""
    src = get_canvas_crs(app)
    if src is None or target_crs is None:
        return xs, ys
    try:
        from gui.projection_engine import transform_xy
        nx, ny, report = transform_xy(xs, ys, src, target_crs)
        try:
            from gui.projection_engine import get_projection_engine
            get_projection_engine(app).last_report = report
        except Exception:
            pass
        return nx, ny
    except Exception:
        try:
            t = _transformer(src, target_crs)
            if t is None:
                return xs, ys
            return t.transform(xs, ys)
        except Exception:
            return xs, ys


def transform_xyz_to_canvas(app, xs, ys, zs, source_crs):
    """Transform XYZ into project coordinates.

    PROJ applies a vertical operation only when the CRS/available grids define
    one. Otherwise Z is passed through rather than guessed.
    """
    dst = get_canvas_crs(app)
    if source_crs is None or dst is None:
        return xs, ys, zs
    try:
        from gui.projection_engine import transform_xyz
        nx, ny, nz, report = transform_xyz(xs, ys, zs, source_crs, dst)
        try:
            from gui.projection_engine import get_projection_engine
            get_projection_engine(app).last_report = report
        except Exception:
            pass
        return nx, ny, nz
    except Exception:
        return xs, ys, zs


def transform_xyz_from_canvas(app, xs, ys, zs, target_crs):
    src = get_canvas_crs(app)
    if src is None or target_crs is None:
        return xs, ys, zs
    try:
        from gui.projection_engine import transform_xyz
        nx, ny, nz, report = transform_xyz(xs, ys, zs, src, target_crs)
        try:
            from gui.projection_engine import get_projection_engine
            get_projection_engine(app).last_report = report
        except Exception:
            pass
        return nx, ny, nz
    except Exception:
        return xs, ys, zs


def project_points_to_canvas(app, points, source_crs, *, copy=True, chunk_size=1_000_000):
    """Transform an Nx2/Nx3 array to canvas CRS with bounded memory."""
    dst = get_canvas_crs(app)
    if source_crs is None or dst is None:
        try:
            import numpy as np
            return np.array(points, dtype=np.float64, copy=copy)
        except Exception:
            return points
    try:
        from gui.projection_engine import transform_points, get_projection_engine
        out, report = transform_points(points, source_crs, dst, copy=copy, chunk_size=chunk_size)
        get_projection_engine(app).last_report = report
        return out
    except Exception:
        return points


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
# CRS resolution: .prj (OGC WKT or Nakshatech)
# --------------------------------------------------------------------------
def resolve_prj_crs(prj_path):
    """Return (crs, source_label) from a .prj file, or (None, None).

    Handles:
      * OGC WKT (PROJCS/GEOGCS/...)            -> parsed as WKT
      * Nakshatech project ([Nakshatech project] -> ProjectionSystem=<EPSG>)
    A Nakshatech *block* file (Block <name> + coords) is geometry, not CRS, and
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
        if head[:32].lstrip().upper().startswith("[NAKSHATECH"):
            with open(prj_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            m = re.search(r"ProjectionSystem\s*=\s*(\d+)", text, re.IGNORECASE)
            if m:
                epsg = int(m.group(1))
                if epsg > 0:
                    return CRS.from_epsg(epsg), f"Nakshatech PRJ ProjectionSystem={epsg}"
            return None, None
        # Reject quickly (e.g. Nakshatech block files starting with "Block ...")
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
def _normalized_delivery_stem(name):
    """Normalize classified/copy stems so 3206_CLASS-1 matches 3206."""
    from pathlib import Path
    stem = Path(str(name)).stem.casefold()
    stem = re.sub(r'[\s_\-]*(?:copy|\(?\d+\)?)+$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'[\s_\-]*class(?:[\s_\-]*\d+)?$', '', stem, flags=re.IGNORECASE)
    stem = re.sub(r'[\s_\-]*(?:copy|\(?\d+\)?)+$', '', stem, flags=re.IGNORECASE)
    return stem.strip()


def _adjacent_prj_groups(path):
    """Group PRJs next to path: exact same-stem, matching normalized-stem, and all siblings."""
    from pathlib import Path
    p = Path(str(path))
    parent = p.parent
    same = p.with_suffix(".prj")
    exact = [same] if same.is_file() else []
    try:
        siblings = sorted([q for q in parent.glob("*.prj") if q.is_file()],
                          key=lambda q: q.name.casefold())
    except Exception:
        siblings = []
    wanted = _normalized_delivery_stem(p.name)
    matching = [
        q for q in siblings
        if q not in exact and _normalized_delivery_stem(q.name) == wanted
    ]
    return exact, matching, siblings


def _unambiguous_crs(pairs):
    """Return (crs, label) if all pairs have identical CRS, else (None, None)."""
    if not pairs:
        return None, None
    first_crs, first_label = pairs[0]
    for crs, label in pairs[1:]:
        if crs is None or first_crs is None:
            return None, None
        try:
            if not first_crs.equals(crs):
                return None, None
        except Exception:
            return None, None
    return first_crs, first_label


def _resolve_dgn_spatial_ref_crs(dgn_path):
    """Parse DGNv8 OLE storage to extract embedded ECSchema SpatialRef WKT."""
    try:
        from pathlib import Path
        import zlib
        from plugins.naksha_converter import olefile
        p = Path(str(dgn_path))
        if not p.is_file():
            return None, None
        with olefile.OleFileIO(str(p)) as ole:
            entries = ole.listdir()
            streams_to_check = [e for e in entries if any("dgn" in str(part).lower() or "$" in str(part) for part in e)]
            if not streams_to_check:
                streams_to_check = entries
            crss = []
            for entry in streams_to_check:
                try:
                    raw = ole.openstream(entry).read()
                    xml_text = None
                    for off in (16, 0):
                        try:
                            decomp = zlib.decompress(raw[off:])
                            for enc in ("utf-16le", "utf-8"):
                                txt = decomp.decode(enc, errors="ignore")
                                if "<SpatialRef>" in txt:
                                    xml_text = txt
                                    break
                            if xml_text:
                                break
                        except Exception:
                            pass
                    if not xml_text:
                        for enc in ("utf-16le", "utf-8", "latin-1"):
                            try:
                                txt = raw.decode(enc, errors="ignore")
                                if "<SpatialRef>" in txt:
                                    xml_text = txt
                                    break
                            except Exception:
                                pass
                    if xml_text and "<SpatialRef>" in xml_text:
                        for part in xml_text.split("<SpatialRef>")[1:]:
                            sref = part.split("</SpatialRef>")[0].strip()
                            if sref:
                                try:
                                    c = CRS.from_user_input(sref)
                                    crss.append(c)
                                except Exception:
                                    pass
                except Exception:
                    pass
            if not crss:
                return None, None
            first = crss[0]
            for other in crss[1:]:
                if not first.equals(other):
                    return None, None
            return first, f"DGN embedded SpatialRef ({p.name})"
    except Exception:
        return None, None


def _resolve_adjacent_dgn_crs(dataset_path):
    """Resolve a directly associated or otherwise unambiguous sibling DGN."""
    try:
        from pathlib import Path
        path = Path(str(dataset_path))
        siblings = sorted(
            (p for p in path.parent.glob("*.dgn") if p.is_file()),
            key=lambda p: p.name.casefold(),
        )
        same = path.with_suffix(".dgn")
        exact = [same] if same.is_file() else []
        wanted = _normalized_delivery_stem(path.name)
        matching = [
            p for p in siblings
            if p not in exact and _normalized_delivery_stem(p.name) == wanted
        ]
        for dgn in exact:
            crs, label = _resolve_dgn_spatial_ref_crs(dgn)
            if crs is not None:
                return crs, label
        matched = []
        for dgn in matching:
            crs, label = _resolve_dgn_spatial_ref_crs(dgn)
            if crs is not None:
                matched.append((crs, label))
        crs, label = _unambiguous_crs(matched)
        if crs is not None:
            return crs, label
        if len(siblings) == 1:
            return _resolve_dgn_spatial_ref_crs(siblings[0])
        resolved = []
        for dgn in siblings:
            crs, label = _resolve_dgn_spatial_ref_crs(dgn)
            if crs is not None:
                resolved.append((crs, label))
        if len(resolved) >= 2:
            return _unambiguous_crs(resolved)
    except Exception:
        pass
    return None, None


def resolve_point_cloud_crs(path):
    """Resolve a LAS/LAZ CRS from its header or an unambiguous adjacent PRJ/DGN.

    The LAS header remains authoritative. Deliveries that omit projection
    VLRs commonly include a same-stem WKT PRJ; classified copies may instead
    use the base project stem. When several unrelated PRJs are present, no
    CRS is guessed unless all parseable definitions agree.
    """
    crs, label = resolve_laz_crs(path)
    if crs is not None:
        return crs, label

    exact, matching, siblings = _adjacent_prj_groups(path)
    if exact:
        crs, label = resolve_prj_crs(str(exact[0]))
        if crs is not None:
            return crs, f"{label} ({exact[0].name})"

    matched = []
    for prj in matching:
        crs, label = resolve_prj_crs(str(prj))
        if crs is not None:
            matched.append((crs, f"{label} ({prj.name})"))
    crs, label = _unambiguous_crs(matched)
    if crs is not None:
        return crs, label

    if len(siblings) == 1:
        crs, label = resolve_prj_crs(str(siblings[0]))
        if crs is not None:
            return crs, f"{label} ({siblings[0].name})"

    resolved = []
    for prj in siblings:
        crs, label = resolve_prj_crs(str(prj))
        if crs is not None:
            resolved.append((crs, f"{label} ({prj.name}; adjacent PRJs agree)"))
    if len(resolved) >= 2:
        crs, label = _unambiguous_crs(resolved)
        if crs is not None:
            return crs, label

    crs, label = _resolve_adjacent_dgn_crs(path)
    if crs is not None:
        return crs, label
    return None, None


def _nakshatech_block_laz(prj_path):
    """Return existing .laz/.las paths referenced by a Nakshatech block list."""
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


def resolve_snt_crs(snt_path):
    """Return (crs, source_label) for an SNT/DGN, or (None, None).

    Chain:
      1. companion DGNv8 embedded SpatialRef WKT
      2. adjacent OGC WKT .prj
      3. adjacent Nakshatech .prj -> ProjectionSystem=<EPSG>
      4. LAZ/LAS listed in the SNT's Nakshatech block list -> their VLR CRS
      5. a single differently-named .prj in the same folder (delivery convention)
    """
    try:
        from pathlib import Path
        if not snt_path:
            return None, None
        p = Path(str(snt_path))

        # DGN-embedded CRS is checked first: it comes straight from the source
        # drawing's own SpatialRef, which is more authoritative than a sidecar
        # .prj (Nakshatech project files in particular are tile-index files
        # that only sometimes carry a ProjectionSystem= code, and adjacent
        # OGC .prj files can be stale copies from an unrelated delivery step).
        crs, label = _resolve_adjacent_dgn_crs(p)
        if crs is not None:
            return crs, label

        exact, matching, siblings = _adjacent_prj_groups(p)

        if exact:
            crs, label = resolve_prj_crs(str(exact[0]))
            if crs is not None:
                return crs, f"{label} ({exact[0].name})"

        matched = []
        for prj in matching:
            crs, label = resolve_prj_crs(str(prj))
            if crs is not None:
                matched.append((crs, f"{label} ({prj.name})"))
        crs, label = _unambiguous_crs(matched)
        if crs is not None:
            return crs, label

        if len(siblings) == 1:
            crs, label = resolve_prj_crs(str(siblings[0]))
            if crs is not None:
                return crs, f"{label} ({siblings[0].name})"

        sibling_resolutions = []
        for prj in siblings:
            crs, label = resolve_prj_crs(str(prj))
            if crs is not None:
                sibling_resolutions.append(
                    (crs, f"{label} ({prj.name}; adjacent PRJs agree)")
                )
        if len(sibling_resolutions) >= 2:
            crs, label = _unambiguous_crs(sibling_resolutions)
            if crs is not None:
                return crs, label

        nakshatechs = exact + [prj for prj in matching if prj not in exact]
        if not nakshatechs and len(siblings) == 1:
            nakshatechs = list(siblings)
        laz_resolutions = []
        for prj in nakshatechs:
            for laz in _nakshatech_block_laz(prj):
                crs, label = resolve_laz_crs(laz)
                if crs is not None:
                    laz_resolutions.append(
                        (crs, f"{label} (via SNT block {os.path.basename(laz)})")
                    )
        crs, label = _unambiguous_crs(laz_resolutions)
        if crs is not None:
            return crs, label
        return None, None
    except Exception:
        return None, None


def embed_snt_crs_metadata(snt_path, *, crs=None, source_label=None):
    """Resolve (or accept) an SNT's CRS and durably write it into the file's
    own META block, so the SNT stops depending on an adjacent .prj/.dgn to be
    re-georeferenced correctly later.

    Uses the existing snt_core.snt_direct_writer metadata_updates hook, so no
    binary/geometry format change is involved - this only replaces the
    writer's placeholder ``{"hemisphere": "unknown", ...}`` "crs" block with
    real authority/code/WKT, or leaves it untouched when nothing resolves.

    Pass an explicit ``crs`` (a pyproj.CRS) when the user picked one manually
    (e.g. a future "Select CRS" prompt); otherwise this calls resolve_snt_crs()
    itself. Returns (success: bool, message: str).
    """
    try:
        from snt_core.snt_direct_writer import direct_write
    except Exception as exc:
        return False, f"snt_core.snt_direct_writer is unavailable: {exc}"

    if crs is None:
        crs, source_label = resolve_snt_crs(snt_path)
    if crs is None:
        return False, "CRS is not defined for this SNT (no DGN/.prj/LAS source carried one); nothing to embed."

    epsg = extract_epsg_code(crs)
    try:
        wkt2 = crs.to_wkt()
    except Exception:
        wkt2 = None
    crs_block = {
        "authority": "EPSG" if epsg else None,
        "code": epsg,
        "name": crs.name,
        "wkt2": wkt2,
        "horizontal_crs": crs.name,
        "vertical_crs": None,
        "compound_crs": bool(getattr(crs, "is_compound", False)),
        "source": source_label,
        "hemisphere": "unknown",  # retained for legacy readers of the old stub shape
        "estimated_lat": None,
        "confidence": "resolved" if epsg else "resolved_no_epsg",
        "notes": [],
    }
    try:
        result = direct_write(snt_path, metadata_updates={"crs": crs_block})
    except Exception as exc:
        return False, f"direct_write failed: {exc}"
    return True, f"Embedded CRS {crs.name} (EPSG:{epsg}) via {source_label}; wrote {result.output_size_bytes} bytes."


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
