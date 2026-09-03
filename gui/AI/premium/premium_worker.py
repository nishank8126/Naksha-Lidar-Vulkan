from __future__ import annotations

import json
import os
import shutil
import traceback
from pathlib import Path

import laspy
import numpy as np
from .lidar_vehicle_client import apply_lidar_vehicle_class0_fence
from .lidar_vehicle_full_client import apply_lidar_vehicle_class0_full_file
from PySide6.QtCore import QThread, Signal

from gui.AI.common.ptc_mapping import (
    remap_classes,
    remap_las_file_in_place,
)
from gui.AI.common.powerline_postprocess import (
    apply_power_asset_postprocess,
    estimate_hag_from_ground,
)


def _las14_point_format_for(source_format_id: int) -> int:
    """
    Return a LAS 1.4 point format that supports
    8-bit classification values 0-255.
    """
    source_format_id = int(source_format_id)

    if source_format_id in (0, 1):
        return 6

    if source_format_id in (2, 3):
        return 7

    if source_format_id == 4:
        return 9

    if source_format_id == 5:
        return 10

    if source_format_id in (6, 7, 8, 9, 10):
        return source_format_id

    return 7


def _make_extended_classification_header(source_header):
    """
    Create LAS 1.4 output header with a point format
    capable of storing classification codes 0-255.
    """
    source_pf = int(source_header.point_format.id)
    target_pf = _las14_point_format_for(source_pf)

    header = laspy.LasHeader(
        version="1.4",
        point_format=target_pf,
    )

    header.scales = np.asarray(source_header.scales).copy()
    header.offsets = np.asarray(source_header.offsets).copy()

    try:
        header.system_identifier = source_header.system_identifier
    except Exception:
        pass

    try:
        header.generating_software = source_header.generating_software
    except Exception:
        pass

    try:
        header.global_encoding = source_header.global_encoding
    except Exception:
        pass

    return header


