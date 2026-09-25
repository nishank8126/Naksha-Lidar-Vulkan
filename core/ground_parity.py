"""Out-of-core Nakshatech/Naksha ground-classification comparison.

The module deliberately does not import any Qt or application state.  It can be
used by tests, a command-line audit, or a future GUI wrapper without loading the
three point clouds into the Naksha renderer.
"""

from __future__ import annotations

import csv
import json
import math
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import laspy
import numpy as np


class GroundParityError(RuntimeError):
    """Base exception for an invalid or incomparable audit input."""


class PointSetMismatchError(GroundParityError):
    """The inputs do not contain the same physical point set."""


class RecordOrderMismatchError(PointSetMismatchError):
    """The point sets may match, but their record order does not."""


def _safe_ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return float(numerator) / float(denominator)


@dataclass
class ParityCounts:
    compared: int = 0
    exact_class_matches: int = 0
    true_ground: int = 0
    overclassified_ground: int = 0
    underclassified_ground: int = 0
    true_nonground: int = 0

    def add_arrays(
        self,
        reference_class: np.ndarray,
        candidate_class: np.ndarray,
        reference_ground: np.ndarray,
        candidate_ground: np.ndarray,
        mask: np.ndarray | None = None,
    ) -> None:
        if mask is not None:
            reference_class = reference_class[mask]
            candidate_class = candidate_class[mask]
            reference_ground = reference_ground[mask]
            candidate_ground = candidate_ground[mask]
        count = int(len(reference_class))
        if not count:
            return
        self.compared += count
        self.exact_class_matches += int(
            np.count_nonzero(reference_class == candidate_class)
        )
        self.true_ground += int(
            np.count_nonzero(reference_ground & candidate_ground)
        )
        self.overclassified_ground += int(
            np.count_nonzero(~reference_ground & candidate_ground)
        )
        self.underclassified_ground += int(
            np.count_nonzero(reference_ground & ~candidate_ground)
        )
        self.true_nonground += int(
            np.count_nonzero(~reference_ground & ~candidate_ground)
        )

    def add_counts(
        self,
        *,
        compared: int,
        exact_class_matches: int,
        true_ground: int,
        overclassified_ground: int,
        underclassified_ground: int,
        true_nonground: int,
    ) -> None:
        self.compared += int(compared)
        self.exact_class_matches += int(exact_class_matches)
        self.true_ground += int(true_ground)
        self.overclassified_ground += int(overclassified_ground)
        self.underclassified_ground += int(underclassified_ground)
        self.true_nonground += int(true_nonground)

    def to_dict(self) -> dict:
        tp = self.true_ground
        fp = self.overclassified_ground
        fn = self.underclassified_ground
        tn = self.true_nonground
        precision = _safe_ratio(tp, tp + fp)
        recall = _safe_ratio(tp, tp + fn)
        f1 = (
            None
            if precision is None
            or recall is None
            or precision + recall == 0
            else 2.0 * precision * recall / (precision + recall)
        )
        return {
            **asdict(self),
            "full_class_disagreements": (
                self.compared - self.exact_class_matches
            ),
            "exact_class_agreement": _safe_ratio(
                self.exact_class_matches, self.compared
            ),
            "binary_ground_agreement": _safe_ratio(tp + tn, self.compared),
            "ground_precision": precision,
            "ground_recall": recall,
            "ground_f1": f1,
            "ground_iou": _safe_ratio(tp, tp + fp + fn),
            "overclassification_rate": _safe_ratio(fp, fp + tn),
            "underclassification_rate": _safe_ratio(fn, tp + fn),
        }

@dataclass
class CellCounts:
    compared: int = 0
    binary_disagreements: int = 0
    full_class_disagreements: int = 0
    overclassified_ground: int = 0
    underclassified_ground: int = 0

    def add(
        self,
        *,
        compared: int,
        binary_disagreements: int,
        full_class_disagreements: int,
        overclassified_ground: int,
        underclassified_ground: int,
    ) -> None:
        self.compared += int(compared)
        self.binary_disagreements += int(binary_disagreements)
        self.full_class_disagreements += int(full_class_disagreements)
        self.overclassified_ground += int(overclassified_ground)
        self.underclassified_ground += int(underclassified_ground)

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "binary_disagreement_rate": _safe_ratio(
                self.binary_disagreements, self.compared
            ),
            "full_class_disagreement_rate": _safe_ratio(
                self.full_class_disagreements, self.compared
            ),
        }


@dataclass(frozen=True)
class Zone:
    name: str
    geometry: object


