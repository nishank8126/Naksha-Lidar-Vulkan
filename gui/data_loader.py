# import os

# import laspy
# import numpy as np
# import open3d as o3d
# from PySide6.QtWidgets import QDialog

# from .dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS, LoadPointCloudDialog


# def restore_class_from_user_data_if_marked(las_obj) -> np.ndarray | None:
#     """
#     If this LAZ/LAS was saved by Naksha as LAS 1.2 with original classes stored
#     in user_data, restore them.
#     """
#     try:
#         vlrs = []
#         if hasattr(las_obj, "vlrs") and las_obj.vlrs:
#             vlrs.extend(list(las_obj.vlrs))
#         if hasattr(las_obj, "header") and hasattr(las_obj.header, "vlrs") and las_obj.header.vlrs:
#             vlrs.extend(list(las_obj.header.vlrs))

#         has_marker = any(
#             (
#                 getattr(v, "user_id", "").strip() == "NakshaAI"
#                 and int(getattr(v, "record_id", -1)) == 1001
#             )
#             for v in vlrs
#         )
#         if not has_marker:
#             return None

#         if "user_data" not in las_obj.point_format.dimension_names:
#             return None

#         ud = np.asarray(las_obj.user_data, dtype=np.uint8)
#         if ud.size == 0:
#             return None

#         npts = len(las_obj.x)
#         if ud.shape[0] != npts:
#             return None

#         if int(ud.max()) > 31:
#             print("Restored original classification from user_data (NakshaAI marker VLR found)")
#             return ud

#         return None

#     except Exception as exc:
#         print(f"Restore-from-user_data failed: {exc}")
#         return None


# def get_default_lidar_import_options(disabled_attrs=None):
#     disabled_attrs = set(disabled_attrs or [])
#     options = {
#         key: value
#         for key, value in DEFAULT_IMPORT_OPTIONS.items()
#         if key != "attributes"
#     }
#     options["attributes"] = dict(DEFAULT_IMPORT_OPTIONS["attributes"])
#     for attr_name in disabled_attrs:
#         if attr_name in options["attributes"]:
#             options["attributes"][attr_name] = False
#     return options


# def _merge_import_options(import_options=None, disabled_attrs=None):
#     merged = get_default_lidar_import_options(disabled_attrs)
#     if not import_options:
#         return merged

#     for key, value in import_options.items():
#         if key == "attributes" and isinstance(value, dict):
#             merged["attributes"].update(value)
#         else:
#             merged[key] = value

#     for attr_name in set(disabled_attrs or []):
#         if attr_name in merged["attributes"]:
#             merged["attributes"][attr_name] = False

#     return merged


# def prompt_lidar_import_options(filename, parent=None, disabled_attrs=None, initial_options=None):
#     dlg = LoadPointCloudDialog(
#         filename,
#         parent=parent,
#         disabled_attrs=disabled_attrs,
#         initial_options=initial_options,
#     )
#     if dlg.exec() != QDialog.Accepted:
#         return None
#     return dlg.get_import_options()


# def _extract_classification_array(las):
#     classification = None
#     if "classification" not in las.point_format.dimension_names:
#         return classification

#     restored = restore_class_from_user_data_if_marked(las)
#     if restored is not None:
#         return restored

#     version = las.header.version
#     if isinstance(version, str):
#         major, minor = map(int, version.split("."))
#     else:
#         major, minor = version.major, version.minor

#     print(f"\n{'=' * 60}")
#     print(f"LAS File Version: {major}.{minor}")
#     print(f"   Point Format: {las.header.point_format.id}")

