"""accept_incremental_upload.py - PROOF that a residency change is INCREMENTAL.

The mission's Part 14 requirement, stated as something that can actually be
measured:

    one block becomes resident  ->  ONE block is uploaded
    existing points re-uploaded ->  0

and its counterpart, Part 11/12: a tile's GPU offset must not move because
another tile arrived.

This harness runs the REAL cache through the REAL `_flush_gpu` /
`_reconcile` decision path with the STRICT headless adapter (which rejects
out-of-range writes, overlaps and short attribute streams, so an allocator bug
that the GPU would turn into silent corruption fails loudly here).

It then runs the SAME scenario a second time with the stable-slot path
disabled (`NAKSHA_GPU_ARENA=0`), which restores the whole-buffer
`np.concatenate -> upload_resident` behaviour, and reports the two totals side
by side. The comparison is the point: the arena path's total is O(points that
actually became resident); the whole-buffer path's is O(sum of all resident
points, recomputed on every change).

Nothing here is a claim about frame rate. It measures BYTES MOVED and SLOT
STABILITY, which is what this change is about.
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader          # noqa: E402
from gui.naksha_cache.stream_manager import (                       # noqa: E402
    GPU_RESIDENT, RENDER_STREAMING_LOD, NakshaStreamManager,
)
from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer  # noqa: E402
from gui.naksha_cache.stream_telemetry import StreamTelemetry       # noqa: E402
from naksha_lod_gate import Camera2D                                # noqa: E402

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
TE_PATH = os.path.join(ROOT, "diagnostics", "incremental_upload_telemetry.jsonl")


class _App:
    """The manager only ever reaches for these through getattr."""


def _scenario(mgr, cam, viewport, label, steps=8):
    """Drive one zoom-in ramp, accumulating what actually crossed to the GPU."""
    cam_pts = 0
    legacy_pts = 0
    flushes = 0
    max_ms = 0.0
    slot_snapshots = []
    seen_slots_moved = 0
    uploads_by_flush = []

    # POSITIONS the legacy path re-sends, accumulated by wrapping the one call
    # that carries them. On the stable-slot path this stays 0.
    original_upload_resident = mgr.adapter.upload_resident

    def counting_upload_resident(xyz, rgb, cls, inten, ranges):
        nonlocal legacy_pts
        legacy_pts += 0 if xyz is None else int(np.asarray(xyz).shape[0])
        return original_upload_resident(xyz, rgb, cls, inten, ranges)

    mgr.adapter.upload_resident = counting_upload_resident

    # A slot that changes while the tile stays resident is a REAL defect, but a
    # tile that was genuinely evicted and re-admitted legitimately lands in a new
    # slot. The two are only distinguishable by asking the allocator which tiles
    # it released during the frame, so that is what is recorded.
    released = set()
    original_release = mgr._arena_release_slot

    def recording_release(v):
        released.add((int(v.node_id), int(v.lod)))
        return original_release(v)

    mgr._arena_release_slot = recording_release
    arena_pts_before = int(getattr(mgr.adapter, "arena_xyz_points", 0) or 0)
    tiles_before = int(getattr(mgr.adapter, "arena_tile_uploads", 0) or 0)

    span_x = viewport[2] * 2.0
    for i in range(steps):
        # Only tiles that ALREADY hold a slot can have one "move". A tile going
        # from no-slot to slotted is an allocation, not a relocation.
        prev = {k: v.first for k, v in mgr.resident.items()
                if v.state == GPU_RESIDENT and int(v.first) >= 0}
        cam.half_w = span_x * 0.5 / (1.0 + 0.7 * i)
        t0 = time.perf_counter()
        mgr.on_frame(cam, viewport)
        ms = (time.perf_counter() - t0) * 1000.0
        max_ms = max(max_ms, ms)
        flushes += 1
        arena_pts = int(getattr(mgr.adapter, "arena_xyz_points", 0) or 0)
        cam_pts = arena_pts
        uploads_by_flush.append(
            int(getattr(mgr.adapter, "arena_tile_uploads", 0) or 0) - tiles_before)
        tiles_before = int(getattr(mgr.adapter, "arena_tile_uploads", 0) or 0)
        # A slot must never move while the tile stayed resident. A tile the
        # allocator RELEASED this frame was genuinely evicted, so a new slot for
        # it is correct, not a violation.
        for k, slot in prev.items():
            v = mgr.resident.get(k)
            if (v is not None and v.state == GPU_RESIDENT
                    and v.first != slot and k not in released):
                seen_slots_moved += 1
        released.clear()
        slot_snapshots.append(len(prev))

    # ---- camera-only frames must upload NOTHING --------------------------
    arena_before_cam = int(getattr(mgr.adapter, "arena_xyz_points", 0) or 0)
    legacy_before_cam = legacy_pts
    for _ in range(15):
        mgr.on_frame(cam, viewport)      # same camera: pure camera frames
    cam_only_arena = int(getattr(mgr.adapter, "arena_xyz_points", 0) or 0) \
        - arena_before_cam
    cam_only_legacy = legacy_pts - legacy_before_cam

    new_resident_points = cam_pts - arena_pts_before
    return {
        "label": label,
        "flushes": flushes,
        "max_flush_ms": max_ms,
        "arena_points_sent": cam_pts,
        "arena_new_points": new_resident_points,
        "legacy_points_sent": legacy_pts,
        "uploads_per_flush": uploads_by_flush,
        "slots_moved": seen_slots_moved,
        "camera_only_arena_points": cam_only_arena,
        "camera_only_legacy_points": cam_only_legacy,
        "whole_buffer_rebuilds": int(getattr(mgr, "whole_buffer_rebuilds", 0) or 0),
        "existing_reuploaded": int(
            getattr(mgr, "existing_points_reuploaded", 0) or 0),
        "new_blocks_uploaded": int(getattr(mgr, "new_blocks_uploaded", 0) or 0),
        "new_points_uploaded": int(getattr(mgr, "new_points_uploaded", 0) or 0),
        "arena": getattr(mgr, "last_arena_telemetry", {}),
        "active_vs_resident": mgr.active_draw_report(),
    }


def run(label):
    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    span_x = float(gmax[0] - gmin[0])
    W, H = 1920, 848
    cam = Camera2D(cx, cy, span_x * 0.5, W, H)
    viewport = (cx, cy, span_x * 0.5, span_x * 0.5 * (H / float(W)))

    adapter = HeadlessTileRenderer(total_points=int(reader.index.total_points))
    te = StreamTelemetry(TE_PATH)
    mgr = NakshaStreamManager(_App(), reader, adapter, te,
                              ram_budget=2 * 1024 ** 3,
                              gpu_budget=1200 * 1024 ** 2)
    mgr.render_mode = RENDER_STREAMING_LOD
    mgr.display_mode = "neutral"
    t0 = time.perf_counter()
    mgr.open_first_frame()
    open_ms = (time.perf_counter() - t0) * 1000.0
    res = _scenario(mgr, cam, viewport, label)
    res["open_ms"] = open_ms
    res["arena_active"] = bool(getattr(mgr, "arena", None) is not None)
    res["arena_capacity"] = int(getattr(mgr, "_arena_capacity", 0) or 0)
    res["resident_blocks"] = sum(
        1 for v in mgr.resident.values() if v.state == GPU_RESIDENT)
    res["stable_slots_disjoint"] = _slots_disjoint(mgr)
    reader.close()
    return res


def _slots_disjoint(mgr):
    runs = sorted((int(v.first), int(v.count)) for v in mgr.resident.values()
                  if v.state == GPU_RESIDENT and int(v.first) >= 0)
    for (f1, c1), (f2, _c2) in zip(runs, runs[1:]):
        if f1 + c1 > f2:
            return False
    return True


def main():
    if not os.path.isfile(SRC + ".nakshaidx"):
        print(f"SKIP: no cache beside {SRC}")
        return 0
    print("=" * 74)
    print("INCREMENTAL GPU UPLOAD - stable slots vs whole-buffer repack")
    print("=" * 74)

    on = run("stable-slot arena")
    os.environ["NAKSHA_GPU_ARENA"] = "0"
    off = run("whole-buffer repack")

    for r in (on, off):
        print(f"\n[{r['label']}]")
        print(f"  arena active                  : {r['arena_active']}")
        print(f"  arena capacity                : {r['arena_capacity']:,} points")
        print(f"  open first frame              : {r['open_ms']:.0f} ms")
        print(f"  frames flushed                : {r['flushes']}")
        print(f"  resident blocks (final)       : {r['resident_blocks']}")
        print(f"  whole-buffer rebuilds         : {r['whole_buffer_rebuilds']}")
        print(f"  existing points re-uploaded   : {r['existing_reuploaded']:,}")
        print(f"  new blocks uploaded           : {r['new_blocks_uploaded']:,}")
        print(f"  new points uploaded           : {r['new_points_uploaded']:,}")
        print(f"  POSITION points written       : "
              f"{r['arena_points_sent'] or r['legacy_points_sent']:,}"
              f"   (arena {r['arena_points_sent']:,} | "
              f"whole-buffer {r['legacy_points_sent']:,})")
        print(f"  uploads per flush             : {r['uploads_per_flush']}")
        print(f"  resident slots that MOVED     : {r['slots_moved']}")
        print(f"  resident slots disjoint       : {r['stable_slots_disjoint']}")
        print(f"  15 camera-only frames         : arena +"
              f"{r['camera_only_arena_points']:,} pts | whole-buffer +"
              f"{r['camera_only_legacy_points']:,} pts")
        av = r["active_vs_resident"]
        print(f"  ACTIVE vs RESIDENT            : "
              f"{av['active_blocks']} blk / {av['active_points']:,} pts  vs  "
              f"{av['resident_blocks']} blk / {av['resident_points']:,} pts")

    checks = [
        ("stable-slot path was used", on["arena_active"]),
        ("arena capacity derived from the budget",
         on["arena_capacity"] > 0),
        ("zero whole-buffer rebuilds on the arena path",
         on["whole_buffer_rebuilds"] == 0),
        ("zero existing-point reuploads on the arena path",
         on["existing_reuploaded"] == 0),
        ("zero whole-buffer position sends on the arena path",
         on["legacy_points_sent"] == 0),
        ("no resident slot moved while resident",
         on["slots_moved"] == 0),
        ("resident slots are disjoint",
         on["stable_slots_disjoint"]),
        ("15 camera-only frames uploaded 0 points (arena)",
         on["camera_only_arena_points"] == 0),
        ("15 camera-only frames uploaded 0 points (legacy)",
         off["camera_only_legacy_points"] == 0),
        ("ACTIVE_DRAWN is a strict subset of GPU_RESIDENT",
         on["active_vs_resident"]["active_points"]
         < on["active_vs_resident"]["resident_points"]),
        ("the stable-slot path moves FAR fewer bytes than the repack",
         off["legacy_points_sent"] > on["arena_points_sent"]),
    ]
    print("\n" + "-" * 74)
    print("[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    if off["legacy_points_sent"] > 0 and on["arena_points_sent"] > 0:
        ratio = off["legacy_points_sent"] / float(on["arena_points_sent"])
        print(f"\n  position bytes moved: whole-buffer is {ratio:.2f}x the "
              f"stable-slot total for this scenario")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 74)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
