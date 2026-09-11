import os
import json
import shutil
import tempfile
import numpy as np
import laspy
from PySide6.QtWidgets import QFileDialog, QMessageBox
from PySide6.QtCore import Qt

def _parse_filter(selected_filter: str):
    sf = (selected_filter or "").lower()
    if "laz" in sf and "1.2" in sf:
        return ".laz", "1.2"
    if "laz" in sf and "1.4" in sf:
        return ".laz", "1.4"
    if "las" in sf and "1.2" in sf:
        return ".las", "1.2"
    if "las" in sf and "1.4" in sf:
        return ".las", "1.4"
    return ".laz", "1.4"

def _ensure_ext(path: str, ext: str) -> str:
    root, cur_ext = os.path.splitext(path)
    if cur_ext.lower() != ext.lower():
        return root + ext
    return path

def _rgb_to_las16(rgb_arr):
    """Convert RGB to LAS 16-bit (0..65535). Accepts float(0..1) / uint8 / uint16."""
    if rgb_arr is None:
        return None
    rgb = np.asarray(rgb_arr)
    if rgb.ndim != 2 or rgb.shape[1] != 3:
        return None

    if np.issubdtype(rgb.dtype, np.floating):
        rgb = np.clip(rgb, 0.0, 1.0)
        return (rgb * 65535.0).round().astype(np.uint16)

    mx = int(rgb.max()) if rgb.size else 0
    if mx <= 255:
        return (rgb.astype(np.uint16) * 256)
    return np.clip(rgb, 0, 65535).astype(np.uint16)

def _intensity_to_uint16(intensity_arr, n_points: int):
    """LAS intensity is uint16. If intensity is missing, return None."""
    if intensity_arr is None:
        return None
    inten = np.asarray(intensity_arr)
    if inten.shape[0] != n_points:
        return None

    if np.issubdtype(inten.dtype, np.floating):
        mx = float(np.nanmax(inten)) if inten.size else 0.0
        if mx <= 1.0:
            inten = np.clip(inten, 0.0, 1.0) * 65535.0
        inten = np.clip(inten, 0.0, 65535.0)
        return inten.round().astype(np.uint16)

    return np.clip(inten, 0, 65535).astype(np.uint16)

def _choose_point_format(las_version: str, has_rgb: bool) -> int:
    """
    Use point formats valid for the chosen LAS version:
      - LAS 1.2: 0 (no RGB) or 2 (RGB)
      - LAS 1.4: 6 (no RGB) or 7 (RGB)
    """
    if las_version == "1.4":
        return 7 if has_rgb else 6
    return 2 if has_rgb else 0


def _import_options_reduce_points(import_options) -> bool:
    if not isinstance(import_options, dict):
        return False
    if bool(import_options.get("only_class")) and bool(import_options.get("class_codes")):
        return True
    if bool(import_options.get("only_every")) and int(import_options.get("nth_point", 1) or 1) > 1:
        return True
    return False


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except Exception:
        return os.path.normcase(a) == os.path.normcase(b)


def _assign_classification_to_las(las, classes_u8: np.ndarray) -> None:
    version = las.header.version
    las_version = f"{int(version.major)}.{int(version.minor)}"

    if las_version == "1.4":
        las.classification = np.clip(classes_u8, 0, 255).astype(np.uint8, copy=False)
        return

    max_cls = int(classes_u8.max()) if classes_u8.size else 0
    if max_cls > 31:
        las.classification = (classes_u8 & 0x1F).astype(np.uint8, copy=False)
        las.synthetic = ((classes_u8 >> 5) & 1).astype(bool)
        las.key_point = ((classes_u8 >> 6) & 1).astype(bool)
        las.withheld = ((classes_u8 >> 7) & 1).astype(bool)
        return

    las.classification = np.clip(classes_u8, 0, 31).astype(np.uint8, copy=False)


def _try_build_preserved_las(
    *,
    source_path: str | None,
    target_path: str,
    xyz,
    classes_u8,
    rgb16,
    intensity16,
    import_options=None,
    requested_version: str | None = None,
    crs_wkt: str | None = None,
    crs_epsg=None,
):
    if not source_path:
        return None, "no source file available"
    if not os.path.exists(source_path):
        return None, f"source file missing: {source_path}"

    source_ext = os.path.splitext(source_path)[1].lower()
    target_ext = os.path.splitext(target_path)[1].lower()
    if source_ext not in (".las", ".laz"):
        return None, f"source is not LAS/LAZ: {source_ext}"
    if target_ext != source_ext:
        return None, f"target format {target_ext} differs from loaded source {source_ext}"
    if _import_options_reduce_points(import_options):
        return None, "loaded dataset is filtered/sampled and no longer matches the full source file"

    try:
        las = laspy.read(source_path)
    except Exception as exc:
        return None, f"could not read source for preserved save: {exc}"

    source_points = int(len(las.points))
    current_points = int(len(np.asarray(xyz)))
    if source_points != current_points:
        return None, (
            f"point-count mismatch between source ({source_points:,}) and loaded data "
            f"({current_points:,})"
        )

    source_version = f"{int(las.header.version.major)}.{int(las.header.version.minor)}"
    if requested_version and requested_version != source_version:
        return None, f"requested LAS version {requested_version} differs from source {source_version}"

    dims = {str(d).lower() for d in las.point_format.dimension_names}
    if rgb16 is not None and not {"red", "green", "blue"}.issubset(dims):
        return None, "loaded RGB data cannot be preserved because source point format has no RGB channels"
    if intensity16 is not None and "intensity" not in dims:
        return None, "loaded intensity data cannot be preserved because source point format has no intensity channel"

    xyz = np.asarray(xyz)
    las.x = xyz[:, 0]
    las.y = xyz[:, 1]
    las.z = xyz[:, 2]
    _assign_classification_to_las(las, np.asarray(classes_u8, dtype=np.uint8, copy=False))

    if rgb16 is not None and {"red", "green", "blue"}.issubset(dims):
        las.red = rgb16[:, 0]
        las.green = rgb16[:, 1]
        las.blue = rgb16[:, 2]
    if intensity16 is not None and "intensity" in dims:
        las.intensity = intensity16

    try:
        import pyproj
        if crs_wkt:
            las.header.parse_crs(pyproj.CRS.from_wkt(crs_wkt))
        elif crs_epsg:
            las.header.parse_crs(pyproj.CRS.from_epsg(crs_epsg))
    except Exception as exc:
        print(f"⚠️ Preserved-save CRS update skipped: {exc}")

    return las, None


def _empty_i64():
    return np.empty(0, dtype=np.int64)


def _empty_i32():
    return np.empty(0, dtype=np.int32)


def _as_i64(values):
    if values is None:
        return _empty_i64()
    return np.asarray(values, dtype=np.int64)


