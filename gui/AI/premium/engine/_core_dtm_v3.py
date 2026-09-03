#!/usr/bin/env python3
"""Build three label-independent DTM/HAG V3 candidates.

The source is the approved 7,000-point safe H5. Only feature column 3 (HAG)
is replaced. LAS classifications are never read.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Mapping, Sequence, Tuple

import h5py
import laspy
import numpy as np
from scipy.ndimage import (
    distance_transform_edt,
    gaussian_filter,
    grey_opening,
    median_filter,
)

HAG_INDEX = 3
NUM_FEATURES = 68

DEFAULT_FILES = [
    "600000_5668500.laz",
    "601500_5674000.laz",
    "604500_5652500.laz",
]

CANDIDATES: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "windows_m": [3, 7, 15],
        "thresholds_m": [0.45, 0.90, 1.80],
    },
    "balanced": {
        "windows_m": [3, 7, 15, 31],
        "thresholds_m": [0.40, 0.80, 1.60, 3.20],
    },
    "strong": {
        "windows_m": [3, 7, 15, 31, 61],
        "thresholds_m": [0.35, 0.70, 1.40, 2.80, 5.60],
    },
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_replace(temp: Path, final: Path) -> None:
    if final.exists():
        final.unlink()
    os.replace(temp, final)


def copy_attrs(source, destination) -> None:
    for key, value in source.attrs.items():
        destination.attrs[key] = value


def build_low_quantile_raster(
    laz_path: Path,
    grid_size: float,
    quantile: float,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Exact per-cell quantile using cell/Z lexicographic sorting."""
    started = time.time()
    las = laspy.read(str(laz_path))

    scales = np.asarray(las.header.scales, dtype=np.float64)
    offsets = np.asarray(las.header.offsets, dtype=np.float64)
    mins = np.asarray(las.header.mins, dtype=np.float64)
    maxs = np.asarray(las.header.maxs, dtype=np.float64)

    raw_x = np.asarray(las.X)
    raw_y = np.asarray(las.Y)
    raw_z = np.asarray(las.Z)

    nx = max(int(math.ceil((maxs[0] - mins[0]) / grid_size)) + 1, 1)
    ny = max(int(math.ceil((maxs[1] - mins[1]) / grid_size)) + 1, 1)

    scaled_x = raw_x.astype(np.float64) * scales[0] + offsets[0]
    ix = np.floor((scaled_x - mins[0]) / grid_size).astype(np.int32)
    del scaled_x
    gc.collect()

    scaled_y = raw_y.astype(np.float64) * scales[1] + offsets[1]
    iy = np.floor((scaled_y - mins[1]) / grid_size).astype(np.int32)
    del scaled_y
    gc.collect()

    ix = np.clip(ix, 0, nx - 1)
    iy = np.clip(iy, 0, ny - 1)
    cell_id = iy.astype(np.int64) * nx + ix.astype(np.int64)

    order = np.lexsort((raw_z, cell_id))
    sorted_cells = cell_id[order]
    starts = np.flatnonzero(
        np.r_[True, sorted_cells[1:] != sorted_cells[:-1]]
    )
    ends = np.r_[starts[1:], sorted_cells.size]
    counts = ends - starts
    offsets_q = np.floor(
        quantile * np.maximum(counts - 1, 0)
    ).astype(np.int64)
    chosen = order[starts + offsets_q]
    unique_cells = sorted_cells[starts]

    chosen_z = (
        raw_z[chosen].astype(np.float64) * scales[2] + offsets[2]
    )

    raster = np.full(nx * ny, np.nan, dtype=np.float32)
    raster[unique_cells] = chosen_z.astype(np.float32)
    raster = raster.reshape(ny, nx)

    report = {
        "source_points": int(raw_z.size),
        "grid_size": float(grid_size),
        "quantile": float(quantile),
        "shape": [int(ny), int(nx)],
        "populated_cells": int(unique_cells.size),
        "populated_fraction": float(
            unique_cells.size / max(nx * ny, 1)
        ),
        "x_min": float(mins[0]),
        "y_min": float(mins[1]),
        "x_max": float(maxs[0]),
        "y_max": float(maxs[1]),
        "elapsed_seconds": float(time.time() - started),
    }

    del (
        las,
        raw_x,
        raw_y,
        raw_z,
        ix,
        iy,
        cell_id,
        order,
        sorted_cells,
        starts,
        ends,
        counts,
        chosen,
        unique_cells,
        chosen_z,
    )
    gc.collect()

    return raster, report