#     if major == 1 and minor >= 4:
#         print("   LAS 1.4+ detected - using full 8-bit classification")
#         if hasattr(las, "classification"):
#             classification = np.array(las.classification, dtype=np.uint8)
#         else:
#             classification = np.array(las.raw_classification, dtype=np.uint8)
#     else:
#         print(f"   LAS {major}.{minor} - checking for extended classes...")
#         if hasattr(las, "raw_classification"):
#             raw_class = np.array(las.raw_classification, dtype=np.uint8)
#             max_class = raw_class.max()
#             if max_class > 31:
#                 print(f"   Extended classes detected (max={max_class}) in LAS {major}.{minor}")
#                 print("   Using raw classification to preserve classes > 31")
#                 classification = raw_class
#             else:
#                 classification = np.array(las.classification, dtype=np.uint8)
#         else:
#             classification = np.array(las.classification, dtype=np.uint8)

#     # Skip the np.unique / np.sum diagnostic scans on huge files
#     # (300M-point np.unique alone took several seconds and 300MB of temps).
#     # Diagnostics are only emitted for small files where the cost is negligible.
#     if len(classification) <= 5_000_000:
#         unique_classes = np.unique(classification)
#         print(f"   Unique classes found: {unique_classes}")

#         if 51 in unique_classes:
#             count_51 = int(np.count_nonzero(classification == 51))
#             print(f"   Class 51 (noise): {count_51:,} points correctly preserved")
#         if 19 in unique_classes:
#             count_19 = int(np.count_nonzero(classification == 19))
#             print(f"   Class 19: {count_19:,} points")
#     else:
#         print(f"   Skipping per-class diagnostics ({len(classification):,} points — large file)")

#     print(f"{'=' * 60}\n")
#     return classification


# def _extract_crs(las):
#     crs_wkt = None
#     crs_epsg = None

#     try:
#         crs = las.header.parse_crs()
#         if crs:
#             crs_wkt = crs.to_wkt()
#             try:
#                 crs_epsg = crs.to_epsg()
#             except Exception:
#                 crs_epsg = None
#     except Exception as exc:
#         print(f"CRS not found in LAS/LAZ header: {exc}")

#     return crs_wkt, crs_epsg


# def _apply_import_filters(xyz, rgb, intensity, classification, options):
#     total_points = len(xyz)
#     if total_points == 0:
#         return xyz, rgb, intensity, classification

#     nth_point = max(1, int(options.get("nth_point", 1) or 1))
#     do_nth = bool(options.get("only_every") and nth_point > 1)

#     # Physical class filter: keep only points whose classification is in the
#     # selected set. This is what the user sees in Point Statistics.
#     if options.get("only_class") and classification is not None:
#         class_codes = options.get("class_codes") or []
#         if class_codes:
#             sel = np.isin(classification, [int(c) for c in class_codes])
#             xyz = xyz[sel]
#             if rgb is not None:
#                 rgb = rgb[sel]
#             if intensity is not None:
#                 intensity = intensity[sel]
#             classification = classification[sel]
#             print(f"   🔍 Class filter applied: {sel.sum():,} / {total_points:,} points kept "
#                   f"(classes {sorted(int(c) for c in class_codes)})")
#             if len(xyz) == 0:
#                 return xyz, rgb, intensity, classification

#     if not do_nth:
#         return xyz, rgb, intensity, classification

#     sl = slice(None, None, nth_point)
#     xyz = xyz[sl]
#     if rgb is not None:
#         rgb = rgb[sl]
#     if intensity is not None:
#         intensity = intensity[sl]
#     if classification is not None:
#         classification = classification[sl]
#     return xyz, rgb, intensity, classification


# def load_lidar_file(
#     filename,
#     parent=None,
#     import_options=None,
#     prompt_user=True,
#     disabled_attrs=None,
# ):
#     """Load LAS/LAZ/PLY, optionally prompting for import settings first."""

#     if filename.lower().endswith((".las", ".laz")):
#         options = _merge_import_options(import_options, disabled_attrs)
#         if prompt_user:
#             options = prompt_lidar_import_options(
#                 filename,
#                 parent=parent,
#                 disabled_attrs=disabled_attrs,
#                 initial_options=options,
#             )
#             if options is None:
#                 return None

