"""[NAKSHA NATIVE CACHE] format benchmark - the measurements that decide V1.

Parts 6, 8, 11, 13 and 16 of the spec all say "measure, do not assume":

  PART 6  leaf block size   64K / 128K / 256K / 512K points
  PART 8  AoS vs SoA        interleaved vs split attribute blocks
  PART 11 Morton ordering   spatial sort inside a block
  PART 13 LOD sampling     points[::N] vs voxel representative
  PART 16 compression      none / LZ4 / ZSTD

Everything runs on REAL NT219 points, never synthetic data, because the point
of the benchmark is the real coordinate magnitude, the real attribute mix and
the real disk.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SRC = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_53.laz"
WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bench")

fails = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def have(mod):
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def load_sample(n=4_000_000):
    """Load real points once; every benchmark reuses this buffer."""
    import laspy
    X, Y, Z, R, G, B, C, I = [], [], [], [], [], [], [], []
    got = 0
    with laspy.open(SRC) as f:
        for ch in f.chunk_iterator(2_000_000):
            X.append(np.asarray(ch.x, np.float64))
            Y.append(np.asarray(ch.y, np.float64))
            Z.append(np.asarray(ch.z, np.float64))
            R.append(np.asarray(ch.red))
            G.append(np.asarray(ch.green))
            B.append(np.asarray(ch.blue))
            C.append(np.asarray(ch.classification))
            I.append(np.asarray(ch.intensity))
            got += len(X[-1])
            if got >= n:
                break
    return [np.concatenate(a)[:n] for a in (X, Y, Z, R, G, B, C, I)]


def rgb8(v):
    v = np.asarray(v)
    if v.dtype == np.uint8:
        return v
    # LAS 1.2 fmt 3 stores RGB as uint16. Truncating with astype(uint8) takes
    # it mod 256 and turns 49920 into 0 - the whole cloud renders black.
    return np.clip(v.astype(np.int64) >> 8, 0, 255).astype(np.uint8)


def morton_code(x, y, origin, span, bits=16):
    """Interleave X and Y into a Morton/Z-order code.

    Z-order maximises the spatial locality of consecutive indices, which is
    what a block reader wants: a sub-range of a block is a compact sub-region
    rather than a scattered set of points.
    """
    side = (1 << bits) - 1
    ix = np.clip(((x - origin[0]) / span * side).astype(np.int64), 0, side)
    iy = np.clip(((y - origin[1]) / span * side).astype(np.int64), 0, side)
    code = np.zeros(ix.size, dtype=np.int64)
    for b in range(bits):
        code |= ((ix >> b) & 1) << (2 * b)
        code |= ((iy >> b) & 1) << (2 * b + 1)
    # bits must satisfy 2*bits <= 63: the interleave yields a 2*bits-wide code.
    # At bits=32 the shift 2*b+1 overflows int64 and SILENTLY WRAPS, producing
    # garbage ordering (and NaN medians after the diff) instead of an error.
    if 2 * bits > 62:
        raise ValueError(f"bits={bits} overflows the int64 code (max 31)")
    return code


def voxel_representative(xyz, cell):
    """One representative point per occupied voxel, biased to the highest z.

    points[::N] is not an acceptable production LOD: a stride samples in FILE
    order, so one building can vanish at LOD3 while a dense ground swath is
    sampled over and over. A voxel representative keeps one point per occupied
    cell and prefers the highest point, so structures survive coarsening.
    """
    ix = np.floor(xyz[:, 0] / cell).astype(np.int64)
    iy = np.floor(xyz[:, 1] / cell).astype(np.int64)
    iz = np.floor(xyz[:, 2] / cell).astype(np.int64)
    key = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791)
    order = np.lexsort((-xyz[:, 2], key))   # within a voxel: highest z first
    first = np.ones(order.size, dtype=bool)
    first[1:] = key[order][1:] != key[order][:-1]
    return order[first]
    return code


def main():
    os.makedirs(WORK, exist_ok=True)
    print("=" * 76)
    print("[NAKSHA NATIVE CACHE] format benchmark")
    print("=" * 76)
    print("\n  codecs available:",
          "lz4" if have("lz4") else "-",
          "zstandard" if have("zstandard") else "-")
    X, Y, Z, R, G, B, C, I = load_sample()
    n = len(X)
    xyz = np.column_stack((X, Y, Z))
    print(f"  loaded {n:,} real points from {os.path.basename(SRC)}")
    print(f"  bounds x[{X.min():,.0f}..{X.max():,.0f}] "
          f"y[{Y.min():,.0f}..{Y.max():,.0f}] z[{Z.min():,.1f}..{Z.max():,.1f}]")
    print(f"  classifications present: {len(np.unique(C))} distinct")

    origin = np.array([X.min(), Y.min()])
    span = max(X.max() - X.min(), Y.max() - Y.min(), 1e-9)

    # ---------------------------------------------------------- PART 6
    print("\n" + "-" * 76)
    print("  PART 6  leaf block size (int32 quantised local coords)")
    print("  " + "-" * 76)
    print(f"  {'block':>9}{'write ms':>10}{'read ms':>10}{'MB':>9}"
          f"{'B/pt':>8}{'pts/s':>14}")
    for size in (65_536, 131_072, 262_144, 524_288):
        lo = xyz[:size].min(0)
        q = ((xyz[:size] - lo) / 1e-3).astype(np.int32)
        path = os.path.join(WORK, f"blk_{size}.bin")
        t0 = time.perf_counter()
        with open(path, "wb") as f:
            f.write(q.tobytes())
        w_ms = (time.perf_counter() - t0) * 1000
        mb = os.path.getsize(path) / 1e6
        total, samples = 0.0, 8
        for _ in range(samples):
            off = np.random.randint(0, max(1, size - 4096))
            t0 = time.perf_counter()
            with open(path, "rb") as f:
                f.seek(off * 12)
                f.read(12 * 4096)
            total += time.perf_counter() - t0
        r_ms = total / samples * 1000
        print(f"  {size:>9,}{w_ms:>10.1f}{r_ms:>10.2f}{mb:>9.2f}"
              f"{mb * 1e6 / size:>8.1f}{4096 / (total / samples):>14,.0f}")

    # ---------------------------------------------------------- PART 8
    print("\n" + "-" * 76)
    print("  PART 8  AoS vs SoA (what a display mode actually has to read)")
    print("  " + "-" * 76)
    size = 262_144
    sub = xyz[:size]
    q = (sub - sub.min(0)).astype(np.int32)
    rgb = np.column_stack((rgb8(R[:size]), rgb8(G[:size]), rgb8(B[:size])))
    cls = C[:size].astype(np.uint8)
    inten = I[:size].astype(np.uint16)
    # A REAL interleaved layout keeps each field at its own width. Building AoS
    # by casting the concatenation to uint8 would truncate the int32 coordinates
    # to a single byte each and make AoS look 2.5x SMALLER than it can ever be -
    # the comparison would then measure a broken layout, not the real trade-off.
    aos_dtype = np.dtype([("x", "<i4"), ("y", "<i4"), ("z", "<i4"),
                          ("r", "u1"), ("g", "u1"), ("b", "u1"),
                          ("class", "u1"), ("intensity", "<u2")])
    aos = np.empty(size, dtype=aos_dtype)
    aos["x"], aos["y"], aos["z"] = q[:, 0], q[:, 1], q[:, 2]
    aos["r"], aos["g"], aos["b"] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    aos["class"] = cls
    aos["intensity"] = inten
    aos_mb = aos.nbytes / 1e6
    print(f"  AoS interleaved {aos.itemsize} B/pt  block {aos_mb:6.2f} MB")
    soa = {"xyz": q.astype(np.int32), "rgb": rgb.astype(np.uint8),
           "classification": cls, "intensity": inten}
    for k, v in soa.items():
        wdt = v.shape[1] if v.ndim > 1 else 1
        print(f"  SoA {k:<16}{v.dtype.str:>6}{wdt:>3} B/pt"
              f"  block {len(v.tobytes()) / 1e6:6.2f} MB")
    # The SoA claim is that a display mode pays only for what it reads.
    # Compare in BYTES on both sides: mixing a byte count with an MB figure
    # makes the check wrong by 1e6 and silently reports a pass/fail that has
    # nothing to do with the format.
    aos_bytes = aos.nbytes
    class_only = (soa["xyz"].nbytes + soa["classification"].nbytes)
    rgb_only = (soa["xyz"].nbytes + soa["rgb"].nbytes)
    print(f"\n  classification mode reads {class_only / 1e6:.2f} MB SoA "
          f"vs {aos_bytes / 1e6:.2f} MB AoS")
    print(f"  RGB mode          reads {rgb_only / 1e6:.2f} MB SoA "
          f"vs {aos_bytes / 1e6:.2f} MB AoS")
    check("PART 8: SoA lets a display mode skip unneeded attributes",
          class_only < aos_bytes and rgb_only < aos_bytes,
          f"classification {aos_bytes / class_only:.2f}x, "
          f"RGB {aos_bytes / rgb_only:.2f}x less traffic with SoA")
    check("PART 8: SoA total is at least as compact as AoS",
          (soa["xyz"].nbytes + soa["rgb"].nbytes + soa["classification"].nbytes
           + soa["intensity"].nbytes) <= aos_bytes,
          f"SoA {sum(v.nbytes for v in soa.values()) / 1e6:.2f} MB "
          f"vs AoS {aos_bytes / 1e6:.2f} MB")

    # ---------------------------------------------------------- PART 11
    print("\n" + "-" * 76)
    print("  PART 11 spatial ordering: Morton vs file order")
    print("  " + "-" * 76)
    m = 1_000_000
    code = morton_code(X[:m], Y[:m], origin, span, bits=16)
    check("PART 11: Morton code fits int64 without wraparound",
          bool(np.all(code >= 0)) and int(code.max()) < (1 << 32),
          f"max code {int(code.max()):,} < 2^32")
    morder = np.argsort(code, kind="stable")
    file_step = np.hypot(np.diff(X[:m]), np.diff(Y[:m]))
    mort_step = np.hypot(np.diff(X[:m][morder]), np.diff(Y[:m][morder]))
    print(f"  median consecutive step, file order  : {np.median(file_step):8.3f} m")
    print(f"  median consecutive step, Morton order : {np.median(mort_step):8.3f} m")
    check("PART 11: Morton ordering improves spatial locality",
          np.median(mort_step) < np.median(file_step),
          f"{np.median(file_step) / max(np.median(mort_step), 1e-9):.1f}x tighter")

    # ---------------------------------------------------------- PART 13
    print("\n" + "-" * 76)
    print("  PART 13 LOD sampling: stride vs voxel representative")
    print("  " + "-" * 76)
    step = 8
    stride_idx = np.arange(0, n, step)
    budget = len(stride_idx)
    # Tune the voxel size so BOTH methods keep ~the same number of points.
    # Comparing voxel(2 m) against stride(8) is not a fair test: it won purely
    # by returning 14x less data. The real question is whether, AT EQUAL BUDGET,
    # voxel sampling keeps more structure and class diversity.
    lo, hi = 0.5, 20.0
    best = None
    for _ in range(18):
        mid = (lo + hi) / 2
        vi = voxel_representative(xyz, mid)
        if vi.size > budget:
            lo = mid
        else:
            hi = mid
            best = (mid, vi)
    if best is None:
        best = (hi, voxel_representative(xyz, hi))
    cell, voxel_idx = best
    cs = set(np.unique(C[stride_idx]).tolist())
    cv = set(np.unique(C[voxel_idx]).tolist())
    print(f"  points[::{step}]  keeps {len(stride_idx):,} pts, "
          f"{len(cs)} distinct classes")
    print(f"  voxel({cell:.2f} m)  keeps {voxel_idx.size:,} pts, "
          f"{len(cv)} distinct classes  (tuned to match the budget)")
    check("PART 13: voxel sampling preserves classification diversity",
          len(cv) >= len(cs),
          f"{len(cv)} classes vs {len(cs)} with stride at equal budget")
    # Structure preservation: a stride samples in file order, so a small dense
    # object can vanish. Compare the vertical spread each method retains.
    zs = Z[stride_idx]
    zv = Z[voxel_idx]
    print(f"  z-range kept: stride {zs.max() - zs.min():.1f} m, "
          f"voxel {zv.max() - zv.min():.1f} m")
    check("PART 13: voxel sampling preserves vertical extent",
          (zv.max() - zv.min()) >= (zs.max() - zs.min()) * 0.999,
          f"voxel {zv.max() - zv.min():.1f} m vs stride {zs.max() - zs.min():.1f} m")

    # ---------------------------------------------------------- PART 16
    print("\n" + "-" * 76)
    print("  PART 16 compression (goal: lowest read+decode+upload latency)")
    print("  " + "-" * 76)
    raw = q.tobytes()
    print(f"  {'codec':<12}{'MB':>9}{'ratio':>8}{'enc ms':>9}{'dec ms':>9}")
    print(f"  {'none':<12}{len(raw) / 1e6:>9.2f}{1.00:>8.2f}"
          f"{0.0:>9.1f}{0.0:>9.1f}")
    if have("lz4"):
        import lz4.frame
        t0 = time.perf_counter()
        c = lz4.frame.compress(raw, 1)
        enc = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        assert lz4.frame.decompress(c) == raw
        dec = (time.perf_counter() - t0) * 1000
        print(f"  {'lz4-1':<12}{len(c) / 1e6:>9.2f}{len(raw) / len(c):>8.2f}"
              f"{enc:>9.1f}{dec:>9.1f}")
    if have("zstandard"):
        import zstandard as zstd
        for lvl in (1, 3):
            zc = zstd.ZstdCompressor(level=lvl)
            t0 = time.perf_counter()
            c = zc.compress(raw)
            enc = (time.perf_counter() - t0) * 1000
            t0 = time.perf_counter()
            assert zstd.ZstdDecompressor().decompress(c) == raw
            dec = (time.perf_counter() - t0) * 1000
            print(f"  {'zstd-' + str(lvl):<12}{len(c) / 1e6:>9.2f}"
                  f"{len(raw) / len(c):>8.2f}{enc:>9.1f}{dec:>9.1f}")
    print("\n  NOTE: decompression is single-threaded CPU work on the STREAM")
    print("        thread. Compression that saves disk but costs more decode")
    print("        time per block reduces the achievable tile rate, so")
    print("        LATENCY - not file size - decides the codec.")

    print("\n" + "=" * 76)
    print(f"  RESULT: {'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
    print("=" * 76)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