def fill_nearest(raster: np.ndarray) -> np.ndarray:
    missing = ~np.isfinite(raster)
    if np.all(missing):
        raise ValueError("No populated terrain raster cells.")
    if not np.any(missing):
        return raster.astype(np.float32, copy=True)

    indices = distance_transform_edt(
        missing,
        return_distances=False,
        return_indices=True,
    )
    return raster[tuple(indices)].astype(np.float32)


def odd_window(window_m: float, grid_size: float) -> int:
    cells = max(int(round(window_m / grid_size)), 1)
    return cells if cells % 2 == 1 else cells + 1


def make_progressive_dtm(
    seed: np.ndarray,
    grid_size: float,
    windows_m: Sequence[float],
    thresholds_m: Sequence[float],
) -> Tuple[np.ndarray, list[dict[str, Any]]]:
    terrain = median_filter(seed, size=3, mode="nearest").astype(np.float32)
    stages: list[dict[str, Any]] = []

    for window_m, threshold_m in zip(windows_m, thresholds_m):
        cells = odd_window(window_m, grid_size)
        opened = grey_opening(
            terrain,
            size=(cells, cells),
            mode="nearest",
        ).astype(np.float32)
        delta = terrain - opened
        replace = delta > float(threshold_m)

        terrain = terrain.copy()
        terrain[replace] = opened[replace]
        terrain = median_filter(
            terrain,
            size=3,
            mode="nearest",
        ).astype(np.float32)

        stages.append(
            {
                "window_m": float(window_m),
                "window_cells": int(cells),
                "threshold_m": float(threshold_m),
                "replaced_cells": int(np.count_nonzero(replace)),
                "replaced_fraction": float(np.mean(replace)),
                "delta_max": float(np.max(delta)),
            }
        )

    terrain = gaussian_filter(
        terrain,
        sigma=0.75,
        mode="nearest",
    ).astype(np.float32)

    if not np.isfinite(terrain).all():
        raise ValueError("Generated DTM contains non-finite values.")

    return terrain, stages


def bilinear(
    raster: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
    x_min: float,
    y_min: float,
    grid_size: float,
) -> np.ndarray:
    fx = (np.asarray(x, dtype=np.float64) - x_min) / grid_size
    fy = (np.asarray(y, dtype=np.float64) - y_min) / grid_size

    fx = np.clip(fx, 0.0, raster.shape[1] - 1.000001)
    fy = np.clip(fy, 0.0, raster.shape[0] - 1.000001)

    x0 = np.floor(fx).astype(np.int64)
    y0 = np.floor(fy).astype(np.int64)
    x1 = np.minimum(x0 + 1, raster.shape[1] - 1)
    y1 = np.minimum(y0 + 1, raster.shape[0] - 1)

    wx = fx - x0
    wy = fy - y0

    values = (
        (1.0 - wx) * (1.0 - wy) * raster[y0, x0]
        + wx * (1.0 - wy) * raster[y0, x1]
        + (1.0 - wx) * wy * raster[y1, x0]
        + wx * wy * raster[y1, x1]
    )
    return values.astype(np.float32)


def init_stats() -> dict[str, float]:
    return {
        "count": 0,
        "sum": 0.0,
        "sum_sq": 0.0,
        "min": float("inf"),
        "max": float("-inf"),
    }


def update_stats(stats: dict[str, float], values: np.ndarray) -> None:
    arr = np.asarray(values, dtype=np.float64)
    stats["count"] += int(arr.size)
    stats["sum"] += float(arr.sum())
    stats["sum_sq"] += float(np.square(arr).sum())
    stats["min"] = min(stats["min"], float(arr.min()))
    stats["max"] = max(stats["max"], float(arr.max()))


def finish_stats(stats: Mapping[str, float]) -> dict[str, Any]:
    count = int(stats["count"])
    mean = stats["sum"] / max(count, 1)
    variance = max(
        stats["sum_sq"] / max(count, 1) - mean * mean,
        0.0,
    )
    return {
        "count": count,
        "mean": float(mean),
        "std": float(math.sqrt(variance)),
        "min": float(stats["min"]),
        "max": float(stats["max"]),
    }


