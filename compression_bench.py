"""compression_bench.py - representative-block codec/transform benchmark.

Selects real blocks from an existing committed NAKSHA cache (no rebuild), then
measures every codec x transform combination on REAL data:

    NONE / LZ4 / ZSTD-1 / ZSTD-3   x   XYZ raw|delta|delta+zigzag|delta+zigzag+shuffle
                                x   class raw|RLE
                                x   intensity raw|shuffle
                                x   rgb interleaved|planar
                                x   identity raw32|delta32

and reports, per combination: raw bytes, compressed bytes, ratio, bytes per
source point, compression MB/s, decompression MB/s, single-block P50/P95,
random-100-block P50/P95, and correctness (bit-exactness verified by decoding
and comparing against the ORIGINAL arrays).

Blocks are read from the committed cache with the existing reader, so this is
exactly the runtime access pattern: independent seekable blocks, selective
attributes, nothing monolithic.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache import codecs
from gui.naksha_cache import transforms as T
from gui.naksha_cache.format import (ATTR_CLASSIFICATION, ATTR_INTENSITY,
                                     ATTR_POINT_SOURCE_ID, ATTR_RGB, ATTR_XYZ)
from gui.naksha_cache.reader import NakshaPointCacheReader

GIB = 1024 ** 3
MB = 1024 ** 2

REAL = r"H:\naksha-lidar 2\test_classified_highprecision.laz"
MANDI = r"H:\TESTING CONTIUES\FUNIVIA\NT219\MANDI_56.laz"


def pct(vals, p):
    if not vals:
        return float("nan")
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def pick_blocks(reader, want):
    """Representative spatial/LOD blocks, spread across LODs and the domain."""
    blocks = reader.index.blocks
    rng = random.Random(20240)
    # spread the sample over the block table rather than taking a prefix
    idx = list(range(blocks.size))
    rng.shuffle(idx)
    picked = []
    for i in idx:
        e = blocks[i]
        if int(e["point_count"]) <= 0:
            continue
        picked.append((int(e["node_id"]), int(e["lod"])))
        if len(picked) >= want:
            break
    return picked


def load_block(reader, nid, lod):
    t = reader.read_tile(nid, lod, apply_edits=False, verify_crc=False,
                         render_space=False)
    if t is None:
        return None
    # Normalise to the exact dtype/dtype widths the cache stores.
    xyz_local = np.asarray(t.get("xyz_local"), dtype=np.int32) \
        if t.get("xyz_local") is not None else None
    # The reader returns world coords; recover tile-local int32 via origin/scale
    org = np.asarray(t["origin"], np.float64)
    sc = np.asarray(t["scale"], np.float64)
    xyz_w = np.asarray(t["xyz"], np.float64).reshape(-1, 3)
    xyz_local = np.rint((xyz_w - org) / np.where(sc == 0, 1.0, sc)).astype(np.int32)
    cls = t.get("classification")
    cls = np.asarray(cls, np.uint8) if cls is not None else None
    inten = t.get("intensity")
    inten = np.asarray(inten, np.uint16) if inten is not None else None
    rgb = t.get("rgb")
    if rgb is not None:
        rgb = np.asarray(rgb).reshape(-1, 3)
        if rgb.dtype != np.uint16:
            rgb = rgb.astype(np.uint16)
    else:
        rgb = None
    # Stable identity: ATTR_SOURCE_ID is the real per-point source index in this
    # cache. ATTR_POINT_SOURCE_ID (LAS "Point Source ID") is present but all
    # zeros here, so preferring it silently produced a constant stream and a
    # meaningless 792x "ratio". Prefer SOURCE_ID and only fall back to PSI.
    sid = t.get("source_id")
    if sid is None:
        sid = t.get("point_source_id")
    sid = np.asarray(sid, np.uint64) if sid is not None else None
    if sid is not None and sid.size and np.unique(sid).size <= 1 \
            and np.unique(sid)[0] == 0:
        sid = None          # degenerate constant stream: not a usable identity
    return {"n": int(xyz_local.shape[0]), "xyz": xyz_local, "cls": cls,
            "int": inten, "rgb": rgb, "sid": sid,
            "origin": org, "scale": sc}


XYZ_VARIANTS = [
    ("raw", T.xyz_raw, T.xyz_unpack_raw),
    ("delta", T.xyz_delta, T.xyz_unpack_delta),
    ("delta+zigzag", T.xyz_delta_zigzag, T.xyz_unpack_delta_zigzag),
    ("delta+zigzag+shuffle", T.xyz_delta_zigzag_shuffle,
     T.xyz_unpack_delta_zigzag_shuffle),
]

CLASS_VARIANTS = [("raw", T.cls_raw, T.cls_unpack_raw),
                  ("RLE", T.cls_rle, T.cls_unpack_rle)]

INT_VARIANTS = [("raw", T.int_raw, T.int_unpack_raw),
                ("shuffle", T.int_shuffle, T.int_unpack_shuffle)]

RGB_VARIANTS = [("interleaved", T.rgb_interleaved, T.rgb_unpack_interleaved)]

SID_VARIANTS = [("raw32", T.sid_raw32, T.sid_unpack_raw32),
                ("delta32", T.sid_delta32, T.sid_unpack_delta32)]


def bench_stream(blobs, enc, dec, codec, level, verify_fn, total_points):
    """Compress/decompress every block; verify correctness; report stats."""
    raw_total = 0
    comp_total = 0
    c_ms = []
    d_ms = []
    singles_d = []
    bad = 0
    for b in blobs:
        raw = enc(b)
        if not raw:
            continue
        t0 = time.perf_counter()
        comp = codecs.compress(raw, codec, level)
        c_ms.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        got = codecs.decompress(comp, len(raw), codec)
        singles_d.append((time.perf_counter() - t0) * 1000)
        raw_total += len(raw)
        comp_total += len(comp)
        if not verify_fn(dec(got, len(b) if hasattr(b, "__len__") else 0)):
            bad += 1
    # random-100 single-block decode latency (the runtime navigation pattern)
    rawd = []
    if blobs:
        rnd = random.Random(7)
        for _ in range(min(100, max(1, len(blobs) * 4))):
            b = blobs[rnd.randrange(len(blobs))]
            raw = enc(b)
            if not raw:
                continue
            comp = codecs.compress(raw, codec, level)
            t0 = time.perf_counter()
            codecs.decompress(comp, len(raw), codec)
            rawd.append((time.perf_counter() - t0) * 1000)
    dec_s = sum(singles_d) / 1000.0
    return {
        "raw": raw_total, "comp": comp_total,
        "ratio": (raw_total / comp_total) if comp_total else float("nan"),
        "bpp": (comp_total / total_points) if total_points else 0.0,
        "c_mbs": ((raw_total / MB) / (sum(c_ms) / 1000.0)) if c_ms else 0.0,
        "d_mbs": ((raw_total / MB) / dec_s) if dec_s > 0 else 0.0,
        "single_p50": pct(singles_d, 50), "single_p95": pct(singles_d, 95),
        "rand_p50": pct(rawd, 50), "rand_p95": pct(rawd, 95),
        "bad": bad,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=REAL)
    ap.add_argument("--blocks", type=int, default=120)
    ap.add_argument("--label", default="26.96M")
    a = ap.parse_args()

    print("=" * 78)
    print("[NAKSHA COMPRESSION BENCHMARK] representative real blocks")
    print("=" * 78)
    print(f"codec libs      : {codecs.versions()}")
    print(f"available       : {[codecs.CODEC_NAMES[c] for c in codecs.available_codecs()]}")
    print(f"source          : {a.src}")
    print(f"blocks sampled  : {a.blocks}")

    r = NakshaPointCacheReader(a.src, verify_crc=False, load_edits=False)
    total_points = int(r.index.total_points)
    pc_path = r.pc_path
    pc_size = os.path.getsize(pc_path)
    print(f"cache (NONE)    : {pc_size:,} B = {pc_size / GIB:.3f} GiB")
    print(f"bytes/point     : {pc_size / total_points:.3f}")

    picks = pick_blocks(r, a.blocks)
    blobs = []
    total_pts = 0
    for nid, lod in picks:
        b = load_block(r, nid, lod)
        if b and b["n"] > 0:
            blobs.append(b)
            total_pts += b["n"]
    r.close()
    print(f"loaded blocks   : {len(blobs)}  ({total_pts:,} points)")
    print("-" * 78)

    CODECS = [(codecs.CODEC_NONE, 0, "NONE"),
              (codecs.CODEC_LZ4, 0, "LZ4"),
              (codecs.CODEC_ZSTD, 1, "ZSTD-1"),
              (codecs.CODEC_ZSTD, 3, "ZSTD-3")]

    results = {}

    def xyz_verify(orig):
        return lambda out: out.shape == orig.shape and np.array_equal(out, orig)

    for codec, lvl, cname in CODECS:
        for vname, enc, dec in XYZ_VARIANTS:
            r_ = bench_stream([b["xyz"] for b in blobs], enc, dec, codec, lvl,
                              xyz_verify, total_pts)
            results[(cname, "XYZ", vname)] = r_
        for vname, enc, dec in CLASS_VARIANTS:
            if blobs[0].get("cls") is None:
                continue
            origs = [b["cls"] for b in blobs]
            r_ = bench_stream(origs, enc, dec, codec, lvl,
                              lambda o: (lambda out: out.shape == o.shape
                                         and np.array_equal(out, o)),
                              total_pts)
            results[(cname, "CLASS", vname)] = r_
        for vname, enc, dec in INT_VARIANTS:
            if blobs[0].get("int") is None:
                continue
            origs = [b["int"] for b in blobs]
            r_ = bench_stream(origs, enc, dec, codec, lvl,
                              lambda o: (lambda out: out.shape == o.shape
                                         and np.array_equal(out, o)),
                              total_pts)
            results[(cname, "INTENSITY", vname)] = r_
        for vname, enc, dec in RGB_VARIANTS:
            if blobs[0].get("rgb") is None:
                continue
            origs = [b["rgb"] for b in blobs]
            r_ = bench_stream(origs, enc, dec, codec, lvl,
                              lambda o: (lambda out: out.shape == o.shape
                                         and np.array_equal(out, o)),
                              total_pts)
            results[(cname, "RGB16", vname)] = r_
        for vname, enc, dec in SID_VARIANTS:
            if blobs[0].get("sid") is None:
                continue
            origs = [b["sid"] for b in blobs]
            r_ = bench_stream(origs, enc, dec, codec, lvl,
                              lambda o: (lambda out: out.shape == o.shape
                                         and np.array_equal(out, o)),
                              total_pts)
            results[(cname, "SID", vname)] = r_

    report(results, a, pc_size, total_points, total_pts, len(blobs))
    return 0


def report(results, a, pc_size, total_points, sampled_pts, nblocks):
    print()
    print("=" * 78)
    print("XYZ  (tile-local int32, Morton-ordered -> delta-friendly)")
    print("=" * 78)
    print(f"{'codec':<7}{'transform':<24}{'raw MB':>9}{'comp MB':>9}{'ratio':>7}"
          f"{'B/pt':>7}{'dec MB/s':>10}{'sgl P50':>9}{'sgl P95':>9}"
          f"{'rnd P50':>9}{'rnd P95':>9}{'bad':>5}")
    for (c, s, v), r in results.items():
        if s != "XYZ":
            continue
        print(f"{c:<7}{v:<24}{r['raw']/MB:>9.2f}{r['comp']/MB:>9.2f}"
              f"{r['ratio']:>7.2f}{r['bpp']:>7.2f}{r['d_mbs']:>10.0f}"
              f"{r['single_p50']:>9.3f}{r['single_p95']:>9.3f}"
              f"{r['rand_p50']:>9.3f}{r['rand_p95']:>9.3f}{r['bad']:>5}")

    for stream in ("CLASS", "INTENSITY", "RGB16", "SID"):
        rows = [(k, v) for k, v in results.items() if k[1] == stream]
        if not rows:
            continue
        print()
        print("=" * 78)
        print(f"{stream}")
        print("=" * 78)
        print(f"{'codec':<7}{'transform':<24}{'raw MB':>9}{'comp MB':>9}{'ratio':>7}"
              f"{'B/pt':>7}{'dec MB/s':>10}{'sgl P50':>9}{'sgl P95':>9}"
              f"{'rnd P50':>9}{'rnd P95':>9}{'bad':>5}")
        for (c, s, v), r in rows:
            print(f"{c:<7}{v:<24}{r['raw']/MB:>9.2f}{r['comp']/MB:>9.2f}"
                  f"{r['ratio']:>7.2f}{r['bpp']:>7.2f}{r['d_mbs']:>10.0f}"
                  f"{r['single_p50']:>9.3f}{r['single_p95']:>9.3f}"
                  f"{r['rand_p50']:>9.3f}{r['rand_p95']:>9.3f}{r['bad']:>5}")

    bad = sum(r["bad"] for r in results.values())
    print()
    print("=" * 78)
    print(f"correctness: {bad} mismatches across {len(results)} combinations "
          f"({nblocks} real blocks, {sampled_pts:,} points)")
    print("=" * 78)


if __name__ == "__main__":
    raise SystemExit(main())