#         las = laspy.read(filename)

#         # Pre-allocate the XYZ output buffer and fill it column-by-column.
#         # This avoids `np.vstack([las.x, las.y, las.z]).T` which builds three
#         # temporary 1-D arrays AND a transposed copy on top of the laspy object
#         # — a 4× memory peak that crashed 200–300M-point files.
#         n_pts = int(las.header.point_count)
#         xyz = np.empty((n_pts, 3), dtype=np.float64)
#         xyz[:, 0] = las.x
#         xyz[:, 1] = las.y
#         xyz[:, 2] = las.z

#         attrs = options.get("attributes", {}) if isinstance(options, dict) else {}
#         rgb = None
#         # Check if RGB channels exist in the file
#         _has_rgb_fields = (
#             hasattr(las, 'red') and
#             hasattr(las, 'green') and
#             hasattr(las, 'blue')
#         )

#         if _has_rgb_fields:
#             if attrs.get("Color", True):
#                 # Stay in the native uint16 dtype that laspy already exposes.
#                 # The previous code cast to uint32 just to compute `r // 257`,
#                 # which allocated three extra 1.2GB buffers for a 300M-pt file.
#                 r = np.asarray(las.red,   dtype=np.uint16)
#                 g = np.asarray(las.green, dtype=np.uint16)
#                 b = np.asarray(las.blue,  dtype=np.uint16)

#                 raw_max = int(max(r.max(), g.max(), b.max()))
#                 print(f"  🔍 RGB raw: dtype=uint16, max={raw_max}")

#                 rgb = np.empty((n_pts, 3), dtype=np.uint8)
#                 if raw_max > 255:
#                     # Standard LAS uint16 [0-65535] → uint8 [0-255].
#                     # `// 257` keeps full uint16 range mapped 0..255 exactly.
#                     # Division stays in uint16, cast to uint8 happens once on
#                     # assignment into the pre-allocated buffer.
#                     np.floor_divide(r, 257, out=r); rgb[:, 0] = r
#                     np.floor_divide(g, 257, out=g); rgb[:, 1] = g
#                     np.floor_divide(b, 257, out=b); rgb[:, 2] = b
#                 else:
#                     # Already in uint8 range — direct cast.
#                     rgb[:, 0] = r
#                     rgb[:, 1] = g
#                     rgb[:, 2] = b
#                 del r, g, b

#                 print(f"  ✅ RGB ready: max={rgb.max()}, min={rgb.min()}, mean={rgb.mean():.1f}")
#             else:
#                 print(f"  ⚠️ RGB skipped (Color=False in options)")
#         else:
#             print(f"  ⚠️ No RGB fields in file")

#         intensity = None
#         if attrs.get("Intensity", False) and "intensity" in las.point_format.dimension_names:
#             # float32 is enough for intensity (16-bit source) and halves the
#             # buffer vs the previous `astype(float)` (== float64) cast.
#             intensity = np.asarray(las.intensity, dtype=np.float32)

#         classification = _extract_classification_array(las)

#         crs_wkt, crs_epsg = _extract_crs(las)

#         version_str = las.header.version
#         if isinstance(version_str, tuple):
#             version = version_str
#         else:
#             version = tuple(map(int, str(version_str).split(".")))

#         point_format_id = las.header.point_format.id

#         # Release the laspy object as soon as we have all extracted arrays.
#         # laspy keeps the full record array (~3-4× xyz size) alive until
#         # garbage collection; freeing it here drops peak RAM dramatically.
#         del las

#         xyz, rgb, intensity, classification = _apply_import_filters(
#             xyz, rgb, intensity, classification, options
#         )

#         if parent is not None:
#             parent.loaded_file = filename
#             parent.last_save_path = filename
#             try:
#                 from .save_pointcloud import load_drawings_from_las

#                 load_drawings_from_las(filename, parent)
#             except Exception as exc:
#                 print(f"Could not load drawings: {exc}")