class ZoneSet:
    """Inclusive polygon zones used for terrain-specific audit metrics."""

    def __init__(self, zones: Iterable[Zone]):
        self.zones = tuple(zones)

    def masks(self, x: np.ndarray, y: np.ndarray):
        if not self.zones:
            return
        try:
            from shapely import intersects_xy
        except ImportError as exc:
            raise GroundParityError(
                "Terrain zones require Shapely."
            ) from exc
        for zone in self.zones:
            yield zone.name, np.asarray(
                intersects_xy(zone.geometry, x, y), dtype=bool
            )


def load_zones(
    path: str | Path,
    *,
    name_field: str | None = None,
    target_crs=None,
) -> ZoneSet:
    """Load polygon zones and reproject them to the point-cloud CRS."""
    try:
        import geopandas as gpd
    except ImportError as exc:
        raise GroundParityError(
            "Terrain zones require GeoPandas."
        ) from exc

    frame = gpd.read_file(str(path))
    if frame.empty:
        raise GroundParityError(f"Zone file contains no features: {path}")
    if target_crs is not None and frame.crs is not None:
        frame = frame.to_crs(target_crs)
    if name_field and name_field not in frame.columns:
        raise GroundParityError(
            f"Zone field {name_field!r} is not present in {path}."
        )

    zones = []
    for position, (_, row) in enumerate(frame.iterrows(), start=1):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        name = (
            str(row[name_field])
            if name_field
            else f"zone_{position}"
        )
        zones.append(Zone(name=name, geometry=geometry))
    if not zones:
        raise GroundParityError(f"Zone file has no usable geometry: {path}")
    return ZoneSet(zones)


def _ground_mask(classes: np.ndarray, ground_classes: Sequence[int]):
    return np.isin(classes, np.asarray(tuple(ground_classes), dtype=np.uint8))


def _duplicate_group_counts(
    reference_class: np.ndarray,
    candidate_class: np.ndarray,
    reference_ground_classes: Sequence[int],
    candidate_ground_classes: Sequence[int],
) -> ParityCounts:
    """Compare an indistinguishable duplicate-key group as a multiset."""
    n = int(len(reference_class))
    ref_ground_count = int(
        np.count_nonzero(
            _ground_mask(reference_class, reference_ground_classes)
        )
    )
    cand_ground_count = int(
        np.count_nonzero(
            _ground_mask(candidate_class, candidate_ground_classes)
        )
    )
    tp = min(ref_ground_count, cand_ground_count)
    fp = max(cand_ground_count - ref_ground_count, 0)
    fn = max(ref_ground_count - cand_ground_count, 0)
    tn = n - tp - fp - fn

    ref_hist = np.bincount(reference_class.astype(np.uint8), minlength=256)
    cand_hist = np.bincount(candidate_class.astype(np.uint8), minlength=256)
    exact = int(np.minimum(ref_hist, cand_hist).sum())
    counts = ParityCounts()
    counts.add_counts(
        compared=n,
        exact_class_matches=exact,
        true_ground=tp,
        overclassified_ground=fp,
        underclassified_ground=fn,
        true_nonground=tn,
    )
    return counts

