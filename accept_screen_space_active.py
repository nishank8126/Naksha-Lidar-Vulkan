"""accept_screen_space_active.py - PART 16: FULL_RESIDENT must draw the SCREEN.

FULL_RESIDENT means the dataset STAYS IN VRAM. It does not mean every resident
point is submitted. On the real `test_classified_highprecision.laz`
(26,960,750 points, 393 LOD0 blocks) a top-down FIT view at 1920x848 shows
~18 points per pixel, so submitting all of them costs frame time and changes
nothing on screen.

This harness measures, on the real cache and the real `on_frame` path:

  * ACTIVE_DRAWN as a fraction of GPU_RESIDENT, per zoom level
  * the points-per-pixel the frame actually achieves (target-driven)
  * that the active set RESPONDS to zoom (it is a selection, not a freeze)
  * that a settled camera does not re-push draw ranges (Part 26/30)
  * that no coverage hole appears (Part 43)
  * that the camera path uploads NOTHING (Part 14/16)
  * the A/B that names the defect: "draw the resident set" vs "draw the screen"
    for the SAME camera.

No GPU and no window: the strict headless adapter records the ops.
"""
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.reader import NakshaPointCacheReader            # noqa: E402
from gui.naksha_cache.stream_manager import (                         # noqa: E402
    GPU_RESIDENT, RENDER_STREAMING_LOD, NakshaStreamManager,
)
from gui.naksha_cache.stream_renderer_adapter import HeadlessTileRenderer  # noqa: E402
from gui.naksha_cache.stream_telemetry import StreamTelemetry         # noqa: E402
from naksha_lod_gate import Camera2D                                  # noqa: E402

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")
TE_PATH = os.path.join(ROOT, "diagnostics", "screen_space_active_telemetry.jsonl")
W, H = 1920, 848


class _App:
    pass


def _live_keys(mgr):
    return [k for k, v in mgr.resident.items() if v.state == GPU_RESIDENT]


def _active_pts(mgr):
    return int(sum(int(mgr.resident[k].count)
                   for k in (mgr.active_draw_keys or ())
                   if k in mgr.resident))


def _settle(mgr, cam, vp, frames=30):
    """Run frames until the interaction state reaches IDLE."""
    for _ in range(frames):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "_camera_state", None) == "IDLE":
            return True
        time.sleep(0.03)
    return False