#         return {
#             "xyz": xyz,
#             "rgb": rgb,
#             "intensity": intensity,
#             "classification": classification,
#             "crs_wkt": crs_wkt,
#             "crs_epsg": crs_epsg,
#             "input_format_version": version,
#             "input_point_format": point_format_id,
#             "import_options": options,
#             "type": "las",
#         }

#     if filename.lower().endswith(".ply"):
#         pcd = o3d.io.read_point_cloud(filename)

#         if parent is not None:
#             parent.loaded_file = filename
#             parent.last_save_path = filename
#             try:
#                 from .save_pointcloud import load_drawings_from_las

#                 load_drawings_from_las(filename, parent)
#             except Exception as exc:
#                 print(f"Could not load drawings: {exc}")

#         return {
#             "xyz": np.asarray(pcd.points),
#             "rgb": np.asarray(pcd.colors),
#             "intensity": None,
#             "classification": None,
#             "crs_wkt": None,
#             "crs_epsg": None,
#             "type": "ply",
#         }

#     raise ValueError("Unsupported format")
import os

import laspy
import numpy as np
import open3d as o3d
from PySide6.QtWidgets import QDialog

from .dialogs.load_pointcloud_dialog import DEFAULT_IMPORT_OPTIONS, LoadPointCloudDialog


def _parse_terrascan_prj(prj_path):
    """Return existing LAS/LAZ files referenced by a TerraScan block PRJ.

    TerraScan block files contain lines such as ``Block tile_name`` followed
    by polygon coordinates. This parser intentionally ignores geometry here;
    ``NakshaApp._load_terrascan_prj`` only needs the referenced point-cloud
    files. Relative paths are resolved against the PRJ directory, duplicates
    are removed while preserving order, and both names with and without an
    explicit .las/.laz suffix are supported.
    """
    from pathlib import Path
    import re

    prj = Path(str(prj_path))
    try:
        lines = prj.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError as exc:
        print(f"⚠️ Failed to read TerraScan PRJ {prj}: {exc}")
        return []

    out = []
    seen = set()
    for line in lines:
        match = re.match(r"\s*Block\s+(.+?)\s*$", line)
        if not match:
            continue
        raw_name = match.group(1).strip().strip('"')
        if not raw_name:
            continue

        ref = Path(raw_name)
        candidates = []
        if ref.suffix.lower() in {".las", ".laz"}:
            candidates.append(ref if ref.is_absolute() else prj.parent / ref)
        else:
            base = ref if ref.is_absolute() else prj.parent / ref
            candidates.extend([base.with_suffix(".laz"), base.with_suffix(".las")])

        for candidate in candidates:
            try:
                if not candidate.exists():
                    continue
                key = os.path.normcase(os.path.abspath(str(candidate)))
            except OSError:
                continue
            if key in seen:
                break
            seen.add(key)
            out.append(str(candidate))
            break

    return out


def restore_class_from_user_data_if_marked(las_obj) -> np.ndarray | None:
    """
    If this LAZ/LAS was saved by Naksha as LAS 1.2 with original classes stored
    in user_data, restore them.
    """
    try:
        vlrs = []
        if hasattr(las_obj, "vlrs") and las_obj.vlrs:
            vlrs.extend(list(las_obj.vlrs))
        if hasattr(las_obj, "header") and hasattr(las_obj.header, "vlrs") and las_obj.header.vlrs:
            vlrs.extend(list(las_obj.header.vlrs))

        has_marker = any(
            (
                getattr(v, "user_id", "").strip() == "NakshaAI"
                and int(getattr(v, "record_id", -1)) == 1001
            )
            for v in vlrs
        )
        if not has_marker:
            return None

        if "user_data" not in las_obj.point_format.dimension_names:
            return None

        ud = np.asarray(las_obj.user_data, dtype=np.uint8)
        if ud.size == 0:
            return None

        npts = len(las_obj.x)
        if ud.shape[0] != npts:
            return None

        if int(ud.max()) > 31:
            print("Restored original classification from user_data (NakshaAI marker VLR found)")
            return ud

        return None

    except Exception as exc:
        print(f"Restore-from-user_data failed: {exc}")
        return None


