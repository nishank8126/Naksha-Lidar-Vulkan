from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np


AI_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_VEHICLE_DIR = AI_ROOT / "vehicle"

_external_root = Path(os.environ.get("NAKSHA_LIDAR_VEHICLE_RUNTIME", r"C:\NakshaLidarVehicleAPI"))
_external_python = _external_root / "venv" / "Scripts" / "python.exe"
RUNTIME_PYTHON = Path(os.environ.get(
    "NAKSHA_LIDAR_VEHICLE_PYTHON",
    str(_external_python if _external_python.is_file() else Path(sys.executable)),
))
RUNTIME_SCRIPT = BUNDLED_VEHICLE_DIR / "vehicle_postfilter_runner.py"
WORK_ROOT = Path(os.environ.get(
    "NAKSHA_LIDAR_VEHICLE_WORK",
    str(_external_root / "work" if _external_root.exists() else Path(os.getenv("TEMP", ".")) / "NakshaVehicleAI"),
))


def _enabled() -> bool:
    value = str(os.environ.get("NAKSHA_LIDAR_VEHICLE_ENABLED", "1")).strip().lower()
    return value not in {"0", "false", "off", "no"}


def apply_lidar_vehicle_class0_fence(
    *,
    classified_path,
    target_local,
    target_classes,
    progress=None,
):
    """
    LiDAR-only Premium fence post-filter.

    The isolated runtime returns local point indices only.
    This function owns the final LAS0 change.

    Safety:
      - only Premium LAS 3/4/5/6 may become LAS 0
      - LAS 2 Ground is never changed
      - any runtime error is fail-open: Premium classes are returned unchanged
    """
    classes = np.asarray(target_classes, dtype=np.uint8).copy()
    target_local = np.asarray(target_local, dtype=np.int64).ravel()

    base_report = {
        "enabled": bool(_enabled()),
        "engine": "Naksha LiDAR-only strict vehicle geometry",
        "status": "DISABLED",
        "vehicle_points_detected": 0,
        "vehicle_points_in_fence": 0,
        "vehicle_points_applied": 0,
        "target_classes_changed_to_las0": 0,
        "protected_las2_changed": 0,
    }

    if not _enabled():
        return classes, base_report

    if target_local.size != classes.size:
        base_report["status"] = "ERROR_FAIL_OPEN"
        base_report["error"] = (
            f"target_local/classes mismatch: {target_local.size}/{classes.size}"
        )
        return classes, base_report

    # Existing Premium code supplies sorted local indices. Refuse to guess if not.
    if target_local.size > 1 and np.any(target_local[1:] < target_local[:-1]):
        base_report["status"] = "ERROR_FAIL_OPEN"
        base_report["error"] = "target_local is not sorted"
        return classes, base_report

    if not RUNTIME_PYTHON.is_file() or not RUNTIME_SCRIPT.is_file():
        base_report["status"] = "RUNTIME_MISSING_FAIL_OPEN"
        base_report["error"] = (
            f"runtime missing: python={RUNTIME_PYTHON.is_file()} "
            f"script={RUNTIME_SCRIPT.is_file()}"
        )
        return classes, base_report

    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="premium_vehicle_", dir=str(WORK_ROOT)))

    restrict_path = work / "target_local.npy"
    output_path = work / "vehicle_local.npy"
    report_path = work / "vehicle_report.json"

    try:
        np.save(restrict_path, target_local)

        if progress:
            progress("LiDAR Vehicle: strict 3D geometry check...")

        cmd = [
            str(RUNTIME_PYTHON),
            str(RUNTIME_SCRIPT),
            "--classified", str(Path(classified_path)),
            "--restrict-indices", str(restrict_path),
            "--output-indices", str(output_path),
            "--report", str(report_path),
            "--mode", "strict",
        ]

        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if proc.returncode != 0:
            raise RuntimeError(
                f"vehicle runtime exit={proc.returncode}: "
                f"{(proc.stderr or proc.stdout).strip()[-2000:]}"
            )

        runtime_report = {}
        if report_path.is_file():
            runtime_report = json.loads(report_path.read_text(encoding="utf-8"))

        vehicle_local = (
            np.asarray(np.load(output_path), dtype=np.int64).ravel()
            if output_path.is_file()
            else np.empty(0, dtype=np.int64)
        )
        vehicle_local = np.unique(vehicle_local)

        # Map exact engine-output local indices to target_classes positions.
        pos = np.searchsorted(target_local, vehicle_local)
        valid = (pos >= 0) & (pos < len(target_local))
        pos = pos[valid]
        checked = vehicle_local[valid]

        if len(pos):
            exact = target_local[pos] == checked
            pos = pos[exact]

        # Final protection is enforced AGAIN in the Naksha process.
        # Only points currently classified by Premium as 3/4/5/6 may become 0.
        if len(pos):
            eligible = np.isin(classes[pos], np.array([3, 4, 5, 6], dtype=np.uint8))
            pos = pos[eligible]

        # Count protected LAS2 before write. This must always remain zero.
        protected_las2 = int(np.count_nonzero(classes[pos] == 2)) if len(pos) else 0
        if protected_las2:
            raise RuntimeError("LAS2 safety assertion failed before vehicle write-back")

        before = classes.copy()
        if len(pos):
            classes[pos] = np.uint8(0)

        changed = int(np.count_nonzero(before != classes))

        # Absolute final safety assertion.
        if np.any((before == 2) & (classes != 2)):
            raise RuntimeError("LAS2 safety assertion failed after vehicle write-back")

        report = dict(base_report)
        report.update(runtime_report)
        report["enabled"] = True
        report["status"] = "APPLIED_CLASS0" if changed else runtime_report.get(
            "status", "NO_VEHICLE"
        )
        report["vehicle_points_applied"] = changed
        report["target_classes_changed_to_las0"] = changed
        report["protected_las2_changed"] = 0

        print(
            "[LiDAR Vehicle] "
            f"status={report['status']} "
            f"clusters={report.get('accepted_vehicle_clusters', 0)} "
            f"detected={report.get('vehicle_points_detected', 0)} "
            f"inside_fence={report.get('vehicle_points_in_fence', 0)} "
            f"LAS0_applied={changed} "
            "LAS2_changed=0",
            flush=True,
        )

        if progress:
            progress(
                f"LiDAR Vehicle: {changed:,} confirmed vehicle point(s) -> LAS 0"
            )

        return classes, report

    except Exception as exc:
        report = dict(base_report)
        report["enabled"] = True
        report["status"] = "ERROR_FAIL_OPEN"
        report["error"] = str(exc)

        print(
            "[LiDAR Vehicle] ERROR_FAIL_OPEN - Premium classes preserved: "
            f"{exc}",
            flush=True,
        )
        return np.asarray(target_classes, dtype=np.uint8).copy(), report

    finally:
        shutil.rmtree(work, ignore_errors=True)
