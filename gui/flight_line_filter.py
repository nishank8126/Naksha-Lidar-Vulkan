"""Shared LAS flight-line visibility filtering for every display pipeline."""

import numpy as np


def flight_line_visibility_for_slot(app, slot=0):
    """Return one view slot's line checks, with legacy Main View fallback."""
    slot = int(slot or 0)
    by_slot = getattr(app, "flight_line_visibility_by_slot", None)
    if isinstance(by_slot, dict):
        value = by_slot.get(slot, by_slot.get(str(slot)))
        if isinstance(value, dict):
            return {int(k): bool(v) for k, v in value.items()}
    if slot == 0:
        return {
            int(k): bool(v)
            for k, v in dict(getattr(app, "flight_line_visibility", {}) or {}).items()
        }
    return {}


def flight_line_visibility_mask(app, length=None, slot=0):
    """Return a cached full-data mask for one view's checked flight lines."""
    data = getattr(app, "data", None)
    xyz = data.get("xyz") if isinstance(data, dict) else None
    n = len(xyz) if xyz is not None else int(length or 0)
    source_ids = data.get("point_source_id") if isinstance(data, dict) else None
    slot = int(slot or 0)
    visibility = flight_line_visibility_for_slot(app, slot)
    signature = flight_line_visibility_signature(app, slot)
    source_valid = source_ids is not None and len(source_ids) == n
    cache_key = (slot, id(source_ids) if source_valid else None, n, signature)

    cache_by_slot = getattr(app, "_flight_line_mask_cache_by_slot", None)
    if not isinstance(cache_by_slot, dict):
        cache_by_slot = {}
        app._flight_line_mask_cache_by_slot = cache_by_slot
    cached_entry = cache_by_slot.get(slot)
    if cached_entry is not None and cached_entry[0] == cache_key:
        cached = cached_entry[1]
        if cached is not None and len(cached) == n:
            return cached

    hidden = [line_id for line_id, shown in signature if not shown]
    if source_valid and visibility and hidden:
        mask = ~np.isin(source_ids, hidden)
    else:
        mask = np.ones(n, dtype=bool)

    count = int(np.count_nonzero(mask))
    cache_by_slot[slot] = (cache_key, mask, count)
    if slot == 0:
        app._flight_line_mask_cache_key = cache_key
        app._flight_line_mask_cache = mask
        app._flight_line_visible_count_cache = count
    return mask


def flight_line_visible_count(app, length=None, slot=0):
    """Return the cached number of points allowed by flight-line selection."""
    mask = flight_line_visibility_mask(app, length, slot)
    cache_by_slot = getattr(app, "_flight_line_mask_cache_by_slot", {})
    entry = cache_by_slot.get(int(slot or 0))
    return int(entry[2] if entry is not None else np.count_nonzero(mask))


def filter_visible_flight_line_indices(app, indices, length=None, slot=0):
    """Return only selected indices whose LAS flight line is enabled."""
    indices = np.asarray(indices, dtype=np.int64).ravel()
    if indices.size == 0:
        return indices
    mask = flight_line_visibility_mask(app, length, slot)
    valid = (indices >= 0) & (indices < len(mask))
    if not np.all(valid):
        indices = indices[valid]
    return indices[mask[indices]] if indices.size else indices


def intersect_with_visible_flight_lines(app, mask, length=None, slot=0):
    """Intersect a full-data boolean eligibility mask with one view's lines."""
    mask = np.asarray(mask, dtype=bool).ravel()
    line_mask = flight_line_visibility_mask(app, length or len(mask), slot)
    if len(mask) != len(line_mask):
        return np.zeros(len(line_mask), dtype=bool)
    return mask & line_mask


def active_classification_flight_line_slot(app):
    """Resolve the per-view line slot for shared classification functions."""
    target = str(getattr(app, "active_classify_target", "") or "").lower()
    if target == "cut":
        return 5
    if target in ("cross", "section", "cross_section"):
        controller = getattr(app, "section_controller", None)
        view_idx = getattr(controller, "active_view", None)
        if view_idx is not None:
            return int(view_idx) + 1
    return 0

def flight_line_visibility_signature(app, slot=0):
    """Stable signature used to invalidate geometry caches after line changes."""
    visibility = flight_line_visibility_for_slot(app, slot)
    return tuple(sorted((int(k), bool(v)) for k, v in visibility.items()))