def get_default_lidar_import_options(disabled_attrs=None):
    disabled_attrs = set(disabled_attrs or [])
    options = {
        key: value
        for key, value in DEFAULT_IMPORT_OPTIONS.items()
        if key != "attributes"
    }
    options["attributes"] = dict(DEFAULT_IMPORT_OPTIONS["attributes"])
    for attr_name in disabled_attrs:
        if attr_name in options["attributes"]:
            options["attributes"][attr_name] = False
    return options


def _merge_import_options(import_options=None, disabled_attrs=None):
    merged = get_default_lidar_import_options(disabled_attrs)
    if not import_options:
        return merged

    for key, value in import_options.items():
        if key == "attributes" and isinstance(value, dict):
            merged["attributes"].update(value)
        else:
            merged[key] = value

    for attr_name in set(disabled_attrs or []):
        if attr_name in merged["attributes"]:
            merged["attributes"][attr_name] = False

    return merged


def prompt_lidar_import_options(filename, parent=None, disabled_attrs=None, initial_options=None):
    dlg = LoadPointCloudDialog(
        filename,
        parent=parent,
        disabled_attrs=disabled_attrs,
        initial_options=initial_options,
    )
    if dlg.exec() != QDialog.Accepted:
        return None
    return dlg.get_import_options()


def _extract_classification_array(las):
    classification = None
    if "classification" not in las.point_format.dimension_names:
        return classification

    restored = restore_class_from_user_data_if_marked(las)
    if restored is not None:
        return restored

    version = las.header.version
    if isinstance(version, str):
        major, minor = map(int, version.split("."))
    else:
        major, minor = version.major, version.minor

    print(f"\n{'=' * 60}")
    print(f"LAS File Version: {major}.{minor}")
    print(f"   Point Format: {las.header.point_format.id}")

    if major == 1 and minor >= 4:
        print("   LAS 1.4+ detected - using full 8-bit classification")
        if hasattr(las, "classification"):
            classification = np.array(las.classification, dtype=np.uint8)
        else:
            classification = np.array(las.raw_classification, dtype=np.uint8)
    else:
        print(f"   LAS {major}.{minor} - checking for extended classes...")
        base_class = np.array(las.classification, dtype=np.uint8)
        dims = {str(d).lower() for d in las.point_format.dimension_names}

        if {"synthetic", "key_point", "withheld"} <= dims:
            # Legacy point formats (0-5) pack classification into one byte:
            # bits 0-4 = classification, bit5=synthetic, bit6=key_point, bit7=withheld.
            # Reconstruct the full code the same way TerraScan/MicroStation write it
            # and the same way save_pointcloud now packs it. Memory-conscious for
            # large files: stays in uint8, no int64 temporaries.
            syn = np.asarray(las.synthetic, dtype=np.uint8)
            key = np.asarray(las.key_point, dtype=np.uint8)
            wit = np.asarray(las.withheld, dtype=np.uint8)

            full_class = (base_class & 0x1F).astype(np.uint8)
            np.left_shift(syn, 5, out=syn); full_class |= syn
            np.left_shift(key, 6, out=key); full_class |= key
            np.left_shift(wit, 7, out=wit); full_class |= wit
            del syn, key, wit

            max_class = int(full_class.max()) if full_class.size else 0
            if max_class > 31:
                print(f"   Extended classes detected (max={max_class}) in LAS {major}.{minor}")
                print("   Reconstructed full code from classification + synthetic/key_point/withheld bits")
            classification = full_class
        else:
            classification = base_class

    # Skip the np.unique / np.sum diagnostic scans on huge files
    # (300M-point np.unique alone took several seconds and 300MB of temps).
    # Diagnostics are only emitted for small files where the cost is negligible.
    if len(classification) <= 5_000_000:
        unique_classes = np.unique(classification)
        print(f"   Unique classes found: {unique_classes}")

        if 51 in unique_classes:
            count_51 = int(np.count_nonzero(classification == 51))
            print(f"   Class 51 (noise): {count_51:,} points correctly preserved")
        if 19 in unique_classes:
            count_19 = int(np.count_nonzero(classification == 19))
            print(f"   Class 19: {count_19:,} points")
    else:
        print(f"   Skipping per-class diagnostics ({len(classification):,} points — large file)")

    print(f"{'=' * 60}\n")
    return classification


