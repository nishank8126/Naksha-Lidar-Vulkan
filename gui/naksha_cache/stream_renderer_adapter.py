"""StreamingRendererAdapter: the minimum seam between the StreamManager and the
GPU. Spec ("Persistent GPU Residency"): "Use existing native point
infrastructure and nkv_set_point_draw_ranges. Add only the minimum persistent-
tile APIs necessary."

EXISTING native API used here:
  * nkv_set_point_cloud(...)        - uploads a contiguous point buffer, used
                                      for the RESIDENT SUBSET ONLY (never the
                                      full dataset)
  * nkv_set_point_draw_ranges(f,c)  - (first,count) sub-ranges of the resident
                                      buffer; camera-only frames call ONLY this
  * nkv_set_render_origin(x,y,z)    - floating origin; camera motion moves the
                                      view WITHOUT re-uploading geometry
  * nkv_get_vram_bytes / vertex_count - VRAM accounting

There is NO native per-tile append API today. When the DLL exports
``nkv_append_point_tile`` (probed at attach) the Vulkan path appends tiles into
a growable GPU buffer. Otherwise it rebuilds the resident buffer once per
STRUCTURAL residency change -- still 0 position reuploads, because camera-only
frames call set_point_draw_ranges only.

A HeadlessTileRenderer records the same ops for acceptance without a GPU.
"""
from __future__ import annotations
import ctypes
import os
import numpy as np


class StreamingRendererAdapter:
    """Minimal contract every renderer adapter implements."""
    def begin_frame(self, gen): ...
    def upload_resident(self, xyz, rgb, cls, inten, ranges): ...
    def set_draw_ranges(self, ranges): ...
    def clear_draw_ranges(self): ...
    def set_render_origin(self, x, y, z): ...
    def set_display_mode(self, mode): ...
    def request_render(self): ...
    def gpu_resident_bytes(self): ...
    def resident_point_count(self): ...
    def has_native_append(self): ...
    # ---- stable-offset arena (see gpu_arena.py / nkv_upload_point_tile) -----
    def supports_arena(self): return False
    def reserve_arena(self, capacity_points): return 0
    def upload_arena_tile(self, first, xyz, cls=None, inten=None, normal=None): return False
    def arena_release(self, first, count): return True
    def stats(self): ...


# The native Instant Shaded Class renderer (NkvDisplayMode 4). It is NOT one of
# DISPLAY_ATTRS on purpose: it draws from the SAME resident point buffer with a
# splat + fullscreen depth-normal lighting pass and needs no CPU-side attribute
# pass, which is exactly what makes it usable while streaming (app.data is
# empty there, and the legacy Delaunay TIN is gated on it).
MODE_SHADED_CLASS_INSTANT = 4
MODE_NEUTRAL = 5
# DEPTH is a real native mode now (NKV_DISPLAY_DEPTH = 6). It was absent from
# this table, which is the direct cause of the production log line
#   [DISPLAY MODE ERROR] unsupported mode='depth'; native mode unchanged
# - the UI and the stream manager both had 'depth', the adapter did not, so the
# previous mode stayed on screen and the viewport silently kept rendering the
# old colours instead of depth.
MODE_DEPTH = 6

_MODE_TO_NKV = {
    "neutral": MODE_NEUTRAL,
    "gray": MODE_NEUTRAL,
    "grey": MODE_NEUTRAL,
    "depth": MODE_DEPTH,
    "elevation": 3, "classification": 1, "intensity": 2, "rgb": 0,
    "shaded_class_instant": MODE_SHADED_CLASS_INSTANT,
    "shaded": MODE_SHADED_CLASS_INSTANT,
    # BUG FIXED: `display_modes.canonical_mode` normalises BOTH "class" and
    # "classification" to the single canonical name "class" - that is the name
    # the stream manager stores and hands to this adapter. The table above only
    # carried "classification", so the canonical name missed it and Class
    # produced, live:
    #     [DISPLAY MODE ERROR] unsupported mode='class'; native mode unchanged
    # i.e. the previous mode stayed on screen and Class never drew. The entry
    # below closes that gap. Both spellings are accepted; neither is silently
    # aliased onto a different mode.
    "class": 1,
}


# Report each unsupported mode name once, never spam the render loop.
_MODE_ERRORS_SEEN = set()


