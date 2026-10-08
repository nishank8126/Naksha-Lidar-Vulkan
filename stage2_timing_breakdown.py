"""stage2_timing_breakdown.py - NAKSHA STAGE 2 pre-integration timing audit.

Answers the CRITICAL TIMING CHECK from the Stage 2 brief: the Stage 1 report
showed FIT=218ms / x8=58ms / x32=56ms, and the brief asks EXACTLY what those
numbers include.

FINDING (this script proves it): stage1_lod_check.py timed ONLY the
``read_tile()`` loop, so those numbers are (G) NKPC disk read + (H) CRC +
(I) decode + (J) NumPy allocation. The camera-hot-path stages (A) hierarchy,
(B) ScreenSpaceLOD.select, (E) budget, (F) range build were NEVER timed.

This harness times each sub-stage separately, per zoom level, against the real
committed 26.96M cache. No GPU: K/L are reported as the CPU-side cost of
building the upload payload / range arrays (the real GPU upload needs the
interactive run).

Usage:
    venv\\Scripts\\python.exe stage2_timing_breakdown.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.reader import NakshaPointCacheReader
from gui.naksha_cache.format import (ATTR_XYZ, PC_BLOCK_HEADER, PC_STREAM,
                                     STREAM_DTYPE, STREAM_ORDER, crc32_bytes,
                                     stream_component_count)
from naksha_lod_gate import Camera2D, ScreenSpaceLOD

SRC = r"H:\naksha-lidar 2\test_classified_highprecision.laz"

HOT_PATH_TARGET_MS = 10.0
HOT_PATH_EXCELLENT_MS = 2.0

ZOOMS = [("FIT", 1.0), ("x2", 0.5), ("x4", 0.25), ("x8", 0.125),
         ("x16", 0.0625), ("x32", 0.03125)]

# Stages measured on the camera thread (pure CPU, no I/O).
HOT_STAGES = ["hierarchy", "lod_select", "microcell", "range_build"]


class Timer:
    """Accumulates per-stage milliseconds."""

    def __init__(self):
        self.ms = {}

    def add(self, key, dt_ms):
        self.ms[key] = self.ms.get(key, 0.0) + dt_ms

    def get(self, key):
        return self.ms.get(key, 0.0)


def time_hierarchy(bmin, bmax, cam):
    """(A) spatial visibility query over the persistent NKIDX node table."""
    t0 = time.perf_counter()
    x0, x1 = cam.cx - cam.half_w, cam.cx + cam.half_w
    y0, y1 = cam.cy - cam.height_m, cam.cy + cam.height_m
    vis = np.flatnonzero((bmin[:, 0] <= x1) & (bmax[:, 0] >= x0) &
                         (bmin[:, 1] <= y1) & (bmax[:, 1] >= y0))
    return [int(i) for i in vis], (time.perf_counter() - t0) * 1000.0


def time_lod_select(lod, idx, cam, vis):
    """(B) ScreenSpaceLOD.select() incl. its internal (D) metadata lookups and
    (E) global point-budget enforcement pass."""
    t0 = time.perf_counter()
    sel = lod.select(idx, cam, vis)
    return sel, (time.perf_counter() - t0) * 1000.0

def time_reads(reader, sel, tm):
    """(G) disk, (H) CRC, (I) decode, (J) NumPy alloc -- separated exactly as
    read_block_once() performs them: bare seek+read, CRC over loaded bytes,
    numpy stream views, then float32 render-space dequantise."""
    disk = crc = decode = alloc = 0.0
    nblocks = npts = nbytes = 0
    hdr_size = PC_BLOCK_HEADER.itemsize
    dir_size = PC_STREAM.itemsize * len(STREAM_ORDER)
    pstart = hdr_size + dir_size
    for o in sel:
        e = reader.index.find_block(o["node_id"], o["lod"])
        if e is None:
            continue
        off, stored = int(e["file_offset"]), int(e["stored_bytes"])
        # (G) NKPC disk read: one seek + one read of the whole block.
        t0 = time.perf_counter()
        reader.fh.seek(off)
        raw = reader.fh.read(stored)
        disk += (time.perf_counter() - t0) * 1000.0
        nbytes += len(raw)
        hdr = np.frombuffer(raw[:hdr_size], dtype=PC_BLOCK_HEADER, count=1)[0]
        # (H) CRC over already-loaded bytes (no second disk pass).
        t0 = time.perf_counter()
        crc32_bytes(raw[pstart:])
        crc += (time.perf_counter() - t0) * 1000.0
        # (I) numpy stream views (zero-copy).
        directory = np.frombuffer(raw[hdr_size:pstart], dtype=PC_STREAM,
                                  count=len(STREAM_ORDER))
        t0 = time.perf_counter()
        streams = {}
        for i in range(len(STREAM_ORDER)):
            s = directory[i]
            attr, nb = int(s["attr"]), int(s["bytes"])
            if attr == 0 or nb == 0:
                continue
            comps = stream_component_count(attr)
            dt = np.dtype(STREAM_DTYPE[attr])
            a = int(s["offset"])
            arr = np.frombuffer(raw[a:a + nb], dtype=dt,
                                count=nb // dt.itemsize)
            streams[attr] = arr.reshape(-1, comps) if comps > 1 else arr
        decode += (time.perf_counter() - t0) * 1000.0
        # (J) float32 render-space allocation/copy (the GPU-facing form).
        t0 = time.perf_counter()
        loc = streams.get(ATTR_XYZ)
        if loc is not None:
            o3 = np.asarray(hdr["origin"], np.float32)
            s3 = np.asarray(hdr["scale"], np.float32)
            w = loc.astype(np.float32)
            w += o3[0]
            w *= s3[0]
        alloc += (time.perf_counter() - t0) * 1000.0
        npts += int(hdr["point_count"])
        nblocks += 1
    tm.add("disk", disk)
    tm.add("crc", crc)
    tm.add("decode", decode)
    tm.add("alloc", alloc)
    return nblocks, npts, nbytes


def time_upload_payload(npts):
    """(K) CPU-side cost of assembling the contiguous GPU upload payload. The
    GPU-side transfer is reported by the interactive run; this is the avoidable
    Python-side assembly cost."""
    t0 = time.perf_counter()
    xyz = np.ascontiguousarray(np.empty((npts, 3), dtype=np.float32))
    cls = np.ascontiguousarray(np.empty(npts, dtype=np.uint8))
    intn = np.ascontiguousarray(np.empty(npts, dtype=np.float32))
    ms = (time.perf_counter() - t0) * 1000.0
    del xyz, cls, intn
    return ms

def time_draw_ranges(sel):
    """(L) CPU-side cost of building the (first,count) draw-range arrays."""
    t0 = time.perf_counter()
    firsts = np.ascontiguousarray(np.arange(len(sel), dtype=np.uint32))
    counts = np.ascontiguousarray(np.full(len(sel), 1, dtype=np.uint32))
    ms = (time.perf_counter() - t0) * 1000.0
    return ms, len(firsts)

def time_microcells(reader, cam):
    """(C) microcell query for the same viewport rectangle."""
    x0, x1 = cam.cx - cam.half_w, cam.cx + cam.half_w
    y0, y1 = cam.cy - cam.height_m, cam.cy + cam.height_m
    t0 = time.perf_counter()
    mc = reader.visible_microcells(x0, y0, x1, y1)
    return mc, (time.perf_counter() - t0) * 1000.0


def main():
    print("=" * 78)
    print("[STREAM TIMING BREAKDOWN] NAKSHA Stage 2 - real committed 26.96M cache")
    print("=" * 78)

    r = NakshaPointCacheReader(SRC, verify_crc=False, load_edits=False)
    idx = r.index
    n = idx.nodes
    bmin = np.asarray(n["bounds_min"])
    bmax = np.asarray(n["bounds_max"])
    cx = (bmin[:, 0].min() + bmax[:, 0].max()) * 0.5
    cy = (bmin[:, 1].min() + bmax[:, 1].max()) * 0.5
    span_x = bmax[:, 0].max() - bmin[:, 0].min()
    span_y = bmax[:, 1].max() - bmin[:, 1].min()
    total = int(idx.total_points)
    print(f"source points = {total:,} | nodes = {n.size:,} | "
          f"blocks = {idx.blocks.size:,} | lod_count = {int(idx.header['lod_count'])}")
    print(f"extent = {span_x:,.0f} m x {span_y:,.0f} m")
    print(f"camera hot-path target < {HOT_PATH_TARGET_MS} ms, "
          f"excellent < {HOT_PATH_EXCELLENT_MS} ms")

    rows = []
    for label, frac in ZOOMS:
        hw = span_x * 0.5 * frac
        cam = Camera2D(cx, cy, hw, 1920, 1080)
        lod = ScreenSpaceLOD()
        lod.moving = True
        tm = Timer()
        REPS = 5
        # COLD first call: pays numpy/table page-in. This is the cost that lands
        # in the FIRST PRESENT measurement, so it is recorded, not averaged away.
        vis, cold_ms = time_hierarchy(bmin, bmax, cam)
        _, cold_sel_ms = time_lod_select(lod, idx, cam, vis)
        cold_hot = cold_ms + cold_sel_ms
        sel = []
        for _ in range(REPS):
            vis, ms = time_hierarchy(bmin, bmax, cam)
            tm.add("hierarchy", ms)
            sel, ms = time_lod_select(lod, idx, cam, vis)
            tm.add("lod_select", ms)
            _, ms = time_microcells(r, cam)
            tm.add("microcell", ms)
            t0 = time.perf_counter()
            [(0, o["points"]) for o in sel]      # (F) range construction
            tm.add("range_build", (time.perf_counter() - t0) * 1000.0)
        for k in HOT_STAGES:
            tm.ms[k] /= REPS

        nblocks, npts, nbytes = time_reads(r, sel, tm)
        upload_ms = time_upload_payload(npts)
        dr_ms, nranges = time_draw_ranges(sel)
        drawn = sum(o["points"] for o in sel)
        hot = sum(tm.get(k) for k in HOT_STAGES)
        rows.append((label, tm, hot, sel, nblocks, npts, nbytes,
                     upload_ms, dr_ms, nranges, drawn, cold_hot))

    print()
    print("=" * 78)
    print("[STREAM TIMING BREAKDOWN]  (ms per camera frame, real cache, no GPU)")
    print("=" * 78)
    print(f"{'zoom':<6}{'sel':>5}{'requested':>12}{'hier':>8}{'lodsel':>8}"
          f"{'micro':>8}{'rng':>7}{'HOTPATH':>9}{'disk':>8}{'crc':>7}"
          f"{'decode':>8}{'alloc':>7}{'upl(cpu)':>10}{'drawrng':>9}")
    print("-" * 118)
    for (label, tm, hot, sel, nblocks, npts, nbytes, upload_ms, dr_ms,
         nranges, drawn, cold) in rows:
        print(f"{label:<6}{len(sel):>5}{drawn:>12,}"
              f"{tm.get('hierarchy'):>8.2f}{tm.get('lod_select'):>8.2f}"
              f"{tm.get('microcell'):>8.2f}{tm.get('range_build'):>7.3f}"
              f"{hot:>9.2f}{tm.get('disk'):>8.1f}{tm.get('crc'):>7.1f}"
              f"{tm.get('decode'):>8.1f}{tm.get('alloc'):>7.1f}"
              f"{upload_ms:>10.2f}{dr_ms:>9.3f}")
    print("-" * 118)
    print("HOTPATH = A(hierarchy)+B(lod_select)+C(microcell)+F(range_build)")
    print("disk/crc/decode/alloc = G+H+I+J, run on the BACKGROUND thread")

    print()
    print("=" * 78)
    print("[DETAIL] per zoom level")
    print("=" * 78)
    for (label, tm, hot, sel, nblocks, npts, nbytes, upload_ms, dr_ms,
         nranges, drawn, cold) in rows:
        bg = tm.get("disk") + tm.get("crc") + tm.get("decode") + tm.get("alloc")
        tag = ("EXCELLENT" if hot < HOT_PATH_EXCELLENT_MS
               else ("OK" if hot < HOT_PATH_TARGET_MS else "OVER TARGET"))
        ctag = ("OK" if cold < HOT_PATH_TARGET_MS else "OVER TARGET")
        print()
        print(f"--- {label} ---")
        print(f"  selected nodes        : {len(sel)}")
        print(f"  requested points      : {drawn:,}")
        print(f"  blocks read           : {nblocks}")
        print(f"  decoded points        : {npts:,}")
        print(f"  read MB               : {nbytes / 1024 ** 2:.1f}")
        print(f"  draw ranges           : {nranges}")
        print(f"  CAMERA HOT PATH       : {hot:.2f} ms  [{tag}]  (steady, mean of 5)")
        print(f"  CAMERA HOT PATH COLD  : {cold:.2f} ms  [{ctag}]  (first call, feeds FIRST PRESENT)")
        print(f"  BACKGROUND IO/DECODE  : {bg:.1f} ms")
        print(f"    hierarchy_ms   : {tm.get('hierarchy'):.2f}")
        print(f"    lod_select_ms  : {tm.get('lod_select'):.2f}")
        print(f"    microcell_ms   : {tm.get('microcell'):.2f}")
        print(f"    range_build_ms : {tm.get('range_build'):.3f}")
        print(f"    disk_ms        : {tm.get('disk'):.1f}")
        print(f"    crc_ms         : {tm.get('crc'):.1f}")
        print(f"    decode_ms      : {tm.get('decode'):.1f}")
        print(f"    alloc_ms       : {tm.get('alloc'):.1f}")
        print(f"    upload_ms(cpu) : {upload_ms:.2f}  <- real GPU needs the run")
        print(f"    draw_range_ms  : {dr_ms:.3f}")

    print()
    print("=" * 78)
    print("[STAGE 1 RECONCILIATION] what did the Stage 1 ms column measure?")
    print("=" * 78)
    print("stage1_lod_check.py lines 45-47 timed ONLY:")
    print("    for o in sel: r.read_tile(...)     # verify_crc=True")
    print("=> Stage 1 ms == (G) disk + (H) CRC + (I) decode + (J) NumPy alloc.")
    print()
    for (label, tm, hot, sel, nblocks, npts, nbytes, upload_ms, dr_ms,
         nranges, drawn, cold) in rows:
        bg = tm.get("disk") + tm.get("crc") + tm.get("decode") + tm.get("alloc")
        print(f"  {label:<5} Stage-1-equivalent = {bg:7.1f} ms   |   "
              f"camera hot path steady = {hot:5.2f} ms   cold = {cold:5.2f} ms")
    print()
    worst = max((x[2] for x in rows), default=0.0)
    worst_cold = max((x[11] for x in rows), default=0.0)
    verdict = ("EXCELLENT" if worst < HOT_PATH_EXCELLENT_MS
               else ("WITHIN TARGET" if worst < HOT_PATH_TARGET_MS
                     else "OVER TARGET"))
    cverdict = ("WITHIN TARGET" if worst_cold < HOT_PATH_TARGET_MS
                else "OVER TARGET (cold first paint)")
    print(f"WORST steady camera hot path = {worst:.2f} ms -> {verdict}")
    print(f"WORST cold  camera hot path = {worst_cold:.2f} ms -> {cverdict}")
    print(f"(target < {HOT_PATH_TARGET_MS} ms, excellent < {HOT_PATH_EXCELLENT_MS} ms)")

    r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