def extract_epsg_from_wkt(wkt_str):
    if not wkt_str:
        return None
    import re
    # Look for ID["EPSG", <num>] or AUTHORITY["EPSG", "<num>"]
    # We want the LAST occurrence, as it is the outermost CRS authority
    matches = re.findall(
        r'(?:AUTHORITY|ID)\s*[\[\(\s]*["\']EPSG["\']\s*,\s*["\']?(\d+)["\']?\s*[\]\)\s]*',
        wkt_str,
        re.IGNORECASE
    )
    if matches:
        return int(matches[-1])
    return None

def extract_epsg_from_vlrs(las):
    vlrs = []
    if hasattr(las, "vlrs") and las.vlrs:
        vlrs.extend(list(las.vlrs))
    if hasattr(las, "header") and hasattr(las.header, "vlrs") and las.header.vlrs:
        vlrs.extend(list(las.header.vlrs))

    for vlr in vlrs:
        # Check WktCoordinateSystemVlr (Record ID 2112)
        if getattr(vlr, "record_id", -1) == 2112:
            for attr in ("wkt", "string", "wkt_string", "text", "string_data", "_string_data"):
                val = getattr(vlr, attr, None)
                if val and isinstance(val, str):
                    epsg = extract_epsg_from_wkt(val)
                    if epsg:
                        return epsg
                elif val and isinstance(val, bytes):
                    try:
                        epsg = extract_epsg_from_wkt(val.decode('utf-8', errors='ignore'))
                        if epsg:
                            return epsg
                    except Exception:
                        pass
        # Check GeoKeyDirectoryVlr (Record ID 34735)
        if getattr(vlr, "record_id", -1) == 34735:
            geo_keys = getattr(vlr, "geo_keys", None)
            if geo_keys:
                try:
                    if isinstance(geo_keys, dict):
                        for key_id in (3072, 2048):
                            if key_id in geo_keys:
                                val = geo_keys[key_id]
                                if isinstance(val, (int, float)) and val > 0:
                                    return int(val)
                    else:
                        for key in geo_keys:
                            key_id = getattr(key, "id", getattr(key, "key_id", -1))
                            if key_id in (3072, 2048):
                                val = getattr(key, "value", getattr(key, "val", None))
                                if isinstance(val, (int, float)) and val > 0:
                                    return int(val)
                except Exception:
                    pass
    return None

def _extract_crs(las):
    crs_wkt = None
    crs_epsg = None

    try:
        crs = las.header.parse_crs()
        if crs:
            crs_wkt = crs.to_wkt()
            try:
                crs_epsg = crs.to_epsg()
            except Exception:
                crs_epsg = None

            if crs_epsg is None:
                try:
                    auth = crs.to_authority()
                    if auth and auth[0].upper() == "EPSG":
                        crs_epsg = int(auth[1])
                except Exception:
                    pass

            if crs_epsg is None and crs_wkt:
                crs_epsg = extract_epsg_from_wkt(crs_wkt)
    except Exception as exc:
        print(f"CRS not found in LAS/LAZ header: {exc}")

    # Fallback to direct VLR scanning if still None
    if crs_epsg is None:
        try:
            crs_epsg = extract_epsg_from_vlrs(las)
        except Exception as e:
            print(f"Error extracting EPSG from VLRs: {e}")

    # If we extracted crs_epsg but crs_wkt is still None, try to generate WKT from pyproj
    if crs_epsg is not None and crs_wkt is None:
        try:
            from pyproj import CRS
            crs_obj = CRS.from_epsg(crs_epsg)
            crs_wkt = crs_obj.to_wkt()
        except Exception:
            pass

    if crs_epsg is not None:
        print(f"📊 Extracted EPSG: {crs_epsg} from LAZ/LAS metadata")
    return crs_wkt, crs_epsg