class ParityAccumulator:
    def __init__(
        self,
        *,
        reference_ground_classes: Sequence[int] = (2,),
        candidate_ground_classes: Sequence[int] = (2,),
        eligible_original_classes: Sequence[int] | None = None,
        cell_size: float = 5.0,
        zones: ZoneSet | None = None,
    ):
        if cell_size <= 0 or not math.isfinite(cell_size):
            raise ValueError("cell_size must be a finite positive number")
        self.reference_ground_classes = tuple(
            int(value) for value in reference_ground_classes
        )
        self.candidate_ground_classes = tuple(
            int(value) for value in candidate_ground_classes
        )
        self.eligible_original_classes = (
            None
            if eligible_original_classes is None
            else tuple(int(value) for value in eligible_original_classes)
        )
        self.cell_size = float(cell_size)
        self.zones = zones
        self.overall = ParityCounts()
        self.by_original_class: dict[int, ParityCounts] = {}
        self.by_zone: dict[str, ParityCounts] = {}
        self.cells: dict[tuple[int, int], CellCounts] = {}
        self.input_points = 0
        self.excluded_points = 0
        self.ambiguous_duplicate_points = 0

    def _eligible(self, original_class: np.ndarray) -> np.ndarray:
        if self.eligible_original_classes is None:
            return np.ones(len(original_class), dtype=bool)
        return np.isin(
            original_class,
            np.asarray(self.eligible_original_classes, dtype=np.uint8),
        )

    def add(
        self,
        original_class: np.ndarray,
        reference_class: np.ndarray,
        candidate_class: np.ndarray,
        x: np.ndarray,
        y: np.ndarray,
        *,
        breakdown_mask: np.ndarray | None = None,
    ) -> None:
        original_class = np.asarray(original_class, dtype=np.uint8)
        reference_class = np.asarray(reference_class, dtype=np.uint8)
        candidate_class = np.asarray(candidate_class, dtype=np.uint8)
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        n = len(original_class)
        if not (
            len(reference_class) == n
            and len(candidate_class) == n
            and len(x) == n
            and len(y) == n
        ):
            raise ValueError("All aligned comparison arrays must have equal length.")
        self.input_points += n

        eligible = self._eligible(original_class)
        self.excluded_points += int(np.count_nonzero(~eligible))
        if not np.any(eligible):
            return

        ref_ground = _ground_mask(
            reference_class, self.reference_ground_classes
        )
        cand_ground = _ground_mask(
            candidate_class, self.candidate_ground_classes
        )
        self.overall.add_arrays(
            reference_class,
            candidate_class,
            ref_ground,
            cand_ground,
            eligible,
        )

        detail_mask = eligible.copy()
        if breakdown_mask is not None:
            detail_mask &= np.asarray(breakdown_mask, dtype=bool)
        for class_code in np.unique(original_class[detail_mask]):
            class_mask = detail_mask & (original_class == class_code)
            counts = self.by_original_class.setdefault(
                int(class_code), ParityCounts()
            )
            counts.add_arrays(
                reference_class,
                candidate_class,
                ref_ground,
                cand_ground,
                class_mask,
            )

        if self.zones is not None:
            for name, zone_mask in self.zones.masks(x, y):
                mask = eligible & zone_mask
                if not np.any(mask):
                    continue
                counts = self.by_zone.setdefault(name, ParityCounts())
                counts.add_arrays(
                    reference_class,
                    candidate_class,
                    ref_ground,
                    cand_ground,
                    mask,
                )

        self._add_cells(
            x[eligible],
            y[eligible],
            reference_class[eligible] != candidate_class[eligible],
            ref_ground[eligible] != cand_ground[eligible],
            ~ref_ground[eligible] & cand_ground[eligible],
            ref_ground[eligible] & ~cand_ground[eligible],
        )

    def add_duplicate_group(
        self,
        original_class: np.ndarray,
        reference_class: np.ndarray,
        candidate_class: np.ndarray,
        x: float,
        y: float,
    ) -> None:
        """Add an XYZ-duplicate group without inventing record correspondence."""
        original_class = np.asarray(original_class, dtype=np.uint8)
        reference_class = np.asarray(reference_class, dtype=np.uint8)
        candidate_class = np.asarray(candidate_class, dtype=np.uint8)
        n = len(original_class)
        self.input_points += n
        self.ambiguous_duplicate_points += n

        eligible = self._eligible(original_class)
        if not np.all(eligible):
            # If duplicate records span eligible/non-eligible original classes,
            # immutable XYZ cannot identify which changed output belongs to
            # which original class. Exclude the entire group conservatively.
            self.excluded_points += n
            return

        group = _duplicate_group_counts(
            reference_class,
            candidate_class,
            self.reference_ground_classes,
            self.candidate_ground_classes,
        )
        self.overall.add_counts(
            compared=group.compared,
            exact_class_matches=group.exact_class_matches,
            true_ground=group.true_ground,
            overclassified_ground=group.overclassified_ground,
            underclassified_ground=group.underclassified_ground,
            true_nonground=group.true_nonground,
        )

        if self.zones is not None:
            point_x = np.asarray([x], dtype=np.float64)
            point_y = np.asarray([y], dtype=np.float64)
            for name, zone_mask in self.zones.masks(point_x, point_y):
                if not bool(zone_mask[0]):
                    continue
                counts = self.by_zone.setdefault(name, ParityCounts())
                counts.add_counts(
                    compared=group.compared,
                    exact_class_matches=group.exact_class_matches,
                    true_ground=group.true_ground,
                    overclassified_ground=group.overclassified_ground,
                    underclassified_ground=group.underclassified_ground,
                    true_nonground=group.true_nonground,
                )

        binary_disagreements = (
            group.overclassified_ground + group.underclassified_ground
        )
        self._add_cell_counts(
            x,
            y,
            compared=group.compared,
            binary_disagreements=binary_disagreements,
            full_class_disagreements=(
                group.compared - group.exact_class_matches
            ),
            overclassified_ground=group.overclassified_ground,
            underclassified_ground=group.underclassified_ground,
        )

    def _add_cells(
        self,
        x: np.ndarray,
        y: np.ndarray,
        full_disagreement: np.ndarray,
        binary_disagreement: np.ndarray,
        overclassified: np.ndarray,
        underclassified: np.ndarray,
    ) -> None:
        if not len(x):
            return
        cell_x = np.floor(x / self.cell_size).astype(np.int64)
        cell_y = np.floor(y / self.cell_size).astype(np.int64)
        pairs = np.column_stack((cell_x, cell_y))
        unique, inverse = np.unique(pairs, axis=0, return_inverse=True)
        totals = np.bincount(inverse, minlength=len(unique))
        full = np.bincount(
            inverse,
            weights=full_disagreement.astype(np.uint8),
            minlength=len(unique),
        ).astype(np.int64)
        binary = np.bincount(
            inverse,
            weights=binary_disagreement.astype(np.uint8),
            minlength=len(unique),
        ).astype(np.int64)
        over = np.bincount(
            inverse,
            weights=overclassified.astype(np.uint8),
            minlength=len(unique),
        ).astype(np.int64)
        under = np.bincount(
            inverse,
            weights=underclassified.astype(np.uint8),
            minlength=len(unique),
        ).astype(np.int64)
        for index, (ix, iy) in enumerate(unique):
            counts = self.cells.setdefault((int(ix), int(iy)), CellCounts())
            counts.add(
                compared=int(totals[index]),
                binary_disagreements=int(binary[index]),
                full_class_disagreements=int(full[index]),
                overclassified_ground=int(over[index]),
                underclassified_ground=int(under[index]),
            )

    def _add_cell_counts(
        self,
        x: float,
        y: float,
        **kwargs,
    ) -> None:
        key = (
            int(math.floor(x / self.cell_size)),
            int(math.floor(y / self.cell_size)),
        )
        self.cells.setdefault(key, CellCounts()).add(**kwargs)

    def to_dict(self) -> dict:
        return {
            "input_points": self.input_points,
            "excluded_points": self.excluded_points,
            "ambiguous_duplicate_points": self.ambiguous_duplicate_points,
            "overall": self.overall.to_dict(),
            "by_original_class": {
                str(code): counts.to_dict()
                for code, counts in sorted(self.by_original_class.items())
            },
            "by_zone": {
                name: counts.to_dict()
                for name, counts in sorted(self.by_zone.items())
            },
        }