class HeadlessTileRenderer(StreamingRendererAdapter):
    """Records every streaming op for acceptance without a GPU/display.

    Tracks the same invariants the real Vulkan path must satisfy: never
    receives the full dataset; position-only camera frames do not re-upload.
    """
    def __init__(self, total_points=0):
        self.total_points = int(total_points)
        self.instant_shaded_calls = 0
        self.instant_shaded_last = None
        self.uploads = 0
        self.draw_range_calls = 0
        self.position_reuploads = 0
        self.render_origin_calls = 0
        self.display_reuploads = 0
        self.full_point_upload_attempts = 0
        self.resident_points = 0
        self.resident_bytes = 0
        self.uploaded_points = 0
        self.resident_normal_points = 0
        self.normal_uploads = 0
        self.present_count = 0
        self.mode = "rgb"
        self._last_sig = None
        # ---- PHASE 1: attribute-stream + LUT state -------------------------
        # `attribute_uploads` is kept separate from `uploads` on purpose:
        # `uploads` means "the position buffer was re-sent", which a display-mode
        # switch must never do. These are the per-attribute counters.
        self.attribute_uploads = 0
        self.resident_classification_points = 0
        self.resident_intensity_points = 0
        self.lut_pushes = 0
        self.visibility_pushes = 0
        self.last_class_lut = None
        self.last_class_visibility = None
        self.last_elevation_range = None
        self.last_intensity_range = None
        self.last_depth_range = None

    def begin_frame(self, gen):
        pass

    def upload_resident(self, xyz, rgb, cls, inten, ranges):
        n = 0 if xyz is None else int(xyz.shape[0])
        if (n >= self.total_points and self.total_points > 0
                and not getattr(self, "full_upload_allowed", False)):
            self.full_point_upload_attempts += 1
            return {"full_attempt": True, "points": n}
        self.uploads += 1
        self.uploaded_points = n
        self.resident_points = n
        stride = 24 + (3 if rgb is not None else 0) \
                   + (1 if cls is not None else 0) \
                   + (4 if inten is not None else 0)
        self.resident_bytes = n * stride
        return {"ok": True, "points": n, "bytes": self.resident_bytes,
                "ranges": len(ranges) if ranges else 0}

    def upload_resident_normals(self, packed) -> bool:
        """PART 7 (headless twin). Records the call; claims nothing about GPU."""
        if packed is None:
            return False
        arr = np.ascontiguousarray(packed, dtype=np.int16)
        n = int(arr.shape[0])
        if n <= 0:
            return False
        self.normal_uploads += 1
        self.resident_normal_points = n
        return True

    def upload_resident_attributes(self, classification=None, intensity=None):
        """PHASE 1 headless twin. Records the call; claims nothing about GPU.

        Deliberately does NOT touch `self.uploads`, because that counter means
        "the POSITION buffer was re-uploaded". A display-mode switch that only
        adds a class or intensity stream must leave it untouched - that is the
        measured form of "XYZ uploads during switches = 0".
        """
        cls_n = 0 if classification is None else int(np.asarray(classification).size)
        int_n = 0 if intensity is None else int(np.asarray(intensity).size)
        if cls_n <= 0 and int_n <= 0:
            return False
        self.attribute_uploads += 1
        self.resident_classification_points = cls_n
        self.resident_intensity_points = int_n
        return True

    def push_display_state(self, class_lut=None, elevation_lut=None,
                           intensity_lut=None, visibility=None,
                           elevation_range=None, intensity_range=None,
                           size_params=None, sprite_params=None):
        """PHASE 1 headless twin. Records the LUT / range pushes only."""
        self.lut_pushes += 1
        if visibility is not None:
            self.visibility_pushes += 1
        if class_lut is not None:
            self.last_class_lut = np.asarray(class_lut, dtype=np.uint8).copy()
        if elevation_range is not None:
            self.last_elevation_range = tuple(float(v) for v in elevation_range)
        if intensity_range is not None:
            self.last_intensity_range = tuple(float(v) for v in intensity_range)
        return True

    def set_class_lut(self, lut):
        """PHASE 1: what NakshaStreamManager.apply_palette looks for.

        Its ABSENCE was why a PTC load reported pushed_to_renderer=False: the
        manager asks the adapter for this exact name and got None, so the class
        palette was rebuilt in Python and then thrown away.
        """
        arr = np.ascontiguousarray(lut, dtype=np.uint8)
        self.lut_pushes += 1
        self.last_class_lut = arr.copy()
        return True

    def set_class_visibility(self, visibility):
        arr = np.ascontiguousarray(visibility, dtype=np.uint8)
        self.visibility_pushes += 1
        self.last_class_visibility = arr.copy()
        return True

    def set_draw_ranges(self, ranges):
        self.draw_range_calls += 1
        return True

    def clear_draw_ranges(self):
        self.draw_range_calls += 1
        return True

    def set_render_origin(self, x, y, z):
        self.render_origin_calls += 1
        return True

    def set_display_mode(self, mode):
        self.mode = mode
        self.display_reuploads += 1
        return True

    def stage_surface(self, result, dataset_revision, surface_revision):
        self.pending_surface = result
        return True

    def push_instant_shaded(self, azimuth_deg, elevation_deg, ambient,
                            visibility, debug_stage=0,
                            splat_footprint_m=0.15, enabled=True,
                            splat_px=0.0, normal_radius_px=0):
        """Records the Instant Shaded Class handoff for headless acceptance.

        Uniforms only, exactly like the Vulkan adapter - no upload, no mesh.
        Kept so a headless run can assert that selecting the mode moves NO
        residency counter. The signature mirrors VulkanTileRendererAdapter so
        the production call site works against either adapter.
        """
        self.instant_shaded_calls += 1
        self.instant_shaded_last = {
            "azimuth": float(azimuth_deg), "elevation": float(elevation_deg),
            "ambient": float(ambient), "debug_stage": int(debug_stage),
            "splat_footprint_m": float(splat_footprint_m),
            "splat_px": float(splat_px) or None,
            "normal_radius_px": int(normal_radius_px) or None,
            "visible_classes": int(sum(1 for v in visibility if v)) if visibility is not None else 0,
            "enabled": bool(enabled),
        }
        return self.set_display_mode(
            "shaded_class_instant" if enabled else "classification")

    def request_render(self):
        self.present_count += 1

    def gpu_resident_bytes(self):
        return self.resident_bytes

    def resident_point_count(self):
        return self.resident_points

    def has_native_append(self):
        return False

    # ---- stable-offset arena (headless, STRICT) -----------------------------
    # Rejects out-of-range or overlapping writes so a test catches an allocator
    # bug the real GPU would turn into silent corruption.
    def supports_arena(self):
        # ONE kill switch for the whole engine. Before this the headless twin
        # ignored NAKSHA_GPU_ARENA, so an "arena off" A/B run silently still used
        # the arena and could not actually compare the two paths.
        if os.environ.get("NAKSHA_GPU_ARENA", "").strip().lower() in (
                "0", "off", "false", "no"):
            return False
        return bool(getattr(self, "arena_enabled", True))

    def reserve_arena(self, capacity_points):
        cap = int(capacity_points)
        if cap <= 0:
            return 0
        rc = 2 if getattr(self, "arena_capacity", 0) else 1
        if getattr(self, "arena_capacity", 0) >= cap:
            return 1
        self.arena_capacity = cap
        self.arena_tiles = {}                    # first -> {"count", "attrs"}
        self.arena_reserves = getattr(self, "arena_reserves", 0) + 1
        return rc

    def upload_arena_tile(self, first, xyz, cls=None, inten=None, normal=None):
        first = int(first)
        tiles = getattr(self, "arena_tiles", None)
        if tiles is None:
            return False
        n = int(np.asarray(xyz).reshape(-1, 3).shape[0]) if xyz is not None else (
            int(np.asarray(cls).size) if cls is not None else
            int(np.asarray(inten).size) if inten is not None else
            int(np.asarray(normal, dtype=np.int16).nbytes // 4) if normal is not None else 0)
        if n <= 0 or first < 0 or first + n > self.arena_capacity:
            return False
        for k, size in ((c, t["count"]) for c, t in tiles.items()):
            if xyz is not None and not (first + n <= k or k + size <= first) and k != first:
                return False                      # overlaps a live tile
        for name, arr in (("cls", cls), ("inten", inten)):
            if arr is not None and int(np.asarray(arr).size) != n:
                return False                      # short stream against XYZ
        if normal is not None and int(np.asarray(normal, dtype=np.int16).nbytes) != n * 4:
            return False
        rec = tiles.get(first)
        if xyz is None:
            if rec is None or rec["count"] != n:
                return False                      # attributes need resident positions
        else:
            if rec is not None and rec["count"] != n:
                return False
            if rec is None:
                rec = tiles[first] = {"count": n, "attrs": set()}
            self.arena_tile_uploads = getattr(self, "arena_tile_uploads", 0) + 1
            self.arena_xyz_points = getattr(self, "arena_xyz_points", 0) + n
            self.arena_xyz_bytes = getattr(self, "arena_xyz_bytes", 0) + n * 12
        attr_bytes = 0
        if cls is not None:
            rec["attrs"].add("cls"); attr_bytes += n
        if inten is not None:
            rec["attrs"].add("inten"); attr_bytes += n * 4
        if normal is not None:
            rec["attrs"].add("normal"); attr_bytes += n * 4
            # The per-tile normal stream is the SAME event the legacy path
            # reported through `normal_uploads`. Recording it here is what keeps
            # "normals actually reached the GPU" observable on the arena path.
            self.normal_uploads += 1
        self.arena_attr_bytes = getattr(self, "arena_attr_bytes", 0) + attr_bytes
        self.resident_points = sum(t["count"] for t in tiles.values())
        self.resident_normal_points = sum(
            t["count"] for t in tiles.values() if "normal" in t["attrs"])
        self.resident_bytes = sum(t["count"] for t in tiles.values()) * 24
        return True

    def arena_release(self, first, count):
        tiles = getattr(self, "arena_tiles", None)
        if tiles is None:
            return False
        rec = tiles.pop(int(first), None)
        self.arena_releases = getattr(self, "arena_releases", 0) + 1
        self.resident_points = sum(t["count"] for t in tiles.values())
        self.resident_normal_points = sum(
            t["count"] for t in tiles.values() if "normal" in t["attrs"])
        self.resident_bytes = sum(t["count"] for t in tiles.values()) * 24
        return rec is not None

    def stats(self):
        return {
            "uploads": self.uploads, "draw_range_calls": self.draw_range_calls,
            "position_reuploads": self.position_reuploads,
            "render_origin_calls": self.render_origin_calls,
            "display_reuploads": self.display_reuploads,
            "arena_capacity": getattr(self, "arena_capacity", 0),
            "arena_tile_uploads": getattr(self, "arena_tile_uploads", 0),
            "arena_xyz_points": getattr(self, "arena_xyz_points", 0),
            "arena_xyz_bytes": getattr(self, "arena_xyz_bytes", 0),
            "arena_attr_bytes": getattr(self, "arena_attr_bytes", 0),
            "arena_reserves": getattr(self, "arena_reserves", 0),
            "present_count": self.present_count,
            "resident_points": self.resident_points,
            "resident_bytes": self.resident_bytes,
            "resident_normal_points": self.resident_normal_points,
            "normal_uploads": self.normal_uploads,
            # PHASE 1: attribute-stream counters, reported alongside (not
            # inside) `uploads` so a mode switch can be shown to have moved the
            # attribute streams while leaving the position counter alone.
            "attribute_uploads": self.attribute_uploads,
            "resident_classification_points": self.resident_classification_points,
            "resident_intensity_points": self.resident_intensity_points,
            "lut_pushes": self.lut_pushes,
            "visibility_pushes": self.visibility_pushes,
            "full_point_upload_attempts": self.full_point_upload_attempts,
            "mode": self.mode,
        }


class VulkanTileRendererAdapter(StreamingRendererAdapter):
    """Drives the EXISTING naksha_vulkan DLL for streaming."""
    def __init__(self, backend, total_points=0):
        self.be = backend
        self.uploads = 0
        self.draw_range_calls = 0
        self.position_reuploads = 0
        self.render_origin_calls = 0
        self.display_reuploads = 0
        self.full_point_upload_attempts = 0
        self._total_points = int(total_points)
        self.normal_uploads = 0
        self.normal_upload_attempts = 0
        # ---- PHASE 1: attribute-stream + LUT state -------------------------
        # `attribute_uploads` is deliberately separate from `uploads`: `uploads`
        # counts POSITION buffer uploads, which a display-mode switch must never
        # do. These are the class/intensity stream and uniform-table counters.
        self.attribute_uploads = 0
        self.attribute_upload_attempts = 0
        self.resident_classification_points = 0
        self.resident_intensity_points = 0
        self.lut_pushes = 0
        self.visibility_pushes = 0
        self.last_class_lut = None
        self.last_class_visibility = None
        self.last_elevation_range = None
        self.last_intensity_range = None
        self.last_depth_range = None
        dll = getattr(backend, "_dll", None)
        self._has_append = dll is not None and hasattr(dll, "nkv_append_point_tile")

    def begin_frame(self, gen):
        pass

    def upload_resident(self, xyz, rgb, cls, inten, ranges):
        n = 0 if xyz is None else int(xyz.shape[0])
        # The "never upload the whole dataset" guard protects STREAMING_LOD. In
        # FULL_RESIDENT the single full upload IS the design, and refusing it
        # left the GPU holding an earlier partial buffer (697,987 of 2,958,460
        # points on 123.las) while the draw ranges covered every tile - 76% of
        # the cloud silently vanished. The manager sets `full_upload_allowed`.
        if (n >= self._total_points and self._total_points > 0
                and not getattr(self, "full_upload_allowed", False)):
            self.full_point_upload_attempts += 1
            return {"full_attempt": True}
        ok = bool(self.be.set_point_cloud(
            np.ascontiguousarray(xyz, dtype=np.float64)
            if xyz is not None else np.empty((0, 3), dtype=np.float64),
            rgb=np.ascontiguousarray(rgb, dtype=np.uint8) if rgb is not None else None,
            classification=np.ascontiguousarray(cls, dtype=np.uint8)
            if cls is not None else None,
            intensity=np.ascontiguousarray(inten, dtype=np.float32)
            if inten is not None else None))
        self.uploads += 1
        return {"ok": ok, "points": n}

    def upload_resident_normals(self, packed) -> bool:
        """PART 7: upload the packed oct16 stream as R16G16_SNORM, 4 B/point.

        Called only AFTER _flush_gpu has verified normal_count == point_count,
        so the native side can reject the whole stream rather than accept a
        partial one. Bytes go up verbatim - never expanded to float32x3.
        """
        if packed is None:
            return False
        arr = np.ascontiguousarray(packed, dtype=np.int16)
        n = int(arr.shape[0])
        if n <= 0:
            return False
        fn = getattr(self.be, "set_point_normals", None)
        if fn is None:
            self.normal_upload_attempts += 1
            return False
        ok = bool(fn(arr))
        if ok:
            self.normal_uploads += 1
        else:
            self.normal_upload_attempts += 1
        return ok

    def upload_resident_attributes(self, classification=None, intensity=None):
        """PHASE 1: push ONLY the class / intensity streams to the GPU.

        The position buffer is already resident and is NOT re-sent, so this is
        what keeps a Neutral -> Class / Neutral -> Intensity switch at zero XYZ
        uploads. `self.uploads` is deliberately untouched for the same reason.

        Returns False when the loaded DLL predates nkv_set_point_attributes, or
        when the native side refuses the count - the caller then reports the mode
        as PENDING rather than drawing an unwired attribute.
        """
        fn = getattr(self.be, "set_point_attributes", None)
        if fn is None:
            self.attribute_upload_attempts += 1
            return False
        cls_n = 0 if classification is None else int(np.asarray(classification).size)
        int_n = 0 if intensity is None else int(np.asarray(intensity).size)
        if cls_n <= 0 and int_n <= 0:
            return False
        try:
            ok = bool(fn(classification=classification, intensity=intensity))
        except Exception:
            ok = False
        if ok:
            self.attribute_uploads += 1
            if cls_n:
                self.resident_classification_points = cls_n
            if int_n:
                self.resident_intensity_points = int_n
        else:
            self.attribute_upload_attempts += 1
        return ok

    # ---- Phase 3: the streaming hardware profiler reads THESE -------------
    # gui/naksha_cache/hardware.py::_probe_adapter_gpu probes `adapter` for
    # gpu_total_bytes / heap_budget_bytes / device_name. Before these existed it
    # found nothing on ANY class and fell through to "software-fallback-1GiB" on
    # a machine whose Vulkan context had already opened a real 4 GB NVIDIA T400 -
    # so the banner said GPU unknown / 1.00 GB while the engine was rendering on
    # the card. These forward to the LIVE backend: no second probe, no guess.
    def gpu_total_bytes(self) -> int:
        try:
            return int(getattr(self.be, "gpu_total_bytes", lambda: 0)() or 0)
        except Exception:
            return 0

    def heap_budget_bytes(self) -> int:
        try:
            return int(getattr(self.be, "heap_budget_bytes", lambda: 0)() or 0)
        except Exception:
            return 0

    def device_name(self) -> str:
        try:
            fn = getattr(self.be, "get_device_name", None)
            return str(fn() or "") if callable(fn) else ""
        except Exception:
            return ""

    # Aliases hardware.py probes for, so it finds the value whichever name it
    # reaches for first.
    total_vram_bytes = gpu_total_bytes
    vram_bytes = gpu_total_bytes
    memory_budget_bytes = heap_budget_bytes
    gpu_budget_bytes = heap_budget_bytes

    def push_display_state(self, class_lut=None, elevation_lut=None,
                           intensity_lut=None, visibility=None,
                           elevation_range=None, intensity_range=None,
                           size_params=None, sprite_params=None,
                           depth_range=None):
        """PHASE 1 (1C): push the canonical LUT / range / visibility state.

        This is the piece the streaming path was missing entirely. The legacy VTK
        route pushed these through render_backend.sync_point_shading, which
        returns early when `app.data` is empty - i.e. it never ran while
        streaming, so the Vulkan renderer kept its built-in default tables. That
        is why Class rendered one flat colour and Elevation collapsed onto a
        single ramp entry.

        Uniform-only by contract: no buffer, no position upload, safe on every
        mode switch, palette edit and slider tick.
        """
        b = self.be
        ok = True
        if class_lut is not None or elevation_lut is not None or intensity_lut is not None:
            fn = getattr(b, "set_point_luts", None)
            if fn is None:
                return False
            ok = bool(fn(class_lut, elevation_lut, intensity_lut)) and ok
            self.lut_pushes += 1
        if visibility is not None:
            fn = getattr(b, "set_class_visibility", None)
            if fn is not None:
                ok = bool(fn(visibility)) and ok
                self.visibility_pushes += 1
        if elevation_range is not None:
            fn = getattr(b, "set_point_elevation_range", None)
            if fn is not None:
                lo, hi, gamma = elevation_range
                ok = bool(fn(lo, hi, gamma)) and ok
                self.last_elevation_range = (float(lo), float(hi), float(gamma))
        if intensity_range is not None:
            fn = getattr(b, "set_point_intensity_range", None)
            if fn is not None:
                lo, hi, contrast, gamma = intensity_range
                ok = bool(fn(lo, hi, contrast, gamma)) and ok
                self.last_intensity_range = (float(lo), float(hi),
                                             float(contrast), float(gamma))
        if depth_range is not None:
            # DEPTH only. Unlike intensity_range - a fixed property of the data -
            # this range comes from the LIVE CAMERA, so it must be re-pushed
            # whenever the camera moves while Depth is the selected mode. Without
            # it the ramp is normalised against 0-1 metres and the cloud reads as
            # one flat colour.
            fn = getattr(b, "set_point_depth_range", None)
            if fn is not None:
                lo, hi, gamma = depth_range
                ok = bool(fn(lo, hi, gamma)) and ok
                self.last_depth_range = (float(lo), float(hi), float(gamma))
        if size_params is not None:
            fn = getattr(b, "set_point_size_params", None)
            if fn is not None:
                ok = bool(fn(*size_params)) and ok
        if sprite_params is not None:
            fn = getattr(b, "set_point_sprite_params", None)
            if fn is not None:
                ok = bool(fn(*sprite_params)) and ok
        return ok

    def set_class_lut(self, lut):
        """PHASE 1: the exact name NakshaStreamManager.apply_palette probes.

        Its absence is why every PTC load reported pushed_to_renderer=False: the
        manager looked this up with getattr, got None, and skipped the push - so
        the rebuilt palette never reached the GPU and a PTC load changed nothing
        on screen. Now a PTC load is a 768-byte uniform write.
        """
        fn = getattr(self.be, "set_point_luts", None)
        if fn is None:
            return False
        arr = np.ascontiguousarray(lut, dtype=np.uint8)
        ok = bool(fn(arr, None, None))
        if ok:
            self.lut_pushes += 1
            self.last_class_lut = arr.copy()
        return ok

    def set_class_visibility(self, visibility):
        fn = getattr(self.be, "set_class_visibility", None)
        if fn is None:
            return False
        arr = np.ascontiguousarray(visibility, dtype=np.uint8)
        ok = bool(fn(arr))
        if ok:
            self.visibility_pushes += 1
            self.last_class_visibility = arr.copy()
        return ok

    def set_draw_ranges(self, ranges):
        firsts = [r[0] for r in ranges]
        counts = [r[1] for r in ranges]
        self.draw_range_calls += 1
        return bool(self.be.set_point_draw_ranges(firsts, counts))

    def clear_draw_ranges(self):
        return bool(self.be.clear_point_draw_ranges())

    def set_render_origin(self, x, y, z):
        self.render_origin_calls += 1
        try:
            dll = self.be._dll
            return bool(dll.nkv_set_render_origin(
                ctypes.c_uint64(self.be._handle),
                ctypes.c_double(x), ctypes.c_double(y), ctypes.c_double(z)))
        except Exception:
            return False

    def set_display_mode(self, mode):
        """Select the native display mode.

        PART 5: an UNKNOWN mode must never silently become RGB. `_MODE_TO_NKV
        .get(mode, 0)` did exactly that - "neutral" was once absent from the
        table, so an RGB-free LAS was rendered through the colour path and came
        out as multicoloured noise. An unsupported name is now reported once
        and the currently selected native mode is left untouched.
        """
        fn = getattr(self.be, "set_display_mode", None)
        if fn is None:
            return False
        key = str(mode or "").strip().lower()
        surface_control = getattr(self.be, "set_streaming_surface_active", None)
        if key == "surface":
            activate = getattr(self.be, "activate_pending_surface", None)
            if surface_control is None or activate is None:
                return False
            activate()
            return bool(surface_control(True))
        nkv = _MODE_TO_NKV.get(key)
        if nkv is None:
            if key not in _MODE_ERRORS_SEEN:
                _MODE_ERRORS_SEEN.add(key)
                print(f"[DISPLAY MODE ERROR] unsupported mode='{mode}'; "
                      f"native mode unchanged", flush=True)
            return False
        if key == "shaded":
            normal_source = getattr(self.be, "set_instant_normal_source", None)
            if normal_source is None or not normal_source(screen_space=False):
                return False
        ok = bool(fn(nkv))
        if ok and surface_control is not None:
            ok = bool(surface_control(False))
        return ok

    def stage_surface(self, result, dataset_revision, surface_revision):
        backend = self.be
        if not backend.set_dataset_revision(dataset_revision) or not backend.set_surface_revision(surface_revision):
            return False
        return backend.set_surface_indexed_blocks(result.positions, result.indices,
            result.colors, result.counts, dataset_revision, surface_revision)

    def push_instant_shaded(self, azimuth_deg, elevation_deg, ambient,
                            visibility, debug_stage=0,
                            splat_footprint_m=0.15, enabled=True,
                            splat_px=0.0, normal_radius_px=0):
        """Select (or leave) the native INSTANT Shaded Class renderer.

        This is the point of the mode while streaming: it renders from the
        RESIDENT GPU point buffer only, with NO CPU-side attribute pass and no
        mesh. The legacy Delaunay/TIN shaded presentation is TEMPORARILY_GATED
        in streaming because it needs the full in-memory app.data; this one
        does not, so Shaded Class works on a 27M-point cache-backed dataset.

        Uniforms only - lighting push constants, the 256-byte class visibility
        table, the splat band and the normal-reconstruction radius. No geometry,
        no position upload, no vkDeviceWaitIdle: switching is a display-mode
        float.
        """
        from gui import render_backend as _rb
        ok = True
        if hasattr(self.be, "set_instant_shading_parameters"):
            ok = bool(self.be.set_instant_shading_parameters(
                float(azimuth_deg), float(elevation_deg), float(ambient),
                int(debug_stage))) and ok
        if hasattr(self.be, "set_instant_normal_radius"):
            radius = int(normal_radius_px) or _rb.shaded_normal_radius_px()
            ok = bool(self.be.set_instant_normal_radius(radius)) and ok
        if hasattr(self.be, "set_class_visibility"):
            ok = bool(self.be.set_class_visibility(visibility)) and ok
        if hasattr(self.be, "set_point_size_params"):
            # 1..3 px splat band, sized from a world footprint so the surface
            # stays closed at any zoom. class_intensity_mix = 0: Shaded Class
            # colours with the class palette and lights it, matching the legacy
            # TIN which never modulated by intensity.
            px = float(splat_px) or _rb.shaded_splat_px_override()
            if px > 0:
                ok = bool(self.be.set_point_size_params(0.0001, px, px, 0.0)) and ok
            else:
                ok = bool(self.be.set_point_size_params(
                    float(splat_footprint_m), 1.0, 3.0, 0.0)) and ok
        ok = self.set_display_mode(
            "shaded_class_instant" if enabled else "classification") and ok
        self.request_render()
        return ok

    def request_render(self):
        self.be.request_render()

    def gpu_resident_bytes(self):
        try:
            return int(self.be._dll.nkv_get_vram_bytes(
                ctypes.c_uint64(self.be._handle)))
        except Exception:
            return 0

    def resident_point_count(self):
        try:
            return int(self.be._dll.nkv_get_surface_vertex_count(
                ctypes.c_uint64(self.be._handle)))
        except Exception:
            return 0

    def has_native_append(self):
        return bool(self._has_append)

    # ---- stable-offset arena (native nkv_upload_point_tile) -----------------
    def supports_arena(self):
        if os.environ.get("NAKSHA_GPU_ARENA", "").strip().lower() in ("0", "off", "false", "no"):
            return False
        fn = getattr(self.be, "supports_point_arena", None)
        return bool(fn is not None and fn())

    def reserve_arena(self, capacity_points):
        rc = int(self.be.reserve_point_capacity(int(capacity_points)))
        if rc:
            self.arena_reserves = getattr(self, "arena_reserves", 0) + 1
            self.arena_capacity = int(capacity_points)
        return rc

    def upload_arena_tile(self, first, xyz, cls=None, inten=None, normal=None):
        import time as _t
        t0 = _t.perf_counter()
        ok = bool(self.be.upload_point_tile(int(first), xyz, classification=cls,
                                            intensity=inten, normals_oct16=normal))
        ms = (_t.perf_counter() - t0) * 1000.0
        self.arena_upload_ms_total = getattr(self, "arena_upload_ms_total", 0.0) + ms
        self.arena_upload_ms_max = max(getattr(self, "arena_upload_ms_max", 0.0), ms)
        if not ok:
            self.arena_upload_failures = getattr(self, "arena_upload_failures", 0) + 1
            return False
        n = int(np.asarray(xyz).reshape(-1, 3).shape[0]) if xyz is not None else 0
        if xyz is not None:
            self.arena_tile_uploads = getattr(self, "arena_tile_uploads", 0) + 1
            self.arena_xyz_points = getattr(self, "arena_xyz_points", 0) + n
            self.arena_xyz_bytes = getattr(self, "arena_xyz_bytes", 0) + n * 12
        ab = 0
        if cls is not None:
            ab += int(np.asarray(cls).size)
        if inten is not None:
            ab += int(np.asarray(inten).size) * 4
        if normal is not None:
            ab += int(np.asarray(normal, dtype=np.int16).nbytes)
            # Same event the legacy whole-stream path counted, so "normal blocks
            # streamed on demand" stays measurable in the live renderer.
            self.normal_uploads += 1
        self.arena_attr_bytes = getattr(self, "arena_attr_bytes", 0) + ab
        return True

    def arena_release(self, first, count):
        # The native arena needs no free: the slot is simply overwritten by the
        # next tile. Counted so telemetry can show eviction activity.
        self.arena_releases = getattr(self, "arena_releases", 0) + 1
        return True

    def stats(self):
        return {
            "uploads": self.uploads, "draw_range_calls": self.draw_range_calls,
            "position_reuploads": self.position_reuploads,
            "render_origin_calls": self.render_origin_calls,
            "display_reuploads": self.display_reuploads,
            "arena_capacity": getattr(self, "arena_capacity", 0),
            "arena_tile_uploads": getattr(self, "arena_tile_uploads", 0),
            "arena_xyz_points": getattr(self, "arena_xyz_points", 0),
            "arena_xyz_bytes": getattr(self, "arena_xyz_bytes", 0),
            "arena_attr_bytes": getattr(self, "arena_attr_bytes", 0),
            "arena_reserves": getattr(self, "arena_reserves", 0),
            "arena_upload_ms_total": round(getattr(self, "arena_upload_ms_total", 0.0), 3),
            "arena_upload_ms_max": round(getattr(self, "arena_upload_ms_max", 0.0), 3),
            "normal_uploads": self.normal_uploads,
            "normal_upload_attempts": self.normal_upload_attempts,
            # PHASE 1: attribute streams and uniform tables. Reported next to
            # `uploads` (never merged into it) so acceptance can prove a mode
            # switch moved the class/intensity stream while the POSITION counter
            # stayed put.
            "attribute_uploads": self.attribute_uploads,
            "attribute_upload_attempts": self.attribute_upload_attempts,
            "resident_classification_points": self.resident_classification_points,
            "resident_intensity_points": self.resident_intensity_points,
            "lut_pushes": self.lut_pushes,
            "visibility_pushes": self.visibility_pushes,
            "full_point_upload_attempts": self.full_point_upload_attempts,
        }