def _apply_import_filters(xyz, rgb, intensity, classification, options, point_source_id=None):
    total_points = len(xyz)
    if total_points == 0:
        return xyz, rgb, intensity, classification, point_source_id

    nth_point = max(1, int(options.get("nth_point", 1) or 1))
    do_nth = bool(options.get("only_every") and nth_point > 1)

    # Physical class filter: keep only points whose classification is in the
    # selected set. This is what the user sees in Point Statistics.
    if options.get("only_class") and classification is not None:
        class_codes = options.get("class_codes") or []
        if class_codes:
            sel = np.isin(classification, [int(c) for c in class_codes])
            xyz = xyz[sel]
            if rgb is not None:
                rgb = rgb[sel]
            if intensity is not None:
                intensity = intensity[sel]
            classification = classification[sel]
            if point_source_id is not None:
                point_source_id = point_source_id[sel]
            print(f"   🔍 Class filter applied: {sel.sum():,} / {total_points:,} points kept "
                  f"(classes {sorted(int(c) for c in class_codes)})")
            if len(xyz) == 0:
                return xyz, rgb, intensity, classification, point_source_id

    if not do_nth:
        return xyz, rgb, intensity, classification, point_source_id

    sl = slice(None, None, nth_point)
    xyz = xyz[sl]
    if rgb is not None:
        rgb = rgb[sl]
    if intensity is not None:
        intensity = intensity[sl]
    if classification is not None:
        classification = classification[sl]
    if point_source_id is not None:
        point_source_id = point_source_id[sl]
    return xyz, rgb, intensity, classification, point_source_id


