import numpy as np

from gui.snt_attachment import _build_prj_spatial_index, _nearby_prj_blocks


def _block(label, x, y):
    return {"label": label, "points_2d": [
        (x, y), (x + 500.0, y), (x + 500.0, y + 500.0),
        (x, y + 500.0),
    ]}


def test_grossly_disjoint_polygon_does_not_fall_back_to_full_prj_scan():
    blocks = [_block("51000_216000", 51000.0, 216000.0)]
    index = _build_prj_spatial_index(blocks)
    polygon = np.asarray([(510000.0, 2160000.0), (515000.0, 2160000.0),
                          (515000.0, 2165000.0), (510000.0, 2165000.0)])
    assert _nearby_prj_blocks(polygon, blocks, index) == []


def test_in_range_empty_bucket_preserves_full_scan_compatibility():
    blocks = [_block("A", 51000.0, 216000.0),
              _block("B", 53000.0, 216000.0)]
    index = _build_prj_spatial_index(blocks)
    polygon = np.asarray([(52000.0, 216000.0), (52100.0, 216000.0),
                          (52100.0, 216100.0), (52000.0, 216100.0)])
    assert _nearby_prj_blocks(polygon, blocks, index) == blocks
