"""DEV-only exact projected occupancy oracle, one decoded tile at a time.

Bounding boxes prune I/O; they never establish occupancy. Actual canonical
XYZ samples establish both source support and submitted representation.
"""
import numpy as np
from .format import ATTR_XYZ


def bin_points(points, viewport, grid=(64, 36)):
    cx, cy, hw, hh = map(float, viewport)
    gx, gy = grid
    x, y = points[:, 0], points[:, 1]
    mask = (x >= cx-hw) & (x < cx+hw) & (y >= cy-hh) & (y < cy+hh)
    ix = np.floor((x[mask]-(cx-hw))*gx/(2*hw)).astype(np.int64)
    iy = np.floor((y[mask]-(cy-hh))*gy/(2*hh)).astype(np.int64)
    return np.bincount(iy*gx+ix, minlength=gx*gy).reshape(gy, gx)


def validate_occupancy(manager, viewport, width, height, bbox_diagnostic=None):
    from .visual_quality import STARVED, _components
    # Resolve partial-cell footprints from actual points on a deterministic
    # 16x16 subgrid. A survey sliver must not be judged against the entire
    # rectangular coarse cell's pixel area.
    source_fine = np.zeros((36*16, 64*16), dtype=np.int64)
    drawn = np.zeros((36, 64), dtype=np.int64)
    cx, cy, hw, hh = viewport
    idx, reader = manager.idx, manager.reader
    nodes = {int(row["node_id"]): row for row in idx.nodes}
    for entry in idx.blocks:
        if int(entry["lod"]) != 0:
            continue
        node = nodes[int(entry["node_id"])]
        lo, hi = node["bounds_min"], node["bounds_max"]
        if hi[0] < cx-hw or lo[0] > cx+hw or hi[1] < cy-hh or lo[1] > cy+hh:
            continue
        tile = reader.read_tile(int(entry["node_id"]), 0, only_attrs=[ATTR_XYZ],
                                apply_edits=False, render_space=False)
        source_fine += bin_points(tile["xyz"], viewport, grid=(64*16, 36*16))
    for node_id, lod in manager.active_draw_keys:
        tile = reader.read_tile(node_id, lod, only_attrs=[ATTR_XYZ],
                                apply_edits=False, render_space=False)
        if tile is not None:
            drawn += bin_points(tile["xyz"], viewport)
    source = source_fine.reshape(36, 16, 64, 16).sum(axis=(1, 3))
    occupied_fraction = (source_fine > 0).reshape(36, 16, 64, 16).mean(axis=(1, 3))
    pixels_per_cell = width * height / source.size
    occupied_pixels = np.maximum(occupied_fraction * pixels_per_cell, 1e-12)
    target = max(float(manager.density.last_target), 1) / (width * height)
    occupied = source > 0
    missing = occupied & (drawn == 0)
    budget_starved = occupied & (drawn > 0) & (drawn / occupied_pixels < .15*target) & (source / occupied_pixels >= target)
    starved = missing | budget_starved
    old = bbox_diagnostic["cls"] == STARVED if bbox_diagnostic is not None else np.zeros_like(occupied)
    components = _components(starved)
    return dict(total_cells=source.size, occupied_cells=int(occupied.sum()),
        genuinely_empty_cells=int((~occupied).sum()),
        occupied_missing_representation=int(missing.sum()),
        occupied_budget_starved=int(budget_starved.sum()),
        occupied_starved_cells=int(starved.sum()),
        largest_occupied_starved_region=max(components, default=0),
        false_starvation_empty_footprint=int((old & ~occupied).sum()),
        false_starvation_density_estimate=int((old & occupied & ~starved).sum()),
        bbox_starved_cells=int(old.sum()), source_counts=source.tolist(),
        drawn_counts=drawn.tolist(), viewport=list(viewport),
        occupied_area_pixels=occupied_pixels.tolist(), occupancy_subgrid=[1024, 576],
        occupancy_truth="actual canonical XYZ; bounding boxes used only to prune reads")