def compare_aligned_arrays(
    original_class: np.ndarray,
    reference_class: np.ndarray,
    candidate_class: np.ndarray,
    xyz: np.ndarray,
    *,
    reference_ground_classes: Sequence[int] = (2,),
    candidate_ground_classes: Sequence[int] = (2,),
    eligible_original_classes: Sequence[int] | None = None,
    cell_size: float = 5.0,
    zones: ZoneSet | None = None,
) -> ParityAccumulator:
    """Compare already aligned arrays; primarily useful for tests and GUI use."""
    xyz = np.asarray(xyz)
    if xyz.ndim != 2 or xyz.shape[1] < 2:
        raise ValueError("xyz must be an N x 2 or N x 3 array")
    accumulator = ParityAccumulator(
        reference_ground_classes=reference_ground_classes,
        candidate_ground_classes=candidate_ground_classes,
        eligible_original_classes=eligible_original_classes,
        cell_size=cell_size,
        zones=zones,
    )
    accumulator.add(
        original_class,
        reference_class,
        candidate_class,
        xyz[:, 0],
        xyz[:, 1],
    )
    return accumulator


def _las_metadata(path: Path) -> dict:
    with laspy.open(str(path)) as reader:
        header = reader.header
        crs = header.parse_crs()
        return {
            "path": str(path.resolve()),
            "point_count": int(header.point_count),
            "point_format": int(header.point_format.id),
            "las_version": str(header.version),
            "scales": [float(value) for value in header.scales],
            "offsets": [float(value) for value in header.offsets],
            "crs": crs.to_string() if crs is not None else None,
        }


def _resolve_coordinate_resolution(
    metadata: Sequence[dict],
    requested: float | None,
) -> float:
    if requested is not None:
        resolution = float(requested)
    else:
        resolution = max(
            abs(float(scale))
            for item in metadata
            for scale in item["scales"]
        )
    if resolution <= 0 or not math.isfinite(resolution):
        raise GroundParityError(
            "Coordinate identity resolution must be finite and positive."
        )
    return resolution