def write_candidate(
    source_h5: Path,
    output_h5: Path,
    terrain: np.ndarray,
    x_min: float,
    y_min: float,
    grid_size: float,
    name: str,
    config: Mapping[str, Any],
    laz_path: Path,
    overwrite: bool,
) -> dict[str, Any]:
    if output_h5.exists() and not overwrite:
        raise FileExistsError(
            f"Output exists: {output_h5}. Use --overwrite."
        )

    output_h5.parent.mkdir(parents=True, exist_ok=True)
    temp = output_h5.with_suffix(".partial.h5")
    if temp.exists():
        temp.unlink()

    stats = init_stats()
    tile_count = 0
    point_count = 0

    with h5py.File(source_h5, "r") as source, h5py.File(
        temp,
        "w",
    ) as destination:
        source_meta = source["metadata"]
        destination_meta = destination.create_group("metadata")
        destination_tiles = destination.create_group("tiles")
        copy_attrs(source_meta, destination_meta)

        destination_meta.attrs["variant"] = f"dtm_v3_{name}"
        destination_meta.attrs["diagnostic_only"] = True
        destination_meta.attrs["oracle_label_leakage"] = False
        destination_meta.attrs["hag_method"] = (
            "label_independent_progressive_morphological_dtm"
        )
        destination_meta.attrs["hag_candidate"] = name
        destination_meta.attrs["hag_candidate_config"] = json.dumps(
            dict(config),
            sort_keys=True,
        )
        destination_meta.attrs["dtm_grid_size"] = float(grid_size)
        destination_meta.attrs["source_laz"] = str(laz_path.resolve())
        destination_meta.attrs["source_laz_sha256"] = sha256_file(laz_path)

        for tile_key in sorted(source["tiles"].keys()):
            source_tile = source["tiles"][tile_key]
            destination_tile = destination_tiles.create_group(tile_key)
            copy_attrs(source_tile, destination_tile)

            coords = source_tile["coords"][:]
            features = source_tile["features"][:].astype(np.float32)
            labels = source_tile["labels"][:]

            if features.shape[1] != NUM_FEATURES:
                raise ValueError(
                    f"{source_h5}:{tile_key}: expected 68 features."
                )

            center_x = float(source_tile.attrs["tile_center_x"])
            center_y = float(source_tile.attrs["tile_center_y"])

            global_x = features[:, 0].astype(np.float64) + center_x
            global_y = features[:, 1].astype(np.float64) + center_y
            absolute_z = features[:, 2].astype(np.float64)

            terrain_z = bilinear(
                terrain,
                global_x,
                global_y,
                x_min,
                y_min,
                grid_size,
            ).astype(np.float64)

            hag = np.clip(
                absolute_z - terrain_z,
                -2.0,
                None,
            ).astype(np.float32)

            if not np.isfinite(hag).all():
                raise ValueError(f"{tile_key}: non-finite HAG.")

            features[:, HAG_INDEX] = hag

            destination_tile.create_dataset(
                "coords",
                data=coords,
                compression="lzf",
            )
            destination_tile.create_dataset(
                "features",
                data=features,
                compression="lzf",
            )
            destination_tile.create_dataset(
                "labels",
                data=labels,
                compression="lzf",
            )
            destination_tile.attrs["hag_mean"] = float(np.mean(hag))
            destination_tile.attrs["hag_std"] = float(np.std(hag))

            update_stats(stats, hag)
            tile_count += 1
            point_count += int(hag.size)

        destination_meta.attrs["total_tiles"] = int(tile_count)
        destination_meta.attrs["total_points"] = int(point_count)

    atomic_replace(temp, output_h5)

    return {
        "candidate": name,
        "source_h5": str(source_h5.resolve()),
        "output_h5": str(output_h5.resolve()),
        "tiles": tile_count,
        "points": point_count,
        "hag": finish_stats(stats),
    }


