"""Shared LAS flight-line visibility filtering for every display pipeline."""

import numpy as np


def flight_line_visibility_mask(app, length=None):
    """Return a full-data boolean mask for checked LAS Point Source IDs.

    Missing source data means no filtering, preserving behaviour for PLY and
    LAS files without Point Source IDs.
    """
    data = getattr(app, "data", None)
    if not isinstance(data, dict):
        return np.ones(int(length or 0), dtype=bool)
    xyz = data.get("xyz")
    n = len(xyz) if xyz is not None else int(length or 0)
    source_ids = data.get("point_source_id")
    if source_ids is None or len(source_ids) != n:
        return np.ones(n, dtype=bool)
    visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
    if not visibility:
        return np.ones(n, dtype=bool)
    selected = [int(v) for v in np.unique(source_ids)
                if bool(visibility.get(int(v), True))]
    return np.isin(source_ids, selected)


def flight_line_visibility_signature(app):
    """Stable signature used to invalidate geometry caches after line changes."""
    visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
    return tuple(sorted((int(k), bool(v)) for k, v in visibility.items()))