def _as_i32(values):
    if values is None:
        return _empty_i32()
    return np.asarray(values, dtype=np.int32)


def _inspect_fenced_parent_session(app):
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return None, None, False

    session_id = data.get("_fence_session_id")
    if not session_id:
        return None, None, False

    session = getattr(app, "_fence_parent_session", None)
    if not isinstance(session, dict) or session.get("session_id") != session_id:
        return None, (
            "The fenced source-file session is no longer available.\n"
            "Reload the fence before using Save."
        ), True

    xyz = data.get("xyz")
    if xyz is None:
        return None, "No fenced data is currently loaded.", True
    point_count = int(len(np.asarray(xyz)))

    source_files = [str(p) for p in list(session.get("source_files") or []) if str(p)]
    if not source_files:
        return None, "This fenced load has no parent-file list attached.", True

    source_file_ids = data.get("_fence_source_file_ids")
    source_point_indices = data.get("_fence_source_point_indices")
    if source_file_ids is None or source_point_indices is None:
        return None, (
            "The fenced point-source mapping is missing.\n"
            "Reload the fence before using Save."
        ), True

    source_file_ids = _as_i32(source_file_ids)
    source_point_indices = _as_i64(source_point_indices)
    if source_file_ids.shape[0] != point_count or source_point_indices.shape[0] != point_count:
        return None, (
            "The fenced dataset no longer matches its source-point map.\n"
            "Use Save As for the fenced subset, or reload the fence before using Save."
        ), True

    if np.any(source_file_ids < 0) or np.any(source_file_ids >= len(source_files)):
        return None, "The fenced source-file IDs are out of range.", True

    duplicate_owner_indices = _as_i64(session.get("duplicate_owner_indices"))
    duplicate_source_file_ids = _as_i32(session.get("duplicate_source_file_ids"))
    duplicate_source_point_indices = _as_i64(session.get("duplicate_source_point_indices"))

    if not (
        duplicate_owner_indices.shape[0] ==
        duplicate_source_file_ids.shape[0] ==
        duplicate_source_point_indices.shape[0]
    ):
        return None, "The fenced duplicate-point mapping is inconsistent.", True

    if duplicate_owner_indices.size:
        if np.any(duplicate_owner_indices < 0) or np.any(duplicate_owner_indices >= point_count):
            return None, "The fenced duplicate-point owners are out of range.", True
        if np.any(duplicate_source_file_ids < 0) or np.any(duplicate_source_file_ids >= len(source_files)):
            return None, "The fenced duplicate-point file IDs are out of range.", True

    return {
        "session_id": session_id,
        "point_count": point_count,
        "source_files": source_files,
        "operation": str(session.get("operation") or "fence"),
        "primary_grid": session.get("primary_grid"),
        "buffer_width": session.get("buffer_width"),
        "source_file_ids": source_file_ids,
        "source_point_indices": source_point_indices,
        "duplicate_owner_indices": duplicate_owner_indices,
        "duplicate_source_file_ids": duplicate_source_file_ids,
        "duplicate_source_point_indices": duplicate_source_point_indices,
    }, None, True


def has_fenced_parent_writeback(app) -> bool:
    session, _error, is_fence_mode = _inspect_fenced_parent_session(app)
    return bool(is_fence_mode and session is not None)


def save_current_pointcloud_in_place(app) -> bool:
    """Silently save the currently editable dataset to its real source."""
    data = getattr(app, "data", None)
    if not isinstance(data, dict) or data.get("xyz") is None:
        return False

    session, error, is_fence_mode = _inspect_fenced_parent_session(app)
    if is_fence_mode:
        if session is None:
            if error:
                print(f"Fence in-place save skipped: {error}")
            return False
        return _save_fenced_parent_files(
            app, session, output_dir=None, in_place=True, show_messages=False,
        )

    path = getattr(app, "last_save_path", None) or getattr(app, "loaded_file", None)
    return save_pointcloud_quick(app, path) if path else False


def _prompt_fenced_save_mode(app, session):
    source_count = len(session.get("source_files") or [])
    first_source = os.path.basename(session["source_files"][0]) if source_count else "parent file"

    box = QMessageBox(app)
    box.setIcon(QMessageBox.Question)
    is_buffered_grid = session.get("operation") == "buffered_grid"
    box.setWindowTitle("Save Buffered Grid" if is_buffered_grid else "Save Fenced Data")
    box.setText(
        "Choose how to save the buffered grid edits."
        if is_buffered_grid else
        "Choose how to save the fenced SNT load."
    )
    if is_buffered_grid:
        box.setInformativeText(
            f"Source grid files: {source_count}\n"
            f"Primary grid: {session.get('primary_grid') or first_source}\n"
            f"Buffer width: {float(session.get('buffer_width') or 0.0):.2f}\n\n"
            "Original source files: writes each edited point back to its own LAS/LAZ file.\n"
            "Full parent copies: creates separately saved complete tile copies.\n"
            "Buffered subset only: saves all displayed points into one combined file."
        )
    else:
        box.setInformativeText(
            f"Fence source files: {source_count}\n"
            f"First source: {first_source}\n\n"
            "Fenced subset only: saves just the visible fenced points into one output file.\n"
            "Full parent files: writes the fence edits back into full source tiles."
        )
    subset_btn = box.addButton(
        "Buffered Subset Only" if is_buffered_grid else "Fenced Subset Only",
        QMessageBox.AcceptRole,
    )
    parents_btn = box.addButton("Full Parent Files", QMessageBox.ActionRole)
    originals_btn = (
        box.addButton("Save to Original Source Files", QMessageBox.DestructiveRole)
        if is_buffered_grid else None
    )
    cancel_btn = box.addButton(QMessageBox.Cancel)
    box.exec()

    clicked = box.clickedButton()
    if clicked == parents_btn:
        return "parents"
    if originals_btn is not None and clicked == originals_btn:
        return "in_place"
    if clicked == subset_btn:
        return "subset"
    if clicked == cancel_btn:
        return None
    return None


def _copy_prj_sidecar(app, source_path: str, target_path: str) -> None:
    src_prj = os.path.splitext(source_path)[0] + ".prj"
    dst_prj = os.path.splitext(target_path)[0] + ".prj"
    try:
        if os.path.exists(src_prj):
            shutil.copyfile(src_prj, dst_prj)
            return
        if getattr(app, "project_crs_wkt", None):
            with open(dst_prj, "w") as fh:
                fh.write(app.project_crs_wkt)
        elif getattr(app, "project_crs_epsg", None):
            with open(dst_prj, "w") as fh:
                fh.write(f"EPSG:{app.project_crs_epsg}")
    except Exception as exc:
        print(f"⚠️ Failed to copy/write .prj for {os.path.basename(target_path)}: {exc}")