def process_file(
    laz_path: Path,
    safe_h5: Path,
    output_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started = time.time()
    print("\n" + "=" * 100)
    print(f"DTM V3 FILE: {laz_path.name}")
    print("=" * 100)

    seed, raster_report = build_low_quantile_raster(
        laz_path,
        args.grid_size,
        args.cell_quantile,
    )
    filled = fill_nearest(seed)
    filled = median_filter(
        filled,
        size=3,
        mode="nearest",
    ).astype(np.float32)

    surfaces: dict[str, np.ndarray] = {}
    stages: dict[str, Any] = {}

    for name, config in CANDIDATES.items():
        surface, stage_report = make_progressive_dtm(
            filled,
            args.grid_size,
            config["windows_m"],
            config["thresholds_m"],
        )
        surfaces[name] = surface
        stages[name] = stage_report

    dtm_dir = output_dir / "dtm_surfaces"
    dtm_dir.mkdir(parents=True, exist_ok=True)
    npz_path = dtm_dir / f"{laz_path.stem}_dtm_v3.npz"
    np.savez_compressed(
        npz_path,
        seed=seed,
        filled=filled,
        conservative=surfaces["conservative"],
        balanced=surfaces["balanced"],
        strong=surfaces["strong"],
        x_min=np.asarray(raster_report["x_min"]),
        y_min=np.asarray(raster_report["y_min"]),
        grid_size=np.asarray(args.grid_size),
    )

    outputs = []
    for name, config in CANDIDATES.items():
        output_h5 = (
            output_dir
            / f"{name}_h5"
            / f"{laz_path.stem}_hag_dtm_{name}_7000.h5"
        )
        result = write_candidate(
            source_h5=safe_h5,
            output_h5=output_h5,
            terrain=surfaces[name],
            x_min=float(raster_report["x_min"]),
            y_min=float(raster_report["y_min"]),
            grid_size=args.grid_size,
            name=name,
            config=config,
            laz_path=laz_path,
            overwrite=args.overwrite,
        )
        outputs.append(result)
        print(
            f"{name.upper():>12}: "
            f"HAG mean={result['hag']['mean']:.3f}, "
            f"std={result['hag']['std']:.3f}"
        )

    report = {
        "source_laz": str(laz_path.resolve()),
        "safe_source_h5": str(safe_h5.resolve()),
        "dtm_npz": str(npz_path.resolve()),
        "raster": raster_report,
        "candidate_stages": stages,
        "outputs": outputs,
        "elapsed_seconds": float(time.time() - started),
        "label_independent": True,
        "classification_used": False,
    }

    report_dir = output_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{laz_path.stem}_dtm_v3_report.json"
    report_path.write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    print(f"REPORT: {report_path}")
    print(f"TIME:   {report['elapsed_seconds'] / 60.0:.2f} minutes")

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--safe-h5-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--files", nargs="+", default=DEFAULT_FILES)
    parser.add_argument("--grid-size", type=float, default=1.0)
    parser.add_argument("--cell-quantile", type=float, default=0.05)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    if len(args.files) != 3:
        raise SystemExit("Exactly three LAZ files are required.")

    args.input_root = args.input_root.resolve()
    args.safe_h5_root = args.safe_h5_root.resolve()
    args.output_dir = args.output_dir.resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 100)
    print("LABEL-INDEPENDENT HAG DTM V3 BAKEOFF")
    print("=" * 100)
    print(f"Input LAZ:      {args.input_root}")
    print(f"Safe 7000 H5:  {args.safe_h5_root}")
    print(f"Output:         {args.output_dir}")
    print(f"Grid:           {args.grid_size:.2f} m")
    print(f"Cell quantile:  {args.cell_quantile:.3f}")
    print("=" * 100)

    reports = []
    failures = []

    for filename in args.files:
        laz_path = (args.input_root / filename).resolve()
        safe_h5 = (
            args.safe_h5_root
            / f"{Path(filename).stem}_hag_safe_7000.h5"
        ).resolve()

        try:
            if not laz_path.is_file():
                raise FileNotFoundError(laz_path)
            if not safe_h5.is_file():
                raise FileNotFoundError(safe_h5)
            reports.append(
                process_file(
                    laz_path,
                    safe_h5,
                    args.output_dir,
                    args,
                )
            )
        except Exception as exc:
            traceback.print_exc()
            failures.append(
                {
                    "file": filename,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break

    summary = {
        "status": (
            "STRUCTURAL_PASS"
            if len(reports) == 3 and not failures
            else "FAILED"
        ),
        "files_completed": len(reports),
        "failures": failures,
        "configuration": {
            "grid_size": args.grid_size,
            "cell_quantile": args.cell_quantile,
            "candidates": CANDIDATES,
            "label_independent": True,
            "classification_used": False,
            "only_hag_replaced": True,
        },
        "files": reports,
    }

    summary_path = args.output_dir / "dtm_v3_build_summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 100)
    print("DTM V3 BUILD COMPLETE")
    print("=" * 100)
    print(f"Status:          {summary['status']}")
    print(f"Files completed: {len(reports)}/3")
    print(f"Summary:         {summary_path}")
    print("=" * 100)

    return 0 if summary["status"] == "STRUCTURAL_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
