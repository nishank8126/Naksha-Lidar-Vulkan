"""[SCREEN SPACE LOD] engine + gate measurements.

Implements the missing piece identified after Stage 2: persistent LOD blocks
exist, but camera selection ALWAYS requested lod=0. Visibility therefore changed
tile COUNT and never traded density for screen area, which is why x8/x16/x32 all
drew ~18-21% of the detailed source.

Pipeline implemented here (Parts 2-11):

    camera -> visible nodes -> projected screen box -> per-LOD projected
    spacing -> coarsest LOD under the error target -> global point budget
    allocation by screen importance -> (node_id, lod) request set

Selection uses PROJECTED SPACING rather than point-count ratios, because the
spacing is what actually determines whether a surface looks right on screen.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache import format as F                # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader   # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "test_classified_highprecision.laz")

# ---------------------------------------------------------------- camera
class Camera2D:
    """Orthographic top-down camera: exact world-units-per-pixel."""

    def __init__(self, cx, cy, half_width_m, width_px=1920, height_px=1080):
        self.cx, self.cy = cx, cy
        self.half_w = half_width_m
        self.width_px, self.height_px = width_px, height_px
        self.height_m = half_width_m * height_px / width_px

    def px_per_m(self):
        return self.width_px / (2.0 * self.half_w)

    def project_box(self, bmin, bmax):
        """World AABB -> projected pixel box (orthographic, exact)."""
        ppm = self.px_per_m()
        w = (float(bmax[0]) - float(bmin[0])) * ppm
        h = (float(bmax[1]) - float(bmin[1])) * ppm
        return w, h

    def describe(self):
        return (f"{self.width_px}x{self.height_px} ortho, "
                f"{self.half_w * 2:.0f} m wide, {self.px_per_m():.2f} px/m")


class Camera3D:
    """Perspective camera; conservative projected box for a world AABB."""

    def __init__(self, eye, target, fov_y_deg, width_px=1920, height_px=1080):
        self.eye = np.asarray(eye, float)
        self.target = np.asarray(target, float)
        self.fov = np.radians(fov_y_deg)
        self.width_px, self.height_px = width_px, height_px

    def project_box(self, bmin, bmax):
        # Project the 8 AABB corners and take the screen-space extent. This is
        # conservative: an AABB larger than the visible frustum over-estimates,
        # which errs toward FINER detail, never toward holes.
        fwd = self.target - self.eye
        n = np.linalg.norm(fwd)
        fwd = fwd / max(n, 1e-9)
        up = np.array([0.0, 0.0, 1.0])
        right = np.cross(fwd, up)
        if np.linalg.norm(right) < 1e-6:
            right = np.array([1.0, 0.0, 0.0])
        right /= np.linalg.norm(right)
        upv = np.cross(right, fwd)
        tan = np.tan(self.fov * 0.5)
        xs, ys = [], []
        for ix in (0, 1):
            for iy in (0, 1):
                for iz in (0, 1):
                    p = np.array([bmin[0] if ix else bmax[0],
                                  bmin[1] if iy else bmax[1],
                                  bmin[2] if iz else bmax[2]])
                    d = p - self.eye
                    z = float(np.dot(d, fwd))
                    if z < 1e-6:
                        z = 1e-6
                    xs.append(float(np.dot(d, right)) / (z * tan *
                                 self.width_px / self.height_px))
                    ys.append(float(np.dot(d, upv)) / (z * tan))
        w = (max(xs) - min(xs)) * 0.5 * self.width_px
        h = (max(ys) - min(ys)) * 0.5 * self.height_px
        return max(w, 1e-6), max(h, 1e-6)

    def describe(self):
        return f"{self.width_px}x{self.height_px} persp, fov {np.degrees(self.fov):.0f}deg"


# ---------------------------------------------------------------- engine
class ScreenSpaceLOD:
    """Chooses a per-node LOD from projected screen error + a point budget."""

    def __init__(self, error_px_moving=1.6, error_px_idle=0.9,
                 point_budget_moving=4_000_000, point_budget_idle=8_000_000,
                 hysteresis=0.15):
        self.error_moving = error_px_moving
        self.error_idle = error_px_idle
        self.budget_moving = point_budget_moving
        self.budget_idle = point_budget_idle
        self.hysteresis = hysteresis
        self.moving = True
        self._current = {}     # node -> currently selected lod (for hysteresis)

    @property
    def error_px(self):
        return self.error_moving if self.moving else self.error_idle

    @property
    def budget(self):
        return self.budget_moving if self.moving else self.budget_idle

    @budget.setter
    def budget(self, points):
        """Screen-density override (Part 1/2).

        The absolute budget_moving/budget_idle defaults (4M/8M) are
        viewport-INDEPENDENT, which is what produced ~5.1 points/pixel at
        1920x848. ScreenDensityBudget pushes the viewport-derived target in
        here so the existing importance-ordered downgrade pass actually limits
        the selection. Setting it for both states keeps the property coherent
        no matter which mode is active.
        """
        points = max(0, int(points))
        self.budget_moving = points
        self.budget_idle = points
        self._budget_override = points

    def clear_budget_override(self):
        self._budget_override = None
        self.budget_moving = 4_000_000
        self.budget_idle = 8_000_000

    def select(self, index, cam, visible, detail_pts=None):
        """Return [(node_id, lod, projected_px, importance)] honouring budget.

        `detail_pts` maps node -> LOD0 point count, used only to rank importance
        when the budget forces a downgrade.
        """
        nodes = index.nodes
        out = []
        # First pass: the ideal LOD for every visible node, ignoring budget.
        for row in visible:
            nid = int(nodes["node_id"][row])
            counts = nodes["lod_point_count"][row]
            spacing = nodes["lod_spacing"][row] if "lod_spacing" in \
                nodes.dtype.names else None
            # A LOD exists only when its POINT COUNT is non-zero. Testing the
            # block id instead is wrong: block ids are global counters, so a node
            # whose first LOD is block 9 read that as "LOD index 9" and indexed
            # past the 8-entry lod_spacing array.
            avail = [(int(l), int(c)) for l, c in enumerate(counts)
                     if int(c) > 0]
            if not avail:
                continue
            wpx, hpx = cam.project_box(nodes["bounds_min"][row],
                                       nodes["bounds_max"][row])
            proj_px = wpx * hpx
            ppm = wpx / max(float(nodes["bounds_max"][row][0] -
                                 nodes["bounds_min"][row][0]), 1e-9)
            lod, npts_sel, px_err = self._pick(avail, spacing, ppm, nid)
            if lod < 0:
                continue
            npts = npts_sel
            # The ideal LOD is an UPPER BOUND on useful detail, not just a
            # starting point: a node never needs to be finer than its own screen
            # error requires. Without this cap a node with a large (inflated)
            # projected area absorbs a large share and is promoted to its FINEST
            # LOD even when the screen cannot use it - which both wastes the
            # budget and flattens the zoom -> finer-detail gradient that the
            # density acceptance harness measures. Recorded here, enforced in the
            # budget pass below.
            ideal = lod
            # Screen importance: centre-screen first, then projected area.
            cxm = (float(nodes["bounds_min"][row][0]) +
                   float(nodes["bounds_max"][row][0])) * 0.5
            cym = (float(nodes["bounds_min"][row][1]) +
                   float(nodes["bounds_max"][row][1])) * 0.5
            dcx, dcy = self._cam_centre(cam)
            r = np.hypot(cxm - dcx, cym - dcy)
            out.append({"node_id": nid, "lod": lod, "points": npts,
                        "proj_px": proj_px, "px_err": px_err,
                        "centre_dist": r, "row": row, "ideal": ideal})
        # ---- Second pass: enforce the GLOBAL point budget (Part 4/8/12) ------
        #
        # THE BUDGET REDUCES DETAIL EVERYWHERE. IT NEVER REMOVES A REGION.
        #
        # The rule this replaces kept the HIGHEST-PRIORITY node at its ideal
        # (finest) LOD until the budget ran out, then collapsed every other
        # visible node to the coarsest level it could afford. Measured on the
        # real 123.las cache that produced a single frame containing
        #   LOD distribution L0:6 L3:1
        # i.e. 69,640 points in one cell and **296** points in the next, with
        # 57-100% of the visible node area below 0.02 points/pixel. A few
        # hundred points spread over a cell-sized footprint IS an empty region
        # on screen, and WHICH cells were dense changed with the camera - which
        # is exactly the reported symptom (a few dense tiles surviving inside
        # large sparse/empty source-covered regions, moving during pan/zoom).
        #
        # The budget is therefore shared out in proportion to projected area, so
        # the per-node POINTS PER PIXEL comes out as even as the LOD ladder
        # allows. Sharing the budget is also what makes the allocation sum
        # bounded: every node's chosen count is <= its share, so the total is
        # <= budget by construction, and the only way to exceed it is the
        # never-blank floor below.
        total = sum(o["points"] for o in out)
        if total > self.budget and out:
            area = [max(float(o["proj_px"]), 1.0) for o in out]
            tot_area = float(sum(area)) or 1.0
            for o, a in zip(out, area):
                counts = nodes["lod_point_count"][o["row"]]
                avail = [(int(l), int(c)) for l, c in enumerate(counts)
                         if int(c) > 0]
                if not avail:
                    continue
                share = self.budget * a / tot_area
                ideal = int(o.get("ideal", o["lod"]))
                # Never finer than this node's own screen error needs (lod >=
                # ideal in this numbering), and never more expensive than its
                # share.
                fits = [t for t in avail if t[1] <= share and t[0] >= ideal]
                if fits:
                    # FINEST useful representation this node's share can pay for.
                    o["lod"], o["points"] = min(fits, key=lambda t: t[0])
                elif any(t[0] >= ideal for t in avail):
                    # Its share cannot buy even the coarsest useful level: keep the
                    # coarsest useful one anyway (never-blank floor).
                    o["lod"], o["points"] = max(
                        [t for t in avail if t[0] >= ideal], key=lambda t: t[0])
                else:
                    # NEVER-BLANK FLOOR. The budget is a soft limit on DETAIL,
                    # not permission to delete a visible node: dropping it left
                    # its whole footprint with no representation. Keep the
                    # coarsest LOD even when that overshoots the share.
                    o["lod"], o["points"] = max(avail, key=lambda t: t[0])
            # Spend whatever is left on refinement, best-first (Part 5), so a
            # frame that can afford more than the even share still uses it -
            # the share pass sets a UNIFORM FLOOR, it does not cap quality.
            left = self.budget - sum(o["points"] for o in out)
            if left > 0:
                diag = max((o["centre_dist"] for o in out), default=1.0) or 1.0
                for o in out:
                    o["priority"] = (1.0 - min(o["centre_dist"] / diag, 1.0)) \
                        * np.log1p(o["proj_px"])
                for o in sorted(out, key=lambda o: -o["priority"]):
                    counts = nodes["lod_point_count"][o["row"]]
                    finer = [(int(l), int(c)) for l, c in enumerate(counts)
                             if int(c) > 0 and int(l) < o["lod"]
                             and int(l) >= int(o.get("ideal", o["lod"]))]
                    if not finer:
                        continue
                    lod, npts = min(finer, key=lambda t: t[0])
                    if npts - o["points"] <= left:
                        left -= (npts - o["points"])
                        o["lod"], o["points"] = lod, npts
            out.sort(key=lambda o: int(o["node_id"]))
        return out

    def _cam_centre(self, cam):
        if isinstance(cam, Camera2D):
            return cam.cx, cam.cy
        return float(cam.target[0]), float(cam.target[1])

    def _pick(self, avail, spacing, ppm, nid):
        """Coarsest LOD whose projected spacing is under the error target."""
        cur = self._current.get(nid)
        err_t = self.error_px
        # Hysteresis: once refined, require a clear improvement to go back up.
        lo_t = err_t * (1.0 - self.hysteresis)
        best = None
        for lod, npts in sorted(avail, key=lambda t: -t[0]):
            sp = (spacing[lod] if spacing is not None else 0.0)
            if sp > 0 and ppm > 0:
                px = sp * ppm
            else:
                # No stored spacing: fall back to a density estimate from the
                # LOD0 count and the node area, not an arbitrary ratio.
                px = 1.0
            ok = px <= (err_t if cur is None or lod <= cur else lo_t)
            if ok:
                best = (lod, npts, px)
                break
        if best is None:
            lod, npts = max(avail, key=lambda t: t[1])   # finest available
            best = (lod, npts, 0.0)
        self._current[nid] = best[0]
        return best[0], best[1], best[2]

    def _coarsest_affordable(self, index, row, remaining):
        if remaining <= 0:
            return None
        counts = index.nodes["lod_point_count"][row]
        cands = [(int(l), int(c)) for l, c in enumerate(counts) if int(c) > 0
                 and int(c) <= remaining]
        if not cands:
            return None
        lod, npts = min(cands, key=lambda t: -t[0])   # coarsest that fits
        return lod, npts


# ---------------------------------------------------------------- tests
def main():
    print("\n" + "=" * 76)
    print("[LOD METADATA AUDIT]")
    print("=" * 76)
    r = NakshaPointCacheReader(SRC, verify_crc=True)
    idx = r.index
    n = idx.nodes
    print(f"  NODE_ENTRY fields: {list(n.dtype.names)}")
    print(f"  spacing stored per LOD: {'lod_spacing' in n.dtype.names}")
    lvl_counts = {}
    for i in range(min(6, n.size)):
        nb = [int(v) for v in n["lod_block"][i]]
        nc = [int(v) for v in n["lod_point_count"][i]]
        ns = [round(float(v), 3) for v in n["lod_spacing"][i]]
        print(f"\n  node {int(n['node_id'][i])}  detail {int(n['point_count'][i]):,} pts")
        for lod in range(len(nb)):
            if nc[lod] > 0:
                print(f"    LOD{lod}: {nc[lod]:>9,} pts  block {nb[lod]:>4}"
                      f"  spacing {ns[lod]} m")
    allb = idx.blocks
    print("\n  block counts by LOD:")
    for lod in sorted(set(int(v) for v in allb["lod"])):
        m = allb["lod"] == lod
        print(f"    LOD{lod}: {int(m.sum()):>4} blocks  "
              f"{int(allb['point_count'][m].min()):>9,}.."
              f"{int(allb['point_count'][m].max()):>9,} pts")

    bmin = np.asarray(n["bounds_min"])
    bmax = np.asarray(n["bounds_max"])
    gmin, gmax = bmin.min(0), bmax.max(0)
    span = gmax - gmin
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5

    for moving, tag in ((True, "MOVING"), (False, "IDLE")):
        print("\n" + "=" * 76)
        print(f"[SCREEN SPACE LOD]  {tag}")
        print("=" * 76)
        lod = ScreenSpaceLOD()
        lod.moving = moving
        print(f"  {'zoom':<7}{'viewport':<34}{'vis':>5}{'LOD dist':>22}"
              f"{'drawn pts':>12}{'% src':>8}{'ms':>8}")
        for label, frac in [("fit", 1.0), ("x2", 0.5), ("x4", 0.25),
                           ("x8", 0.125), ("x16", 0.0625),
                           ("x32", 0.03125)]:
            cam = Camera2D(cx, cy, span[0] * 0.5 * frac)
            vis = [i for i in range(n.size)
                   if bmin[i][0] <= cam.cx + cam.half_w
                   and bmax[i][0] >= cam.cx - cam.half_w
                   and bmin[i][1] <= cam.cy + cam.height_m
                   and bmax[i][1] >= cam.cy - cam.height_m]
            sel = lod.select(idx, cam, vis)
            dist = {}
            for o in sel:
                dist[o["lod"]] = dist.get(o["lod"], 0) + 1
            drawn = sum(o["points"] for o in sel)
            t0 = time.perf_counter()
            got = 0
            for o in sel:
                t = r.read_tile(o["node_id"], o["lod"], render_space=True)
                if t:
                    got += t["point_count"]
            ms = (time.perf_counter() - t0) * 1000
            ds = " ".join(f"L{k}:{v}" for k, v in sorted(dist.items()))
            print(f"  {label:<7}{cam.describe():<34}{len(vis):>5}{ds:>22}"
                  f"{got:>12,}{got / 26960750 * 100:>7.1f}%{ms:>8.0f}")
    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