def _parent_upgrade_point_format(las_obj) -> int:
    dims = {str(d).lower() for d in las_obj.point_format.dimension_names}
    has_rgb = {"red", "green", "blue"}.issubset(dims)
    has_waveform = any(
        name in dims for name in (
            "wavepacket_index",
            "byte_offset_to_waveform_data",
            "waveform_packet_size_in_bytes",
            "return_point_waveform_location",
        )
    )
    if has_waveform:
        return 10 if has_rgb else 9
    return 7 if has_rgb else 6


def _build_fenced_parent_assignments(app, session):
    data = getattr(app, "data", {}) or {}
    point_count = int(session["point_count"])
    if point_count <= 0:
        return {}, None, None

    positions = np.arange(point_count, dtype=np.int64)
    source_files = list(session["source_files"])
    source_file_ids = session["source_file_ids"]
    source_point_indices = session["source_point_indices"]
    duplicate_owner_indices = session["duplicate_owner_indices"]
    duplicate_source_file_ids = session["duplicate_source_file_ids"]
    duplicate_source_point_indices = session["duplicate_source_point_indices"]

    assignments = {}
    for file_id, source_path in enumerate(source_files):
        point_idx_parts = []
        data_idx_parts = []

        primary_mask = (source_file_ids == file_id)
        if np.any(primary_mask):
            point_idx_parts.append(source_point_indices[primary_mask])
            data_idx_parts.append(positions[primary_mask])

        dup_mask = (duplicate_source_file_ids == file_id)
        if np.any(dup_mask):
            point_idx_parts.append(duplicate_source_point_indices[dup_mask])
            data_idx_parts.append(duplicate_owner_indices[dup_mask])

        if not point_idx_parts:
            continue

        point_indices = np.concatenate(point_idx_parts).astype(np.int64, copy=False)
        data_indices = np.concatenate(data_idx_parts).astype(np.int64, copy=False)

        order = np.argsort(point_indices, kind="stable")
        point_indices = point_indices[order]
        data_indices = data_indices[order]

        if point_indices.size > 1:
            keep_mask = np.ones(point_indices.size, dtype=bool)
            keep_mask[:-1] = point_indices[:-1] != point_indices[1:]
            point_indices = point_indices[keep_mask]
            data_indices = data_indices[keep_mask]

        assignments[source_path] = {
            "point_indices": point_indices,
            "data_indices": data_indices,
        }

    rgb16 = _rgb_to_las16(data.get("rgb"))
    intensity16 = _intensity_to_uint16(data.get("intensity"), n_points=point_count)
    return assignments, rgb16, intensity16