def load_lidar_file(
    filename,
    parent=None,
    import_options=None,
    prompt_user=True,
    disabled_attrs=None,
):
    """Load LAS/LAZ/PLY, optionally prompting for import settings first."""

    if filename.lower().endswith((".las", ".laz")):
        options = _merge_import_options(import_options, disabled_attrs)
        if prompt_user:
            options = prompt_lidar_import_options(
                filename,
                parent=parent,
                disabled_attrs=disabled_attrs,
                initial_options=options,
            )
            if options is None:
                return None

        las = laspy.read(filename)

        # Pre-allocate the XYZ output buffer and fill it column-by-column.
        # This avoids `np.vstack([las.x, las.y, las.z]).T` which builds three
        # temporary 1-D arrays AND a transposed copy on top of the laspy object
        # — a 4× memory peak that crashed 200–300M-point files.
        n_pts = int(las.header.point_count)
        xyz = np.empty((n_pts, 3), dtype=np.float64)
        xyz[:, 0] = las.x
        xyz[:, 1] = las.y
        xyz[:, 2] = las.z

        attrs = options.get("attributes", {}) if isinstance(options, dict) else {}
        rgb = None
        # Check if RGB channels exist in the file
        _has_rgb_fields = (
            hasattr(las, 'red') and
            hasattr(las, 'green') and
            hasattr(las, 'blue')
        )

        if _has_rgb_fields:
            if attrs.get("Color", True):
                # Stay in the native uint16 dtype that laspy already exposes.
                # The previous code cast to uint32 just to compute `r // 257`,
                # which allocated three extra 1.2GB buffers for a 300M-pt file.
                r = np.asarray(las.red,   dtype=np.uint16)
                g = np.asarray(las.green, dtype=np.uint16)
                b = np.asarray(las.blue,  dtype=np.uint16)

                raw_max = int(max(r.max(), g.max(), b.max()))
                print(f"  🔍 RGB raw: dtype=uint16, max={raw_max}")

                rgb = np.empty((n_pts, 3), dtype=np.uint8)
                if raw_max > 255:
                    # Standard LAS uint16 [0-65535] → uint8 [0-255].
                    # `// 257` keeps full uint16 range mapped 0..255 exactly.
                    # Division stays in uint16, cast to uint8 happens once on
                    # assignment into the pre-allocated buffer.
                    np.floor_divide(r, 257, out=r); rgb[:, 0] = r
                    np.floor_divide(g, 257, out=g); rgb[:, 1] = g
                    np.floor_divide(b, 257, out=b); rgb[:, 2] = b
                else:
                    # Already in uint8 range — direct cast.
                    rgb[:, 0] = r
                    rgb[:, 1] = g
                    rgb[:, 2] = b
                del r, g, b

                print(f"  ✅ RGB ready: max={rgb.max()}, min={rgb.min()}, mean={rgb.mean():.1f}")
            else:
                print(f"  ⚠️ RGB skipped (Color=False in options)")
        else:
            print(f"  ⚠️ No RGB fields in file")

        intensity = None
        if attrs.get("Intensity", False) and "intensity" in las.point_format.dimension_names:
            # float32 is enough for intensity (16-bit source) and halves the
            # buffer vs the previous `astype(float)` (== float64) cast.
            intensity = np.asarray(las.intensity, dtype=np.float32)

        classification = _extract_classification_array(las)
        # LAS Point Source ID is the conventional flight-line identifier.
        # Keep it aligned with xyz through import filtering so Display Mode can
        # reproduce MicroStation's selected-flight-lines behaviour.
        point_source_id = None
        if "point_source_id" in {str(d).lower() for d in las.point_format.dimension_names}:
            point_source_id = np.asarray(las.point_source_id, dtype=np.uint16).copy()

        crs_wkt, crs_epsg = _extract_crs(las)

        version_str = las.header.version
        if isinstance(version_str, tuple):
            version = version_str
        else:
            version = tuple(map(int, str(version_str).split(".")))

        point_format_id = las.header.point_format.id

        # Release the laspy object as soon as we have all extracted arrays.
        # laspy keeps the full record array (~3-4× xyz size) alive until
        # garbage collection; freeing it here drops peak RAM dramatically.
        del las

        xyz, rgb, intensity, classification, point_source_id = _apply_import_filters(
            xyz, rgb, intensity, classification, options, point_source_id
        )

        if parent is not None:
            parent.loaded_file = filename
            parent.last_save_path = filename
            try:
                from .save_pointcloud import load_drawings_from_las

                load_drawings_from_las(filename, parent)
            except Exception as exc:
                print(f"Could not load drawings: {exc}")

        return {
            "xyz": xyz,
            "rgb": rgb,
            "intensity": intensity,
            "classification": classification,
            "point_source_id": point_source_id,
            "crs_wkt": crs_wkt,
            "crs_epsg": crs_epsg,
            "input_format_version": version,
            "input_point_format": point_format_id,
            "import_options": options,
            "type": "las",
        }

    if filename.lower().endswith(".ply"):
        pcd = o3d.io.read_point_cloud(filename)

        if parent is not None:
            parent.loaded_file = filename
            parent.last_save_path = filename
            try:
                from .save_pointcloud import load_drawings_from_las

                load_drawings_from_las(filename, parent)
            except Exception as exc:
                print(f"Could not load drawings: {exc}")

        return {
            "xyz": np.asarray(pcd.points),
            "rgb": np.asarray(pcd.colors),
            "intensity": None,
            "classification": None,
            "crs_wkt": None,
            "crs_epsg": None,
            "type": "ply",
        }

    raise ValueError("Unsupported format")


