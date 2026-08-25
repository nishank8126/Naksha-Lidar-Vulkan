import numpy as np
import pytest

from gui.elevation_models import (
    ElevationModelCancelled,
    ElevationModelError,
    build_elevation_model,
    render_elevation_relief_rgba,
    write_elevation_geotiff,
)


def _sample_points():
    # Two cells across a 2 x 2 north-up grid.  Cell 0 contains both ground and
    # an above-ground return; cell 3 has an above-ground return only.
    xyz = np.array(
        [
            [100.1, 201.9, 10.0],
            [100.2, 201.8, 9.0],
            [100.3, 201.7, 20.0],
            [101.1, 200.9, 15.0],
        ],
        dtype=np.float64,
    )
    classification = np.array([2, 2, 6, 5], dtype=np.uint8)
    return xyz, classification


def test_dsm_uses_highest_return_in_each_cell():
    xyz, classification = _sample_points()
    model = build_elevation_model(
        xyz,
        classification,
        "DSM",
        resolution=1.0,
        coverage_buffer=0.0,
    )
    assert model.elevation.shape == (2, 2)
    assert model.elevation[0, 0] == pytest.approx(20.0)
    assert model.elevation[1, 1] == pytest.approx(15.0)
    assert np.isnan(model.elevation[0, 1])
    assert np.isnan(model.elevation[1, 0])


def test_dtm_averages_selected_ground_returns_and_fills_coverage():
    xyz, classification = _sample_points()
    model = build_elevation_model(
        xyz,
        classification,
        "DTM",
        resolution=1.0,
        ground_classes=(2,),
        coverage_buffer=0.0,
    )
    assert model.elevation[0, 0] == pytest.approx(9.5)
    # The second occupied survey cell contains no ground point, so the nearest
    # observed ground elevation is interpolated into the shared footprint.
    assert model.elevation[1, 1] == pytest.approx(9.5)
    assert np.isnan(model.elevation[0, 1])
    assert np.isnan(model.elevation[1, 0])


def test_dtm_and_dsm_use_the_same_point_cloud_coverage_mask():
    xyz, classification = _sample_points()
    dtm = build_elevation_model(
        xyz, classification, "DTM", resolution=1.0, coverage_buffer=1.0
    )
    dsm = build_elevation_model(
        xyz, classification, "DSM", resolution=1.0, coverage_buffer=1.0
    )
    np.testing.assert_array_equal(
        np.isfinite(dtm.elevation), np.isfinite(dsm.elevation)
    )


def test_dtm_requires_points_in_selected_ground_classes():
    xyz, classification = _sample_points()
    with pytest.raises(ElevationModelError, match="No points use"):
        build_elevation_model(
            xyz,
            classification,
            "DTM",
            resolution=1.0,
            ground_classes=(42,),
        )


def test_grid_safety_limit_suggests_coarser_resolution():
    xyz = np.array([[0.0, 0.0, 1.0], [100.0, 100.0, 2.0]])
    with pytest.raises(ElevationModelError, match="safety limit"):
        build_elevation_model(
            xyz, np.array([2, 2]), "DSM", resolution=0.1, max_cells=100
        )


def test_build_honors_cancellation():
    xyz, classification = _sample_points()
    with pytest.raises(ElevationModelCancelled):
        build_elevation_model(
            xyz,
            classification,
            "DSM",
            cancelled=lambda: True,
        )


def test_relief_renderer_adds_edge_shading_and_transparent_nodata():
    elevation = np.full((9, 9), 100.0, dtype=np.float32)
    elevation[3:6, 3:6] = 104.0
    valid = np.ones_like(elevation, dtype=bool)
    valid[0, 0] = False

    flat = render_elevation_relief_rgba(
        elevation, valid, pixel_size=0.5, vertical_exaggeration=0.0
    )
    shaded = render_elevation_relief_rgba(
        elevation, valid, pixel_size=0.5, vertical_exaggeration=2.0
    )

    assert shaded.shape == (9, 9, 4)
    np.testing.assert_array_equal(shaded[0, 0], [0, 0, 0, 0])
    assert shaded[4, 4, 3] == 255
    assert np.any(shaded[..., :3] != flat[..., :3])


def test_geotiff_export_matches_sample_product_shape_and_metadata(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    xyz, classification = _sample_points()
    model = build_elevation_model(
        xyz,
        classification,
        "DSM",
        resolution=0.5,
        coverage_buffer=0.0,
        crs="EPSG:32643",
    )
    output = tmp_path / "surface_DSM.tif"
    write_elevation_geotiff(model, output)

    with rasterio.open(output) as src:
        assert src.count == 1
        assert src.dtypes == ("float32",)
        assert src.nodata == pytest.approx(-32767.0)
        assert src.crs.to_epsg() == 32643
        assert src.res == pytest.approx((0.5, 0.5))
        assert src.tags()["SURFACE_MODEL"] == "DSM"
        assert src.descriptions == ("DSM elevation",)
        data = src.read(1)
        assert np.any(data == src.nodata)
        assert float(data[data != src.nodata].max()) == pytest.approx(20.0)
