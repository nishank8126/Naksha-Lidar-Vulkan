from __future__ import annotations

import os
import re
from pathlib import Path

import laspy
import numpy as np


# ============================================================
# PTC DESCRIPTION -> AI SEMANTIC CLASS
# ============================================================

def _semantic_key(description):
    """Normalize a PTC class label into an AI semantic key.

    Numeric PTC codes are never hardcoded here.  Only semantic names/aliases
    are recognized.  Core model keys use integers 0..4.  Optional post-pass
    semantics use strings.
    """
    text = re.sub(r"[^a-z0-9]+", "", str(description or "").lower())
    if not text:
        return None

    # Core 5-class semantics.
    if text == "ground" or text.startswith("groundpoint") or text.startswith("terrain"):
        return 0
    if text.startswith("lowvegetation") or text.startswith("lowveg"):
        return 1
    if (
        text.startswith("mediumvegetation")
        or text.startswith("mediumveg")
        or text.startswith("midvegetation")
        or text.startswith("midveg")
    ):
        return 2
    if text.startswith("highvegetation") or text.startswith("highveg"):
        return 3
    if text == "building" or text.startswith("buildings") or text.startswith("buildingroof"):
        return 4

    # Power assets.  ENEL/client PTCs often use conductor wording rather than wire.
    if (
        text.startswith("powerlinewire")
        or text == "wire"
        or text.startswith("electricwire")
        or text.startswith("bareconductor")
        or text.startswith("overheadconductor")
        or text.startswith("powerconductor")
        or text == "conductor"
        or text == "conductors"
    ):
        return "wire"

    if (
        text.startswith("powerlinepole")
        or text.startswith("powerpole")
        or text == "pole"
        or text == "poles"
        or text.startswith("electricpole")
        or text.startswith("utilitypole")
        or text.startswith("transmissionpole")
    ):
        return "pole"

    # Premium/Advanced QC semantics.
    if (
        text.startswith("uncategorized")
        or text.startswith("uncategorised")
        or text.startswith("unclassified")
        or text.startswith("uncertainground")
    ):
        return "uncategorized"

    if (
        text.startswith("lowpoint")
        or text.startswith("lowpoints")
        or text.startswith("lownoise")
    ):
        return "low_point"

    if (
        text.startswith("highpoint")
        or text.startswith("highpoints")
        or text.startswith("highnoise")
    ):
        return "high_noise"

    # Power V2: ENEL / utility naming aliases for Pole/Pylon class.
    # "Pylons or Poles" normalizes to "pylonsorpoles".
    if (
        text.startswith("pylonsorpoles")
        or text.startswith("pylonorpole")
        or text == "pylon"
        or text == "pylons"
        or text == "pole"
        or text == "poles"
        or text.startswith("transmissiontower")
        or text.startswith("powertower")
        or text.startswith("utilitypole")
        or text.startswith("overheadlinepole")
    ):
        return "pole"

    return None

def spin_codes_from_ptc_schema(ptc_schema):
    """Read semantic class codes from the PTC explicitly Applied in Display Mode.

    Different PTC formats store the human semantic name in different fields.
    ENEL-style schemas commonly use ``lvl`` while ``description`` contains a
    drafting rule such as "Zero length line".  We therefore inspect a small set
    of known text fields and use the first semantic match.
    """
    result = {}
    if not isinstance(ptc_schema, dict):
        return result

    for raw_code, info in ptc_schema.items():
        if not isinstance(info, dict):
            continue

        key = None
        for candidate in (
            info.get("description", ""),
            info.get("lvl", ""),
            info.get("level", ""),
            info.get("name", ""),
            info.get("label", ""),
        ):
            candidate_key = _semantic_key(candidate)
            if candidate_key is not None:
                key = candidate_key
                break

        if key is None:
            continue

        try:
            code = int(raw_code)
        except Exception:
            continue
        if not 0 <= code <= 255:
            continue

        if key not in result:
            result[key] = code
            print(
                f"[PTC] Semantic match: code={code} "
                f"description={info.get('description', '')!r} "
                f"lvl={info.get('lvl', '')!r} -> semantic={key}",
                flush=True,
            )

    return result


# ============================================================
# CLASS ARRAY REMAPPING
# ============================================================

