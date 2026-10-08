"""Stage 3 CORRECTNESS: bit-exact MANDI_56 cache vs source LAZ.

Samples one real tile per LOD level (skipping the synthetic overview block),
matches by the stable source_id, does a SINGLE streaming source scan, and
asserts per-attribute equality: classification/intensity/rgb/return_number
exact; xyz within 5 cm.
"""
import os
import sys
import time
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gui.naksha_cache.reader import NakshaPointCacheReader
import laspy

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"


def main():
    r = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    idx = r.index
    n = idx.nodes
    nlod = int(idx.header["lod_count"])
    print("=" * 76)
    print("[DISK CORRECTNESS] bit-exact MANDI_56 cache vs source LAZ")
    samples = []
    for lo in range(nlod):
        rows = np.where(n["lod_block"][:, lo] >= 0)[0]
        if rows.size:
            # pick the median valid row -> a real spatial leaf, not the synthetic overview
            sel = rows[len(rows) // 2]
            samples.append((int(n["node_id"][sel]), lo))
    print("  sampled one (real) tile per LOD: %s" % samples)

    tgt = {}
    for nid, lo in samples:
        t = r.read_tile(nid, lo, render_space=True, apply_edits=False, verify_crc=False)
        if not t:
            continue
        sid = t["source_id"]
        cls = t["classification"]
        inten = t["intensity"]
        rgb = t["rgb"]
        rn = t["return_number"]
        xyz = np.asarray(t["xyz"], dtype=np.float64)
        world = xyz + t["origin"]
        for i in range(sid.size):
            s = int(sid[i])
            if s not in tgt:
                tgt[s] = {"cls": int(cls[i]), "inten": int(inten[i]),
                          "rgb": (int(rgb[i, 0]), int(rgb[i, 1]), int(rgb[i, 2])),
                          "rn": int(rn[i]), "world": world[i]}
    r.close()
    need = len(tgt)
    print("  target source points to verify: %s" % f"{need:,}")
    if need == 0:
        print("  FAIL: no targets"); return 1

    sids = np.array(sorted(tgt), dtype=np.int64)
    lcls = np.empty(need, dtype=np.uint8)
    lint = np.empty(need, dtype=np.uint16)
    lrn = np.empty(need, dtype=np.uint8)
    lrgb = np.empty((need, 3), dtype=np.uint16)
    lxyz = np.empty((need, 3), dtype=np.float64)
    found = np.zeros(need, dtype=bool)
    rgb_ok = True
    t0 = time.perf_counter()
    scanned = 0
    off = 0
    with laspy.open(SRC) as f:
        for ch in f.chunk_iterator(2_000_000):
            cn = len(ch)
            scanned += cn
            lo_i = np.searchsorted(sids, off)
            hi_i = np.searchsorted(sids, off + cn)
            if hi_i > lo_i:
                loc = sids[lo_i:hi_i] - off
                cc = np.asarray(ch["classification"])
                ii = np.asarray(ch["intensity"])
                rr = np.asarray(ch["return_number"])
                xs = np.ascontiguousarray(ch["x"], dtype=np.float64)
                ys = np.ascontiguousarray(ch["y"], dtype=np.float64)
                zs = np.ascontiguousarray(ch["z"], dtype=np.float64)
                lcls[lo_i:hi_i] = cc[loc]
                lint[lo_i:hi_i] = ii[loc]
                lrn[lo_i:hi_i] = rr[loc]
                lxyz[lo_i:hi_i, 0] = xs[loc]
                lxyz[lo_i:hi_i, 1] = ys[loc]
                lxyz[lo_i:hi_i, 2] = zs[loc]
                try:
                    lrgb[lo_i:hi_i] = np.asarray(ch["rgb"])[loc]
                except Exception:
                    rgb_ok = False
                found[lo_i:hi_i] = True
            off += cn
            if found.all():
                break
    scan_ms = (time.perf_counter() - t0) * 1000
    unfound = int((~found).sum())
    print("  source scan: %s pts in %.1fs (single pass, early-stop) unfound=%d" % (
        f"{scanned:,}", scan_ms / 1000, unfound))

    from collections import Counter
    mism = Counter()
    maxerr = 0.0
    checked = 0
    for i in range(need):
        if not found[i]:
            continue
        c = tgt[int(sids[i])]
        checked += 1
        if int(lcls[i]) != c["cls"]:
            mism["classification"] += 1
        if int(lint[i]) != c["inten"]:
            mism["intensity"] += 1
        if int(lrn[i]) != c["rn"]:
            mism["return_number"] += 1
        if rgb_ok and tuple(int(v) for v in lrgb[i]) != c["rgb"]:
            mism["rgb"] += 1
        e = float(np.max(np.abs(lxyz[i] - c["world"])))
        if e > maxerr:
            maxerr = e
    print("  compared %s / %s points across %d LOD tiles" % (
        f"{checked:,}", f"{need:,}", len(samples)))
    print("  mismatches: %s" % dict(mism))
    print("  max XYZ error (cache world vs LAZ world, meters): %.4f" % maxerr)
    ok = (not any(mism.values())) and maxerr < 0.05 and unfound == 0
    print("  [DISK CORRECTNESS] %s  (bit-exact attrs, XYZ tol<5cm, all sampled found)" % (
        "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())