def _quantized_coordinates(points, resolution: float):
    return (
        np.rint(np.asarray(points.x) / resolution).astype(np.int64),
        np.rint(np.asarray(points.y) / resolution).astype(np.int64),
        np.rint(np.asarray(points.z) / resolution).astype(np.int64),
    )


def _validate_point_counts(metadata: Sequence[dict]) -> int:
    counts = [int(item["point_count"]) for item in metadata]
    if len(set(counts)) != 1:
        raise PointSetMismatchError(
            "Point counts differ: "
            + ", ".join(
                f"{Path(item['path']).name}={item['point_count']:,}"
                for item in metadata
            )
        )
    return counts[0]


def _compare_record_order(
    paths: Sequence[Path],
    *,
    resolution: float,
    accumulator: ParityAccumulator,
    chunk_size: int,
) -> None:
    readers = [laspy.open(str(path)) for path in paths]
    try:
        iterators = [reader.chunk_iterator(chunk_size) for reader in readers]
        offset = 0
        while True:
            chunks = []
            for iterator in iterators:
                try:
                    chunks.append(next(iterator))
                except StopIteration:
                    chunks.append(None)
            if all(chunk is None for chunk in chunks):
                break
            if any(chunk is None for chunk in chunks):
                raise PointSetMismatchError(
                    "A point-cloud stream ended before the other inputs."
                )
            lengths = [len(chunk) for chunk in chunks]
            if len(set(lengths)) != 1:
                raise PointSetMismatchError(
                    f"Chunk lengths differ near record {offset:,}: {lengths}"
                )

            original_q = _quantized_coordinates(chunks[0], resolution)
            for input_index, chunk in enumerate(chunks[1:], start=1):
                candidate_q = _quantized_coordinates(chunk, resolution)
                same = (
                    (original_q[0] == candidate_q[0])
                    & (original_q[1] == candidate_q[1])
                    & (original_q[2] == candidate_q[2])
                )
                if not np.all(same):
                    local = int(np.flatnonzero(~same)[0])
                    raise RecordOrderMismatchError(
                        "Record-order identity failed at point "
                        f"{offset + local:,} for {paths[input_index].name}. "
                        "Use identity alignment for reordered outputs."
                    )

            x = original_q[0].astype(np.float64) * resolution
            y = original_q[1].astype(np.float64) * resolution
            accumulator.add(
                np.asarray(chunks[0].classification, dtype=np.uint8),
                np.asarray(chunks[1].classification, dtype=np.uint8),
                np.asarray(chunks[2].classification, dtype=np.uint8),
                x,
                y,
            )
            offset += lengths[0]
    finally:
        for reader in readers:
            reader.close()


_IDENTITY_DTYPE = np.dtype(
    [
        ("qx", "<i8"),
        ("qy", "<i8"),
        ("qz", "<i8"),
        ("classification", "u1"),
    ],
    align=False,
)
_IDENTITY_KEY_FIELDS = ("qx", "qy", "qz")


def _build_identity_records(
    path: Path,
    *,
    point_count: int,
    resolution: float,
    chunk_size: int,
    output_path: Path,
) -> np.memmap:
    records = np.memmap(
        output_path,
        mode="w+",
        dtype=_IDENTITY_DTYPE,
        shape=(point_count,),
    )
    offset = 0
    with laspy.open(str(path)) as reader:
        for points in reader.chunk_iterator(chunk_size):
            end = offset + len(points)
            qx, qy, qz = _quantized_coordinates(points, resolution)
            records["qx"][offset:end] = qx
            records["qy"][offset:end] = qy
            records["qz"][offset:end] = qz
            records["classification"][offset:end] = np.asarray(
                points.classification, dtype=np.uint8
            )
            offset = end
    if offset != point_count:
        raise PointSetMismatchError(
            f"{path.name} yielded {offset:,} points; expected {point_count:,}."
        )
    # Including classification after the immutable keys gives deterministic
    # multiset ordering within indistinguishable XYZ duplicate groups.
    records.sort(
        order=(*_IDENTITY_KEY_FIELDS, "classification"),
        kind="quicksort",
    )
    records.flush()
    return records


def _same_identity(left, right):
    return (
        (left["qx"] == right["qx"])
        & (left["qy"] == right["qy"])
        & (left["qz"] == right["qz"])
    )


