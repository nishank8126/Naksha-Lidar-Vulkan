from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


AI_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_VEHICLE_DIR = AI_ROOT / "vehicle"

_external_root = Path(os.environ.get("NAKSHA_LIDAR_VEHICLE_RUNTIME", r"C:\NakshaLidarVehicleAPI"))
_external_python = _external_root / "venv" / "Scripts" / "python.exe"
RUNTIME_PYTHON = Path(os.environ.get(
    "NAKSHA_LIDAR_VEHICLE_PYTHON",
    str(_external_python if _external_python.is_file() else Path(sys.executable)),
))
FULL_RUNNER = BUNDLED_VEHICLE_DIR / "vehicle_fullfile_tiled_runner.py"
WORK_ROOT = Path(os.environ.get(
    "NAKSHA_LIDAR_VEHICLE_WORK",
    str(_external_root / "work" if _external_root.exists() else Path(os.getenv("TEMP", ".")) / "NakshaVehicleAI"),
))


def _truthy(name: str, default: str = "1") -> bool:
    value = str(os.environ.get(name, default)).strip().lower()
    return value not in {"0", "false", "off", "no"}


def apply_lidar_vehicle_class0_full_file(
    *,
    classified_path,
    progress=None,
):
    """
    Full-file LiDAR-only vehicle post-filter.

    The isolated runner edits ONLY the generated Premium-classified output,
    transactionally. The raw input LAS/LAZ is never modified here.

    Safety:
      - only LAS 3/4/5/6 may become LAS 0
      - LAS 2 Ground is protected
      - tiled processing prevents one giant DBSCAN on the complete cloud
      - runner failure is fail-open; the Premium result remains unchanged
    """
    report = {
        "enabled": False,
        "engine": "Naksha LiDAR-only tiled full-file vehicle geometry",
        "status": "DISABLED",
        "vehicle_points_detected": 0,
        "vehicle_points_applied": 0,
        "protected_las2_changed": 0,
    }

    if not _truthy("NAKSHA_LIDAR_VEHICLE_ENABLED", "1"):
        return report
    if not _truthy("NAKSHA_LIDAR_VEHICLE_FULL_ENABLED", "1"):
        return report

    classified_path = Path(classified_path)

    if not classified_path.is_file():
        report["enabled"] = True
        report["status"] = "OUTPUT_MISSING_FAIL_OPEN"
        report["error"] = f"Premium classified output missing: {classified_path}"
        return report

    if not RUNTIME_PYTHON.is_file() or not FULL_RUNNER.is_file():
        report["enabled"] = True
        report["status"] = "RUNTIME_MISSING_FAIL_OPEN"
        report["error"] = (
            f"runtime missing: python={RUNTIME_PYTHON.is_file()} "
            f"runner={FULL_RUNNER.is_file()}"
        )
        return report

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="premium_vehicle_full_", dir=str(WORK_ROOT)))
    report_path = work / "vehicle_full_report.json"

    tile_size = float(os.environ.get("NAKSHA_VEHICLE_FULL_TILE_M", "80.0"))
    overlap = float(os.environ.get("NAKSHA_VEHICLE_FULL_OVERLAP_M", "20.0"))
    timeout_s = int(os.environ.get("NAKSHA_VEHICLE_FULL_TIMEOUT_S", "7200"))

    try:
        if progress:
            progress(
                f"LiDAR Vehicle FULL: tiled geometry pass "
                f"(tile={tile_size:g}m, overlap={overlap:g}m)..."
            )

        cmd = [
            str(RUNTIME_PYTHON),
            str(FULL_RUNNER),
            "--classified", str(classified_path),
            "--report", str(report_path),
            "--tile-size", str(tile_size),
            "--overlap", str(overlap),
            "--mode", "strict",
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"full vehicle runner exit={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[-4000:]}"
            )

        runtime_report = {}
        if report_path.is_file():
            runtime_report = json.loads(report_path.read_text(encoding="utf-8"))

        report.update(runtime_report)
        report["enabled"] = True

        print(
            "[LiDAR Vehicle FULL] "
            f"status={report.get('status', 'UNKNOWN')} "
            f"tiles={report.get('tiles_processed', 0)} "
            f"clusters={report.get('accepted_vehicle_clusters', 0)} "
            f"detected={report.get('vehicle_points_detected', 0)} "
            f"LAS0_applied={report.get('vehicle_points_applied', 0)} "
            f"LAS2_changed={report.get('protected_las2_changed', 0)}",
            flush=True,
        )

        if progress:
            progress(
                f"LiDAR Vehicle FULL: "
                f"{report.get('vehicle_points_applied', 0):,} confirmed "
                "vehicle point(s) -> LAS 0"
            )

        return report

    except Exception as exc:
        report["enabled"] = True
        report["status"] = "ERROR_FAIL_OPEN"
        report["error"] = str(exc)

        print(
            "[LiDAR Vehicle FULL] ERROR_FAIL_OPEN - "
            f"Premium full-file classes preserved: {exc}",
            flush=True,
        )
        return report

    finally:
        shutil.rmtree(work, ignore_errors=True)
