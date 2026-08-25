import json

import laspy
import numpy as np
import pytest
from shapely.geometry import box

from core.ground_parity import (
    PointSetMismatchError,
    RecordOrderMismatchError,
    Zone,
    ZoneSet,
    compare_aligned_arrays,
    compare_las_files,
    write_audit_outputs,
)


def _write_las(path, xyz, classification):
    header = laspy.LasHeader(point_format=3, version="1.2")
    header.scales = np.array([0.01, 0.01, 0.01])
    header.offsets = np.array([0.0, 0.0, 0.0])
    cloud = laspy.LasData(header)
    xyz = np.asarray(xyz, dtype=np.float64)
    cloud.x = xyz[:, 0]
    cloud.y = xyz[:, 1]
    cloud.z = xyz[:, 2]
    cloud.classification = np.asarray(classification, dtype=np.uint8)
    cloud.write(path)


def test_aligned_metrics_distinguish_over_and_underclassification():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [3.0, 0.0, 0.0],
        ]
    )
    accumulator = compare_aligned_arrays(
        original_class=np.array([1, 1, 1, 1]),
        reference_class=np.array([2, 2, 1, 1]),
        candidate_class=np.array([2, 1, 2, 1]),
        xyz=xyz,
        cell_size=2.0,
    )

    overall = accumulator.to_dict()["overall"]
    assert overall["compared"] == 4
    assert overall["true_ground"] == 1
    assert overall["true_nonground"] == 1
    assert overall["overclassified_ground"] == 1
    assert overall["underclassified_ground"] == 1
    assert overall["ground_precision"] == pytest.approx(0.5)
    assert overall["ground_recall"] == pytest.approx(0.5)
    assert overall["ground_f1"] == pytest.approx(0.5)
    assert overall["ground_iou"] == pytest.approx(1 / 3)
    assert overall["binary_ground_agreement"] == pytest.approx(0.5)
    assert sum(cell.compared for cell in accumulator.cells.values()) == 4


def test_original_class_filter_and_polygon_zone_breakdowns():
    xyz = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [10.0, 0.0, 0.0],
        ]
    )
    zones = ZoneSet(
        [
            Zone("west", box(-1.0, -1.0, 2.0, 1.0)),
            Zone("east", box(9.0, -1.0, 11.0, 1.0)),
        ]
    )
    accumulator = compare_aligned_arrays(
        original_class=np.array([0, 1, 6]),
        reference_class=np.array([2, 2, 6]),
        candidate_class=np.array([2, 1, 2]),
        xyz=xyz,
        eligible_original_classes=(0, 1),
        zones=zones,
    )

    metrics = accumulator.to_dict()
    assert metrics["overall"]["compared"] == 2
    assert metrics["excluded_points"] == 1
    assert set(metrics["by_original_class"]) == {"0", "1"}
    assert metrics["by_zone"]["west"]["compared"] == 2
    assert "east" not in metrics["by_zone"]


def test_record_order_alignment_streams_matching_las_files(tmp_path):
    xyz = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    )
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, xyz, [1, 1, 1])
    _write_las(reference, xyz, [2, 2, 1])
    _write_las(candidate, xyz, [2, 1, 1])

    report = compare_las_files(
        original,
        reference,
        candidate,
        alignment="order",
        chunk_size=2,
    )

    assert report["configuration"]["used_alignment"] == "order"
    assert report["metrics"]["overall"]["compared"] == 3
    assert report["metrics"]["overall"]["underclassified_ground"] == 1


def test_order_alignment_detects_reordered_output(tmp_path):
    xyz = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    )
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, xyz, [1, 1, 1])
    _write_las(reference, xyz, [2, 2, 1])
    order = np.array([2, 0, 1])
    _write_las(candidate, xyz[order], np.array([1, 2, 1])[order])

    with pytest.raises(RecordOrderMismatchError, match="identity"):
        compare_las_files(
            original,
            reference,
            candidate,
            alignment="order",
            chunk_size=2,
        )


def test_auto_alignment_falls_back_to_disk_backed_identity_sort(tmp_path):
    xyz = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]
    )
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, xyz, [1, 1, 1])
    _write_las(reference, xyz, [2, 2, 1])
    order = np.array([2, 0, 1])
    candidate_classes = np.array([2, 1, 1])
    _write_las(candidate, xyz[order], candidate_classes[order])

    report = compare_las_files(
        original,
        reference,
        candidate,
        alignment="auto",
        chunk_size=2,
        temp_directory=tmp_path,
    )

    assert report["configuration"]["used_alignment"] == "identity"
    assert report["configuration"]["record_order_failure"]
    assert report["metrics"]["overall"]["underclassified_ground"] == 1
    assert report["metrics"]["overall"]["overclassified_ground"] == 0


def test_identity_alignment_compares_exact_xyz_duplicates_as_multiset(tmp_path):
    xyz = np.array(
        [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [1.0, 0.0, 0.0]]
    )
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, xyz, [1, 1, 1])
    _write_las(reference, xyz, [2, 1, 1])
    _write_las(candidate, xyz[[1, 0, 2]], [1, 2, 1])

    report = compare_las_files(
        original,
        reference,
        candidate,
        alignment="identity",
        chunk_size=2,
        temp_directory=tmp_path,
    )

    overall = report["metrics"]["overall"]
    assert overall["exact_class_agreement"] == 1.0
    assert overall["binary_ground_agreement"] == 1.0
    assert report["metrics"]["ambiguous_duplicate_points"] == 2
    # Original-class attribution is deliberately omitted for ambiguous points.
    assert report["metrics"]["by_original_class"]["1"]["compared"] == 1


def test_identity_alignment_rejects_different_point_sets(tmp_path):
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, [[0, 0, 0], [1, 0, 0]], [1, 1])
    _write_las(reference, [[0, 0, 0], [1, 0, 0]], [2, 1])
    _write_las(candidate, [[0, 0, 0], [9, 0, 0]], [2, 1])

    with pytest.raises(PointSetMismatchError, match="same quantized"):
        compare_las_files(
            original,
            reference,
            candidate,
            alignment="identity",
            temp_directory=tmp_path,
        )


def test_audit_outputs_include_json_markdown_cells_and_heatmap(tmp_path):
    xyz = np.array([[0.0, 0.0, 0.0], [5.0, 0.0, 0.0]])
    original = tmp_path / "original.las"
    reference = tmp_path / "reference.las"
    candidate = tmp_path / "candidate.las"
    _write_las(original, xyz, [1, 1])
    _write_las(reference, xyz, [2, 1])
    _write_las(candidate, xyz, [1, 2])
    report = compare_las_files(original, reference, candidate)

    output = tmp_path / "report"
    paths = write_audit_outputs(output, report)

    assert set(paths) == {"json", "markdown", "cells_csv", "heatmap"}
    assert all(path.is_file() for path in paths.values())
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["metrics"]["overall"]["overclassified_ground"] == 1
    assert payload["metrics"]["overall"]["underclassified_ground"] == 1
    assert "_cells" not in payload
    assert "Binary ground agreement" in paths["markdown"].read_text(
        encoding="utf-8"
    )