def _compare_identity_sorted(
    records: Sequence[np.memmap],
    *,
    resolution: float,
    accumulator: ParityAccumulator,
    chunk_size: int,
) -> None:
    total = len(records[0])
    start = 0
    while start < total:
        end = min(start + chunk_size, total)
        # Never split an indistinguishable duplicate group across chunks.
        while (
            end < total
            and bool(_same_identity(records[0][end - 1], records[0][end]))
        ):
            end += 1

        original = records[0][start:end]
        reference = records[1][start:end]
        candidate = records[2][start:end]
        if not np.all(_same_identity(original, reference)):
            local = int(
                np.flatnonzero(~_same_identity(original, reference))[0]
            )
            raise PointSetMismatchError(
                "Nakshatech reference does not contain the same quantized "
                f"point identity near sorted record {start + local:,}."
            )
        if not np.all(_same_identity(original, candidate)):
            local = int(
                np.flatnonzero(~_same_identity(original, candidate))[0]
            )
            raise PointSetMismatchError(
                "Naksha candidate does not contain the same quantized "
                f"point identity near sorted record {start + local:,}."
            )

        n = len(original)
        equal_previous = np.zeros(n, dtype=bool)
        equal_next = np.zeros(n, dtype=bool)
        if n > 1:
            adjacency = _same_identity(original[:-1], original[1:])
            equal_previous[1:] = adjacency
            equal_next[:-1] = adjacency
        ambiguous = equal_previous | equal_next
        unique = ~ambiguous

        if np.any(unique):
            x = original["qx"][unique].astype(np.float64) * resolution
            y = original["qy"][unique].astype(np.float64) * resolution
            accumulator.add(
                original["classification"][unique],
                reference["classification"][unique],
                candidate["classification"][unique],
                x,
                y,
            )

        group_starts = np.flatnonzero(ambiguous & ~equal_previous)
        group_ends = np.flatnonzero(ambiguous & ~equal_next) + 1
        for group_start, group_end in zip(group_starts, group_ends):
            x = float(original["qx"][group_start]) * resolution
            y = float(original["qy"][group_start]) * resolution
            accumulator.add_duplicate_group(
                original["classification"][group_start:group_end],
                reference["classification"][group_start:group_end],
                candidate["classification"][group_start:group_end],
                x,
                y,
            )
        start = end


def compare_las_files(
    original_path: str | Path,
    reference_path: str | Path,
    candidate_path: str | Path,
    *,
    alignment: str = "auto",
    coordinate_resolution: float | None = None,
    reference_ground_classes: Sequence[int] = (2,),
    candidate_ground_classes: Sequence[int] = (2,),
    eligible_original_classes: Sequence[int] | None = None,
    cell_size: float = 5.0,
    zones_path: str | Path | None = None,
    zone_name_field: str | None = None,
    chunk_size: int = 1_000_000,
    temp_directory: str | Path | None = None,
) -> dict:
    """Compare original, Nakshatech-reference and Naksha-candidate LAS/LAZ files."""
    paths = tuple(
        Path(path) for path in (
            original_path, reference_path, candidate_path
        )
    )
    for path in paths:
        if not path.is_file():
            raise GroundParityError(f"Input file does not exist: {path}")
        if path.suffix.lower() not in {".las", ".laz"}:
            raise GroundParityError(f"Expected LAS/LAZ input: {path}")
    if alignment not in {"auto", "order", "identity"}:
        raise ValueError("alignment must be auto, order, or identity")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    metadata = [_las_metadata(path) for path in paths]
    point_count = _validate_point_counts(metadata)
    resolution = _resolve_coordinate_resolution(
        metadata, coordinate_resolution
    )
    target_crs = metadata[0]["crs"]
    zones = (
        load_zones(
            zones_path,
            name_field=zone_name_field,
            target_crs=target_crs,
        )
        if zones_path is not None
        else None
    )

    def new_accumulator():
        return ParityAccumulator(
            reference_ground_classes=reference_ground_classes,
            candidate_ground_classes=candidate_ground_classes,
            eligible_original_classes=eligible_original_classes,
            cell_size=cell_size,
            zones=zones,
        )

    accumulator = new_accumulator()
    used_alignment = alignment
    order_failure = None
    if alignment in {"auto", "order"}:
        try:
            _compare_record_order(
                paths,
                resolution=resolution,
                accumulator=accumulator,
                chunk_size=chunk_size,
            )
            used_alignment = "order"
        except RecordOrderMismatchError as exc:
            if alignment == "order":
                raise
            order_failure = str(exc)
            accumulator = new_accumulator()
            used_alignment = "identity"

    if used_alignment == "identity":
        with tempfile.TemporaryDirectory(
            prefix="naksha_ground_parity_",
            dir=str(temp_directory) if temp_directory is not None else None,
        ) as temporary:
            temporary_path = Path(temporary)
            records = []
            try:
                for index, path in enumerate(paths):
                    records.append(
                        _build_identity_records(
                            path,
                            point_count=point_count,
                            resolution=resolution,
                            chunk_size=chunk_size,
                            output_path=temporary_path / f"identity_{index}.bin",
                        )
                    )
                _compare_identity_sorted(
                    records,
                    resolution=resolution,
                    accumulator=accumulator,
                    chunk_size=chunk_size,
                )
            finally:
                for record in records:
                    try:
                        record.flush()
                        record._mmap.close()
                    except Exception:
                        pass

    result = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "original": metadata[0],
            "reference": metadata[1],
            "candidate": metadata[2],
        },
        "configuration": {
            "requested_alignment": alignment,
            "used_alignment": used_alignment,
            "record_order_failure": order_failure,
            "coordinate_resolution": resolution,
            "reference_ground_classes": list(reference_ground_classes),
            "candidate_ground_classes": list(candidate_ground_classes),
            "eligible_original_classes": (
                None
                if eligible_original_classes is None
                else list(eligible_original_classes)
            ),
            "cell_size": float(cell_size),
            "zones_path": (
                str(Path(zones_path).resolve())
                if zones_path is not None
                else None
            ),
            "zone_name_field": zone_name_field,
            "chunk_size": int(chunk_size),
        },
        "metrics": accumulator.to_dict(),
        "_cells": accumulator.cells,
    }
    return result