class PremiumAIWorker(QThread):
    """GUI bridge for frozen Premium V4.2.

    Full-file mode
    --------------
    Runs the validated V4.2 engine on the complete LAS/LAZ.

    Fence mode
    ----------
    Runs V4.2 only on a LOCAL temporary LAS/LAZ around the selected fence,
    not on the complete source cloud.  The local file contains the selected
    fence points plus a configurable XY support buffer so HAG/PCA/geometry
    near the fence boundary still has neighbouring points.  Only the selected
    target_indices are merged back into the viewer/final output; support points
    are never applied to the project.

    This is the fast GUI fence path.  Because the frozen V4.2 engine sees a
    cropped cloud, fence-local predictions are not guaranteed byte-identical to
    a full-file V4.2 run.  Full-file mode remains the strict frozen reference.
    """

    progress = Signal(int, str)
    finished = Signal()
    error = Signal(str)

    STREAM_CHUNK = 1_000_000

    # 20 m is intentionally larger than the 10 m PCA/context radius while
    # keeping fence runs much smaller than the full cloud.  Change with the
    # environment variable NAKSHA_PREMIUM_FENCE_BUFFER_M if required.
    FENCE_CONTEXT_BUFFER_M = float(
        os.environ.get("NAKSHA_PREMIUM_FENCE_BUFFER_M", "20.0")
    )

    def __init__(
        self,
        data_dict,
        class_mapping,
        power_mapping,
        premium_controller,
        target_indices=None,
        advanced_config=None,
        enable_power_lines=False,
        ptc_active=False,
    ):
        super().__init__()
        self.data_dict = data_dict
        self.class_mapping = dict(class_mapping or {})
        self.power_mapping = dict(power_mapping or {})
        self.premium_controller = premium_controller
        self.target_indices = target_indices
        self.advanced_config = dict(advanced_config or {})
        self.enable_power_lines = bool(enable_power_lines)
        self.ptc_active = bool(ptc_active)
        self.ai_mode = "premium"
        self.output_path: Path | None = None
        self._cancel_requested = False
        self._temp_root: Path | None = None
        self._vehicle_report: dict = {
            "enabled": False,
            "status": "DISABLED",
            "vehicle_points_applied": 0,
        }
        self._power_report: dict = {
            "enabled": bool(self.enable_power_lines),
            "status": "DISABLED" if not self.enable_power_lines else "PENDING",
            "wires_final": 0,
            "poles_final": 0,
        }

    def cancel(self) -> None:
        self._cancel_requested = True
        try:
            self.premium_controller.cancel()
        except Exception:
            pass

    @staticmethod
    def _source_path(data_dict) -> Path:
        source = data_dict.get("_source_file_path")
        if not source:
            raise RuntimeError(
                "Source LAS/LAZ path missing. Premium V4.2 needs the original file path."
            )
        path = Path(source).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Source LAS/LAZ not found: {path}")
        return path

    @staticmethod
    def _classification_array(data_dict, expected_points: int) -> np.ndarray:
        classes = data_dict.get("classification")
        if classes is None:
            raise RuntimeError("Viewer classification array is missing.")
        classes = np.asarray(classes)
        if len(classes) != expected_points:
            raise RuntimeError(
                "Viewer/source point-count mismatch: "
                f"viewer={len(classes):,}, source={expected_points:,}"
            )
        return classes

    @staticmethod
    def _xyz_array(data_dict, expected_points: int) -> np.ndarray:
        xyz = data_dict.get("xyz")
        if xyz is None:
            raise RuntimeError("Viewer XYZ array is missing; Premium fence mode needs XYZ.")
        xyz = np.asarray(xyz)
        if xyz.ndim != 2 or xyz.shape[1] < 2:
            raise RuntimeError(f"Viewer XYZ has invalid shape: {xyz.shape}")
        if len(xyz) != expected_points:
            raise RuntimeError(
                "Viewer/source XYZ point-count mismatch: "
                f"viewer={len(xyz):,}, source={expected_points:,}"
            )
        return xyz

    @staticmethod
    def _point_count(path: Path) -> int:
        with laspy.open(str(path), mode="r") as reader:
            return int(reader.header.point_count)

    @staticmethod
    def _valid_targets(target_indices, n_total: int) -> np.ndarray:
        target = np.asarray(target_indices, dtype=np.int64).ravel()
        target = target[(target >= 0) & (target < n_total)]
        if target.size == 0:
            raise RuntimeError("Premium fence mode received zero valid target points.")
        return np.unique(target)

    def _read_classification_stream(self, path: Path) -> np.ndarray:
        n = self._point_count(path)
        out = np.empty(n, dtype=np.uint8)
        pos = 0
        with laspy.open(str(path), mode="r") as reader:
            for points in reader.chunk_iterator(self.STREAM_CHUNK):
                if self._cancel_requested:
                    raise RuntimeError("Premium AI classification cancelled by user.")
                count = len(points)
                out[pos:pos + count] = np.asarray(points.classification, dtype=np.uint8)
                pos += count
        if pos != n:
            raise RuntimeError(f"Premium class stream mismatch: {pos:,}/{n:,}")
        return out

    def _write_classification_stream_in_place(self, path: Path, classes) -> None:
        classes = np.asarray(classes, dtype=np.uint8).reshape(-1)
        n = self._point_count(path)
        if len(classes) != n:
            raise RuntimeError(
                f"Premium power write length mismatch: {len(classes):,}/{n:,}"
            )

        tmp = path.with_name(f"{path.stem}.power_tmp{path.suffix}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

        pos = 0
        try:
            with laspy.open(str(path), mode="r") as reader:
                header = reader.header.copy()
                with laspy.open(
                    str(tmp),
                    mode="w",
                    header=header,
                    do_compress=(path.suffix.lower() == ".laz"),
                ) as writer:
                    for points in reader.chunk_iterator(self.STREAM_CHUNK):
                        if self._cancel_requested:
                            raise RuntimeError("Premium AI classification cancelled by user.")
                        count = len(points)
                        points.classification = classes[pos:pos + count]
                        writer.write_points(points)
                        pos += count
            if pos != n:
                raise RuntimeError(f"Premium power write mismatch: {pos:,}/{n:,}")
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise

    def _run_power_postpass_full_file(self, classified_path: Path) -> None:
        if not self.enable_power_lines:
            return

        try:
            self.progress.emit(99, "Premium external Power post-pass: Wire/Pole...")
            n = self._point_count(classified_path)
            xyz = self._xyz_array(self.data_dict, n)
            classes = self._read_classification_stream(classified_path)
            hag = estimate_hag_from_ground(
                xyz,
                classes,
                ground_codes=(2,),
                log=None,
            )

            updated, report = apply_power_asset_postprocess(
                xyz,
                classes,
                hag,
                wire_candidate_codes=(3, 4, 5),
                pole_candidate_codes=(4, 5, 6),
                protected_codes=(0, 2, 7, 18),
                wire_output_code=14,
                pole_output_code=15,
                active_indices=None,
                config=self.advanced_config,
                cl_coords=self.advanced_config.get("cl_corridor_coords"),
                cl_width=float(self.advanced_config.get(
                    "cl_corridor_width",
                    self.advanced_config.get("power_corridor_width", 0.0),
                ) or 0.0),
                log=None,
            )
            self._power_report = dict(report or {})

            if not np.array_equal(updated, classes):
                self._write_classification_stream_in_place(classified_path, updated)

            print(
                f"[Premium Power] wire={int(self._power_report.get('wires_final', 0)):,} "
                f"pole={int(self._power_report.get('poles_final', 0)):,}",
                flush=True,
            )
        except Exception as exc:
            # Fail-open: preserve completed frozen Premium + vehicle output.
            self._power_report = {
                "enabled": True,
                "status": "ERROR_FAIL_OPEN",
                "wires_final": 0,
                "poles_final": 0,
                "error": str(exc),
            }
            print(
                f"[Premium Power] WARNING: external power post-pass skipped; "
                f"Premium result preserved: {exc}",
                flush=True,
            )

    def _run_power_postpass_fence(
        self,
        engine_output: Path,
        target_local: np.ndarray,
        target_classes: np.ndarray,
    ) -> np.ndarray:
        if not self.enable_power_lines:
            return target_classes

        try:
            local_las = laspy.read(str(engine_output))
            xyz = np.column_stack([
                np.asarray(local_las.x, dtype=np.float64),
                np.asarray(local_las.y, dtype=np.float64),
                np.asarray(local_las.z, dtype=np.float64),
            ])
            classes = np.asarray(local_las.classification, dtype=np.uint8).copy()
            target_local = np.asarray(target_local, dtype=np.int64)

            # Vehicle post-filter has already run on target_classes. Put those
            # final target values into the support cloud before Power detection.
            classes[target_local] = np.asarray(target_classes, dtype=np.uint8)

            hag = estimate_hag_from_ground(
                xyz,
                classes,
                ground_codes=(2,),
                log=None,
            )

            updated, report = apply_power_asset_postprocess(
                xyz,
                classes,
                hag,
                wire_candidate_codes=(3, 4, 5),
                pole_candidate_codes=(4, 5, 6),
                protected_codes=(0, 2, 7, 18),
                wire_output_code=14,
                pole_output_code=15,
                active_indices=target_local,
                config=self.advanced_config,
                cl_coords=self.advanced_config.get("cl_corridor_coords"),
                cl_width=float(self.advanced_config.get(
                    "cl_corridor_width",
                    self.advanced_config.get("power_corridor_width", 0.0),
                ) or 0.0),
                log=None,
            )
            self._power_report = dict(report or {})
            return np.asarray(updated[target_local], dtype=np.uint8)

        except Exception as exc:
            self._power_report = {
                "enabled": True,
                "status": "ERROR_FAIL_OPEN",
                "wires_final": 0,
                "poles_final": 0,
                "error": str(exc),
            }
            print(
                f"[Premium Power] WARNING: fence power post-pass skipped; "
                f"Premium result preserved: {exc}",
                flush=True,
            )
            return target_classes

    def _combined_final_mapping(self):
        mapping = {}
        source_to_internal = {}

        if self.ptc_active:
            mapping.update(dict(self.class_mapping or {}))
            mapping.update(dict(
                self.advanced_config.get("_ptc_optional_semantic_codes", {}) or {}
            ))
            source_to_internal.update({
                2: 0, 3: 1, 4: 2, 5: 3, 6: 4,
                0: "uncategorized",
                7: "low_point",
                18: "high_noise",
            })

        if self.enable_power_lines:
            mapping.update(dict(self.power_mapping or {}))
            source_to_internal.update({14: 5, 15: 6})

        return mapping, source_to_internal

    def _apply_full_output_to_viewer(self, classified_path: Path) -> int:
        n_total = self._point_count(classified_path)
        live = self._classification_array(self.data_dict, n_total)

        pos = 0
        with laspy.open(str(classified_path), mode="r") as reader:
            for points in reader.chunk_iterator(self.STREAM_CHUNK):
                if self._cancel_requested:
                    raise RuntimeError("Premium AI classification cancelled by user.")
                count = len(points)
                cls = np.asarray(points.classification, dtype=np.uint8)
                live[pos:pos + count] = cls
                pos += count

        if pos != n_total:
            raise RuntimeError(f"Premium output read mismatch: {pos:,}/{n_total:,}")
        return n_total

    def _build_fence_local_input(
        self,
        source_path: Path,
        subset_path: Path,
        target_indices,
    ) -> tuple[np.ndarray, np.ndarray, int, tuple[float, float, float, float]]:
        """Create a temporary local LAS/LAZ around the selected fence.

        Returns
        -------
        target_global : sorted original source indices selected by the fence
        target_local  : corresponding indices inside the temporary local file
        subset_count  : number of points V4.2 will actually process
        bbox           : buffered XY bounds used for the local source
        """
        n_total = self._point_count(source_path)
        target = self._valid_targets(target_indices, n_total)
        xyz = self._xyz_array(self.data_dict, n_total)

        target_xy = np.asarray(xyz[target, :2], dtype=np.float64)
        buffer_m = max(float(self.FENCE_CONTEXT_BUFFER_M), 0.0)

        min_x = float(np.min(target_xy[:, 0]) - buffer_m)
        max_x = float(np.max(target_xy[:, 0]) + buffer_m)
        min_y = float(np.min(target_xy[:, 1]) - buffer_m)
        max_y = float(np.max(target_xy[:, 1]) + buffer_m)
        bbox = (min_x, max_x, min_y, max_y)

        subset_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if subset_path.exists():
                subset_path.unlink()
        except Exception:
            pass

        original_index_parts: list[np.ndarray] = []
        source_pos = 0
        subset_count = 0

        self.progress.emit(
            4,
            f"Building Premium fence-local input: {len(target):,} target points + "
            f"{buffer_m:.1f} m context...",
        )

        with laspy.open(str(source_path), mode="r") as reader:
            header = reader.header.copy()
            with laspy.open(
                str(subset_path),
                mode="w",
                header=header,
                do_compress=(subset_path.suffix.lower() == ".laz"),
            ) as writer:
                for points in reader.chunk_iterator(self.STREAM_CHUNK):
                    if self._cancel_requested:
                        raise RuntimeError("Premium AI classification cancelled by user.")

                    count = len(points)
                    x = np.asarray(points.x, dtype=np.float64)
                    y = np.asarray(points.y, dtype=np.float64)
                    mask = (
                        (x >= min_x) & (x <= max_x) &
                        (y >= min_y) & (y <= max_y)
                    )

                    if np.any(mask):
                        local_global = np.flatnonzero(mask).astype(np.int64) + source_pos
                        original_index_parts.append(local_global)
                        selected_points = points[mask]
                        writer.write_points(selected_points)
                        subset_count += len(selected_points)

                    source_pos += count

        if source_pos != n_total:
            raise RuntimeError(
                f"Premium fence source scan mismatch: {source_pos:,}/{n_total:,}"
            )
        if subset_count == 0 or not original_index_parts:
            raise RuntimeError("Premium fence-local crop contains zero points.")

        subset_global_indices = np.concatenate(original_index_parts)
        if len(subset_global_indices) != subset_count:
            raise RuntimeError("Premium fence-local index map size mismatch.")

        # subset_global_indices is naturally sorted because source chunks are
        # streamed in original point order.
        target_local = np.searchsorted(subset_global_indices, target)
        if np.any(target_local >= len(subset_global_indices)):
            raise RuntimeError("Premium fence target-to-local mapping overflow.")
        if not np.array_equal(subset_global_indices[target_local], target):
            missing = target[subset_global_indices[target_local] != target]
            raise RuntimeError(
                f"Premium fence-local crop lost {len(missing):,} selected target points."
            )

        return target, target_local.astype(np.int64), int(subset_count), bbox

    def _read_target_classes_from_local_output(
        self,
        engine_output: Path,
        target_local: np.ndarray,
    ) -> np.ndarray:
        subset_count = self._point_count(engine_output)
        if target_local.size == 0:
            raise RuntimeError("Premium target-local index list is empty.")
        if int(target_local[-1]) >= subset_count:
            raise RuntimeError(
                "Premium local output is shorter than the target index map: "
                f"max_target={int(target_local[-1]):,}, output={subset_count:,}"
            )

        selected_cls = np.empty(len(target_local), dtype=np.uint8)
        output_pos = 0

        with laspy.open(str(engine_output), mode="r") as reader:
            for points in reader.chunk_iterator(self.STREAM_CHUNK):
                if self._cancel_requested:
                    raise RuntimeError("Premium AI classification cancelled by user.")

                count = len(points)
                end = output_pos + count
                lo = int(np.searchsorted(target_local, output_pos, side="left"))
                hi = int(np.searchsorted(target_local, end, side="left"))
                if hi > lo:
                    local_idx = target_local[lo:hi] - output_pos
                    cls = np.asarray(points.classification, dtype=np.uint8)
                    selected_cls[lo:hi] = cls[local_idx]
                output_pos = end

        if output_pos != subset_count:
            raise RuntimeError(
                f"Premium local output read mismatch: {output_pos:,}/{subset_count:,}"
            )
        return selected_cls

    def _merge_fence_local_output(
        self,
        source_path: Path,
        engine_output: Path,
        final_output: Path,
        target_global: np.ndarray,
        target_local: np.ndarray,
    ) -> int:
        """Apply only selected fence predictions; outside fence stays unchanged."""
        n_total = self._point_count(source_path)
        live = self._classification_array(self.data_dict, n_total)
        original_viewer_classes = np.asarray(live, dtype=np.uint8).copy()

        target_classes = self._read_target_classes_from_local_output(
            engine_output=engine_output,
            target_local=target_local,
        )
        if len(target_classes) != len(target_global):
            raise RuntimeError("Premium fence result count mismatch.")

        # -----------------------------------------------------------------
        # Premium Open3D-ML Vehicle AI (post-filter only)
        # -----------------------------------------------------------------
        # # Frozen V4.2/V3.3 has already completed. The external PointPillars
        # # runtime is allowed to return point indices only; this worker owns the
        # # final fence restriction and LAS0 write-back. LAS2 Ground is protected
        # # by the vehicle runtime. If Open3D-ML is unavailable, the wrapper can
        # # fall back to the existing conservative V2 guard. Any failure is
        # # fail-open: successful Premium classes are preserved.
        # try:
        #     self.progress.emit(99, "Premium Open3D Vehicle AI: detecting vehicles...")
        #     vehicle_local, self._vehicle_report = detect_premium_vehicle_indices(
        #         classified_path=engine_output,
        #         restrict_indices=target_local,
        #         cfg=VEHICLE_AI_CONFIG,
        #         progress=lambda msg: self.progress.emit(99, msg),
        #     )

        #     if len(vehicle_local) > 0:
        #         vehicle_pos = np.searchsorted(target_local, vehicle_local)
        #         valid = (vehicle_pos >= 0) & (vehicle_pos < len(target_local))
        #         vehicle_pos = vehicle_pos[valid]
        #         vehicle_local_checked = vehicle_local[valid]
        #         exact = target_local[vehicle_pos] == vehicle_local_checked
        #         vehicle_pos = vehicle_pos[exact]

        #         if len(vehicle_pos) > 0:
        #             target_classes[vehicle_pos] = 0
        #             self._vehicle_report["vehicle_points_applied"] = int(len(vehicle_pos))
        #             print(
        #                 f"[Premium Open3D Vehicle AI] Fence write-back: {len(vehicle_pos):,} "
        #                 "confirmed vehicle point(s) -> LAS 0",
        #                 flush=True,
        #             )
        # except Exception as vehicle_exc:
        #     self._vehicle_report = {
        #         "enabled": bool(VEHICLE_AI_CONFIG.enabled),
        #         "engine": "Open3D-ML PointPillars isolated runtime",
        #         "status": "ERROR_FAIL_OPEN",
        #         "vehicle_points_applied": 0,
        #         "error": str(vehicle_exc),
        #     }
        #     print(
        #         f"[Premium Open3D Vehicle AI] WARNING: skipped after error; "
        #         f"Premium classes preserved: {vehicle_exc}",
        #         flush=True,
        #     )
        # -----------------------------------------------------------------
        # Naksha LiDAR-only Vehicle Post-filter
        # Confirmed vehicle points from Premium LAS 3/4/5/6 -> LAS 0.
        # LAS 2 Ground is protected in both the isolated runtime and here.
        # Any failure is fail-open and preserves Premium classifications.
        # -----------------------------------------------------------------
        target_classes, self._vehicle_report = apply_lidar_vehicle_class0_fence(
            classified_path=engine_output,
            target_local=target_local,
            target_classes=target_classes,
            progress=lambda msg: self.progress.emit(99, msg),
        )

        # External Power runs after frozen Premium QC and after the vehicle
        # post-filter. It can only change exact fence targets; support points are
        # used for geometry/continuity only.
        target_classes = self._run_power_postpass_fence(
            engine_output=engine_output,
            target_local=target_local,
            target_classes=target_classes,
        )

        # Final PTC/custom mapping. No PTC means frozen Premium 0/2..7/18 stays
        # untouched; Power still respects the user's Wire/Pole output codes.
        final_mapping, source_to_internal = self._combined_final_mapping()
        if final_mapping and source_to_internal:
            target_classes = remap_classes(
                target_classes,
                source_to_internal=source_to_internal,
                class_mapping=final_mapping,
            )
            print(
                f"[PTC/Power] Premium fence final mapping: {final_mapping}",
                flush=True,
            )

        live[target_global] = target_classes
        final_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            if final_output.exists():
                final_output.unlink()
        except Exception:
            pass

        written = 0
        start = 0

        with laspy.open(str(source_path), mode="r") as src_reader:

            output_header = _make_extended_classification_header(
                src_reader.header
            )

            print(
                f"[Premium] Extended classification output: "
                f"LAS 1.4 | source PF={src_reader.header.point_format.id} "
                f"-> output PF={output_header.point_format.id}",
                flush=True,
            )

            with laspy.open(
                str(final_output),
                mode="w",
                header=output_header,
                do_compress=(final_output.suffix.lower() == ".laz"),
            ) as writer:

                for src_points in src_reader.chunk_iterator(self.STREAM_CHUNK):

                    if self._cancel_requested:
                        raise RuntimeError(
                            "Premium AI classification cancelled by user."
                        )

                    count = len(src_points)
                    end = start + count

                    merged_cls = original_viewer_classes[start:end].copy()

                    lo = int(
                        np.searchsorted(
                            target_global,
                            start,
                            side="left"
                        )
                    )

                    hi = int(
                        np.searchsorted(
                            target_global,
                            end,
                            side="left"
                        )
                    )

                    if hi > lo:
                        global_idx = target_global[lo:hi]
                        local_idx = global_idx - start

                        merged_cls[local_idx] = target_classes[lo:hi]
                        written += hi - lo

                    # Convert legacy LAS point record into LAS 1.4
                    # point format BEFORE assigning classes > 31.
                    out_points = laspy.PackedPointRecord.from_point_record(
                        src_points,
                        output_header.point_format,
                    )

                    out_points.classification = np.asarray(
                        merged_cls,
                        dtype=np.uint8,
                    )

                    writer.write_points(out_points)

                    start = end

        if start != n_total:
            raise RuntimeError(f"Premium fence final-write mismatch: {start:,}/{n_total:,}")
        if written != len(target_global):
            raise RuntimeError(
                f"Premium fence merge updated {written:,}/{len(target_global):,} selected points."
            )
        return int(written)

    def _write_gui_report(
        self,
        final_output: Path,
        source_path: Path,
        processed_points: int,
        total_points: int,
        fence_mode: bool,
        engine_points: int | None = None,
        fence_bbox: tuple[float, float, float, float] | None = None,
    ) -> None:
        report = {
            "status": "COMPLETED",
            "integration": "Naksha GUI Premium V4.2",
            "engine": "premium_best_guarded_inference_v4_2_persistent_exact_candidate",
            "source": str(source_path),
            "output": str(final_output),
            "fence_mode": bool(fence_mode),
            "processed_points": int(processed_points),
            "total_source_points": int(total_points),
            "engine_input_points": int(engine_points if engine_points is not None else total_points),
            "fence_context_buffer_m": (
                float(self.FENCE_CONTEXT_BUFFER_M) if fence_mode else None
            ),
            "fence_bbox_xy": list(fence_bbox) if fence_bbox is not None else None,
            "vehicle_filter": dict(self._vehicle_report),
            "power_postpass": dict(self._power_report),
            "ptc_active": bool(self.ptc_active),
            "fence_accuracy_policy": (
                "Fence-local fast mode: frozen V4.2 runs only on a buffered local crop; "
                "only selected target indices are merged back. Faster than full-cloud fence "
                "mode, but cropped-context predictions are not guaranteed byte-identical "
                "to a full-file V4.2 run."
                if fence_mode else
                "Frozen V4.2 full-file classification."
            ),
        }
        path = final_output.with_suffix(final_output.suffix + ".premium_gui_report.json")
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    def run(self) -> None:
        engine_output: Path | None = None
        fence_mode = self.target_indices is not None
        engine_points: int | None = None
        fence_bbox: tuple[float, float, float, float] | None = None

        try:
            self.progress.emit(1, "Preparing Premium AI V4.2...")
            source = self._source_path(self.data_dict)
            total_points = self._point_count(source)
            self._classification_array(self.data_dict, total_points)

            output_dir = source.parent / "Premium_AI_Classified"
            output_dir.mkdir(parents=True, exist_ok=True)
            final_output = output_dir / f"{source.stem}_premium_classified{source.suffix}"
            self.output_path = final_output

            if fence_mode:
                self._temp_root = output_dir / "_premium_v42_fence_work"
                shutil.rmtree(self._temp_root, ignore_errors=True)
                self._temp_root.mkdir(parents=True, exist_ok=True)

                local_input = self._temp_root / f"{source.stem}_fence_local_input{source.suffix}"
                engine_output = self._temp_root / f"{source.stem}_fence_local_classified{source.suffix}"

                target_global, target_local, engine_points, fence_bbox = \
                    self._build_fence_local_input(
                        source_path=source,
                        subset_path=local_input,
                        target_indices=self.target_indices,
                    )

                reduction = 100.0 * (1.0 - (engine_points / max(total_points, 1)))
                self.progress.emit(
                    6,
                    f"Premium fence-local mode: target={len(target_global):,}, "
                    f"engine input={engine_points:,}/{total_points:,} points "
                    f"({reduction:.1f}% less than full cloud).",
                )

                engine_output = self.premium_controller.run(
                    input_path=local_input,
                    output_path=engine_output,
                    progress_callback=self.progress.emit,
                )

                if self._cancel_requested:
                    raise RuntimeError("Premium AI classification cancelled by user.")

                self.progress.emit(99, "Applying Premium result only inside selected fence(s)...")
                processed = self._merge_fence_local_output(
                    source_path=source,
                    engine_output=engine_output,
                    final_output=final_output,
                    target_global=target_global,
                    target_local=target_local,
                )

            else:
                engine_output = final_output
                engine_points = total_points
                self.progress.emit(3, f"Premium full-file mode: {total_points:,} points.")

                engine_output = self.premium_controller.run(
                    input_path=source,
                    output_path=engine_output,
                    progress_callback=self.progress.emit,
                )

                if self._cancel_requested:
                    raise RuntimeError("Premium AI classification cancelled by user.")

                # -------------------------------------------------------------
                # Naksha LiDAR-only FULL-FILE Vehicle Post-filter
                # Runs AFTER frozen Premium classification and BEFORE viewer load.
                # Only LAS 3/4/5/6 can become LAS 0. LAS 2 Ground is protected.
                # Tiled external runtime; any failure is fail-open.
                # -------------------------------------------------------------
                self._vehicle_report = apply_lidar_vehicle_class0_full_file(
                    classified_path=engine_output,
                    progress=lambda msg: self.progress.emit(99, msg),
                )

                # Frozen Premium QC and vehicle filtering are complete. Only now
                # run the isolated external Power post-pass.
                self._run_power_postpass_full_file(engine_output)

                # PTC/custom remap is the final layer. The frozen engine is never
                # modified and no-PCT/no-Power behavior stays byte-semantically
                # equivalent in classification codes.
                final_mapping, source_to_internal = self._combined_final_mapping()
                if final_mapping and source_to_internal:
                    self.progress.emit(
                        99,
                        "Applying final PTC / Power output mapping..."
                    )
                    remap_las_file_in_place(
                        engine_output,
                        source_to_internal=source_to_internal,
                        class_mapping=final_mapping,
                        chunk_size=self.STREAM_CHUNK,
                    )
                # Full-file Premium-only Open3D-ML vehicle post-filter. The
                # # frozen engine output is complete first. Vehicle AI returns a
                # # point mask and only those confirmed LAS3/4/5/6 points become
                # # LAS0. Failure is fail-open and leaves Premium output intact.
                # try:
                #     self.progress.emit(99, "Premium Open3D Vehicle AI: detecting vehicles...")
                #     self._vehicle_report = apply_premium_vehicle_ai_to_file(
                #         classified_path=engine_output,
                #         cfg=VEHICLE_AI_CONFIG,
                #         progress=lambda msg: self.progress.emit(99, msg),
                #     )
                # except Exception as vehicle_exc:
                #     self._vehicle_report = {
                #         "enabled": bool(VEHICLE_AI_CONFIG.enabled),
                #         "engine": "Open3D-ML PointPillars isolated runtime",
                #         "status": "ERROR_FAIL_OPEN",
                #         "vehicle_points_applied": 0,
                #         "error": str(vehicle_exc),
                #     }
                #     print(
                #         f"[Premium Open3D Vehicle AI] WARNING: skipped after error; "
                #         f"Premium classes preserved: {vehicle_exc}",
                #         flush=True,
                #     )

                # self.progress.emit(99, "Loading Premium classifications into viewer...")
                processed = self._apply_full_output_to_viewer(engine_output)

            self._write_gui_report(
                final_output=final_output,
                source_path=source,
                processed_points=processed,
                total_points=total_points,
                fence_mode=fence_mode,
                engine_points=engine_points,
                fence_bbox=fence_bbox,
            )

            self.progress.emit(
                100,
                "Premium AI V4.2 classification complete.",
            )
            self.finished.emit()

        except Exception:
            self.error.emit(traceback.format_exc())

        finally:
            if fence_mode and self._temp_root is not None:
                shutil.rmtree(self._temp_root, ignore_errors=True)


# Current ai_dialog imports this exact legacy name.
PremiumInferenceWorker = PremiumAIWorker