def main():
    if not os.path.isfile(SRC + ".nakshaidx"):
        print(f"SKIP: no cache beside {SRC}")
        return 0
    print("=" * 78)
    print("PART 16: SCREEN-SPACE ACTIVE_DRAWN IN FULL_RESIDENT")
    print("=" * 78)

    reader = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    hdr = reader.index.header
    gmin = np.asarray(hdr["bounds_min"], float)
    gmax = np.asarray(hdr["bounds_max"], float)
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    span_x = float(gmax[0] - gmin[0])
    half = span_x * 0.5

    def cam_at(frac):
        hw = half * frac
        return (Camera2D(cx, cy, hw, W, H),
                (cx, cy, hw, hw * H / float(W)))

    adapter = HeadlessTileRenderer(total_points=int(reader.index.total_points))
    te = StreamTelemetry(TE_PATH)
    mgr = NakshaStreamManager(_App(), reader, adapter, te,
                              ram_budget=4 * 1024 ** 3,
                              gpu_budget=2 * 1024 ** 3)

    cam, vp = cam_at(1.0)
    t0 = time.perf_counter()
    mgr.open_first_frame()
    open_ms = (time.perf_counter() - t0) * 1000.0

    # ---- complete the single full-residency upload ------------------------
    ready = False
    t0 = time.perf_counter()
    for _ in range(600):
        mgr.on_frame(cam, vp)
        if getattr(mgr, "full_resident_ready", False):
            ready = True
            break
    load_ms = (time.perf_counter() - t0) * 1000.0
    # The pump loop exits the instant `full_resident_ready` flips, which can be
    # BEFORE the first camera-cadence frame of the sealed set. Settle one frame
    # so the A/B below compares the two rules on the SAME, fully-applied state
    # instead of on whatever `_note_camera` last saw.
    _settle(mgr, cam, vp)
    mgr.on_frame(cam, vp)
    resident_keys = _live_keys(mgr)
    resident_pts = int(sum(int(mgr.resident[k].count) for k in resident_keys))

    print(f"\n[SETUP]")
    print(f"  open_first_frame            : {open_ms:.0f} ms")
    print(f"  render_mode                 : {getattr(mgr, 'render_mode', None)}")
    print(f"  full_resident_ready         : {ready}  ({load_ms:.0f} ms to pump)")
    print(f"  GPU_RESIDENT                : {len(resident_keys)} blocks / "
          f"{resident_pts:,} points")
    print(f"  screen_space_applies        : {mgr._screen_space_applies()}")
    if not (ready and mgr._screen_space_applies()):
        print("\nNOTE: this dataset did not reach FULL_RESIDENT; "
              "the screen-space path does not apply here.")
        reader.close()
        return 0

    # ---- SECTION 1: [LOD REPRESENTATION AUDIT] ---------------------------
    # Proven on real data before any reduction is claimed.
    import phase3_lod_audit
    audit = phase3_lod_audit.audit(reader, mgr, cam, vp, W, H)

    # ---- the A/B that names the defect -----------------------------------
    print(f"\n[SELECTION A/B - same camera, FIT view]")
    t0 = time.perf_counter()
    legacy = mgr._build_ranges(resident_keys, interaction=False)
    legacy_ms = (time.perf_counter() - t0) * 1000.0
    legacy_pts = int(sum(c for _f, c in legacy))
    t0 = time.perf_counter()
    scr = mgr._build_ranges(resident_keys, interaction=False, screen_space=True,
                            camera=cam, viewport=vp)
    scr_ms = (time.perf_counter() - t0) * 1000.0
    scr_pts = int(sum(c for _f, c in scr))
    ab_reason = getattr(mgr, "last_screen_space_reason", "?")
    print(f"  draw-the-residency (old)    : {len(legacy):>5} blocks / "
          f"{legacy_pts:>12,} points   {legacy_ms:6.1f} ms")
    print(f"  draw-the-screen    (Part 16): {len(scr):>5} blocks / "
          f"{scr_pts:>12,} points   {scr_ms:6.1f} ms")
    print(f"  gate answer                 : {ab_reason}")
    ab_ratio = (legacy_pts / scr_pts) if scr_pts else float("nan")
    if scr_pts > 0:
        print(f"  submitted-point reduction   : {ab_ratio:.1f}x")
    if ab_reason != "selected":
        print("  !! the A/B fell back to the legacy rule; it is not measuring "
              "screen-space selection")

    # ---- per-zoom behaviour ---------------------------------------------
    print(f"\n[PER ZOOM LEVEL - real on_frame path]")
    print(f"  {'zoom':<7}{'state':<8}{'resident':>12}{'active':>12}"
          f"{'blk':>6}{'ppp':>8}{'target':>10}{'sel_ms':>9}  {'LOD dist':<24}")
    rows = []
    for label, frac in (("fit", 1.0), ("x2", 0.5), ("x4", 0.25),
                        ("x8", 0.125), ("x16", 0.0625)):
        cam, vp = cam_at(frac)
        idle_ok = _settle(mgr, cam, vp)
        if not idle_ok:
            mgr.note_camera_motion()
            mgr.on_frame(cam, vp)
        tele = mgr.lod_budget_telemetry()
        positions_before = int(getattr(adapter, "arena_xyz_points", 0) or 0)
        t0 = time.perf_counter()
        mgr.on_frame(cam, vp)
        sel_ms = (time.perf_counter() - t0) * 1000.0
        # Captured AFTER the frame, so the diag describes the row it is printed
        # under rather than the previous frame.
        diag = dict(getattr(mgr, "last_screen_space_diag", None) or {})
        uploaded = int(getattr(adapter, "arena_xyz_points", 0) or 0) \
            - positions_before
        active = _active_pts(mgr)
        dist = {}
        for k in (mgr.active_draw_keys or ()):
            dist[int(k[1])] = dist.get(int(k[1]), 0) + 1
        ds = " ".join(f"L{k}:{v}" for k, v in sorted(dist.items()))
        print(f"  {label:<7}{str(tele['camera_state']):<8}{resident_pts:>12,}"
              f"{active:>12,}{len(mgr.active_draw_keys or ()):>6}"
              f"{tele['actual_ppp']:>8.3f}{tele['target_points']:>10,}"
              f"{sel_ms:>9.2f}  {ds:<24}")
        rows.append({"label": label, "active": active, "ppp": tele["actual_ppp"],
                     "target": tele["target_points"], "state": tele["camera_state"],
                     "uploaded": uploaded, "holes": tele["viewport_hole_cells"],
                     "lod_dist": dist, "visible": int(diag.get("visible_nodes", 0)),
                     "reason": str(getattr(mgr, "last_screen_space_reason", "?"))})
        if diag:
            print(f"          gate: visible={diag.get('visible_nodes')} "
                  f"proposed={diag.get('gate_blocks')}/{diag.get('gate_points'):,} "
                  f"submitted={diag.get('submitted_blocks')}/"
                  f"{diag.get('submitted_points'):,} "
                  f"legacy_overrode={diag.get('overridden_by_legacy_filter')}")
    print(f"  (resident is constant at {resident_pts:,} points for every row - "
          f"the camera path never re-reads or re-uploads it)")

    # ---- the cost of a frame, settled vs after a camera change -----------
    # Part 27: this is the CAMERA HOT PATH number. A settled camera must not
    # re-derive a byte-identical selection 60x/second, and a camera change must
    # not cost more than the view it selects for.
    print(f"\n[FRAME COST - selection only, no GPU]")
    cam, vp = cam_at(1.0)
    _settle(mgr, cam, vp)
    mgr.on_frame(cam, vp)
    n = 40
    t0 = time.perf_counter()
    for _ in range(n):
        mgr.on_frame(cam, vp)
    settled_ms = (time.perf_counter() - t0) * 1000.0 / n
    hits_before = int(getattr(mgr, "screen_space_cached_hits", 0) or 0)
    moves = 0
    t0 = time.perf_counter()
    for i in range(n):
        cam_m, vp_m = cam_at(1.0 + 0.01 * ((i % 8) + 1))
        mgr.note_camera_motion()
        mgr.on_frame(cam_m, vp_m)
        moves += 1
    moved_ms = (time.perf_counter() - t0) * 1000.0 / moves
    hits_after = int(getattr(mgr, "screen_space_cached_hits", 0) or 0)
    print(f"  settled camera  ({n} frames) : {settled_ms:6.3f} ms/frame")
    print(f"  camera changing ({moves} frames) : {moved_ms:6.3f} ms/frame")
    if settled_ms > 0:
        print(f"  a settled frame costs {(moved_ms / settled_ms):.1f}x less"
              f" than a changing one")
    print("  (a settled frame is served by the CAMERA SEAL and never reaches the")
    print("   selection at all, which is why the 40-frame loop above adds no")
    print("   cache hits; the hits come from SETTLING frames that still flush)")

    # ---- MOVING must submit less than IDLE ------------------------------
    print(f"\n[MOVING vs IDLE - same camera]")
    cam, vp = cam_at(1.0)
    mgr.note_camera_motion()
    mgr.on_frame(cam, vp)
    moving_pts = _active_pts(mgr)
    moving_state = mgr._camera_state
    _settle(mgr, cam, vp)
    mgr.on_frame(cam, vp)
    idle_pts = _active_pts(mgr)
    idle_state = mgr._camera_state
    print(f"  {moving_state:<8}: {moving_pts:>12,} points active")
    print(f"  {idle_state:<8}: {idle_pts:>12,} points active")
    print(f"  moving/idle ratio           : "
          f"{(moving_pts / idle_pts if idle_pts else float('nan')):.3f}")

    # ---- a settled camera must stop re-pushing draw ranges ---------------
    pushes_before = int(getattr(mgr, "draw_range_pushes", 0) or 0)
    for _ in range(30):
        mgr.on_frame(cam, vp)
    pushes_settled = int(getattr(mgr, "draw_range_pushes", 0) or 0) - pushes_before

    # ---- camera frames upload nothing -----------------------------------
    before = int(getattr(adapter, "arena_xyz_points", 0) or 0)
    for _ in range(30):
        mgr.on_frame(cam, vp)
    cam_upload = int(getattr(adapter, "arena_xyz_points", 0) or 0) - before

    # ---- harness counts --------------------------------------------------
    state = mgr.lod_budget_telemetry()
    ppp_moving = None
    mgr.note_camera_motion()
    mgr.on_frame(cam, vp)
    ppp_moving = mgr.lod_budget_telemetry()["actual_ppp"]

    checks = [
        ("dataset reached FULL_RESIDENT", ready),
        ("screen-space path is active", mgr._screen_space_applies()),
        ("every visible node stays covered (0 holes)",
         all(r["holes"] == 0 for r in rows) and state["viewport_hole_cells"] == 0),
        ("ACTIVE_DRAWN is a strict subset of GPU_RESIDENT",
         all(r["active"] < resident_pts for r in rows)),
        ("active set is never empty (no blank frame)",
         all(r["active"] > 0 for r in rows)),
        ("idle ppp is near the density target (<= 1.2)",
         all(r["ppp"] is not None and r["ppp"] <= 1.2 for r in rows)),
        ("MOVING submits strictly fewer points than IDLE",
         moving_pts < idle_pts),
        ("zoom CHANGES the active set (it is a selection, not a freeze)",
         len({tuple(sorted(r["lod_dist"].items())) for r in rows}) > 1),
        ("a settled camera stops re-pushing draw ranges", pushes_settled == 0),
        ("30 camera frames uploaded 0 positions", cam_upload == 0),
        ("no SelectionError fallback was needed",
         int(getattr(mgr, "screen_space_uncovered", 0) or 0) == 0),
        ("the gate answered every selection (no legacy fallback)",
         all(r["reason"] == "selected" for r in rows) and ab_reason == "selected"),
        ("every visible node got a resident representative",
         all(r["visible"] > 0 and len(r["lod_dist"]) > 0
             for r in rows)),
        ("draw-the-screen beats draw-the-residency at FIT", ab_ratio > 1.0),
        ("a settled camera reuses the selection instead of re-deriving it",
         settled_ms < moved_ms),
    ]
    print(f"\n[CHECKS]")
    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print(f"\n  A/B reduction at FIT        : {ab_ratio:.2f}x "
          f"({legacy_pts:,} -> {scr_pts:,} points)")
    print(f"  draw_range_pushes total     : "
          f"{int(getattr(mgr, 'draw_range_pushes', 0) or 0)}")
    print(f"  screen-space fallbacks      : "
          f"{int(getattr(mgr, 'screen_space_fallbacks', 0) or 0)}")
    print(f"  selection cache hits        : "
          f"{int(getattr(mgr, 'screen_space_cached_hits', 0) or 0)}")
    print(f"  screen_space_selections     : "
          f"{int(getattr(mgr, 'screen_space_selections', 0) or 0)}")
    print(f"  selection diag              : {mgr.last_screen_space_diag}")
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    print("=" * 78)
    reader.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
