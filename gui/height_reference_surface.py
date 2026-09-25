"""Temporary TIN reference surface for height-based classification."""

from __future__ import annotations

import numpy as np
from scipy.spatial import Delaunay, QhullError


class ReferenceSurfaceError(ValueError):
    """Raised when a usable reference TIN cannot be constructed."""


def heights_above_reference_tin(reference_xyz, query_xyz, max_triangle,
                                batch_size=500_000):
    """Return height above a temporary XY Delaunay TIN and validity mask.

    A query is valid only when it lies inside a triangle whose three XY edge
    lengths do not exceed ``max_triangle``. Z is evaluated on the triangle
    plane by barycentric interpolation. This matches Nakshatech's documented
    By height from ground surface/Max triangle semantics.
    """
    reference_xyz = np.asarray(reference_xyz)
    query_xyz = np.asarray(query_xyz)
    max_edge = float(max_triangle)
    if reference_xyz.ndim != 2 or reference_xyz.shape[1] < 3:
        raise ReferenceSurfaceError("Reference points must be an N x 3 array")
    if query_xyz.ndim != 2 or query_xyz.shape[1] < 3:
        raise ReferenceSurfaceError("Query points must be an N x 3 array")
    if max_edge <= 0 or not np.isfinite(max_edge):
        raise ReferenceSurfaceError("Max triangle must be greater than zero")

    finite_ref = np.all(np.isfinite(reference_xyz[:, :3]), axis=1)
    ref = np.asarray(reference_xyz[finite_ref, :3], dtype=np.float64)
    if len(ref) < 3:
        raise ReferenceSurfaceError("At least three finite reference points are required")

    # Qhull cannot triangulate duplicate XY vertices. Keep a deterministic
    # representative (the first) without copying the full cloud repeatedly.
    _, unique_pos = np.unique(ref[:, :2], axis=0, return_index=True)
    ref = ref[np.sort(unique_pos)]
    if len(ref) < 3:
        raise ReferenceSurfaceError("At least three unique XY reference points are required")
    try:
        tin = Delaunay(ref[:, :2])
    except QhullError as exc:
        raise ReferenceSurfaceError("Reference points cannot form a 2D surface") from exc

    faces = np.asarray(tin.simplices, dtype=np.intp)
    triangle_xy = ref[faces, :2]
    edge2 = np.maximum.reduce((
        np.sum((triangle_xy[:, 0] - triangle_xy[:, 1]) ** 2, axis=1),
        np.sum((triangle_xy[:, 1] - triangle_xy[:, 2]) ** 2, axis=1),
        np.sum((triangle_xy[:, 2] - triangle_xy[:, 0]) ** 2, axis=1),
    ))
    usable_triangle = edge2 <= max_edge * max_edge

    heights = np.full(len(query_xyz), np.nan, dtype=np.float64)
    valid = np.zeros(len(query_xyz), dtype=bool)
    chunk = max(1, int(batch_size))
    for start in range(0, len(query_xyz), chunk):
        stop = min(start + chunk, len(query_xyz))
        query = np.asarray(query_xyz[start:stop, :3], dtype=np.float64)
        finite = np.all(np.isfinite(query), axis=1)
        if not np.any(finite):
            continue
        local_rows = np.flatnonzero(finite)
        simplex = tin.find_simplex(query[finite, :2])
        supported = simplex >= 0
        supported[supported] &= usable_triangle[simplex[supported]]
        if not np.any(supported):
            continue

        sid = simplex[supported]
        xy = query[finite, :2][supported]
        transform = tin.transform[sid]
        first_two = np.einsum(
            "nij,nj->ni", transform[:, :2, :], xy - transform[:, 2, :]
        )
        weights = np.column_stack((first_two, 1.0 - first_two.sum(axis=1)))
        surface_z = np.einsum("ni,ni->n", weights, ref[faces[sid], 2])
        output_rows = start + local_rows[supported]
        heights[output_rows] = query[finite, 2][supported] - surface_z
        valid[output_rows] = True

    return heights, valid, {
        "reference_points": int(len(ref)),
        "triangles": int(len(faces)),
        "usable_triangles": int(np.count_nonzero(usable_triangle)),
        "max_triangle": max_edge,
    }
