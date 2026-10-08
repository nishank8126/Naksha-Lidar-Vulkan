"""Stage 3 ONE-FILE CAMERA LADDER on the MANDI_56 cache (268.81M pts)."""
import os, sys, time, threading, numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui.naksha_cache.reader import NakshaPointCacheReader
from naksha_lod_gate import Camera2D, ScreenSpaceLOD
SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"
TOTAL = 268_809_706
try:
    import psutil
    def rss_mb():
        return psutil.Process(os.getpid()).memory_info().rss / 1e6
except Exception:
    def rss_mb():
        return 0.0
def main():
    r = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    idx = r.index; nodes = idx.nodes
    bmin = np.asarray(nodes["bounds_min"]); bmax = np.asarray(nodes["bounds_max"])
    gmin, gmax = bmin.min(0), bmax.max(0); span = gmax - gmin
    cx, cy = (gmin[0] + gmax[0]) * 0.5, (gmin[1] + gmax[1]) * 0.5
    print("[ONE-FILE CAMERA LADDER] MANDI_56 total=%s" % f"{TOTAL:,}", flush=True)
    import laspy
    zooms = [("fit", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
             ("x16", 0.0625), ("x32", 0.03125)]
    cams = [Camera2D(cx, cy, min(span[0], span[1]) * 0.5 * frac) for _, frac in zooms]
    in_view = np.zeros(len(zooms), dtype=np.int64)
    pix = np.array([c.width_px * c.height_px for c in cams], dtype=np.float64)
    print("  scanning source LAZ once for ideal spatial counts...", flush=True)
    t0 = time.perf_counter()
    with laspy.open(SRC) as f:
        for ch in f.chunk_iterator(2_000_000):
            x = np.ascontiguousarray(ch["x"], dtype=np.float64)
            y = np.ascontiguousarray(ch["y"], dtype=np.float64)
            for k, c in enumerate(cams):
                m = ((x >= c.cx - c.half_w) & (x <= c.cx + c.half_w) &
                     (y >= c.cy - c.height_m) & (y <= c.cy + c.height_m))
                in_view[k] += int(m.sum())
    scan_ms = (time.perf_counter() - t0) * 1000
    print(f"  source scan done in {scan_ms/1000:.1f}s  in_view={list(in_view)}", flush=True)
    peak = [rss_mb()]; stop = [False]
    def samp():
        while not stop[0]:
            peak[0] = max(peak[0], rss_mb()); time.sleep(0.3)
    threading.Thread(target=samp, daemon=True).start()
    print("\n  zoom  pages  mc   LODdist          req       dec   %src     bytes   us   ms tot  amp", flush=True)
    for k, (label, _) in enumerate(zooms):
        cam = cams[k]
        vis = [i for i in range(nodes.size)
               if bmin[i][0] <= cam.cx + cam.half_w and bmax[i][0] >= cam.cx - cam.half_w
               and bmin[i][1] <= cam.cy + cam.height_m and bmax[i][1] >= cam.cy - cam.half_w]
        lod = ScreenSpaceLOD(); lod.moving = False
        sel = lod.select(idx, cam, vis)
        dist = {}
        for o in sel:
            dist[int(o["lod"])] = dist.get(int(o["lod"]), 0) + 1
        req = sum(int(o["points"]) for o in sel)
        mc_vis = r.visible_microcells(cam.cx - cam.half_w, cam.cy - cam.height_m,
                                      cam.cx + cam.half_w, cam.cy + cam.height_m)
        pages = set(); dec = 0; rd0 = r.stats["bytes_read"]; t1 = time.perf_counter()
        for o in sel:
            pages.add(int(o["node_id"]))
            t = r.read_tile(o["node_id"], o["lod"], render_space=True)
            if t:
                dec += int(t["point_count"])
        rd = r.stats["bytes_read"] - rd0
        ms = (time.perf_counter() - t1) * 1000
        ds = " ".join("L%d:%d" % (kk, v) for kk, v in sorted(dist.items()))
        ideal = min(float(in_view[k]), float(pix[k]))
        amp = dec / ideal if ideal > 0 else float("inf")
        print(f"  {label:<5} {len(pages):>5} {len(mc_vis):>3} {ds:>14} {req:>9,} {dec:>8,} {dec/TOTAL*100:>5.2f}% {rd:>9,} {ms*1000:>5.0f} {ms:>4.0f} {ms:>4.0f} {amp:>5.1f}x", flush=True)
    stop[0] = True
    print(f"\n  RAM working set = {peak[0]:.0f} MB  source scan = {scan_ms/1000:.1f}s", flush=True)
    print("[Stage3 reader.stats] %s" % r.stats, flush=True)
    r.close()
if __name__ == "__main__":
    main()