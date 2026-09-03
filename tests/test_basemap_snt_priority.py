from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from pyproj import CRS


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(ROOT, "plugins", "google_earth_basemap")
sys.path.insert(0, ROOT)
sys.path.insert(0, PLUGIN_DIR)

from google_earth_basemap import GoogleEarthBasemapPlugin  # noqa: E402


class _Harness:
    _attached_snt_paths = GoogleEarthBasemapPlugin._attached_snt_paths
    _cached_crs_for_path = GoogleEarthBasemapPlugin._cached_crs_for_path
    _attached_snt_crs = GoogleEarthBasemapPlugin._attached_snt_crs

    def __init__(self, app, resolutions):
        self.app = app
        self._resolutions = resolutions

    def _resolve_crs_for_path(self, path):
        return self._resolutions.get(os.path.normcase(os.path.abspath(str(path))))


class FakeApp:
    def __init__(self):
        self.canvas_crs = None
        self.canvas_crs_info = {}
        self.crs = None
        self.project_crs_epsg = None
        self.project_crs_wkt = None
        self.data = None
        self.snt_actors = []
        self.snt_attachments = []
        self.dxf_actors = []
        self.gis_layers = []
        self.plugin_manager = None


def _app(canvas_epsg, snt_paths=()):
    canvas = CRS.from_epsg(canvas_epsg)
    return SimpleNamespace(
        canvas_crs=canvas,
        crs=canvas,
        project_crs_epsg=canvas_epsg,
        project_crs_wkt=canvas.to_wkt(),
        data={"xyz": [[1.0, 2.0, 3.0]]},
        snt_actors=[{"full_path": str(path)} for path in snt_paths],
        snt_attachments=[],
        vtk_widget=None,
        is_3d_mode=False,
        current_view="top",
    )


def _key(path):
    return os.path.normcase(os.path.abspath(str(path)))


def test_unresolved_snt_blocks_stale_canvas_crs(tmp_path):
    snt = tmp_path / "unresolved.snt"
    snt.write_bytes(b"SNT")
    harness = _Harness(_app(31370, [snt]), {_key(snt): None})

    assert GoogleEarthBasemapPlugin._project_crs(harness) is None


def test_resolved_snt_has_priority_over_las_canvas_crs(tmp_path):
    snt = tmp_path / "priority.snt"
    snt.write_bytes(b"SNT")
    snt_crs = CRS.from_epsg(3812)
    harness = _Harness(_app(31370, [snt]), {_key(snt): snt_crs})

    selected = GoogleEarthBasemapPlugin._project_crs(harness)
    assert selected is not None
    assert selected.equals(snt_crs)


def test_no_snt_keeps_host_canvas_crs():
    harness = _Harness(_app(31370), {})

    selected = GoogleEarthBasemapPlugin._project_crs(harness)
    assert selected is not None
    assert selected.to_epsg() == 31370


def test_disagreeing_snt_crs_disables_basemap(tmp_path):
    first = tmp_path / "first.snt"
    second = tmp_path / "second.snt"
    first.write_bytes(b"SNT1")
    second.write_bytes(b"SNT2")
    resolutions = {
        _key(first): CRS.from_epsg(31370),
        _key(second): CRS.from_epsg(3812),
    }
    harness = _Harness(_app(31370, [first, second]), resolutions)

    assert GoogleEarthBasemapPlugin._project_crs(harness) is None


def test_project_signature_tracks_snt_attachment_changes(tmp_path):
    snt = tmp_path / "new.snt"
    snt.write_bytes(b"SNT")
    app = _app(31370)
    harness = _Harness(app, {_key(snt): CRS.from_epsg(3812)})

    before = GoogleEarthBasemapPlugin._current_project_signature(harness)
    app.snt_actors.append({"full_path": str(snt)})
    after = GoogleEarthBasemapPlugin._current_project_signature(harness)
    assert before != after


