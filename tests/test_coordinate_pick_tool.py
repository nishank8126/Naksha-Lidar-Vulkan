from gui.coordinate_pick_tool import format_coordinate, to_wgs84_lonlat


class _FakeGeographicCRS:
    is_geographic = True


class _FakeProjectedCRS:
    is_geographic = False


def test_projected_crs_uses_meters_xyz():
    text = format_coordinate(583245.1234, 4519823.4567, 812.3401, _FakeProjectedCRS())
    assert text == "X: 583245.123  Y: 4519823.457  Z: 812.340"


def test_geographic_crs_uses_lon_lat():
    text = format_coordinate(77.594566, 12.971599, 812.34, _FakeGeographicCRS())
    assert text == "Lon: 77.594566  Lat: 12.971599  Z: 812.34"


def test_missing_crs_falls_back_to_xyz_and_flags_local():
    text = format_coordinate(1.0, 2.0, 3.0, None)
    assert text == "X: 1.000  Y: 2.000  Z: 3.000  (local, CRS unknown)"


def test_utm_projects_to_plausible_wgs84():
    from pyproj import CRS
    lon, lat = to_wgs84_lonlat(302119.028, 2547779.079, CRS.from_epsg(32643))
    assert abs(lon - 73.069) < 0.01
    assert abs(lat - 23.027) < 0.01


def test_compound_crs_uses_horizontal_component():
    from pyproj.crs import CRS, CompoundCRS
    compound = CompoundCRS(
        name="UTM43N + EGM2008 height",
        components=[CRS.from_epsg(32643), CRS.from_epsg(3855)],
    )
    lon, lat = to_wgs84_lonlat(302119.028, 2547779.079, compound)
    assert abs(lon - 73.069) < 0.01
    assert abs(lat - 23.027) < 0.01


def test_no_crs_cannot_be_projected():
    assert to_wgs84_lonlat(1.0, 2.0, None) is None


if __name__ == "__main__":
    test_projected_crs_uses_meters_xyz()
    test_geographic_crs_uses_lon_lat()
    test_missing_crs_falls_back_to_xyz_and_flags_local()
    test_utm_projects_to_plausible_wgs84()
    test_compound_crs_uses_horizontal_component()
    test_no_crs_cannot_be_projected()
    print("OK")