def remap_classes(
    classes,
    source_to_internal,
    class_mapping,
):
    """
    Remap finished AI output into the class codes selected
    from the Applied PTC.

    source_to_internal example for Basic / Advanced:

        1 -> internal 0 Ground
        2 -> internal 1 Low vegetation
        3 -> internal 2 Medium vegetation
        4 -> internal 3 High vegetation
        5 -> internal 4 Building

    Premium:

        2 -> internal 0 Ground
        3 -> internal 1 Low vegetation
        4 -> internal 2 Medium vegetation
        5 -> internal 3 High vegetation
        6 -> internal 4 Building
    """

    source = np.asarray(
        classes,
        dtype=np.uint8
    )

    result = source.copy()

    mapping = dict(class_mapping or {})

    # IMPORTANT:
    # Masks always use ORIGINAL source array.
    # This prevents cascading if a destination code equals
    # another source code.
    for source_code, internal_class in source_to_internal.items():

        if internal_class not in mapping:
            continue

        try:
            destination_code = int(mapping[internal_class])
        except Exception:
            continue

        if not 0 <= destination_code <= 255:
            raise ValueError(
                f"PTC class code must be 0-255, got {destination_code}"
            )

        result[source == int(source_code)] = np.uint8(
            destination_code
        )

    return result


# ============================================================
# LAS 1.4 SUPPORT FOR PTC CODES > 31
# ============================================================

def _las14_point_format_for(source_format_id: int) -> int:

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


def _make_extended_header(source_header):

    target_pf = _las14_point_format_for(
        source_header.point_format.id
    )

    header = laspy.LasHeader(
        version="1.4",
        point_format=target_pf,
    )

    header.scales = np.asarray(
        source_header.scales
    ).copy()

    header.offsets = np.asarray(
        source_header.offsets
    ).copy()

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


# ============================================================
# REMAP COMPLETE LAS/LAZ OUTPUT FILE
# ============================================================

def remap_las_file_in_place(
    path,
    source_to_internal,
    class_mapping,
    chunk_size=1_000_000,
):
    """
    Remap an already-completed AI LAS/LAZ file.

    The AI engine finishes FIRST using its normal validated
    internal output classes.

    Only afterwards are those classes changed to the
    Applied PTC codes.

    If a PTC class is >31 and the source uses a legacy LAS
    point format, output is automatically upgraded to LAS 1.4.
    """

    path = Path(path)

    if not path.is_file():
        raise FileNotFoundError(
            f"PTC remap input not found: {path}"
        )

    mapping = dict(class_mapping or {})

    effective = {}

    for source_code, internal_class in source_to_internal.items():

        if internal_class not in mapping:
            continue

        destination = int(
            mapping[internal_class]
        )

        if not 0 <= destination <= 255:
            raise ValueError(
                f"Invalid PTC class code {destination}"
            )

        effective[int(source_code)] = destination

    if not effective:
        return False

    # Nothing actually changes.
    if all(
        int(source_code) == int(destination)
        for source_code, destination in effective.items()
    ):
        return False

    temp_path = path.with_name(
        f"{path.stem}.ptc_remap_tmp{path.suffix}"
    )

    try:
        if temp_path.exists():
            temp_path.unlink()
    except Exception:
        pass

    print(
        f"[PTC] Remapping AI output: {effective}",
        flush=True,
    )

    try:

        with laspy.open(
            str(path),
            mode="r"
        ) as reader:

            source_pf = int(
                reader.header.point_format.id
            )

            max_destination = max(
                effective.values()
            )

            needs_extended = (
                source_pf <= 5
                and max_destination > 31
            )

            if needs_extended:
                output_header = _make_extended_header(
                    reader.header
                )

                print(
                    f"[PTC] LAS upgrade required: "
                    f"PF {source_pf} -> "
                    f"LAS 1.4 PF {output_header.point_format.id}",
                    flush=True,
                )
            else:
                output_header = reader.header.copy()

            with laspy.open(
                str(temp_path),
                mode="w",
                header=output_header,
                do_compress=(
                    path.suffix.lower() == ".laz"
                ),
            ) as writer:

                for points in reader.chunk_iterator(
                    int(chunk_size)
                ):

                    mapped = remap_classes(
                        points.classification,
                        source_to_internal=source_to_internal,
                        class_mapping=mapping,
                    )

                    if needs_extended:

                        out_points = (
                            laspy.PackedPointRecord.from_point_record(
                                points,
                                output_header.point_format,
                            )
                        )

                    else:
                        out_points = points

                    out_points.classification = mapped

                    writer.write_points(
                        out_points
                    )

        os.replace(
            str(temp_path),
            str(path)
        )

        print(
            f"[PTC] Output remap complete: {path}",
            flush=True,
        )

        return True

    except Exception:

        try:
            if temp_path.exists():
                temp_path.unlink()
        except Exception:
            pass

        raise