import laspy, numpy as np
SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"
print("=== probe ch['x'] behavior ===", flush=True)
with laspy.open(SRC) as f:
    h = f.header
    print("scales=", h.scales, "offsets=", h.offsets, flush=True)
    print("header mins=", h.mins, "maxs=", h.maxs, flush=True)
    ch = next(f.chunk_iterator(2_000_000))
    xra = ch["x"]; yra = ch["y"]
    print("type(ch['x'])=", type(xra).__name__, " dtype=", xra.dtype, flush=True)
    xa = np.ascontiguousarray(xra, dtype=np.float64)
    ya = np.ascontiguousarray(yra, dtype=np.float64)
    print("x after ascontiguous: min=%.3f max=%.3f n=%d" % (xa.min(), xa.max(), xa.size), flush=True)
    print("y after ascontiguous: min=%.3f max=%.3f n=%d" % (ya.min(), ya.max(), ya.size), flush=True)
    # test the exact viewport filter used by probe_scan
    cam_cx, cam_cy, hw, hm = 685500.0, 3504500.0, 500.05, 281.27
    m = (xa >= cam_cx - hw) & (xa <= cam_cx + hw) & (ya >= cam_cy - hm) & (ya <= cam_cy + hm)
    print("pts in fit viewport: %d of %d (%.1f%%)" % (int(m.sum()), xa.size, 100*m.sum()/xa.size), flush=True)
    # raw X (integer) check
    X = np.ascontiguousarray(ch["X"], dtype=np.int32)
    print("raw X min/max:", X.min(), X.max(), "-> scaled x min/max:", (X*h.scales[0]).min(), (X*h.scales[0]).max(), flush=True)