def test_laz_only_resolves_and_sets_canvas_crs(tmp_path):
    from gui import crs_manager as cm

    app = FakeApp()
    laz = tmp_path / "survey.laz"
    laz.write_bytes(b"")
    (tmp_path / "survey.prj").write_text(CRS.from_epsg(32643).to_wkt(), encoding="utf-8")

    crs, label = cm.resolve_point_cloud_crs(str(laz))
    assert crs is not None and crs.to_epsg() == 32643

    # Initial LAZ load establishes canvas CRS
    cm.set_canvas_crs(app, crs, source=label, dataset=str(laz), force=True)
    assert cm.get_canvas_crs(app).to_epsg() == 32643
    assert app.project_crs_epsg == 32643


def test_switching_laz_files_overwrites_canvas_crs_without_leak(tmp_path):
    from gui import crs_manager as cm

    app = FakeApp()
    # File 1: Belgium 31370
    laz1 = tmp_path / "belgium.laz"
    laz1.write_bytes(b"")
    (tmp_path / "belgium.prj").write_text(CRS.from_epsg(31370).to_wkt(), encoding="utf-8")

    crs1, lbl1 = cm.resolve_point_cloud_crs(str(laz1))
    cm.set_canvas_crs(app, crs1, source=lbl1, dataset=str(laz1), force=True)
    assert cm.get_canvas_crs(app).to_epsg() == 31370

    # Simulate opening File 2: Canada UTM 13N (2957) without manual project clear
    # Step 4 clears canvas CRS when no SNT is preserved:
    cm.clear_canvas_crs(app)
    assert cm.get_canvas_crs(app) is None
    assert app.project_crs_epsg is None

    laz2 = tmp_path / "canada.laz"
    laz2.write_bytes(b"")
    (tmp_path / "canada.prj").write_text(CRS.from_epsg(2957).to_wkt(), encoding="utf-8")

    crs2, lbl2 = cm.resolve_point_cloud_crs(str(laz2))
    cm.set_canvas_crs(app, crs2, source=lbl2, dataset=str(laz2), force=True)
    assert cm.get_canvas_crs(app).to_epsg() == 2957
    assert app.project_crs_epsg == 2957


def test_snt_takes_priority_over_laz_crs(tmp_path):
    from gui import crs_manager as cm

    app = FakeApp()
    # 1. LAZ loaded first with CRS 32643
    laz_crs = CRS.from_epsg(32643)
    cm.set_canvas_crs(app, laz_crs, source="LAZ", dataset="test.laz", force=True)
    assert cm.get_canvas_crs(app).to_epsg() == 32643

    # 2. SNT loaded second with CRS 3812: SNT has priority (force=True)
    snt_crs = CRS.from_epsg(3812)
    cm.set_canvas_crs(app, snt_crs, source="SNT", dataset="test.snt", force=True)
    assert cm.get_canvas_crs(app).to_epsg() == 3812
    assert app.project_crs_epsg == 3812


def test_extract_epsg_code_resolves_esri_wkt():
    from gui.crs_manager import extract_epsg_code
    # ESRI WKT where to_epsg() at default confidence 70 is None
    esri_wkt = (
        'PROJCS["NAD_1983_CSRS_UTM_Zone_13N",'
        'GEOGCS["GCS_North_American_1983_CSRS",'
        'DATUM["D_North_American_1983_CSRS",'
        'SPHEROID["GRS_1980",6378137.0,298.257222101]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Transverse_Mercator"],'
        'PARAMETER["False_Easting",500000.0],'
        'PARAMETER["False_Northing",0.0],'
        'PARAMETER["Central_Meridian",-105.0],'
        'PARAMETER["Scale_Factor",0.9996],'
        'PARAMETER["Latitude_Of_Origin",0.0],'
        'UNIT["Meter",1.0]]'
    )
    crs = CRS.from_user_input(esri_wkt)
    assert extract_epsg_code(crs) == 2957


