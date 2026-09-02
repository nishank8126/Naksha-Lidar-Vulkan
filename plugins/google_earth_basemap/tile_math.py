"""Pure slippy-map/Web-Mercator helpers for the Naksha Basemap plugin.

This module deliberately has no Qt/VTK imports so it can be unit-tested alone.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Tuple

MAX_MERCATOR_LAT = 85.05112878
EARTH_RADIUS_M = 6378137.0
WEB_MERCATOR_HALF_WORLD_M = math.pi * EARTH_RADIUS_M


def clamp_lat(lat: float) -> float:
    return max(-MAX_MERCATOR_LAT, min(MAX_MERCATOR_LAT, float(lat)))


def lon_to_world_x(lon: float) -> float:
    return (float(lon) + 180.0) / 360.0


def lat_to_world_y(lat: float) -> float:
    lat = clamp_lat(lat)
    lat_rad = math.radians(lat)
    return (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0


def world_y_to_lat(world_y: float) -> float:
    n = math.pi * (1.0 - 2.0 * float(world_y))
    return math.degrees(math.atan(math.sinh(n)))


def tile_x_to_lon(tile_x: float, zoom: int) -> float:
    n = float(1 << int(zoom))
    return float(tile_x) / n * 360.0 - 180.0


def tile_y_to_lat(tile_y: float, zoom: int) -> float:
    n = float(1 << int(zoom))
    return world_y_to_lat(float(tile_y) / n)


def tile_bounds_lonlat(x: int, y: int, zoom: int) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) for a slippy-map tile."""
    west = tile_x_to_lon(x, zoom)
    east = tile_x_to_lon(x + 1, zoom)
    north = tile_y_to_lat(y, zoom)
    south = tile_y_to_lat(y + 1, zoom)
    return west, south, east, north


def lonlat_to_web_mercator(lon: float, lat: float) -> Tuple[float, float]:
    lat = clamp_lat(lat)
    x = EARTH_RADIUS_M * math.radians(float(lon))
    y = EARTH_RADIUS_M * math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))
    return x, y


def web_mercator_to_lonlat(x: float, y: float) -> Tuple[float, float]:
    lon = math.degrees(float(x) / EARTH_RADIUS_M)
    lat = math.degrees(2.0 * math.atan(math.exp(float(y) / EARTH_RADIUS_M)) - math.pi / 2.0)
    return lon, clamp_lat(lat)


def _normalized_bounds(west: float, south: float, east: float, north: float) -> Tuple[float, float, float, float]:
    west = max(-180.0, min(180.0, float(west)))
    east = max(-180.0, min(180.0, float(east)))
    south = clamp_lat(south)
    north = clamp_lat(north)
    if south > north:
        south, north = north, south
    if west > east:
        # This plugin intentionally treats antimeridian-spanning viewports as
        # a full-width interval. Normal project work rarely spans +/-180 deg,
        # and this avoids accidental huge tile bursts.
        west, east = -180.0, 180.0
    return west, south, east, north


def tile_range_for_bounds(
    west: float,
    south: float,
    east: float,
    north: float,
    zoom: int,
    margin: int = 0,
) -> Tuple[int, int, int, int]:
    """Return inclusive x_min, y_min, x_max, y_max for geographic bounds."""
    west, south, east, north = _normalized_bounds(west, south, east, north)
    z = int(zoom)
    n = 1 << z

    # Keep east/south infinitesimally inside their boundary so a viewport that
    # ends exactly on a tile edge does not request the next tile unnecessarily.
    eps = 1e-12
    x0 = int(math.floor(lon_to_world_x(west) * n))
    x1 = int(math.floor(lon_to_world_x(east - eps) * n))
    y0 = int(math.floor(lat_to_world_y(north) * n))
    y1 = int(math.floor(lat_to_world_y(south - eps) * n))

    x0 = max(0, min(n - 1, x0 - int(margin)))
    x1 = max(0, min(n - 1, x1 + int(margin)))
    y0 = max(0, min(n - 1, y0 - int(margin)))
    y1 = max(0, min(n - 1, y1 + int(margin)))
    return x0, y0, x1, y1


def enumerate_tiles(
    west: float,
    south: float,
    east: float,
    north: float,
    zoom: int,
    margin: int = 0,
) -> List[Tuple[int, int, int]]:
    x0, y0, x1, y1 = tile_range_for_bounds(west, south, east, north, zoom, margin=margin)
    return [(int(zoom), x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


def choose_zoom(
    west: float,
    south: float,
    east: float,
    north: float,
    viewport_width_px: int,
    viewport_height_px: int,
    *,
    tile_size_px: int = 256,
    min_zoom: int = 0,
    max_zoom: int = 22,
    max_tiles: int = 36,
    margin: int = 0,
) -> int:
    """Pick a zoom that roughly matches screen resolution without tile bursts."""
    west, south, east, north = _normalized_bounds(west, south, east, north)
    width_px = max(1, int(viewport_width_px))
    height_px = max(1, int(viewport_height_px))
    tile_px = max(1, int(tile_size_px))

    span_x = max(1e-12, abs(lon_to_world_x(east) - lon_to_world_x(west)))
    span_y = max(1e-12, abs(lat_to_world_y(south) - lat_to_world_y(north)))

    target_tiles_x = max(1.0, width_px / tile_px)
    target_tiles_y = max(1.0, height_px / tile_px)
    z_x = math.log2(target_tiles_x / span_x)
    z_y = math.log2(target_tiles_y / span_y)
    z = int(round(max(z_x, z_y)))
    z = max(int(min_zoom), min(int(max_zoom), z))

    # Ensure margin-expanded tile count stays inside the configured budget.
    while z > int(min_zoom):
        tiles = enumerate_tiles(west, south, east, north, z, margin=margin)
        if len(tiles) <= int(max_tiles):
            break
        z -= 1
    return z


def max_zoom_for_center(max_zoom_rects: Iterable[dict], lon: float, lat: float, fallback: int = 22) -> int:
    """Choose the most specific maxZoom rectangle covering the viewport center."""
    lon = float(lon)
    lat = float(lat)
    candidates = []
    for rect in max_zoom_rects or []:
        try:
            north = float(rect["north"])
            south = float(rect["south"])
            east = float(rect["east"])
            west = float(rect["west"])
            max_zoom = int(rect["maxZoom"])
        except Exception:
            continue
        lat_ok = south <= lat <= north
        if west <= east:
            lon_ok = west <= lon <= east
        else:
            lon_ok = lon >= west or lon <= east
        if lat_ok and lon_ok:
            candidates.append(max_zoom)
    return max(candidates) if candidates else int(fallback)
