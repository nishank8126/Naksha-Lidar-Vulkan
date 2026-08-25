# gui/file_loader_worker.py
"""
Background worker that handles the I/O-heavy portion of LiDAR loading.
No Qt widget access is allowed here — only signals back to the main thread.
"""
import os
import numpy as np
from PySide6.QtCore import QThread, Signal


class FileLoaderWorker(QThread):
    """
    Runs Phase 1-3 of the LiDAR load pipeline off the main thread:
      Phase 1 – scan / read each file via load_lidar_file()
      Phase 2 – pre-allocate merged numpy arrays
      Phase 3 – fill / merge arrays (zero-copy)

    Everything that touches VTK or Qt widgets is left to the main thread.
    """

    # ── signals ────────────────────────────────────────────────────────
    progress       = Signal(int, str)   # (percent 0-100, status text)
    points_counted = Signal(int)        # emitted once total is known
    finished       = Signal(dict)       # merged result dict on success
    error          = Signal(str)        # human-readable error on failure
    cancelled      = Signal()           # user cancelled

    def __init__(self, filenames, import_options, parent=None):
        super().__init__(parent)
        self.filenames      = filenames
        self.import_options = import_options
        self._cancel        = False

    # ── public API ─────────────────────────────────────────────────────
    def request_cancel(self):
        self._cancel = True

    # ── thread entry point ─────────────────────────────────────────────
    def run(self):
        try:
            self._load()
        except MemoryError:
            self.error.emit(
                "Out of memory!\n"
                "Try loading fewer files or close other applications."
            )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.error.emit(str(exc))

    # ── internal ───────────────────────────────────────────────────────
    def _load(self):
        # Late import — safe inside QThread (no Qt widget access)
        from gui.data_loader import load_lidar_file

        filenames      = self.filenames
        import_options = self.import_options

        # ── Phase 1: scan each file ────────────────────────────────────
        self.progress.emit(2, "Scanning files…")
        file_info    = []
        total_points = 0

        for i, filename in enumerate(filenames):
            if self._cancel:
                self.cancelled.emit()
                return

            try:
                # Force RGB + Intensity ON (same logic as original)
                forced          = dict(import_options) if import_options else {}
                forced_attrs    = dict(forced.get("attributes", {}))
                forced_attrs["Color"]     = True
                forced_attrs["Intensity"] = True
                forced["attributes"]      = forced_attrs

                tile_data = load_lidar_file(
                    filename,
                    parent=None,
                    import_options=forced,
                    prompt_user=False,
                )
                if not tile_data:
                    print(f"   ⚠️ Skipping: {os.path.basename(filename)}")
                    continue

                n          = len(tile_data.get("xyz", []))
                has_rgb    = tile_data.get("rgb")       is not None
                has_int    = tile_data.get("intensity") is not None
                has_source = tile_data.get("point_source_id") is not None
                total_points += n

                # Debug output (same as original)
                if has_rgb:
                    rgb_check = tile_data["rgb"]
                    print(
                        f"   ✅ RGB loaded: dtype={rgb_check.dtype}, "
                        f"max={rgb_check.max()}, min={rgb_check.min()}, "
                        f"mean={rgb_check.mean():.1f}"
                    )
                else:
                    print(f"   ⚠️ RGB is None after forced load!")

                file_info.append({
                    "filename"     : filename,
                    "data"         : tile_data,
                    "n_points"     : n,
                    "has_rgb"      : has_rgb,
                    "has_intensity": has_int,
                    "has_point_source_id": has_source,
                })
                print(f"   {i+1}. {os.path.basename(filename)}: {n:,} points")

            except Exception as exc:
                import traceback
                print(f"   ❌ Failed to scan {os.path.basename(filename)}: {exc}")
                traceback.print_exc()

            # Incremental progress for scan phase (2-10%)
            pct = 2 + int((i + 1) / len(filenames) * 8)
            self.progress.emit(pct, f"Scanned {i+1}/{len(filenames)} files…")

        if not file_info:
            self.error.emit("No files could be read successfully.")
            return

        self.points_counted.emit(total_points)
        print(f"\n✅ Scan complete: {len(file_info)} files, {total_points:,} total points")

        # ── Single-file fast path ──────────────────────────────────────
        # The Phase-2 merge below allocates a *second* copy of every array
        # (xyz alone is 7.2GB for 300M float64 points), then copies the
        # already-loaded arrays in. For a one-file load that doubles peak RAM
        # for zero benefit. Reuse the arrays directly.
        if len(file_info) == 1:
            self.progress.emit(60, "Finalising arrays…")
            only = file_info[0]
            td = only["data"]
            result = {
                "xyz"            : td["xyz"],
                "classification" : td["classification"],
                "crs_epsg"       : td.get("crs_epsg"),
                "crs_wkt"        : td.get("crs_wkt"),
                "input_format_version": td.get("input_format_version"),
                "first_file"     : only["filename"],
                "layer_info_list": [{
                    "filename" : only["filename"],
                    "n_points" : only["n_points"],
                    "crs_epsg" : td.get("crs_epsg"),
                }],
                "total_points"   : only["n_points"],
                "num_files"      : 1,
            }
            if only["has_rgb"]:
                result["rgb"] = td["rgb"]
            if only["has_intensity"]:
                result["intensity"] = td["intensity"]
            if only["has_point_source_id"]:
                result["point_source_id"] = td["point_source_id"]

            # Drop the wrapper so the only reference left is `result`.
            file_info.clear()
            del td

            self.progress.emit(95, "Transferring to main thread…")
            self.finished.emit(result)
            return

        # ── Phase 2: pre-allocate (multi-file merge) ───────────────────
        self.progress.emit(10, "Allocating memory…")
        merged_xyz  = np.empty((total_points, 3), dtype=np.float64)
        merged_cls  = np.empty(total_points,       dtype=np.uint8)
        has_any_rgb = any(f["has_rgb"]       for f in file_info)
        has_any_int = any(f["has_intensity"] for f in file_info)
        has_any_source = any(f["has_point_source_id"] for f in file_info)
        merged_rgb  = np.empty((total_points, 3), dtype=np.uint8)  if has_any_rgb else None
        merged_int  = np.empty(total_points,       dtype=np.float32) if has_any_int else None
        merged_source = np.empty(total_points,     dtype=np.uint16) if has_any_source else None

        print(f"   ✅ Allocated {total_points:,} points")
        print(f"      XYZ:            {merged_xyz.nbytes / (1024**2):.1f} MB")
        print(f"      Classification: {merged_cls.nbytes  / (1024**2):.1f} MB")
        if merged_rgb is not None:
            print(f"      RGB:            {merged_rgb.nbytes / (1024**2):.1f} MB")
        if merged_int is not None:
            print(f"      Intensity:      {merged_int.nbytes / (1024**2):.1f} MB")

        # ── Phase 3: fill / merge (zero copy) ─────────────────────────
        print(f"\n📥 Phase 3: Filling arrays…")
        offset           = 0
        first_crs_epsg   = None
        first_crs_wkt    = None
        first_input_format_version = None
        first_file       = file_info[0]["filename"]
        layer_info_list  = []   # serialisable metadata for main thread

        for i, info in enumerate(file_info):
            if self._cancel:
                self.cancelled.emit()
                return

            pct = 15 + int((i / len(file_info)) * 70)
            self.progress.emit(pct, f"Merging file {i+1}/{len(file_info)}…")

            td = info["data"]
            n  = info["n_points"]

            merged_xyz[offset:offset + n] = td["xyz"]
            merged_cls[offset:offset + n] = td["classification"]

            if merged_rgb is not None:
                merged_rgb[offset:offset + n] = (
                    td["rgb"] if info["has_rgb"] else 128
                )
            if merged_int is not None:
                merged_int[offset:offset + n] = (
                    td["intensity"] if info["has_intensity"] else 0
                )
            if merged_source is not None:
                merged_source[offset:offset + n] = (
                    td["point_source_id"] if info["has_point_source_id"] else 0
                )

            # CRS from first file only
            if i == 0:
                first_crs_epsg = td.get("crs_epsg")
                first_crs_wkt  = td.get("crs_wkt")
                first_input_format_version = td.get("input_format_version")

            # Collect layer metadata (lightweight — no large arrays)
            layer_info_list.append({
                "filename"  : info["filename"],
                "n_points"  : n,
                "crs_epsg"  : td.get("crs_epsg"),
            })

            offset += n
            # `del td` alone leaves the arrays alive via info["data"]. Clear
            # both references so the tile arrays are freed before the next
            # iteration allocates the next tile.
            info["data"] = None
            del td

        print("   ✅ All files merged (zero copy)")

        # ── Emit result dict ───────────────────────────────────────────
        self.progress.emit(88, "Transferring to main thread…")
        result = {
            "xyz"            : merged_xyz,
            "classification" : merged_cls,
            "crs_epsg"       : first_crs_epsg,
            "crs_wkt"        : first_crs_wkt,
            "input_format_version": first_input_format_version,
            "first_file"     : first_file,
            "layer_info_list": layer_info_list,
            "total_points"   : total_points,
            "num_files"      : len(filenames),
        }
        if merged_rgb is not None:
            result["rgb"] = merged_rgb
        if merged_int is not None:
            result["intensity"] = merged_int
        if merged_source is not None:
            result["point_source_id"] = merged_source

        self.finished.emit(result)