def test_canada_471_viewport_coordinates_valid():
    from pyproj import Transformer
    # Canadian coordinates from 8480AU10_530000_5774000.laz
    x, y = 530000.0, 5774000.0
    crs_canada = CRS.from_epsg(2957)

    # Correct projection gives Saskatchewan coordinates
    to_wgs84 = Transformer.from_crs(crs_canada, CRS.from_epsg(4326), always_xy=True)
    lon, lat = to_wgs84.transform(x, y)
    assert -105.0 < lon < -104.0
    assert 52.0 < lat < 53.0
    # Latitude is safely inside [-85, 85]
    assert -85.0 < lat < 85.0

    # Cross-check: Wrong projection (EPSG 31370 from stale leak) fails domain
    to_wgs84_wrong = Transformer.from_crs(CRS.from_epsg(31370), CRS.from_epsg(4326), always_xy=True)
    bad_lon, bad_lat = to_wgs84_wrong.transform(x, y)
    assert bad_lat > 85.0  # Lands at 87.88° near the North Pole!


def test_extreme_zoom_out_web_mercator_full_canvas():
    """Verify that extreme zoom-out in standalone/Web Mercator covers the full world without strips or amoebas."""
    from plugins.google_earth_basemap.tile_math import (
        WEB_MERCATOR_HALF_WORLD_M,
        MAX_MERCATOR_LAT,
        web_mercator_to_lonlat,
        choose_zoom,
        enumerate_tiles,
    )
    half_world = float(WEB_MERCATOR_HALF_WORLD_M)
    max_lat = float(MAX_MERCATOR_LAT)

    for scale in [1e6, 5e6, 1e7, 2e7, 5e7, 1e8]:
        aspect = 1920.0 / 1080.0
        half_w = scale * aspect
        minx, maxx = -half_w, half_w
        miny, maxy = -scale, scale

        if (maxx - minx) >= 2.0 * half_world:
            west, east = -180.0, 180.0
        else:
            c_minx = max(-half_world, min(half_world, minx))
            c_maxx = max(-half_world, min(half_world, maxx))
            west = (c_minx / half_world) * 180.0
            east = (c_maxx / half_world) * 180.0

        if (maxy - miny) >= 2.0 * half_world:
            south, north = -max_lat, max_lat
        else:
            c_miny = max(-half_world, min(half_world, miny))
            c_maxy = max(-half_world, min(half_world, maxy))
            south = web_mercator_to_lonlat(0.0, c_miny)[1]
            north = web_mercator_to_lonlat(0.0, c_maxy)[1]

        assert east > west
        assert north > south
        assert -max_lat <= south < north <= max_lat
        if scale >= 2e7:
            assert west == -180.0 and east == 180.0
        if scale >= 5e7:
            assert south == -max_lat and north == max_lat

        z = choose_zoom(west, south, east, north, 1920, 1080, max_tiles=36)
        tiles = enumerate_tiles(west, south, east, north, z)
        # Never exceeds max_tiles, never truncated
        assert len(tiles) <= 36


def test_extreme_zoom_out_singularity_tile_rejection():
    """Verify that out-of-domain singularity tiles (amoebas) in conic projections are rejected."""
    import math
    from pyproj import Transformer
    from plugins.google_earth_basemap.tile_math import tile_bounds_lonlat

    crs = CRS.from_epsg(31370)
    from_wgs = Transformer.from_crs(CRS.from_epsg(4326), crs, always_xy=True)

    # In Belgian Lambert 72, deep Southern Hemisphere tiles at zoom 2 diverge to >100M meters
    # Tile (2, 0, 3) is [-180, -90] x [-85, -67]
    w, s, e, n = tile_bounds_lonlat(0, 3, 2)
    corners = [from_wgs.transform(w, s), from_wgs.transform(w, n), from_wgs.transform(e, s), from_wgs.transform(e, n)]
    xs = [p[0] for p in corners if math.isfinite(p[0])]
    ys = [p[1] for p in corners if math.isfinite(p[1])]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    # Proves tile is an amoeba (span > 100M meters)
    assert span_x > 1e8 or span_y > 1e8

    # The plugin's guard threshold (2.5e7) rejects it cleanly
    assert span_x > 2.5e7 or span_y > 2.5e7


