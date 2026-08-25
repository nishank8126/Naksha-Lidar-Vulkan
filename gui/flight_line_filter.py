"""Shared LAS flight-line visibility filtering for every display pipeline."""

import numpy as np


def flight_line_visibility_mask(app, length=None):
    """Return a cached full-data mask for checked LAS Point Source IDs."""
    data = getattr(app, "data", None)
    xyz = data.get("xyz") if isinstance(data, dict) else None
    n = len(xyz) if xyz is not None else int(length or 0)
    source_ids = data.get("point_source_id") if isinstance(data, dict) else None
    visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
    signature = flight_line_visibility_signature(app)
    source_valid = source_ids is not None and len(source_ids) == n
    cache_key = (id(source_ids) if source_valid else None, n, signature)

    if getattr(app, "_flight_line_mask_cache_key", None) == cache_key:
        cached = getattr(app, "_flight_line_mask_cache", None)
        if cached is not None and len(cached) == n:
            return cached

    hidden = [line_id for line_id, shown in signature if not shown]
    if source_valid and visibility and hidden:
        mask = ~np.isin(source_ids, hidden)
    else:
        mask = np.ones(n, dtype=bool)

    app._flight_line_mask_cache_key = cache_key
    app._flight_line_mask_cache = mask
    app._flight_line_visible_count_cache = int(np.count_nonzero(mask))
    return mask


def flight_line_visible_count(app, length=None):
    """Return the cached number of points allowed by flight-line selection."""
    mask = flight_line_visibility_mask(app, length)
    cached_count = getattr(app, "_flight_line_visible_count_cache", None)
    return int(cached_count if cached_count is not None else np.count_nonzero(mask))

def flight_line_visibility_signature(app):
    """Stable signature used to invalidate geometry caches after line changes."""
    visibility = dict(getattr(app, "flight_line_visibility", {}) or {})
    return tuple(sorted((int(k), bool(v)) for k, v in visibility.items()))
