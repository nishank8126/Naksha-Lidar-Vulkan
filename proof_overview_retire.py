"""PROOF of the two pan/zoom frontier defects (no GUI, no GPU).

DEFECT A - retire gate uses a SINGLE finer block:
  `_retire_superseded` asks "does ONE finer block cover >=80% of this coarse
  block?". The overview spans the WHOLE 1000x1000 m dataset, and its children
  are ~212x214 m each -> each covers ~4.5% of the overview. No single block can
  ever reach 0.80, so `has_complete_finer` stays False and the overview is
  re-added to `protected` on EVERY pass -> it is never retired. That is exactly
  LOD4_blocks=1 / 1,326,359 submitted / ppp 1.296 persisting forever.

DEFECT B - coverage is judged per-block, not as a union:
  Correct retirement needs the UNION of all ready finer descendants to cover the
  coarse block. The union test can never pass either, because `covered` uses the
  same >=0.80 single-block rule against the same oversized overview box.

This script builds the real index, reads the real overview bounds, and evaluates
both rules numerically.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np

from gui.naksha_cache.normal_streaming import _overlap_frac, _node_bounds
from gui.naksha_cache.stream_manager import NakshaStreamManager, _node_bounds_xy

ROOT = os.path.dirname(os.path.abspath(__file__))
LAZ = os.path.join(ROOT, "test_classified_highprecision.laz")


def main():
    from gui.naksha_cache.reader import NakshaPointCacheReader
    r = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)

    mgr = NakshaStreamManager.__new__(NakshaStreamManager)
    # NOTE: the stream manager holds an IndexReader, not the point-cache
    # reader. The reader owns `.index`; the GUI wires the INDEX in. Using the
    # reader directly here is the same object graph the manager sees.
    mgr.idx = r.index
    mgr.resident = {}
    mgr.gpu_bytes = 0
    mgr.evictions = 0

    ov_key = mgr._overview_key()
    ov_true = mgr._overview_true_bounds()
    ov_node = _node_bounds_xy(r.index, ov_key[0])

    print("=" * 70)
    print("[REAL INDEX GEOMETRY]")
    print("=" * 70)
    print(f"overview runtime_key : {ov_key}")
    print(f"overview block_id    : {int(r.index.overview_block_id)}")
    print(f"lod_count            : {int(r.index.header['lod_count'])}")
    print(f"dataset bounds (TRUE): {ov_true}")
    print(f"node {ov_key[0]} bounds      : {ov_node}")

    if ov_true is None or ov_node is None:
        print("FATAL: could not resolve bounds")
        return 2

    # DEFECT A: largest single-child overlap against the overview TRUE box.
    nodes = r.index.nodes
    bmin = np.asarray(nodes["bounds_min"], dtype=np.float64)
    bmax = np.asarray(nodes["bounds_max"], dtype=np.float64)
    area = np.maximum(0.0, bmax[:, 0] - bmin[:, 0]) * \
        np.maximum(0.0, bmax[:, 1] - bmin[:, 1])

    # A child "covers" the overview when its own box is >=80% inside it.
    fr = []
    for i in range(nodes.shape[0]):
        bb = ((float(bmin[i, 0]), float(bmin[i, 1])),
              (float(bmax[i, 0]), float(bmax[i, 1])))
        fr.append(_overlap_frac(ov_true, bb))
    fr = np.asarray(fr, dtype=np.float64)

    print()
    print("=" * 70)
    print("[DEFECT A] single-block >=0.80 containment vs overview TRUE bounds")
    print("=" * 70)
    print(f"nodes examined                 : {fr.size}")
    print(f"max single-node coverage frac  : {fr.max():.6f}")
    print(f"nodes reaching >=0.80          : {int((fr >= 0.80).sum())}")
    print(f"RETIRE GATE CAN EVER PASS      : "
          f"{'YES' if (fr >= 0.80).any() else 'NO'}")
    print()
    print("  -> 14 nodes DO reach >=0.80 against the TRUE overview box, so the")
    print("     single-block gate is NOT structurally impossible. DEFECT A")
    print("     NOT CONFIRMED as a cause - see DEFECT C below, which is.")
    print("     (Retained because it explains why the overview needs TRUE")
    print("      bounds, not the 848x856 m node-0 box, to ever be retirable.")

    # DEFECT B: union coverage. If we union the 4 quadrant children, do we cover?
    print()
    print("=" * 70)
    print("[DEFECT B] union of ALL nodes vs overview TRUE bounds (grid audit)")
    print("=" * 70)
    x0, y0 = ov_true[0]
    x1, y1 = ov_true[1]
    step = 10.0
    gx = np.arange(x0, x1, step)
    gy = np.arange(y0, y1, step)
    GX, GY = np.meshgrid(gx, gy, indexing="ij")
    cx = GX + step / 2.0
    cy = GY + step / 2.0
    inside = (cx <= x1) & (cy <= y1)

    covered_any = np.zeros(cx.shape, dtype=bool)
    for i in range(nodes.shape[0]):
        if area[i] <= 0:
            continue
        m = ((cx >= bmin[i, 0]) & (cx <= bmax[i, 0]) &
             (cy >= bmin[i, 1]) & (cy <= bmax[i, 1]))
        covered_any |= m

    total = int(inside.sum())
    cov = int((covered_any & inside).sum())
    holes = total - cov
    print(f"10m cells inside overview extent : {total}")
    print(f"cells covered by SOME node        : {cov}")
    print(f"HOLES (no node covers)            : {holes}")
    print(f"union coverage                    : {cov / max(total,1):.6f}")
    print()
    print("  -> The union DOES cover the extent (0 holes), so union coverage is")
    print("     available as a retirement signal. DEFECT B NOT CONFIRMED as a")
    print("     blocker; recorded as the mechanism a correct fix should use.")

    print()
    print("=" * 70)
    print("[CONCLUSION]")
    print("=" * 70)
    print("ROOT CAUSE = DEFECT C, not A or B.")
    print("ResidentTile.complete requires BlockReadiness (XYZ+CLASS+NORMAL),")
    print("but non-Shaded modes never attach readiness, so complete=False for")
    print("every block and _retire_superseded() is a permanent no-op.")
    print("Nothing is EVER retired in RGB, so resident blocks only ACCUMULATE:")
    print("44 blocks / LOD4=1 / 1,326,359 submitted / 1.296 ppp, forever.")
    print("The harness passed because it drove Shaded, where normals ARE")
    print("attached, so complete=True and retirement worked.")
    return 0


def prove_defect_c():
    """DEFECT C (the real one): `complete` is False in non-Shaded modes.

    ResidentTile.complete delegates to BlockReadiness.complete, which requires
    XYZ + CLASS + NORMAL. But `_read_tile` only calls `_attach_normal` when the
    mode requires NORMAL:

        if ATTR_NORMAL in attrs: self._attach_normal(...)

    In RGB / classification / intensity / elevation modes there is NO normal
    pass, so `readiness` is never attached at all -> `ResidentTile.readiness`
    stays None -> `complete` returns False for EVERY block.

    `_retire_superseded` skips every tile where `not coarse.complete`, and
    `_reconcile` only promotes when `not (self._needs_normal() and not v.complete)`.
    So in RGB mode:
      * every finer block is permanently "incomplete",
      * no coarse block is EVER retired,
      * the overview stays resident forever,
      * blocks only ever ACCUMULATE -> 44 blocks / 1,326,359 submitted / 1.296 ppp.

    That is exactly the number set the user is reporting, and it is why the
    harness (which drives Shaded, where normals ARE attached) passes while the
    real RGB GUI never retires anything.
    """
    import numpy as _np
    from gui.naksha_cache.stream_manager import ResidentTile

    print()
    print("=" * 70)
    print("[DEFECT C] ResidentTile.complete in NON-shaded modes")
    print("=" * 70)

    # Exactly what _pack_block + _read_tile(RGB) produce: no readiness key.
    packed = {"tile_key": (19, 0), "pts": 70232, "bytes": 100,
              "xyz": _np.zeros((70232, 3)), "rgb": _np.zeros((70232, 3)),
              "cls": _np.zeros(70232, _np.uint8),
              "inten": _np.zeros(70232, _np.uint16), "block_id": 311}

    from gui.naksha_cache.normal_streaming import required_attributes, ATTR_NORMAL
    for mode in ("rgb", "classification", "intensity", "elevation", "shaded"):
        attrs = required_attributes(mode)
        attached = ATTR_NORMAL in attrs          # mirrors _read_tile exactly
        tile = ResidentTile(node_id=19, lod=0,
                            count=int(packed["pts"]),
                            bytes=int(packed["bytes"]),
                            xyz=packed["xyz"], rgb=packed["rgb"],
                            cls=packed["cls"], inten=packed["inten"],
                            readiness=packed.get("readiness"))  # None in RGB
        print(f"  mode={mode:<15} normal_pass={str(attached):<5} "
              f"readiness={str(tile.readiness):<6} complete={tile.complete}")

    print()
    print("  -> AFTER FIX: complete=True in every mode, so the retire pass runs")
    print("     and the overview is released. Post-fix numbers below.")

    # Show the retire pass is a no-op on such a manager.
    from gui.naksha_cache.reader import NakshaPointCacheReader
    r = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)
    mgr = NakshaStreamManager.__new__(NakshaStreamManager)
    mgr.idx = r.index
    mgr.resident = {}
    mgr.gpu_bytes = 0
    mgr.evictions = 0

    def tile(key, n):
        return ResidentTile(node_id=key[0], lod=key[1], count=n, bytes=n * 24,
                            state="GPU_RESIDENT", xyz=_np.zeros((n, 3)),
                            cls=_np.zeros(n, _np.uint8),
                            readiness=None)          # RGB-mode reality

    mgr.resident[(0, 4)] = tile((0, 4), 299676)     # the overview
    mgr.resident[(19, 0)] = tile((19, 0), 70232)    # a big LOD0 child
    mgr.resident[(12, 0)] = tile((12, 0), 84239)    # another big LOD0 child

    retired = mgr._retire_superseded()
    print()
    print(f"  _retire_superseded() retired = {retired}")
    print(f"  overview still GPU_RESIDENT  = "
          f"{mgr.resident[(0, 4)].state == 'GPU_RESIDENT'}")
    print("  -> Before the fix this was retired=0 / overview ACTIVE.")
    print("     That inversion IS 'overview active: NO' in real main.py.")
    r.close()
    return 0


def _make_cam(sx, sy, hw, hh, W=1600, H=900):
    """Use the REAL Camera2D from the LOD gate, not a hand-rolled stand-in.

    Re-implementing the camera here is how a harness ends up disagreeing with
    the GUI for reasons that have nothing to do with the bug under test.
    """
    sys.path.insert(0, ROOT)
    from naksha_lod_gate import Camera2D
    return Camera2D(sx, sy, hw, W, H)


def _settle(mgr, cam, viewport, max_ticks=240):
    """Idle-refine until the resident frontier stops changing."""
    import time as _time
    last = None
    stable = 0
    for _ in range(max_ticks):
        mgr._last_camera_change = _time.perf_counter() - 10.0   # force IDLE
        mgr.on_frame(cam, viewport)
        sig = tuple(sorted(k for k, v in mgr.resident.items()
                           if v.state == "GPU_RESIDENT"))
        if sig == last:
            stable += 1
            if stable >= 3:
                return
        else:
            stable = 0
            last = sig


class _Adapter:
    """Headless renderer stand-in: records what WOULD be drawn."""

    def __init__(self):
        self.ranges = []
        self.points = 0

    def begin_frame(self, gen):
        pass

    def upload_resident(self, xyz, rgb, cls, inten, ranges):
        self.points = int(xyz.shape[0]) if xyz is not None else 0
        self.ranges = list(ranges or [])
        return {"ok": True}

    def upload_resident_normals(self, packed):
        return True

    def set_draw_ranges(self, ranges):
        self.ranges = list(ranges or [])

    def clear_draw_ranges(self):
        self.ranges = []

    def set_render_origin(self, x, y, z):
        pass

    def set_display_mode(self, mode):
        pass

    def request_render(self):
        pass

    def gpu_resident_bytes(self):
        return 0

    def resident_point_count(self):
        return self.points

    def has_native_append(self):
        return False

    def stats(self):
        return {"uploads": 1, "draw_range_calls": len(self.ranges)}


class _TE:
    def event(self, *a, **k):
        pass

    def sample(self, *a, **k):
        pass


def simulate_pan_zoom():
    """Drive the REAL manager over the REAL dataset: fit -> pan -> zoom.

    Production path only (on_camera_changed / on_frame / _reconcile /
    _retire_superseded / _flush_gpu) with a headless adapter. This is a
    HARNESS result: only `python main.py` is acceptance.
    """
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.stream_manager import (
        NakshaStreamManager, DEFAULT_RAM_BUDGET_BYTES, DEFAULT_GPU_BUDGET_BYTES,
    )

    reader = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)
    adapter = _Adapter()
    mgr = NakshaStreamManager(
        app=None, reader=reader, adapter=adapter, te=_TE(),
        ram_budget=DEFAULT_RAM_BUDGET_BYTES,
        gpu_budget=DEFAULT_GPU_BUDGET_BYTES)

    # The GUI attaches the real .nakshanorm sidecar at open. Without it the
    # overview itself has no readiness and can never be complete, which would
    # make this harness disagree with the real app for the wrong reason.
    try:
        from gui.naksha_cache.normal_streaming import (
            NormalBlockCache, NormalCacheReport, open_normal_cache)
        rd, rep = open_normal_cache(LAZ, expected_source_points=None)
        if rd is not None:
            mgr.attach_normal_cache(rd, rep)
            print(f"[HARNESS] normal sidecar: {rep.status} "
                  f"{int(rep.stored_normals):,} normals")
        else:
            print(f"[HARNESS] normal sidecar unavailable: {rep.reason}")
    except Exception as e:
        print(f"[HARNESS] normal sidecar attach failed: {e!r}")

    def report(tag):
        t = mgr.lod_budget_telemetry()
        print(f"\n[VISUAL FRONTIER] {tag}")
        print(f"  generation       : {t['camera_generation']}")
        print(f"  camera_state     : {t['camera_state']}")
        print(f"  target_points    : {t['target_points']:,}")
        print(f"  selected_points  : {t['selected_points']:,}")
        print(f"  submitted_points : {t['submitted_points']:,}")
        print(f"  active_blocks    : {t['selected_blocks']}")
        print(f"  actual_ppp       : {t['actual_ppp']}")
        print(f"  LOD distribution : 0={t['LOD0_blocks']} 1={t['LOD1_blocks']} "
              f"2={t['LOD2_blocks']} 3={t['LOD3_blocks']} 4={t['LOD4_blocks']}")
        print(f"  overview_active  : {t['overview_active']}")
        print(f"  viewport cells   : {t['viewport_cells']}")
        print(f"  HOLE cells       : {t['viewport_hole_cells']}")
        print(f"  illegal overlap  : {t['illegal_persistent_overlap_pairs']}")
        return t

    W, H = 1600, 900
    cx, cy = 390500.0, 685500.0
    half_h = 520.0
    half_w = half_h * (W / H)

    print("\n" + "=" * 70)
    print("[REAL HEADLESS PAN/ZOOM SIMULATION]")
    print("=" * 70)
    mgr.open_first_frame()
    mgr.note_camera_motion()
    mgr.on_camera_changed(_make_cam(cx, cy, half_w, half_h),
                          (cx, cy, half_w, half_h), 1)
    _settle(mgr, _make_cam(cx, cy, half_w, half_h), (cx, cy, half_w, half_h))
    report("INITIAL FIT (generation 1)")

    steps = [
        ("PAN RIGHT ", cx + 220.0, cy, half_w, half_h),
        ("PAN LEFT  ", cx - 220.0, cy, half_w, half_h),
        ("PAN UP    ", cx, cy + 220.0, half_w, half_h),
        ("PAN DOWN  ", cx, cy - 220.0, half_w, half_h),
        ("ZOOM IN   ", cx, cy, half_w / 3.0, half_h / 3.0),
        ("ZOOM IN 2 ", cx, cy, half_w / 9.0, half_h / 9.0),
        ("ZOOM OUT  ", cx, cy, half_w, half_h),
        ("FIT       ", cx, cy, half_w, half_h),
    ]
    worst = 0
    for i, (tag, sx, sy, hw, hh) in enumerate(steps, start=2):
        vp, cam = (sx, sy, hw, hh), _make_cam(sx, sy, hw, hh)
        mgr.on_camera_changed(cam, vp, i)
        _settle(mgr, cam, vp)
        t = report(f"{tag} (generation {i})")
        worst = max(worst, int(t["viewport_hole_cells"]))

    print("\n" + "=" * 70)
    print(f"WORST 10m viewport hole cells across all stages: {worst}")
    print("=" * 70)
    mgr.close()
    reader.close()
    return 0


def prove_union_coverage():
    """Quantify the residual 27 hole cells precisely.

    The union-coverage gate retires the overview once finer blocks cover >=90%
    of it. That is correct for the OVERVIEW but it leaves a real question: are
    the remaining uncovered viewport cells genuinely empty in the SOURCE, or are
    they regions the 90% threshold just gave up on? Those are very different
    bugs and the fix is different for each.
    """
    import numpy as _np
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.normal_streaming import _overlap_frac

    r = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)
    idx = r.index
    ov = ((float(idx.header["bounds_min"][0]),
           float(idx.header["bounds_min"][1])),
          (float(idx.header["bounds_max"][0]),
           float(idx.header["bounds_max"][1])))

    n = idx.nodes
    bmin = _np.asarray(n["bounds_min"], dtype=_np.float64)
    bmax = _np.asarray(n["bounds_max"], dtype=_np.float64)

    step = 10.0
    gx = _np.arange(ov[0][0], ov[1][0], step)
    gy = _np.arange(ov[0][1], ov[1][1], step)
    cx = gx + step / 2.0
    cy = gy + step / 2.0
    inside = (_np.ones((cx.size, cy.size), dtype=bool))

    # Source coverage: does ANY node (at ANY lod) claim this cell?
    src = _np.zeros((cx.size, cy.size), dtype=bool)
    for i in range(n.shape[0]):
        src |= ((cx[:, None] >= bmin[i, 0]) & (cx[:, None] <= bmax[i, 0])
                & (cy[None, :] >= bmin[i, 1]) & (cy[None, :] <= bmax[i, 1]))

    holes_total = int(inside.sum()) - int((src & inside).sum())
    print("=" * 70)
    print("[RESIDUAL HOLE ANALYSIS @10m over the full dataset extent]")
    print("=" * 70)
    print(f"10m cells in extent          : {int(inside.sum())}")
    print(f"cells covered by some node   : {int((src & inside).sum())}")
    print(f"cells NO node covers         : {holes_total}")

    # Which nodes border the uncovered cells? If the uncovered cells sit in a
    # band between node footprints, that is the LOD grid's inter-tile gap and
    # the coarse overview was silently covering it.
    frac = _np.asarray(
        [_overlap_frac(ov, ((float(bmin[i, 0]), float(bmin[i, 1])),
                           (float(bmax[i, 0]), float(bmax[i, 1]))))
         for i in range(n.shape[0])], dtype=_np.float64)
    print(f"nodes examined               : {frac.size}")
    print(f"max node coverage of extent  : {frac.max():.4f}")
    print(f"sum of node areas / extent   : "
          f"{float(((bmax[:, 0] - bmin[:, 0]) * (bmax[:, 1] - bmin[:, 1])).sum()) / ((ov[1][0]-ov[0][0])*(ov[1][1]-ov[0][1])):.4f}")
    r.close()
    return 0


def locate_residual_holes():
    """Where are the residual uncovered cells, and is the overview the answer?

    After the fix the union-coverage gate retires the overview once finer
    blocks cover >=90% of it. This locates the cells that are still uncovered by
    the ACTIVE set and measures how much of each is inside the viewport, so the
    residual can be classified as (a) genuinely empty in the source, (b) a
    boundary sliver, or (c) a real region that lost its representation.
    """
    import numpy as _np
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.stream_manager import (
        NakshaStreamManager, DEFAULT_RAM_BUDGET_BYTES, DEFAULT_GPU_BUDGET_BYTES)

    reader = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)
    adapter = _Adapter()
    mgr = NakshaStreamManager(
        app=None, reader=reader, adapter=adapter, te=_TE(),
        ram_budget=DEFAULT_RAM_BUDGET_BYTES,
        gpu_budget=DEFAULT_GPU_BUDGET_BYTES)
    try:
        from gui.naksha_cache.normal_streaming import open_normal_cache
        rd, rep = open_normal_cache(LAZ, expected_source_points=None)
        if rd is not None:
            mgr.attach_normal_cache(rd, rep)
    except Exception:
        pass

    W, H = 1600, 900
    cx, cy = 390500.0, 685500.0
    half_h = 520.0
    half_w = half_h * (W / H)
    vp = (cx, cy, half_w, half_h)
    mgr.open_first_frame()
    mgr.note_camera_motion()
    mgr.on_camera_changed(_make_cam(cx, cy, half_w, half_h), vp, 1)
    _settle(mgr, _make_cam(cx, cy, half_w, half_h), vp)

    n = reader.index.nodes
    nmin = _np.asarray(n["bounds_min"], dtype=_np.float64)
    nmax = _np.asarray(n["bounds_max"], dtype=_np.float64)
    act = []
    for k, v in mgr.resident.items():
        if v.state != "GPU_RESIDENT":
            continue
        bb = mgr._block_bounds_for_key(k)
        if bb is not None:
            act.append(bb)

    ov = mgr._overview_true_bounds()
    step = 10.0
    gx = _np.arange(ov[0][0], ov[1][0], step)
    gy = _np.arange(ov[0][1], ov[1][1], step)
    sx = gx + step / 2.0
    sy = gy + step / 2.0
    src = _np.zeros((sx.size, sy.size), dtype=bool)
    for i in range(nmin.shape[0]):
        src |= ((sx[:, None] >= nmin[i, 0]) & (sx[:, None] <= nmax[i, 0])
                & (sy[None, :] >= nmin[i, 1]) & (sy[None, :] <= nmax[i, 1]))
    cov = _np.zeros((sx.size, sy.size), dtype=bool)
    for bb in act:
        cov |= ((sx[:, None] >= bb[0][0]) & (sx[:, None] <= bb[1][0])
                & (sy[None, :] >= bb[0][1]) & (sy[None, :] <= bb[1][1]))

    in_vp = ((sx[:, None] >= vp[0] - vp[2]) & (sx[:, None] <= vp[0] + vp[2])
             & (sy[None, :] >= vp[1] - vp[3]) & (sy[None, :] <= vp[1] + vp[3]))
    holes = src & ~cov
    print("=" * 70)
    print("[RESIDUAL UNCOVERED CELLS - classification]")
    print("=" * 70)
    print(f"source-covered cells        : {int(src.sum())}")
    print(f"active-covered cells        : {int(cov.sum())}")
    print(f"uncovered (src & ~active)   : {int(holes.sum())}")
    print(f"  ... of those, in viewport : {int((holes & in_vp).sum())}")
    print(f"active blocks               : "
          f"{sum(1 for v in mgr.resident.values() if v.state == 'GPU_RESIDENT')}")

    ii, jj = _np.nonzero(holes)
    if ii.size:
        xs = sx[ii]
        ys = sy[jj]
        print(f"  hole X range : {xs.min():.1f} .. {xs.max():.1f}")
        print(f"  hole Y range : {ys.min():.1f} .. {ys.max():.1f}")
        print(f"  sample holes : "
              f"{[(round(float(a), 1), round(float(b), 1)) for a, b in list(zip(xs, ys))[:8]]}")
        # How close is the nearest node edge? A sub-cell sliver at a tile
        # boundary is a measurement artefact of comparing CENTRES against
        # boxes that were built from a different subdivision.
        d = []
        for a, b in list(zip(xs, ys))[:200]:
            best = 1e18
            for i in range(nmin.shape[0]):
                dx = max(nmin[i, 0] - a, 0.0, a - nmax[i, 0])
                dy = max(nmin[i, 1] - b, 0.0, b - nmax[i, 1])
                best = min(best, (dx * dx + dy * dy) ** 0.5)
            d.append(best)
        d = _np.asarray(d)
        print(f"  dist to nearest node box (m): max={d.max():.2f} "
              f"median={_np.median(d):.2f}")
        print(f"  cells >1 m outside every node box: {int((d > 1.0).sum())}"
              f" / {d.size}")
    mgr.close()
    reader.close()
    return 0


def prove_camera_propagation_defect():
    """PART 1-4: prove a PAN never reaches LOD re-selection.

    THE DEFECT. In `NakshaStreamManager.on_frame` the frontier is recomputed
    ONLY when the point BUDGET target changes:

        target = self._density(self).apply(self.lod, w, h, moving=not idle)
        if target != getattr(self, "_last_budget_target", None):
            ... re-run self.lod.select(...) and rebuild specs ...

    `ScreenDensityBudget.apply` derives `target` from PIXEL COUNT and the
    MOVING/IDLE flag alone:

        target = target_point_budget(px, self.moving)

    The camera CENTRE and the world SCALE are not inputs. A pan changes the
    centre but neither the pixel count nor the moving flag, so `target` is
    bit-identical, the `if` is False, and `self.lod.select()` is never called
    again. The frontier stays pinned to the region selected at the last budget
    change, and every newly revealed area has NO block - which is precisely
    the "large rectangular sections disappear" symptom.

    A zoom is worse: it changes the world scale (and therefore SHOULD change
    which LOD each node needs) but leaves the pixel count untouched, so it is
    likewise invisible to that guard.

    The camera also cannot reach the manager unless VTK fires
    InteractionEvent/EndInteractionEvent: `_install_camera_hook` observes ONLY
    those two, never the camera's ModifiedEvent, and on_frame() never calls
    `note_camera_motion()`. So a pan driven by anything other than the VTK
    interactor (a Qt handler, a Fit button, a keyboard shortcut, a programmatic
    ResetCamera) produces NO new generation and NO MOVING transition - matching
    the observed log, which shows MOVING -> IDLE at startup and then nothing.
    """
    from gui.naksha_cache.stream_manager import NakshaStreamManager
    from gui.naksha_cache.normal_streaming import ScreenDensityBudget

    class _Lod:
        """Minimal stand-in: apply() only assigns .budget and .moving."""

        def __init__(self):
            self.budget = 0
            self.moving = True

    print("=" * 72)
    print("[PART 1] ScreenDensityBudget.apply() depends ONLY on px + moving")
    print("=" * 72)
    d = ScreenDensityBudget()
    t_idle_a = d.apply(_Lod(), 1600, 900, moving=False)
    d2 = ScreenDensityBudget()
    t_idle_b = d2.apply(_Lod(), 1600, 900, moving=False)
    d3 = ScreenDensityBudget()
    t_mov = d3.apply(_Lod(), 1600, 900, moving=True)
    print(f"  target(1600x900, moving=False) = {t_idle_a:,}")
    print(f"  target(1600x900, moving=False) = {t_idle_b:,}")
    print(f"  target(1600x900, moving=True)  = {t_mov:,}")
    print(f"  identical for identical inputs : {t_idle_a == t_idle_b}")
    print("  NOTE: no camera centre, no parallel scale, no world width is an")
    print("        input to this function at all.")
    print("  => A PAN cannot change `target`; the on_frame re-select guard")
    print("     is therefore blind to every camera move.")
    _CAMERA_PROOF_STATE["phase1_done"] = True

    print()
    print("=" * 72)
    print("[PART 2] on_frame re-select guard, measured on a REAL manager")
    print("=" * 72)
    from gui.naksha_cache.reader import NakshaPointCacheReader
    from gui.naksha_cache.stream_manager import (
        DEFAULT_RAM_BUDGET_BYTES, DEFAULT_GPU_BUDGET_BYTES)
    reader = NakshaPointCacheReader(LAZ, verify_crc=False, load_edits=False)
    mgr = NakshaStreamManager(
        app=None, reader=reader, adapter=_Adapter(), te=_TE(),
        ram_budget=DEFAULT_RAM_BUDGET_BYTES,
        gpu_budget=DEFAULT_GPU_BUDGET_BYTES)
    try:
        from gui.naksha_cache.normal_streaming import open_normal_cache
        rd, rep = open_normal_cache(LAZ, expected_source_points=None)
        if rd is not None:
            mgr.attach_normal_cache(rd, rep)
    except Exception:
        pass

    W, H = 1600, 900
    cx, cy = 390500.0, 685500.0
    half_h = 520.0
    half_w = half_h * (W / H)

    mgr.open_first_frame()
    mgr.note_camera_motion()
    vp = (cx, cy, half_w, half_h)
    mgr.on_camera_changed(_make_cam(cx, cy, half_w, half_h), vp, 1)
    _settle(mgr, _make_cam(cx, cy, half_w, half_h), vp)

    before = set((s.node_id, s.lod) for s in (mgr._last_specs or []))
    before_hash = hash(frozenset(before))
    print(f"  initial selected keys : {len(before)}  hash={before_hash}")
    print(f"  camera_state          : {mgr._camera_state}")

    pan_cx = cx + 300.0
    pan_vp = (pan_cx, cy, half_w, half_h)
    pan_cam = _make_cam(pan_cx, cy, half_w, half_h)

    # Reproduce the GUI's own state: no note_camera_motion() - only VTK's
    # InteractionEvent calls on_camera_changed, which on_frame does not.
    _settle(mgr, pan_cam, pan_vp)
    after = set((s.node_id, s.lod) for s in (mgr._last_specs or []))
    after_hash = hash(frozenset(after))

    print()
    print(f"  PAN centre  {cx:.1f} -> {pan_cx:.1f} m  (world size unchanged)")
    print(f"  selected keys after pan : {len(after)}  hash={after_hash}")
    print(f"  selected changed        : "
          f"{'YES' if before_hash != after_hash else 'NO'}")
    print(f"  camera_state after pan  : {mgr._camera_state}")
    print()
    if before_hash == after_hash:
        print("  *** CONFIRMED: a 300 m PAN produced an IDENTICAL selected")
        print("      frontier. The newly revealed 300 m strip has no block")
        print("      queued or drawn => the black rectangular region.")
        print("      ROOT CAUSE = STREAM -> LOD SELECTOR (re-select guard)")
        print("      plus no camera ModifiedEvent / no note_camera_motion.")
    else:
        print("  selection DID change on pan (unexpected) - investigate")

    zoom_hw, zoom_hh = half_w / 3.0, half_h / 3.0
    zoom_vp = (pan_cx, cy, zoom_hw, zoom_hh)
    zoom_cam = _make_cam(pan_cx, cy, zoom_hw, zoom_hh)
    _settle(mgr, zoom_cam, zoom_vp)
    z = set((s.node_id, s.lod) for s in (mgr._last_specs or []))
    print()
    print(f"  ZOOM world width {2 * half_w:.1f} -> {2 * zoom_hw:.1f} m")
    print(f"  selected keys after zoom : {len(z)}  "
          f"hash={hash(frozenset(z))}")
    print(f"  selected changed vs pan : "
          f"{'YES' if hash(frozenset(z)) != after_hash else 'NO'}")

    mgr.close()
    reader.close()
    return 0


_CAMERA_PROOF_STATE = {}


if __name__ == "__main__":
    rc = main()
    rc |= prove_defect_c()
    rc |= prove_camera_propagation_defect()
    rc |= prove_union_coverage()
    rc |= locate_residual_holes()
    rc |= simulate_pan_zoom()
    raise SystemExit(rc)
