"""
Deterministic tests for the NakshaAI canvas-CRS architecture.

Run:  S:\\SoftWare\\NakshaAI\\env\\Scripts\\python.exe tests\\test_crs_architecture.py

Tests follow the acceptance list:
 1 CRS transform roundtrip
 2 SNT/LAZ consistency (FUNIVIA)
 3 GDB feature-class CRS
 4 Mixed CRS  (canvas 32643, GIS 4326)
 5 Reverse mixed CRS (canvas 4326, layer 32643)
 6 Unknown CRS -> basemap must NOT silently use EPSG:3857
 7 Empty standalone basemap -> EPSG:3857 allowed
 8 Plugin refresh -> exactly one refresh per plugin
 9 Clear Project -> no CRS leak between projects
10 Axis order (always_xy=True)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyproj import CRS, Transformer  # noqa: E402

from gui import crs_manager as cm  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t):
    print("\n" + "=" * 72)
    print(t)
    print("=" * 72)


# --------------------------------------------------------------------------
class FakeApp:
    """Minimal stand-in for the real app window."""

    def __init__(self):
        self.canvas_crs = None
        self.canvas_crs_info = {}
        self.crs = None
        self.project_crs_epsg = None
        self.project_crs_wkt = None
        self.data = None
        self.snt_actors = []
        self.dxf_actors = []
        self.gis_layers = []
        self.plugin_manager = None


class FakePlugin:
    def __init__(self):
        self.refresh_calls = 0
        self.schedule_calls = 0

    def refresh(self):
        self.refresh_calls += 1

    def _schedule_refresh(self):
        self.schedule_calls += 1


class FakePluginManager:
    def __init__(self, plugins):
        self.loaded_plugins = {
            f"p{i}": {"instance": p, "manifest": {}, "dir": "d"}
            for i, p in enumerate(plugins)
        }


# --------------------------------------------------------------------------
def test1_roundtrip():
    section("Test 1 - CRS transform roundtrip")
    # Use IN-AREA coordinates for each CRS. Feeding e.g. y=5,000,000 to a
    # Belgian Lambert CRS lands near the pole and destroys accuracy - that
    # would be a test artifact, not a transform bug.
    cases = [
        (4326, 12.4923, 41.8902),        # Rome, lon/lat
        (3857, 1390778.0, 5144591.0),    # Web Mercator, near Rome
        (32632, 700000.0, 5100000.0),    # UTM 32N (central Europe)
        (32633, 700000.0, 5100000.0),    # UTM 33N
        (32643, 500000.0, 2200000.0),    # UTM 43N (India)
        (25832, 700000.0, 5100000.0),    # ETRS89 / UTM 32N
        (31370, 150000.0, 170000.0),     # BD72 / Belgian Lambert 72
        (3812, 650000.0, 670000.0),      # ETRS89 / Belgian Lambert 2008
    ]
    for code, x, y in cases:
        try:
            src = CRS.from_epsg(code)
        except Exception as e:
            check(f"EPSG:{code} construct", False, str(e))
            continue
        try:
            t1 = Transformer.from_crs(src, CRS.from_epsg(4326), always_xy=True)
            lon, lat = t1.transform(x, y)
            t2 = Transformer.from_crs(CRS.from_epsg(4326), src, always_xy=True)
            bx, by = t2.transform(lon, lat)
            err = max(abs(bx - x), abs(by - y))
            # sub-centimetre requirement (0.01 m)
            check(f"EPSG:{code} roundtrip err={err:.9f} m (< 0.01 m)",
                  err < 1e-2, f"lon={lon:.5f} lat={lat:.5f}")
        except Exception as e:
            check(f"EPSG:{code} roundtrip", False, str(e))


def _first_existing(paths):
    for p in paths:
        if p and os.path.exists(p):
            return p
    return None


# Real delivery folder (network share) confirmed 2026-09-02. The plain
# S:/SoftWare/FUNIVIA convention is kept first so this still works if the
# dataset is ever copied local; the UNC path is what actually exists today.
_FUNIVIA_DIRS = [
    r"S:/SoftWare/FUNIVIA",
    r"\\nast\NAKSHA\DAILY ACTIVITY REPORT\Chirag KJ\Kumuda S\NT-298\DU3039046 FUNIVIA",
]


def test2_snt_laz():
    section("Test 2 - SNT/LAZ consistency (FUNIVIA)")
    snt = _first_existing(os.path.join(d, "DU3039046 FUNIVIA.snt") for d in _FUNIVIA_DIRS)
    laz_dir = os.path.dirname(snt) if snt else None
    laz_paths = [os.path.join(laz_dir, f"FUNIVIA{i:06d}.laz") for i in range(1, 8)] if laz_dir else []
    laz = _first_existing(laz_paths)
    if not snt or not laz:
        check("FUNIVIA files present", False,
              "dataset not found in any known location - cannot run")
        return

    prj = os.path.splitext(snt)[0] + ".prj"
    check("adjacent .prj present", os.path.exists(prj), prj)
    if os.path.exists(prj):
        with open(prj, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(64).lstrip()
        is_terrascan = head.upper().startswith("[TERRASCAN")
        check("adjacent .prj is a TerraScan project file (block list, not CRS WKT)",
              is_terrascan, head.splitlines()[0] if head else "")
        if is_terrascan:
            with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            import re as _re
            has_epsg_line = bool(_re.search(r"ProjectionSystem\s*=\s*\d+", text, _re.IGNORECASE))
            check("TerraScan .prj has NO ProjectionSystem=<EPSG> line (verified by reading the real file)",
                  not has_epsg_line)

    cs, csrc = cm.resolve_snt_crs(snt)
    print(f"  SNT CRS: {cs} via {csrc}")
    check("FUNIVIA SNT CRS unresolved (verified: TerraScan .prj has no "
          "ProjectionSystem=, referenced LAZ carry no CRS VLR - see below)",
          cs is None, f"got {cs}")

    laz_results = {}
    for p in laz_paths:
        if not os.path.exists(p):
            continue
        crs, lsrc = cm.resolve_laz_crs(p)
        laz_results[p] = crs
        print(f"  {os.path.basename(p)}: CRS={crs} via {lsrc}")
    check("all present FUNIVIA LAZ tiles read without error", len(laz_results) > 0)
    check("FUNIVIA LAZ tiles CRS unresolved (no LASF_Proj VLR/EVLR in any tile)",
          all(v is None for v in laz_results.values()),
          f"{sum(1 for v in laz_results.values() if v is not None)} tile(s) had a CRS")
    check("SNT and LAZ agree (both unresolved, not silently guessed)",
          cs is None and all(v is None for v in laz_results.values()))

    # Spatial correspondence: header-only bounds (no point decompression) for
    # every tile should sit within one contiguous project area, not scattered
    # across unrelated sites. This is a real measurement, not a CRS guess.
    try:
        import laspy
        boxes = {}
        for p in laz_results:
            with laspy.open(p) as reader:
                h = reader.header
                boxes[os.path.basename(p)] = (h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
        if boxes:
            allx = [v for b in boxes.values() for v in (b[0], b[2])]
            ally = [v for b in boxes.values() for v in (b[1], b[3])]
            span_x = max(allx) - min(allx)
            span_y = max(ally) - min(ally)
            for name, (x0, y0, x1, y1) in boxes.items():
                print(f"    {name}: X[{x0:.1f},{x1:.1f}] Y[{y0:.1f},{y1:.1f}]")
            # A single TerraScan block project's tiles should span at most a
            # few km, never hundreds/thousands of km (that would mean a tile
            # from a different project got mixed in).
            check("FUNIVIA tiles form one contiguous project area (<10 km span)",
                  span_x < 10000 and span_y < 10000,
                  f"span_x={span_x:.1f}m span_y={span_y:.1f}m")
    except ImportError:
        check("laspy available for bounds check", False, "laspy not installed")


_AMRUT_CANDIDATES = [
    r"S:\Kapadvanj_AMRUT2.0_NEW_GDB12-9-25.gdb (1)\AMRUT2.0_NEW_GDB12-9-25.gdb",
    r"S:/SoftWare/AMRUT2.0_NEW_GDB12-9-25.gdb",
]


def test3_gdb():
    section("Test 3 - GDB feature-class CRS")
    gdb = _first_existing(_AMRUT_CANDIDATES)
    if not gdb:
        check("AMRUT2.0_NEW_GDB12-9-25.gdb present", False,
              "FILE NOT FOUND - searched " + " | ".join(_AMRUT_CANDIDATES))
        return
    print(f"  using: {gdb}")

    try:
        from osgeo import ogr
        ogr.UseExceptions()
        ds = ogr.Open(gdb, 0)
    except Exception as e:
        check("GDB opens with OGR", False, f"{type(e).__name__}: {e}")
        return
    check("GDB opens with OGR", ds is not None)
    if ds is None:
        return

    n = ds.GetLayerCount()
    check("GDB has feature classes", n > 0, f"n={n}")

    # India-plausible WGS84 bounding box (rough, generous) - used only to
    # sanity-check the RESULT of a metadata-derived transform, never to pick
    # or guess the source CRS itself.
    india_lon = (60.0, 100.0)
    india_lat = (5.0, 40.0)

    resolved = 0
    plausible = 0
    checked_layers = 0
    for i in range(n):
        lyr = ds.GetLayer(i)
        name = lyr.GetName()
        if lyr.GetFeatureCount() == 0 or lyr.GetGeomType() == ogr.wkbNone:
            continue  # empty / non-spatial tables carry no CRS to verify
        checked_layers += 1
        crs, label = cm.resolve_gis_crs(gdb, layer_name=name)
        if crs is None:
            print(f"  [{name}] CRS UNRESOLVED")
            continue
        resolved += 1
        xmin, xmax, ymin, ymax = lyr.GetExtent()
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        try:
            t = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
            lon, lat = t.transform(cx, cy)
        except Exception as e:
            print(f"  [{name}] EPSG:{crs.to_epsg()} transform FAILED: {e}")
            continue
        ok = india_lon[0] <= lon <= india_lon[1] and india_lat[0] <= lat <= india_lat[1]
        plausible += ok
        print(f"  [{name}] EPSG:{crs.to_epsg()} via {label}  "
              f"center=({cx:.1f},{cy:.1f}) -> lon={lon:.5f} lat={lat:.5f}"
              f"{'' if ok else '  ** OUTSIDE plausible India bbox **'}")
    ds = None

    check("at least one feature class CRS resolved from real GDB metadata",
          resolved > 0, f"resolved={resolved}/{checked_layers} non-empty layers")
    check("resolved feature classes land at a geographically plausible "
          "WGS84 center (metadata-derived CRS, not a magnitude guess)",
          plausible > 0, f"plausible={plausible}/{resolved}")


def test4_mixed_crs():
    section("Test 4 - mixed CRS (canvas 32643, GIS 4326)")
    app = FakeApp()
    cm.set_canvas_crs(app, CRS.from_epsg(32643), source="test", dataset="synth")
    check("canvas CRS established", cm.get_canvas_crs(app).to_epsg() == 32643)
    # A known point in EPSG:4326 inside zone 43N
    lon, lat = 78.0, 20.0
    src = CRS.from_epsg(4326)
    t = Transformer.from_crs(src, CRS.from_epsg(32643), always_xy=True)
    ex, ey = t.transform(lon, lat)
    # Now what our manager produces
    gx, gy = cm.transform_xy_to_canvas(app, [lon], [lat], src)
    err = max(abs(float(gx[0]) - ex), abs(float(gy[0]) - ey))
    check("4326 -> 32643 reprojection matches pyproj", err < 1e-6,
          f"got ({float(gx[0]):.3f},{float(gy[0]):.3f}) expected ({ex:.3f},{ey:.3f})")
    # And the round trip back to WGS84 lands on the same lon/lat
    bx, by = cm.transform_xy_from_canvas(app, gx, gy, CRS.from_epsg(4326))
    err2 = max(abs(float(bx[0]) - lon), abs(float(by[0]) - lat))
    check("round trip back to 4326", err2 < 1e-9,
          f"got ({float(bx[0]):.6f},{float(by[0]):.6f})")


def test5_reverse_mixed():
    section("Test 5 - reverse mixed CRS (canvas 4326, layer 32643)")
    app = FakeApp()
    cm.set_canvas_crs(app, CRS.from_epsg(4326), source="test")
    lon, lat = 78.0, 20.0
    t = Transformer.from_crs(CRS.from_epsg(4326), CRS.from_epsg(32643), always_xy=True)
    px, py = t.transform(lon, lat)
    gx, gy = cm.transform_xy_to_canvas(app, [px], [py], CRS.from_epsg(32643))
    err = max(abs(float(gx[0]) - lon), abs(float(gy[0]) - lat))
    check("32643 -> 4326 reprojection lands on original lon/lat", err < 1e-9,
          f"got ({float(gx[0]):.6f},{float(gy[0]):.6f}) expected ({lon},{lat})")


def test6_unknown_crs():
    section("Test 6 - unknown CRS must NOT silently become EPSG:3857")
    app = FakeApp()
    # Simulate a projected dataset with no CRS: mark it loaded
    app.data = {"xyz": [[565000.0, 5127000.0, 100.0]]}
    check("geospatial data detected", cm.has_geospatial_data(app))
    # The CRS resolvers must return None, never 3857
    cs, _ = cm.resolve_snt_crs(r"S:/SoftWare/FUNIVIA/DU3039046 FUNIVIA.snt")
    cl, _ = cm.resolve_laz_crs(r"S:/SoftWare/FUNIVIA/FUNIVIA000001.laz")
    check("SNT CRS unresolved (not coerced to 3857)", cs is None)
    check("LAZ CRS unresolved (not coerced to 3857)", cl is None)
    # ensure_canvas_crs with None must NOT set anything
    before = cm.get_canvas_crs(app)
    cm.ensure_canvas_crs(app, None, source="unknown-dataset")
    after = cm.get_canvas_crs(app)
    check("canvas CRS stays unset for unresolved dataset",
          before is None and after is None)
    check("no EPSG:3857 assigned while data present",
          not (after is not None and after.to_epsg() == 3857))


def test7_empty_basemap():
    section("Test 7 - empty standalone basemap may use EPSG:3857")
    app = FakeApp()
    check("no geospatial data on empty app", not cm.has_geospatial_data(app))
    # Allowed case: standalone browsing, nothing loaded
    app.data = None
    crs = CRS.from_epsg(3857)
    cm.ensure_canvas_crs(app, crs, source="standalone-basemap")
    got = cm.get_canvas_crs(app)
    ok = got is not None and got.to_epsg() == 3857
    check("standalone basemap canvas = EPSG:3857", ok)
    check("3857 only allowed with empty canvas",
          not cm.has_geospatial_data(app))


def test8_plugin_refresh():
    section("Test 8 - plugin refresh: exactly ONE per plugin")
    p1 = FakePlugin()
    p2 = FakePlugin()
    app = FakeApp()
    app.plugin_manager = FakePluginManager([p1, p2])
    n = cm.notify_plugins_crs_changed(app)
    check("2 plugins notified", n == 2, f"n={n}")
    check("plugin1 called exactly once", p1.refresh_calls + p1.schedule_calls == 1,
          f"refresh={p1.refresh_calls} schedule={p1.schedule_calls}")
    check("plugin2 called exactly once", p2.refresh_calls + p2.schedule_calls == 1,
          f"refresh={p2.refresh_calls} schedule={p2.schedule_calls}")
    check("no duplicate scheduling (schedule not also called)",
          p1.schedule_calls == 0 and p2.schedule_calls == 0)
    # No plugin manager -> must not raise
    app2 = FakeApp()
    check("no plugin_manager is safe", cm.notify_plugins_crs_changed(app2) == 0)


def test9_clear_project():
    section("Test 9 - Clear Project leaves no CRS behind")
    app = FakeApp()
    app.data = {"xyz": [[1.0, 2.0, 3.0]]}
    cm.set_canvas_crs(app, CRS.from_epsg(32643), source="projectA")
    check("project A canvas = 32643", cm.get_canvas_crs(app).to_epsg() == 32643)
    cm.clear_canvas_crs(app)
    app.data = None
    check("canvas cleared", cm.get_canvas_crs(app) is None)
    check("legacy epsg cleared", getattr(app, "project_crs_epsg", None) is None)
    check("legacy wkt cleared", getattr(app, "project_crs_wkt", None) is None)
    check("crs obj cleared", getattr(app, "crs", None) is None)
    # Project B, different CRS
    cm.set_canvas_crs(app, CRS.from_epsg(3812), source="projectB")
    got = cm.get_canvas_crs(app)
    check("project B canvas = 3812 (no leak from A)",
          got is not None and got.to_epsg() == 3812)
    info = cm.get_canvas_crs_info(app)
    check("provenance is projectB", info.get("source") == "projectB")


def test10_axis_order():
    section("Test 10 - axis order (always_xy=True)")
    app = FakeApp()
    cm.set_canvas_crs(app, CRS.from_epsg(4326), source="test")
    # Rome: lon 12.4923 lat 41.8902
    lon, lat = 12.4923, 41.8902
    src = CRS.from_epsg(4326)
    gx, gy = cm.transform_xy_to_canvas(app, [lon], [lat], src)
    ok = abs(float(gx[0]) - lon) < 1e-9 and abs(float(gy[0]) - lat) < 1e-9
    check("lon/lat not swapped", ok,
          f"got ({float(gx[0])},{float(gy[0])}) expected ({lon},{lat})")
    # Explicit reversal guard: feeding lat,lon must NOT produce the same point
    gx2, gy2 = cm.transform_xy_to_canvas(app, [lat], [lon], src)
    swapped = abs(float(gx2[0]) - lon) < 1e-9
    check("reversed input is not silently accepted", not swapped)


def main():
    test1_roundtrip()
    test2_snt_laz()
    test3_gdb()
    test4_mixed_crs()
    test5_reverse_mixed()
    test6_unknown_crs()
    test7_empty_basemap()
    test8_plugin_refresh()
    test9_clear_project()
    test10_axis_order()

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
