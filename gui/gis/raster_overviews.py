# ─────────────────────────────────────────────────────────────────────────────
# raster_overviews.py — build resolution pyramids for imported GeoTIFFs
#
# raster_lod.py re-reads a windowed crop of the source file on every zoom
# level, matching QGIS's dynamic-refresh behaviour. But without a pyramid
# (overview levels) already baked into the file, every one of those reads -
# at ANY zoom, including zoomed far out - has to decimate the full-resolution
# base raster on the fly. That's the same cost Nakshatech/QGIS/ArcGIS avoid
# by relying on pre-built overviews: GDAL picks the closest matching pyramid
# level transparently for a decimated read, with no code-level change needed
# on the reading side.
#
# This module builds that pyramid once per file, the first time it's
# imported, as an EXTERNAL sidecar (<file>.ovr) - the source GeoTIFF itself
# is never opened for writing and never modified. That matters here more than
# usual: ortho files are routinely imported straight off a network share
# (\\nast\...) that may be read-only for this user or actively open elsewhere,
# and an in-place internal-overview write is exactly the kind of operation
# that can corrupt a file if interrupted or contended. An external .ovr next
# to it carries none of that risk - worst case, building it fails and this
# falls back to exactly today's behaviour (direct full-res decimation).
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import os
import threading

_MIN_OVERVIEW_DIM = 256   # stop generating levels once a factor would shrink below this
_MAX_OVERVIEW_FACTOR = 64  # hard ceiling regardless of source size
_building = set()          # paths with a build currently in flight (this process)
_lock = threading.Lock()


def _overview_factors(width, height):
    factors = []
    factor = 2
    while (width // factor) >= _MIN_OVERVIEW_DIM and (height // factor) >= _MIN_OVERVIEW_DIM:
        factors.append(factor)
        if factor >= _MAX_OVERVIEW_FACTOR:
            break
        factor *= 2
    return factors


def _build_overviews_worker(path):
    # Runs on a background thread: file I/O and GDAL calls only, no Qt/VTK/app
    # state (same convention as raster_lod.py's background reads).
    try:
        from osgeo import gdal
    except Exception as exc:
        print(f"   ⚠️ Overview build skipped for {os.path.basename(path)}: GDAL unavailable ({exc})")
        return

    try:
        # Deliberately NOT calling gdal.UseExceptions() here: it is a
        # process-wide flag, not thread-local, and this runs on a background
        # thread on every raster import. Flipping it here would silently
        # turn every OTHER gdal.Open()/ogr.Open() in the app - including on
        # the main thread - from "returns None on failure" (what the rest of
        # this codebase checks for) into "raises an exception" from that
        # point on. This function already handles failure via `if ds is
        # None` and the outer try/except; it doesn't need exceptions mode.
        #
        # Opened read-only: GDAL cannot write internal overviews into a
        # read-only-opened dataset, so BuildOverviews() below is routed to an
        # external <path>.ovr sidecar instead - the source file is never
        # touched. This is the same mechanism as `gdaladdo -ro`.
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is None:
            print(f"   ⚠️ Overview build skipped for {os.path.basename(path)}: GDAL could not open it")
            return
        width, height = ds.RasterXSize, ds.RasterYSize
        factors = _overview_factors(width, height)
        if not factors:
            ds = None
            return
        ds.BuildOverviews("AVERAGE", factors)
        ds = None
        print(f"   🏔️ Built overview pyramid for {os.path.basename(path)}: factors {factors}")
    except Exception as exc:
        # Read-only network share, no write permission, corrupt file, etc.
        # Not fatal - raster_lod.py's reads work exactly as before without it.
        print(f"   ⚠️ Overview build failed for {os.path.basename(path)} (falling back to direct reads): {exc}")
    finally:
        with _lock:
            _building.discard(path)


def ensure_overviews_async(path):
    """Fire-and-forget: build <path>.ovr in the background if it doesn't
    already exist. Safe to call on every import - idempotent and cheap to
    skip once the sidecar is present. Never blocks the caller and never
    raises."""
    try:
        if not path or os.path.exists(path + ".ovr"):
            return
        with _lock:
            if path in _building:
                return
            _building.add(path)
        threading.Thread(
            target=_build_overviews_worker, args=(path,),
            name="raster-overview-build", daemon=True,
        ).start()
    except Exception as exc:
        print(f"   ⚠️ Could not schedule overview build for {os.path.basename(path) if path else path}: {exc}")


def _demo():
    """Self-check for the factor computation (no GDAL/file I/O needed)."""
    # Small file: no level would clear _MIN_OVERVIEW_DIM (256) at factor 2.
    assert _overview_factors(400, 300) == []
    # Just big enough for one level.
    assert _overview_factors(600, 600) == [2]
    # A large ortho: keeps doubling until a level would drop below 256px.
    # (8000 // 32 == 250 < 256, so 32 is excluded - stops at 16.)
    assert _overview_factors(10000, 8000) == [2, 4, 8, 16]
    # Very large: capped at _MAX_OVERVIEW_FACTOR (64), not left unbounded.
    assert _overview_factors(200000, 200000) == [2, 4, 8, 16, 32, 64]
    # Non-square: the smaller dimension governs when levels stop.
    assert _overview_factors(20000, 600) == [2]
    print("raster_overviews self-check OK")


if __name__ == "__main__":
    _demo()
