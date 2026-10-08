"""Render MICROCELLS: sub-ranges inside a physical NKPC page (Phase A).

The measured problem this solves:

    leaf extent   min 155 m   median 202 m   P95 1353 m   max 1414 m
    a 31 m view (x32) still intersected 39 physical leaves

Shrinking the physical pages to 5K points fixed zoom but destroyed storage
granularity (832 leaves, 5,460 pts median). So storage granularity and render
granularity are SEPARATED here:

    physical page  ~150K-256K points   -> how we read from disk efficiently
      +- microcell ~8K-32K points      -> what part of the page matters now
      +- microcell
      +- ...

Points inside a page are already MORTON ORDERED, so a microcell is a CONTIGUOUS
[point_start, point_start+point_count) range of every SoA stream. No point data
is duplicated, and a microcell read is a byte-range read inside one page.

MICROCELL_ENTRY stores enough to cull and to fetch without touching the page
header, including a per-microcell checksum so integrity survives subrange reads
(Part A5): a 100 KB request must not CRC the whole 6 MB page.
"""
import numpy as np

from .format import (ATTR_XYZ, crc32_bytes, dequantize_xyz,
                     stream_component_count)

# MICROCELL_ENTRY. One row per render microcell.
MICROCELL_ENTRY = np.dtype([
    ("block_id", "u4"),        # parent physical block
    ("lod", "u2"), ("reserved0", "u2"),
    ("point_start", "u8"),
    ("point_count", "u8"),
    ("bounds_min", "f8", 3),
    ("bounds_max", "f8", 3),
    ("spacing", "f8"),         # local point spacing in metres
    ("crc", "u4"),
])

# Candidate targets (Part A2). These are BENCHMARK inputs, not a decision:
# build the table at several settings and compare culling precision against
# draw-range count.
MICROCELL_TARGET_POINTS = 12_000
MICROCELL_MAX_EXTENT_M = 45.0
# Below this a microcell is not worth a separate range.
MICROCELL_MIN_POINTS = 2_000


def build_microcells(block_id, lod, local_xyz, origin, scale,
                     target_points=MICROCELL_TARGET_POINTS,
                     max_extent=MICROCELL_MAX_EXTENT_M,
                     min_points=MICROCELL_MIN_POINTS):
    """Split one Morton-ordered page into contiguous microcell ranges.

    Points arrive Morton-ordered, so a fixed point count gives a spatially
    compact range with no reordering and no copy. The walk also splits early if
    a range already spans more than `max_extent` metres, which bounds how large
    a culled region can be even where density is high.
    """
    n = local_xyz.shape[0]
    if n == 0:
        return np.zeros(0, dtype=MICROCELL_ENTRY)
    o = np.asarray(origin, dtype=np.float64)
    s = np.asarray(scale, dtype=np.float64)
    world = np.asarray(local_xyz, dtype=np.float64) * s + o

    starts = []
    step = max(1, int(target_points))
    i = 0
    while i < n:
        j = min(i + step, n)
        # Split early if the candidate range is already spatially too wide.
        # Cheaper than measuring every candidate: check the running extent of
        # the slice we are about to emit.
        sub = world[i:j]
        ext = float(np.max(sub[:, 0]) - np.min(sub[:, 0]))
        if ext > max_extent and j - i > 1:
            j = i + max(1, int((j - i) * max_extent / ext))
        if j - i < min_points and starts:
            # merge a short tail into the previous range
            break
        starts.append((i, j - i))
        i = j
    if i < n and starts:
        s0, c0 = starts[-1]
        starts[-1] = (s0, n - s0)

    rows = np.zeros(len(starts), dtype=MICROCELL_ENTRY)
    for k, (st, ct) in enumerate(starts):
        sub = world[st:st + ct]
        rows[k]["block_id"] = block_id
        rows[k]["lod"] = lod
        rows[k]["point_start"] = st
        rows[k]["point_count"] = ct
        rows[k]["bounds_min"] = (sub[:, 0].min(), sub[:, 1].min(),
                                 sub[:, 2].min())
        rows[k]["bounds_max"] = (sub[:, 0].max(), sub[:, 1].max(),
                                 sub[:, 2].max())
        # Local spacing from the OCCUPIED area of this microcell, not the full
        # page AABB (Phase B): an elongated page otherwise looks far coarser
        # than its data actually is.
        area = max((float(sub[:, 0].max() - sub[:, 0].min()) *
                    float(sub[:, 1].max() - sub[:, 1].min())), 1e-9)
        rows[k]["spacing"] = float(np.sqrt(area / max(ct, 1)))
    return rows


def local_spacing_estimate(local_xyz, origin, scale, sample=4096):
    """Representative LOCAL point spacing (Phase B, method 1).

    Samples deterministic Morton-local XY neighbour distances rather than using
    sqrt(full_AABB_area / count). The AABB form is dominated by EMPTY area and
    by an elongated flight-line bounding box, so sparse or skewed leaves appear
    far coarser than the data really is, which in turn suppresses finer LOD
    rungs at runtime.
    """
    n = local_xyz.shape[0]
    if n < 2:
        return 0.0
    o = np.asarray(origin, dtype=np.float64)
    s = np.asarray(scale, dtype=np.float64)
    step = max(1, n // max(sample, 1))
    pts = np.asarray(local_xyz[::step], dtype=np.float64)[:, :2] * s[:2]
    if pts.shape[0] < 2:
        return 0.0
    d = np.hypot(np.diff(pts[:, 0]), np.diff(pts[:, 1]))
    d = d[d > 0]
    if d.size == 0:
        return 0.0
    return float(np.median(d))