def _json_ready_report(report: dict) -> dict:
    return {
        key: value
        for key, value in report.items()
        if not key.startswith("_")
    }


def write_cells_csv(
    path: str | Path,
    cells: dict[tuple[int, int], CellCounts],
    *,
    cell_size: float,
) -> None:
    path = Path(path)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "cell_x",
                "cell_y",
                "min_x",
                "min_y",
                "max_x",
                "max_y",
                "compared",
                "binary_disagreements",
                "binary_disagreement_rate",
                "full_class_disagreements",
                "full_class_disagreement_rate",
                "overclassified_ground",
                "underclassified_ground",
            ],
        )
        writer.writeheader()
        ranked = sorted(
            cells.items(),
            key=lambda item: (
                item[1].binary_disagreements,
                item[1].full_class_disagreements,
            ),
            reverse=True,
        )
        for (cell_x, cell_y), counts in ranked:
            data = counts.to_dict()
            writer.writerow(
                {
                    "cell_x": cell_x,
                    "cell_y": cell_y,
                    "min_x": cell_x * cell_size,
                    "min_y": cell_y * cell_size,
                    "max_x": (cell_x + 1) * cell_size,
                    "max_y": (cell_y + 1) * cell_size,
                    **data,
                }
            )


def _format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.4f}%"


