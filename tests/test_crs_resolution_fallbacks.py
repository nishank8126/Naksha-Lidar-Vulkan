from io import BytesIO
import zlib

from pyproj import CRS

from gui.crs_manager import resolve_point_cloud_crs, resolve_snt_crs


def _write_wkt(path, epsg):
    path.write_text(CRS.from_epsg(epsg).to_wkt(), encoding="utf-8")


def _mock_dgn_spatial_refs(monkeypatch, *crss):
    xml = "".join(
        f"<FeatureDefinition><SpatialRef>{crs.to_wkt(version='WKT1_ESRI')}"
        "</SpatialRef></FeatureDefinition>"
        for crs in crss
    ).encode("utf-16le")
    stream = (b"\x00" * 16) + zlib.compress(xml)

    class FakeOle:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def listdir(self):
            return [["Dgn^Nm", "$1"]]

        def openstream(self, _parts):
            return BytesIO(stream)

    from plugins.naksha_converter import olefile
    monkeypatch.setattr(olefile, "OleFileIO", lambda _path: FakeOle())


def test_point_cloud_uses_same_stem_prj_when_header_has_no_crs(tmp_path):
    laz = tmp_path / "survey.laz"
    laz.write_bytes(b"")
    _write_wkt(tmp_path / "survey.prj", 32643)

    crs, source = resolve_point_cloud_crs(str(laz))

    assert crs is not None and crs.to_epsg() == 32643
    assert "survey.prj" in source


def test_classified_snt_matches_base_prj_among_multiple_files(tmp_path):
    snt = tmp_path / "3206_CLASS-1.snt"
    snt.write_bytes(b"")
    _write_wkt(tmp_path / "3206.prj", 32643)
    _write_wkt(tmp_path / "unrelated.prj", 4326)

    crs, source = resolve_snt_crs(str(snt))

    assert crs is not None and crs.to_epsg() == 32643
    assert "3206.prj" in source


def test_multi_prj_folder_with_conflicting_crs_stays_unresolved(tmp_path):
    snt = tmp_path / "survey.snt"
    snt.write_bytes(b"")
    _write_wkt(tmp_path / "alpha.prj", 32643)
    _write_wkt(tmp_path / "beta.prj", 4326)

    crs, source = resolve_snt_crs(str(snt))

    assert crs is None
    assert source is None


def test_multi_prj_folder_is_accepted_when_all_crs_definitions_agree(tmp_path):
    snt = tmp_path / "survey.snt"
    snt.write_bytes(b"")
    _write_wkt(tmp_path / "alpha.prj", 32643)
    _write_wkt(tmp_path / "beta.prj", 32643)

    crs, source = resolve_snt_crs(str(snt))

    assert crs is not None and crs.to_epsg() == 32643
    assert "adjacent PRJs agree" in source


def test_snt_uses_companion_dgn_embedded_spatial_ref(monkeypatch, tmp_path):
    snt = tmp_path / "survey.snt"
    snt.write_bytes(b"")
    (tmp_path / "survey.dgn").write_bytes(b"placeholder")
    (tmp_path / "survey.prj").write_text(
        "[TerraScan project]\nBlock tile.laz\n0 0\n1 0\n1 1\n0 0\n",
        encoding="utf-8",
    )
    _mock_dgn_spatial_refs(monkeypatch, CRS.from_epsg(2957))

    crs, source = resolve_snt_crs(str(snt))

    assert crs is not None and crs.to_epsg() == 2957
    assert source == "DGN embedded SpatialRef (survey.dgn)"


def test_laz_only_uses_single_companion_dgn_spatial_ref(monkeypatch, tmp_path):
    laz = tmp_path / "tile.laz"
    laz.write_bytes(b"")
    (tmp_path / "delivery.dgn").write_bytes(b"placeholder")
    _mock_dgn_spatial_refs(monkeypatch, CRS.from_epsg(2957))

    crs, source = resolve_point_cloud_crs(str(laz))

    assert crs is not None and crs.to_epsg() == 2957
    assert source == "DGN embedded SpatialRef (delivery.dgn)"


def test_conflicting_dgn_spatial_refs_stay_unresolved(monkeypatch, tmp_path):
    snt = tmp_path / "survey.snt"
    snt.write_bytes(b"")
    (tmp_path / "survey.dgn").write_bytes(b"placeholder")
    _mock_dgn_spatial_refs(
        monkeypatch,
        CRS.from_epsg(2957),
        CRS.from_epsg(32632),
    )

    crs, source = resolve_snt_crs(str(snt))

    assert crs is None
    assert source is None