def _save_fenced_parent_files(app, session, output_dir=None, in_place=True, show_messages=True):
    data = getattr(app, "data", None)
    if not isinstance(data, dict) or data.get("xyz") is None:
        if show_messages:
            QMessageBox.warning(app, "Save", "No fenced data is loaded.")
        return False

    classes = data.get("classification")
    if classes is None:
        classes = np.zeros(session["point_count"], dtype=np.uint8)
    classes_u8 = np.asarray(classes).astype(np.uint8, copy=False)

    assignments, rgb16, intensity16 = _build_fenced_parent_assignments(app, session)
    if not assignments:
        if show_messages:
            QMessageBox.information(app, "Save", "No parent-file updates were found for this fence selection.")
        return False

    if not in_place:
        if not output_dir:
            if show_messages:
                QMessageBox.information(app, "Save As", "No output folder was selected.")
            return False

        basenames = {}
        for source_path in assignments:
            base_key = os.path.basename(source_path).lower()
            prev = basenames.get(base_key)
            if prev is not None and os.path.normcase(prev) != os.path.normcase(source_path):
                if show_messages:
                    QMessageBox.critical(
                        app,
                        "Save As",
                        "Two different parent files share the same output name.\n"
                        "Choose another workflow or rename the source files first."
                    )
                return False
            basenames[base_key] = source_path

    written_paths = []
    updated_points_total = 0

    try:
        for source_path, assignment in assignments.items():
            point_indices = np.asarray(assignment["point_indices"], dtype=np.int64)
            data_indices = np.asarray(assignment["data_indices"], dtype=np.int64)
            if point_indices.size == 0:
                continue

            if not os.path.exists(source_path):
                raise FileNotFoundError(f"Parent file not found: {source_path}")

            target_path = source_path if in_place else os.path.join(output_dir, os.path.basename(source_path))
            target_dir = os.path.dirname(target_path) or os.getcwd()
            os.makedirs(target_dir, exist_ok=True)

            las = laspy.read(source_path)
            total_points = int(len(las.points))
            if np.any(point_indices < 0) or np.any(point_indices >= total_points):
                raise IndexError(
                    f"Source-point index out of range for {os.path.basename(source_path)} "
                    f"(file points={total_points:,})."
                )

            source_version = f"{las.header.version.major}.{las.header.version.minor}"
            source_max_cls = int(classes_u8[data_indices].max()) if data_indices.size else 0
            if source_max_cls > 31 and source_version == "1.2":
                try:
                    if not hasattr(laspy, "convert"):
                        raise RuntimeError("laspy.convert is not available in this runtime")
                    las = laspy.convert(
                        las,
                        point_format_id=_parent_upgrade_point_format(las),
                        file_version="1.4",
                    )
                    print(
                        f"⚠️ Fence parent write-back auto-upgraded {os.path.basename(source_path)} "
                        f"to LAS 1.4 (classification max={source_max_cls})"
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Could not upgrade {os.path.basename(source_path)} to LAS 1.4 "
                        f"for classification values above 31: {exc}"
                    ) from exc

            las.classification[point_indices] = classes_u8[data_indices]

            dims = {str(d).lower() for d in las.point_format.dimension_names}
            if rgb16 is not None and {"red", "green", "blue"}.issubset(dims):
                las.red[point_indices] = rgb16[data_indices, 0]
                las.green[point_indices] = rgb16[data_indices, 1]
                las.blue[point_indices] = rgb16[data_indices, 2]
            if intensity16 is not None and "intensity" in dims:
                las.intensity[point_indices] = intensity16[data_indices]

            fd, tmp_path = tempfile.mkstemp(
                prefix=os.path.splitext(os.path.basename(target_path))[0] + "_tmp_",
                suffix=os.path.splitext(target_path)[1],
                dir=target_dir,
            )
            os.close(fd)
            try:
                las.write(tmp_path)
                os.replace(tmp_path, target_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            if not in_place:
                _copy_prj_sidecar(app, source_path, target_path)

            written_paths.append(target_path)
            updated_points_total += int(point_indices.size)

        if not written_paths:
            if show_messages:
                QMessageBox.information(app, "Save", "No parent files were written.")
            return False

        setattr(app, "_last_fence_parent_write_paths", list(written_paths))

        if hasattr(app, "statusBar"):
            scope_text = "source files" if in_place else "full parent copies"
            app.statusBar().showMessage(
                f"Saved fence edits into {len(written_paths)} {scope_text}.",
                6000,
            )

        if show_messages:
            scope_text = "original parent files" if in_place else "full parent-file copies"
            QMessageBox.information(
                app,
                "Save",
                f"Saved fence edits into {len(written_paths)} {scope_text}.\n"
                f"Updated point records: {updated_points_total:,}"
            )

        print(
            f"💾 Fence parent write-back complete: files={len(written_paths)} "
            f"updated_points={updated_points_total:,} in_place={in_place}"
        )
        for path in written_paths:
            print(f"   ↳ {path}")
        return True

    except Exception as exc:
        print(f"❌ Fence parent write-back failed: {exc}")
        if show_messages:
            QMessageBox.critical(app, "Save", f"Fence parent write-back failed:\n\n{exc}")
        return False

def _serialize_drawings(app) -> bytes:
    """
    Serialize all drawing objects from app into JSON bytes.
    ✅ FIXED: Proper numpy array handling
    """
    try:
        # Get drawings from digitizer if available
        if hasattr(app, 'digitizer') and hasattr(app.digitizer, 'drawings'):
            drawings = app.digitizer.drawings
            print(f"📐 Found {len(drawings)} drawings in digitizer")
        else:
            drawings = getattr(app, "drawings", [])
        
        if not drawings:
            print("ℹ️ No drawings to serialize")
            return b""
        
        serializable = []
        for obj in drawings:
            # ✅ FIXED: Handle numpy arrays properly
            coords = None
            if 'coords' in obj:
                coords = obj['coords']
            elif 'points' in obj:
                coords = obj['points']
            
            # Check if coords is valid
            if coords is None:
                print(f"  ⚠️ Skipping drawing with no coordinates")
                continue
            
            # Check if it's an empty array/list
            try:
                if isinstance(coords, np.ndarray):
                    if coords.size == 0:
                        print(f"  ⚠️ Skipping drawing with empty array")
                        continue
                elif len(coords) == 0:
                    print(f"  ⚠️ Skipping drawing with empty list")
                    continue
            except Exception:
                print(f"  ⚠️ Skipping drawing with invalid coordinates")
                continue
            
            obj_copy = {
                'type': obj.get('type', 'line'),
                'points': []
            }
            
            # Convert coordinates safely
            try:
                if isinstance(coords, np.ndarray):
                    obj_copy['points'] = coords.tolist()
                elif isinstance(coords, list):
                    obj_copy['points'] = [[float(c) for c in pt] for pt in coords]
                else:
                    print(f"  ⚠️ Unknown coordinate format: {type(coords)}")
                    continue
            except Exception as e:
                print(f"  ⚠️ Failed to convert coordinates: {e}")
                continue
            
            # Copy metadata
            if 'text' in obj:
                obj_copy['text'] = str(obj['text'])
            
            # Handle color
            if 'original_color' in obj:
                obj_copy['color'] = [float(c) for c in obj['original_color']]
            elif 'color' in obj:
                color = obj['color']
                if isinstance(color, (list, tuple, np.ndarray)):
                    obj_copy['color'] = [float(c) for c in color]
            
            # Handle width
            if 'original_width' in obj:
                obj_copy['width'] = float(obj['original_width'])
            elif 'width' in obj:
                obj_copy['width'] = float(obj['width'])
            
            # Naksha SNT/SBM workflow metadata
            if 'layer' in obj:
                obj_copy['layer'] = str(obj['layer'])
            if 'snt_file' in obj:
                obj_copy['snt_file'] = str(obj['snt_file'])
            if 'snt_committed' in obj:
                obj_copy['snt_committed'] = bool(obj['snt_committed'])
            if 'snt_stored' in obj:
                obj_copy['snt_stored'] = bool(obj['snt_stored'])
            if 'sbm_class' in obj:
                obj_copy['sbm_class'] = obj['sbm_class']
            if 'source' in obj:
                obj_copy['source'] = str(obj['source'])
            
            serializable.append(obj_copy)
        
        if not serializable:
            print("ℹ️ No valid drawings after processing")
            return b""
        
        json_str = json.dumps(serializable, separators=(',', ':'))
        print(f"✅ Serialized {len(serializable)} drawing(s) to JSON ({len(json_str)} bytes)")
        return json_str.encode('utf-8')
    
    except Exception as e:
        print(f"⚠️ Failed to serialize drawings: {e}")
        import traceback
        traceback.print_exc()
        return b""


def _deserialize_drawings(data: bytes) -> list:
    """
    Deserialize drawing objects from JSON bytes.
    Returns list of drawing dictionaries.
    """
    try:
        if not data:
            return []
        json_str = data.decode('utf-8')
        drawings = json.loads(json_str)
        
        # Convert lists back to numpy arrays where needed
        for obj in drawings:
            if 'points' in obj and isinstance(obj['points'], list):
                obj['points'] = np.array(obj['points'])
            if 'properties' in obj and isinstance(obj['properties'], dict):
                for key, val in obj['properties'].items():
                    if isinstance(val, list) and key in ['color', 'vertices']:
                        obj['properties'][key] = np.array(val)      
        return drawings  
    except Exception as e:
        print(f"⚠️ Failed to deserialize drawings: {e}")
        return []

def _sidecar_drawings_path(las_path: str) -> str:
    root, _ = os.path.splitext(las_path)
    return root + ".naksha_drawings.json"


def _save_drawings_sidecar(las_path: str, drawing_data: bytes) -> str:
    """
    Persist oversized drawing payloads next to the LAS/LAZ file.
    """
    sidecar_path = _sidecar_drawings_path(las_path)
    payload = {
        "encoding": "utf-8",
        "drawings_json": drawing_data.decode("utf-8", errors="replace"),
    }
    with open(sidecar_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return sidecar_path


def _remove_drawings_sidecar(las_path: str) -> None:
    sidecar_path = _sidecar_drawings_path(las_path)
    try:
        if os.path.exists(sidecar_path):
            os.remove(sidecar_path)
    except Exception:
        pass


def _remove_drawings_vlr(las) -> None:
    """Drop Naksha drawings VLR entries before writing."""
    las.vlrs = [
        v for v in getattr(las, "vlrs", [])
        if not (v.user_id.strip() == "NakshaAI" and int(v.record_id) == 1002)
    ]


def _attach_drawings_storage(las, las_path: str, drawing_data: bytes, description: str, *,
                             verbose: bool = True) -> str | None:
    """
    Store drawings in a normal VLR when they fit; otherwise save them to a
    JSON sidecar and ensure no oversize VLR remains attached to the LAS object.

    Returns the sidecar path when sidecar fallback is used, else None.
    """
    _remove_drawings_vlr(las)

    if not drawing_data:
        _remove_drawings_sidecar(las_path)
        return None

    max_vlr_len = np.iinfo("uint16").max
    if len(drawing_data) > max_vlr_len:
        sidecar_path = _save_drawings_sidecar(las_path, drawing_data)
        if verbose:
            print(f"📎 Drawing payload too large for VLR; saved sidecar instead → {sidecar_path}")
        return sidecar_path

    drawing_vlr = laspy.VLR(
        user_id="NakshaAI",
        record_id=1002,
        description=description,
        record_data=drawing_data,
    )
    las.vlrs.append(drawing_vlr)
    _remove_drawings_sidecar(las_path)
    return None


def _atomic_write_las(las, path: str) -> None:
    """
    Write to a temp file in the same directory and replace the destination only
    after a successful full write so failures cannot truncate the live file.
    """
    out_dir = os.path.dirname(path) or "."
    suffix = os.path.splitext(path)[1] or ".laz"
    fd, tmp_path = tempfile.mkstemp(prefix=".naksha_save_", suffix=suffix, dir=out_dir)
    os.close(fd)
    try:
        las.write(tmp_path)
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

def load_drawings_from_las(las_path: str, app):
    """
    Load digitized drawings from VLR in existing LAS/LAZ file.
    ✅ FIXED: Stores drawings temporarily, to be restored AFTER point cloud renders
    """
    try:
        with laspy.open(las_path) as reader:
            for vlr in reader.header.vlrs:
                if vlr.user_id.strip() == "NakshaAI" and int(vlr.record_id) == 1002:
                    print("📐 Found drawing data in LAS file")
                    drawings = _deserialize_drawings(vlr.record_data)
                    
                    if not hasattr(app, 'digitizer') or not hasattr(app.digitizer, 'drawings'):
                        print("⚠️ Digitizer not available, skipping drawing load")
                        return False
                    
                    # ✅ CRITICAL FIX: Store for later, don't add to renderer yet
                    app._pending_drawings_restore = drawings
                    print(f"💾 Stored {len(drawings)} drawings for post-render restoration")
                    
                    return True

        sidecar_path = _sidecar_drawings_path(las_path)
        if os.path.exists(sidecar_path):
            print(f"📐 Found drawing sidecar: {sidecar_path}")
            with open(sidecar_path, "r", encoding="utf-8") as f:
                sidecar = json.load(f)
            drawings_json = sidecar.get("drawings_json", "")
            drawings = _deserialize_drawings(drawings_json.encode("utf-8"))
            if drawings:
                if not hasattr(app, 'digitizer') or not hasattr(app.digitizer, 'drawings'):
                    print("⚠️ Digitizer not available, skipping drawing load")
                    return False
                app._pending_drawings_restore = drawings
                print(f"💾 Stored {len(drawings)} drawings from sidecar for post-render restoration")
                return True

        if hasattr(app, 'digitizer'):
            if not hasattr(app.digitizer, 'drawings'):
                app.digitizer.drawings = []
        return False
    except Exception as e:
        print(f"⚠️ Failed to load drawings from {las_path}: {e}")
        import traceback
        traceback.print_exc()
        
        if hasattr(app, 'digitizer'):
            if not hasattr(app.digitizer, 'drawings'):
                app.digitizer.drawings = []
        
        return False
    
def finalize_drawing_render(app):
    """
    ✅ Restore drawings AFTER point cloud is rendered - forces them ON TOP
    """
    if not hasattr(app, '_pending_drawings_restore'):
        return
    
    drawings = app._pending_drawings_restore
    delattr(app, '_pending_drawings_restore')
    
    if not drawings:
        return
    
    print(f"\n{'='*60}")
    print(f"🎨 RESTORING {len(drawings)} DRAWINGS ON TOP")
    print(f"{'='*60}")
    
    try:
        app.digitizer.drawings = []  # Clear existing
        renderer = app.vtk_widget.renderer
        
        for drawing_data in drawings:
            coords = drawing_data.get('points', [])
            
            if len(coords) == 0:
                continue
            
            dtype = drawing_data.get('type', 'line')
            color = drawing_data.get('color', (1, 0, 0))
            width = drawing_data.get('width', 2)
            
            # Create actor
            actor = None
            curve_tool = getattr(app, "curve_tool", None)
            if dtype == 'curve' and curve_tool is not None and hasattr(curve_tool, '_create_curve_actor'):
                try:
                    actor = curve_tool._create_curve_actor(coords, color=color, width=width)
                except Exception as e:
                    print(f"  ⚠️ Curve actor creation failed, falling back to polyline: {e}")
            
            if actor is None:
                actor = app.digitizer._make_polyline_actor(
                    coords,
                    color=color,
                    width=width
                )
            
            if actor:
                # ✅ Force rendering ON TOP with multiple strategies
                try:
                    # Strategy 1: Polygon offset (pulls toward camera)
                    mapper = actor.GetMapper()
                    if mapper:
                        mapper.SetResolveCoincidentTopologyToPolygonOffset()
                        mapper.SetRelativeCoincidentTopologyPolygonOffsetParameters(-2.0, -2.0)
                    
                    # Strategy 2: Thick, bright lines
                    prop = actor.GetProperty()
                    prop.SetLineWidth(max(5, width * 2))
                    prop.SetOpacity(1.0)
                    
                    # Strategy 3: Disable lighting
                    prop.LightingOff()
                    
                    # Strategy 4: Render as tubes for visibility
                    prop.SetRenderLinesAsTubes(True)
                    
                except Exception as e:
                    print(f"  ⚠️ Render setup warning: {e}")
                
                # Add to renderer (will be added AFTER point cloud)
                renderer.AddActor(actor)
                
                entry = {
                    'type': dtype,
                    'coords': coords,
                    'actor': actor,
                    'bounds': actor.GetBounds(),
                    'original_color': color,
                    'original_width': width
                }
                
                if 'text' in drawing_data:
                    entry['text'] = drawing_data['text']
                
                # Restore SNT/SBM metadata if present
                for key in ('layer', 'snt_file', 'snt_committed', 'snt_stored', 'sbm_class', 'source'):
                    if key in drawing_data:
                        entry[key] = drawing_data[key]
                
                # If it's a curve, also register it in curve_tool
                if dtype == 'curve' and curve_tool is not None:
                    curve_data = {
                        'actor': actor,
                        'control_points': coords,
                        'interpolated': coords,
                        'coords': [tuple(pt) for pt in coords],
                        'type': 'curve',
                        'source': 'curve_tool',
                        'color': color,
                        'original_color': color,
                        'original_width': width,
                    }
                    # Copy other restored metadata to curve_data too
                    for key in ('layer', 'snt_file', 'snt_committed', 'snt_stored', 'sbm_class'):
                        if key in entry:
                            curve_data[key] = entry[key]
                    entry = curve_data
                    curve_tool.finalized_actors.append(curve_data)

                app.digitizer.drawings.append(entry)
        
        print(f"✅ {len(app.digitizer.drawings)} drawings restored ON TOP")
        
        # Reset clipping range
        renderer.ResetCameraClippingRange()
        
        # Force render
        renderer.GetRenderWindow().Render()
        
        print(f"{'='*60}\n")       
    except Exception as e:
        print(f"❌ Drawing restoration failed: {e}")
        import traceback
        traceback.print_exc()
    
def debug_renderer_setup(app):
    """Debug helper to understand renderer configuration"""
    print("\n🔍 RENDERER DEBUG:")
    
    if hasattr(app, 'digitizer'):
        print(f"  Digitizer renderer: {id(app.digitizer.renderer)}")
    
    if hasattr(app, 'vtk_widget'):
        print(f"  VTK widget renderer: {id(app.vtk_widget.renderer)}")
        
    if hasattr(app, 'renderer'):
        print(f"  App renderer: {id(app.renderer)}")
    
    # Check if they're the same object
    if hasattr(app, 'digitizer') and hasattr(app, 'vtk_widget'):
        same = app.digitizer.renderer == app.vtk_widget.renderer
        print(f"  Same renderer? {same}")    
    print()    

def save_pointcloud(app, path=None, file_format=None, las_version=None, show_dialog=True):
    """
    ✅ Enhanced Save behavior:
    - Saves point cloud data (XYZ, RGB, classification, intensity)
    - Saves digitized drawings (lines, polygons, annotations) as VLR
    - Drawings are fully editable after reload
    """

    data = getattr(app, "data", None)
    if data is None or "xyz" not in data or data["xyz"] is None:
        print("⚠️ No dataset loaded")
        return False

    fence_session, fence_error, is_fence_mode = _inspect_fenced_parent_session(app)
    if is_fence_mode and fence_session is not None:
        if show_dialog:
            save_mode = _prompt_fenced_save_mode(app, fence_session)
            if save_mode is None:
                return False
            if save_mode == "in_place":
                return _save_fenced_parent_files(
                    app,
                    fence_session,
                    output_dir=None,
                    in_place=True,
                    show_messages=True,
                )
            if save_mode == "parents":
                first_source = fence_session["source_files"][0]
                start_dir = os.path.dirname(first_source) if first_source else ""
                output_dir = QFileDialog.getExistingDirectory(
                    app,
                    "Save Full Parent Files",
                    start_dir,
                )
                if not output_dir:
                    return False
                return _save_fenced_parent_files(
                    app,
                    fence_session,
                    output_dir=output_dir,
                    in_place=False,
                    show_messages=True,
                )
        else:
            return _save_fenced_parent_files(
                app,
                fence_session,
                output_dir=None,
                in_place=True,
                show_messages=False,
            )
    elif is_fence_mode and fence_error:
        if show_dialog:
            QMessageBox.warning(
                app,
                "Save Fenced Data",
                fence_error + "\n\nContinuing with fenced-subset Save As only.",
            )
        else:
            QMessageBox.warning(app, "Save", fence_error)
            return False

    xyz = np.asarray(data["xyz"])
    n = xyz.shape[0]

    classes = data.get("classification")
    if classes is None or np.asarray(classes).shape[0] != n:
        classes = np.zeros(n, dtype=np.uint8)
    else:
        classes = np.asarray(classes)

    rgb16 = _rgb_to_las16(data.get("rgb"))
    intensity16 = _intensity_to_uint16(data.get("intensity"), n_points=n)

    # ---------------------------------------------------------
    # ✅ Save As dialog
    # ---------------------------------------------------------
    if show_dialog:
        if path:
            start_dir = os.path.dirname(path)
            base_name = os.path.splitext(os.path.basename(path))[0] or "untitled"
        elif getattr(app, "last_save_path", None):
            start_dir = os.path.dirname(app.last_save_path)
            base_name = os.path.splitext(os.path.basename(app.last_save_path))[0] or "untitled"
        elif getattr(app, "loaded_file", None):
            start_dir = os.path.dirname(app.loaded_file)
            base_name = os.path.splitext(os.path.basename(app.loaded_file))[0] or "untitled"
        elif is_fence_mode and fence_session is not None:
            first_source = fence_session["source_files"][0]
            start_dir = os.path.dirname(first_source)
            base_name = os.path.splitext(os.path.basename(first_source))[0] + "_fence_subset"
        else:
            start_dir = ""
            base_name = "untitled"

        default_path = os.path.join(start_dir, base_name) if start_dir else base_name

        filters = "LAZ 1.2 (*.laz);;LAZ 1.4 (*.laz);;LAS 1.2 (*.las);;LAS 1.4 (*.las)"
        default_filter = "LAZ 1.4 (*.laz)"

        try:
            loaded = getattr(app, "loaded_file", None) or getattr(app, "current_file_path", None)
            if loaded:
                loaded_ext = os.path.splitext(loaded)[1].lower()
                src_ver = None
                if hasattr(app, "data") and isinstance(app.data, dict):
                    src_ver = app.data.get("las_version") or app.data.get("version")

                if src_ver is None and loaded_ext in (".las", ".laz") and os.path.exists(loaded):
                    try:
                        hdr = laspy.read(loaded).header
                        src_ver = f"{hdr.version.major}.{hdr.version.minor}"
                    except Exception:
                        src_ver = None

                if src_ver not in ("1.2", "1.4"):
                    src_ver = "1.4"

                if loaded_ext == ".las":
                    default_filter = f"LAS {src_ver} (*.las)"
                else:
                    default_filter = f"LAZ {src_ver} (*.laz)"

        except Exception:
            pass

        picked_path, selected_filter = QFileDialog.getSaveFileName(
            app,
            "Save As",
            default_path,
            filters,
            default_filter
        )
        if not picked_path:
            return False

        ext, picked_version = _parse_filter(selected_filter)
        path = _ensure_ext(picked_path, ext)

        las_version = picked_version
        file_format = "laz" if ext == ".laz" else "las"

    if not path:
        print("⚠️ No output path provided")
        return False

    ext = os.path.splitext(path)[1].lower()
    if ext not in (".las", ".laz"):
        print(f"⚠️ Unsupported extension: {ext}")
        return False

    source_path = getattr(app, "loaded_file", None)
    import_options = data.get("import_options") if isinstance(data, dict) else None

    # Infer version if needed
    if las_version is None:
        probe = path
        if not os.path.exists(probe):
            probe = getattr(app, "loaded_file", None)

        if probe and os.path.exists(probe):
            try:
                with laspy.open(probe) as reader:
                    hv = reader.header.version
                    las_version = f"{hv.major}.{hv.minor}"
            except Exception as e:
                print(f"⚠️ Could not infer LAS version from '{probe}': {e}")

    if las_version is None:
        las_version = "1.4"

    if (
        not show_dialog
        and _same_path(path, source_path)
        and _import_options_reduce_points(import_options)
    ):
        print(
            "❌ Refusing in-place save: the loaded dataset is a filtered/sampled subset, "
            "so overwriting the original file would reduce it."
        )
        return False

    # ✅ LAS 1.2: 5-bit classification (0-31). For codes > 31, use user_data
    # field (MicroStation/TerraScan approach) — never remap or truncate.
    classes_u8 = np.asarray(classes).astype(np.uint8, copy=False)
    max_cls = int(classes_u8.max()) if classes_u8.size else 0

    pack_class_into_flags = False
    if max_cls > 31 and las_version == "1.2":
        # MicroStation/TerraScan compatible: pack the full code straight into
        # the raw classification byte using the synthetic/key_point/withheld
        # flag bits. The on-disk byte then EQUALS the real code (0-255).
        pack_class_into_flags = True
        print(f"\n{'='*60}")
        print(f"📌 LAS 1.2 with extended classes (max={max_cls})")
        print(f"   Packing full code into classification byte via flag bits")
        print(f"   (MicroStation/TerraScan compatible)")
        print(f"{'='*60}\n")

    preserved_las, preserve_reason = _try_build_preserved_las(
        source_path=source_path,
        target_path=path,
        xyz=xyz,
        classes_u8=classes_u8,
        rgb16=rgb16,
        intensity16=intensity16,
        import_options=import_options,
        requested_version=las_version,
        crs_wkt=getattr(app, "project_crs_wkt", None),
        crs_epsg=getattr(app, "project_crs_epsg", None),
    )
    if preserved_las is not None:
        try:
            drawing_data = _serialize_drawings(app)
            if drawing_data:
                decoded = json.loads(drawing_data.decode("utf-8"))
                print(f"✅ Prepared {len(decoded)} drawings for VLR")
                sidecar_path = _attach_drawings_storage(
                    preserved_las,
                    path,
                    drawing_data,
                    "Digitized drawings (lines, polygons, annotations)",
                    verbose=True,
                )
                if sidecar_path is None:
                    print(f"📐 Saved {len(decoded)} drawing object(s) to VLR")
        except Exception as e:
            print(f"⚠️ Failed to save drawings: {e}")

        _atomic_write_las(preserved_las, path)
        print(f"💾 Saved {ext.upper()} {las_version} using source-preserving rewrite: {path}")

        prj_path = os.path.splitext(path)[0] + ".prj"
        try:
            if getattr(app, "project_crs_wkt", None):
                with open(prj_path, "w") as f:
                    f.write(app.project_crs_wkt)
                print(f"📌 CRS WKT saved to {prj_path}")
            elif getattr(app, "project_crs_epsg", None):
                with open(prj_path, "w") as f:
                    f.write(f"EPSG:{app.project_crs_epsg}")
                print(f"📌 EPSG code saved to {prj_path}")
        except Exception as e:
            print(f"⚠️ Failed to write .prj: {e}")

        app.last_save_path = path
        app.last_save_format = file_format
        app.last_save_version = las_version
        if hasattr(app, "statusBar"):
            app.statusBar().showMessage(f"Saved: {path}", 5000)
        return True

    print(f"ℹ️ Falling back to synthesized save path: {preserve_reason}")

    major, minor = map(int, las_version.split("."))

    point_format_id = _choose_point_format(las_version, has_rgb=(rgb16 is not None))
    header = laspy.LasHeader(point_format=point_format_id, version=f"{major}.{minor}")

    # Embed CRS
    try:
        import pyproj
        if getattr(app, "project_crs_wkt", None):
            crs = pyproj.CRS.from_wkt(app.project_crs_wkt)
            header.parse_crs(crs)
            print("📌 Embedded CRS from WKT into LAS header")
        elif getattr(app, "project_crs_epsg", None):
            crs = pyproj.CRS.from_epsg(app.project_crs_epsg)
            header.parse_crs(crs)
            print(f"📌 Embedded CRS EPSG:{app.project_crs_epsg} into LAS header")
    except Exception as e:
        print(f"⚠️ Failed to embed CRS into {ext.upper()} header: {e}")

    las = laspy.LasData(header)
    las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]

    # Classification handling
    if las_version == "1.4":
        las.classification = np.clip(classes_u8, 0, 255).astype(np.uint8, copy=False)
    elif pack_class_into_flags:
        # LAS 1.2 with codes > 31: write the raw classification byte exactly
        # the way TerraScan/MicroStation does. laspy exposes the byte as four
        # bit-packed sub-fields on point formats 0-5, so the on-disk byte ends
        # up bit-for-bit equal to the real code (0-255). MicroStation reads the
        # whole byte and shows the correct value — no VLR, no user_data needed.
        las.classification = (classes_u8 & 0x1F).astype(np.uint8, copy=False)
        las.synthetic = ((classes_u8 >> 5) & 1).astype(bool)
        las.key_point = ((classes_u8 >> 6) & 1).astype(bool)
        las.withheld  = ((classes_u8 >> 7) & 1).astype(bool)
    else:
        # LAS 1.2: safe, all codes <= 31
        las.classification = np.clip(classes_u8, 0, 31).astype(np.uint8, copy=False)

    if rgb16 is not None:
        las.red, las.green, las.blue = rgb16[:, 0], rgb16[:, 1], rgb16[:, 2]
    if intensity16 is not None:
        las.intensity = intensity16


    # ---------------------------------------------------------
    # ✅ NEW: Save digitized drawings as VLR (record_id=1002)
    # ---------------------------------------------------------
    try:
        drawing_data = _serialize_drawings(app)
        if drawing_data:
            decoded = json.loads(drawing_data.decode('utf-8'))
            print(f"✅ Prepared {len(decoded)} drawings for VLR")

            try:
                sidecar_path = _attach_drawings_storage(
                    las,
                    path,
                    drawing_data,
                    "Digitized drawings (lines, polygons, annotations)",
                    verbose=True,
                )
                if sidecar_path is None:
                    print(f"📐 Saved {len(decoded)} drawing object(s) to VLR")
            except Exception:
                raise
            

    except Exception as e:
        print(f"⚠️ Failed to save drawings: {e}")

    _atomic_write_las(las, path)
    print(f"💾 Saved {ext.upper()} {las_version}, Point Format {point_format_id}: {path}")

    # .prj sidecar
    prj_path = os.path.splitext(path)[0] + ".prj"
    try:
        if getattr(app, "project_crs_wkt", None):
            with open(prj_path, "w") as f:
                f.write(app.project_crs_wkt)
            print(f"📌 CRS WKT saved to {prj_path}")
        elif getattr(app, "project_crs_epsg", None):
            with open(prj_path, "w") as f:
                f.write(f"EPSG:{app.project_crs_epsg}")
            print(f"📌 EPSG code saved to {prj_path}")
    except Exception as e:
        print(f"⚠️ Failed to write .prj: {e}")

    app.last_save_path = path
    app.last_save_format = file_format
    app.last_save_version = las_version
    if hasattr(app, "statusBar"):
        app.statusBar().showMessage(f"Saved: {path}", 5000)
    return True


# ---------------- QUICK AUTO-BACKUP SAVE ----------------
def save_pointcloud_quick(app, path):
    """
    Silent quick-save version used for auto-backup.
    ✅ Also saves drawings.
    """
    try:
        data = getattr(app, "data", None)
        if data is None or "xyz" not in data or data["xyz"] is None:
            print("⚠️ No data to save for backup")
            return False

        fence_session, fence_error, is_fence_mode = _inspect_fenced_parent_session(app)
        if is_fence_mode and fence_session is not None:
            return _save_fenced_parent_files(
                app,
                fence_session,
                output_dir=None,
                in_place=True,
                show_messages=False,
            )
        if is_fence_mode and fence_error:
            print(f"⚠️ Fence quick-save skipped: {fence_error}")
            return False

        ext = os.path.splitext(path)[1].lower()
        if ext not in (".las", ".laz"):
            print(f"⚠️ Unsupported backup extension: {ext}")
            return False

        xyz = np.asarray(data["xyz"])
        n = xyz.shape[0]

        # Determine backup LAS version
        # Prefer the last explicit save version so backups match Save / Save As.
        las_version = getattr(app, "last_save_version", None)
        try:
            v = data.get("input_format_version", None)
            if las_version not in ("1.2", "1.4") and isinstance(v, tuple) and len(v) >= 2:
                las_version = f"{int(v[0])}.{int(v[1])}"
        except Exception:
            pass

        if las_version not in ("1.2", "1.4"):
            probe = getattr(app, "loaded_file", None)
            if probe and os.path.exists(probe):
                try:
                    with laspy.open(probe) as reader:
                        hv = reader.header.version
                        las_version = f"{hv.major}.{hv.minor}"
                except Exception as e:
                    print(f"⚠️ Could not infer LAS version for backup: {e}")

        if las_version not in ("1.2", "1.4"):
            las_version = "1.4"

        source_path = getattr(app, "loaded_file", None)
        import_options = data.get("import_options") if isinstance(data, dict) else None
        if _same_path(path, source_path) and _import_options_reduce_points(import_options):
            print(
                "❌ Quick-save refused: the current dataset is a filtered/sampled subset, "
                "so saving over the source would reduce it."
            )
            return False

        classes = data.get("classification", np.zeros(n, dtype=np.uint8))
        classes_u8 = np.asarray(classes).astype(np.uint8, copy=False)
        if classes_u8.shape[0] != n:
            classes_u8 = np.zeros(n, dtype=np.uint8)

        rgb16 = _rgb_to_las16(data.get("rgb"))
        intensity16 = _intensity_to_uint16(data.get("intensity"), n_points=n)

        preserved_las, preserve_reason = _try_build_preserved_las(
            source_path=source_path,
            target_path=path,
            xyz=xyz,
            classes_u8=classes_u8,
            rgb16=rgb16,
            intensity16=intensity16,
            import_options=import_options,
            requested_version=las_version,
            crs_wkt=getattr(app, "project_crs_wkt", None),
            crs_epsg=getattr(app, "project_crs_epsg", None),
        )
        if preserved_las is not None:
            try:
                drawing_data = _serialize_drawings(app)
                if drawing_data:
                    _attach_drawings_storage(
                        preserved_las,
                        path,
                        drawing_data,
                        "Digitized drawings",
                        verbose=False,
                    )
            except Exception as e:
                print(f"⚠️ Backup: failed to save drawings: {e}")

            _atomic_write_las(preserved_las, path)
            print(f"💾 Auto-backup saved → {path} (preserved source layout)")
            return True

        print(f"ℹ️ Auto-backup fallback to synthesized save path: {preserve_reason}")

        max_cls = int(classes_u8.max()) if classes_u8.size else 0
        use_user_data_for_class = (max_cls > 31 and las_version == "1.2")

        point_format = _choose_point_format(las_version, has_rgb=(rgb16 is not None))
        header = laspy.LasHeader(point_format=point_format, version=las_version)

        try:
            import pyproj
            if getattr(app, "project_crs_wkt", None):
                header.parse_crs(pyproj.CRS.from_wkt(app.project_crs_wkt))
            elif getattr(app, "project_crs_epsg", None):
                header.parse_crs(pyproj.CRS.from_epsg(app.project_crs_epsg))
        except Exception as e:
            print(f"⚠️ CRS embedding skipped: {e}")

        las = laspy.LasData(header)
        las.x, las.y, las.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]

        if las_version == "1.4":
            las.classification = np.clip(classes_u8, 0, 255).astype(np.uint8, copy=False)
        elif use_user_data_for_class:
            # LAS 1.2 with codes > 31: pack full code into the raw classification
            # byte via flag bits (TerraScan/MicroStation compatible). On-disk byte
            # == real code (0-255). No VLR / user_data needed.
            las.classification = (classes_u8 & 0x1F).astype(np.uint8, copy=False)
            las.synthetic = ((classes_u8 >> 5) & 1).astype(bool)
            las.key_point = ((classes_u8 >> 6) & 1).astype(bool)
            las.withheld  = ((classes_u8 >> 7) & 1).astype(bool)
        else:
            # LAS 1.2: safe, all codes <= 31
            las.classification = np.clip(classes_u8, 0, 31).astype(np.uint8, copy=False)

        if rgb16 is not None:
            las.red, las.green, las.blue = rgb16[:, 0], rgb16[:, 1], rgb16[:, 2]
        if intensity16 is not None:
            las.intensity = intensity16

        # ✅ Save drawings in backup
        try:
            drawing_data = _serialize_drawings(app)
            if drawing_data:
                _attach_drawings_storage(
                    las,
                    path,
                    drawing_data,
                    "Digitized drawings",
                    verbose=False,
                )
        except Exception as e:
            print(f"⚠️ Backup: failed to save drawings: {e}")

        _atomic_write_las(las, path)
        print(f"💾 Auto-backup saved → {path} (version={las_version}, point_format={point_format})")
        return True

    except Exception as e:
        print(f"⚠️ Quick-save failed: {e}")
        return False