def write_markdown_report(path: str | Path, report: dict) -> None:
    path = Path(path)
    metrics = report["metrics"]
    overall = metrics["overall"]
    config = report["configuration"]
    lines = [
        "# Ground Classification Parity Report",
        "",
        f"Generated: {report['generated_at_utc']}",
        "",
        "## Summary",
        "",
        f"- Compared points: {overall['compared']:,}",
        f"- Exact class agreement: "
        f"{_format_metric(overall['exact_class_agreement'])}",
        f"- Binary ground agreement: "
        f"{_format_metric(overall['binary_ground_agreement'])}",
        f"- Ground precision: {_format_metric(overall['ground_precision'])}",
        f"- Ground recall: {_format_metric(overall['ground_recall'])}",
        f"- Ground F1: {_format_metric(overall['ground_f1'])}",
        f"- Ground IoU: {_format_metric(overall['ground_iou'])}",
        f"- Overclassified as ground: "
        f"{overall['overclassified_ground']:,}",
        f"- Underclassified as ground: "
        f"{overall['underclassified_ground']:,}",
        f"- Excluded points: {metrics['excluded_points']:,}",
        f"- Ambiguous duplicate-identity points: "
        f"{metrics['ambiguous_duplicate_points']:,}",
        "",
        "## Alignment",
        "",
        f"- Requested: {config['requested_alignment']}",
        f"- Used: {config['used_alignment']}",
        f"- Coordinate identity resolution: "
        f"{config['coordinate_resolution']:.12g}",
        f"- Cell size: {config['cell_size']:.3f}",
    ]
    if config.get("record_order_failure"):
        lines.extend(
            [
                f"- Record-order fallback reason: "
                f"{config['record_order_failure']}",
            ]
        )

    lines.extend(
        [
            "",
            "## Confusion counts",
            "",
            "| Result | Count |",
            "|---|---:|",
            f"| True ground | {overall['true_ground']:,} |",
            f"| True non-ground | {overall['true_nonground']:,} |",
            f"| Overclassified ground | "
            f"{overall['overclassified_ground']:,} |",
            f"| Underclassified ground | "
            f"{overall['underclassified_ground']:,} |",
        ]
    )

    if metrics["by_zone"]:
        lines.extend(
            [
                "",
                "## Terrain zones",
                "",
                "| Zone | Points | Ground F1 | Over | Under |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for name, values in metrics["by_zone"].items():
            lines.append(
                f"| {name} | {values['compared']:,} | "
                f"{_format_metric(values['ground_f1'])} | "
                f"{values['overclassified_ground']:,} | "
                f"{values['underclassified_ground']:,} |"
            )

    if metrics["by_original_class"]:
        lines.extend(
            [
                "",
                "## Original-class breakdown",
                "",
                "| Original class | Points | Binary agreement | Over | Under |",
                "|---:|---:|---:|---:|---:|",
            ]
        )
        for code, values in metrics["by_original_class"].items():
            lines.append(
                f"| {code} | {values['compared']:,} | "
                f"{_format_metric(values['binary_ground_agreement'])} | "
                f"{values['overclassified_ground']:,} | "
                f"{values['underclassified_ground']:,} |"
            )

    lines.extend(
        [
            "",
            "## Files",
            "",
            f"- Original: `{report['inputs']['original']['path']}`",
            f"- Nakshatech reference: `{report['inputs']['reference']['path']}`",
            f"- Naksha candidate: `{report['inputs']['candidate']['path']}`",
            "",
            "See `parity_metrics.json` for complete machine-readable metrics "
            "and `disagreement_cells.csv` for the spatial audit grid.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_heatmap(
    path: str | Path,
    cells: dict[tuple[int, int], CellCounts],
    *,
    cell_size: float,
) -> bool:
    disagreement_cells = [
        (key, counts)
        for key, counts in cells.items()
        if counts.binary_disagreements > 0
    ]
    if not disagreement_cells:
        return False
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = np.asarray(
        [(key[0] + 0.5) * cell_size for key, _ in disagreement_cells]
    )
    y = np.asarray(
        [(key[1] + 0.5) * cell_size for key, _ in disagreement_cells]
    )
    rates = np.asarray(
        [
            counts.binary_disagreements / counts.compared
            for _, counts in disagreement_cells
        ]
    )
    sizes = np.asarray(
        [
            12.0 + 12.0 * math.log10(1 + counts.binary_disagreements)
            for _, counts in disagreement_cells
        ]
    )
    figure, axes = plt.subplots(figsize=(11, 8), constrained_layout=True)
    scatter = axes.scatter(
        x,
        y,
        c=100.0 * rates,
        s=sizes,
        cmap="magma",
        vmin=0.0,
        vmax=max(1.0, float(100.0 * rates.max())),
        linewidths=0,
    )
    axes.set_aspect("equal", adjustable="datalim")
    axes.set_title("Nakshatech vs Naksha Ground Disagreement")
    axes.set_xlabel("Easting / X")
    axes.set_ylabel("Northing / Y")
    colorbar = figure.colorbar(scatter, ax=axes)
    colorbar.set_label("Binary ground disagreement (%)")
    figure.savefig(path, dpi=160)
    plt.close(figure)
    return True


def write_audit_outputs(
    output_directory: str | Path,
    report: dict,
    *,
    include_heatmap: bool = True,
) -> dict[str, Path]:
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)
    cells = report.get("_cells", {})
    cell_size = float(report["configuration"]["cell_size"])
    output_paths = {
        "json": output_directory / "parity_metrics.json",
        "markdown": output_directory / "parity_report.md",
        "cells_csv": output_directory / "disagreement_cells.csv",
    }
    output_paths["json"].write_text(
        json.dumps(_json_ready_report(report), indent=2),
        encoding="utf-8",
    )
    write_markdown_report(output_paths["markdown"], report)
    write_cells_csv(output_paths["cells_csv"], cells, cell_size=cell_size)
    if include_heatmap:
        heatmap = output_directory / "disagreement_heatmap.png"
        if write_heatmap(heatmap, cells, cell_size=cell_size):
            output_paths["heatmap"] = heatmap
    return output_paths

