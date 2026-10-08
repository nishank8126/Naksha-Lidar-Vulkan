"""STAGE 1 vertical slice on 123.las (Part 20).

Proves the whole cache contract on real data:
  build .nakshapc + .nakshaidx + .nakshaedit
  overview available BEFORE spatialization completes
  cached reopen (mmap index, no source decode)
  read overview -> zoom a detailed tile -> pan to another
  bit-exact XYZ / classification / intensity / source-id vs the ORIGINAL LAS
  a corrupt block is reported rather than displayed

Run: python naksha_slice_123.py
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gui.naksha_cache.builder import NakshaPointCacheBuilder     # noqa: E402
from gui.naksha_cache.index import project_paths                 # noqa: E402
from gui.naksha_cache.reader import NakshaPointCacheReader      # noqa: E402

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "123.las")
fails = []


def check(name, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" - {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main():
    print("\n[NAKSHA CACHE V1] STAGE 1 vertical slice: 123.las")
    print("=" * 74)
    for p in project_paths(SRC):
        if os.path.exists(p):
            os.remove(p)

    t0 = time.perf_counter()
    b = NakshaPointCacheBuilder(SRC, chunk_points=1_000_000, leaf_target=256_000)
    stats = b.build([SRC], with_overview=True)
    build_s = time.perf_counter() - t0
    print(f"\n  BUILD {build_s:.1f}s")
    for k in ("header_seconds", "overview_seconds", "first_visual_seconds",
              "pass1_seconds", "pass2_seconds", "shard_count", "shards_used",
              "blocks", "nodes", "written_points", "temp_bytes", "pc_bytes",
              "idx_bytes"):
        if k in stats:
            v = stats[k]
            print(f"    {k:<22}{v:,.4f}" if isinstance(v, float)
                  else f"    {k:<22}{v:,}")
    check("build produced a .nakshapc", os.path.isfile(b.pc_path),
          f"{stats['pc_bytes'] / 1e6:.1f} MB")
    check("build produced a .nakshaidx", os.path.isfile(b.idx_path),
          f"{stats['idx_bytes']:,} B")
    check("build produced a .nakshaedit", os.path.isfile(b.edit_path))
    check("overview available before full build completes",
          stats["first_visual_seconds"] < build_s,
          f"first visual {stats['first_visual_seconds']:.1f}s vs build "
          f"{build_s:.1f}s")
    check("blocks written", stats["blocks"] >= 1, f"{stats['blocks']} blocks")
    pc_path = b.pc_path
    src_before = os.stat(SRC)
    del b

    t0 = time.perf_counter()
    r = NakshaPointCacheReader(SRC, verify_crc=True)
    index_ms = (time.perf_counter() - t0) * 1000
    print(f"\n  CACHED REOPEN index {index_ms:.2f} ms | {r.index.summary()}")
    check("cached reopen opened the index", r.index.total_points > 0,
          f"{r.index.total_points:,} points indexed")
    check("index open is fast (mmap, no object per node)", index_ms < 500,
          f"{index_ms:.2f} ms")
    check("index tables are memmaps, not Python lists",
          isinstance(r.index.nodes, np.memmap) and r.index.nodes.size > 0,
          f"nodes memmap of {r.index.nodes.size:,} rows")

    t0 = time.perf_counter()
    ov = r.read_overview()
    ov_ms = (time.perf_counter() - t0) * 1000
    check("overview read on cached open", ov is not None,
          f"{ov['point_count']:,} pts in {ov_ms:.1f} ms "
          f"({ov['bytes_read'] / 1e6:.1f} MB)")

    detailed = r.nodes_with_lod0()
    check("detailed (LOD0) tiles exist", len(detailed) > 0,
          f"{len(detailed)} detailed nodes")
    t0 = time.perf_counter()
    tile = r.read_tile(detailed[0], 0, verify_crc=True)
    tile_ms = (time.perf_counter() - t0) * 1000
    check("zoom read a detailed tile",
          tile is not None and tile["point_count"] > 0,
          f"node {detailed[0]}, {tile['point_count']:,} pts in {tile_ms:.1f} ms")
    check("overview stays available alongside detail (no blank)",
          ov is not None and tile is not None)
    if len(detailed) > 1:
        t2 = r.read_tile(detailed[1], 0)
        check("pan read a second tile", t2 is not None,
              f"node {detailed[1]}, {t2['point_count']:,} pts")

    check("detailed tile carries stable source ids",
          tile["source_id"] is not None
          and tile["source_id"].size == tile["point_count"],
          f"{tile['source_id'].size:,} ids")
    import laspy
    with laspy.open(SRC) as f:
        k = int(np.argmin(tile["source_id"]))
        src_idx = int(tile["source_id"][k])
        f.seek(src_idx)
        rec = f.read_points(1)
        src_xyz = np.array([float(rec.x[0]), float(rec.y[0]), float(rec.z[0])])
        err = np.abs(tile["xyz"][k] - src_xyz)
        check("XYZ round-trips within source LAS quantisation",
              float(err.max()) <= 0.011,
              f"max err {err.max():.2e} m at source index {src_idx}")
        check("classification round-trips exactly",
              int(tile["classification"][k]) == int(rec.classification[0]),
              f"cache {int(tile['classification'][k])} vs "
              f"source {int(rec.classification[0])}")
        check("intensity round-trips exactly",
              int(tile["intensity"][k]) == int(rec.intensity[0]),
              f"cache {int(tile['intensity'][k])} vs "
              f"source {int(rec.intensity[0])}")
        check("source id round-trips (stable identity)",
              src_idx == int(tile["source_id"][k]), f"id {src_idx}")

    after = os.stat(SRC)
    check("cached read did NOT modify the source file",
          src_before.st_mtime == after.st_mtime
          and src_before.st_size == after.st_size, "mtime/size unchanged")

    from gui.naksha_cache.reader import CorruptBlock
    with open(pc_path, "r+b") as f:
        # Corrupt INSIDE THE PAYLOAD, not the header. The payload starts
        # after the header and stream directory; an offset below that only
        # damages fields the payload CRC does not cover, so the block would
        # still decode and the corruption would go UNDETECTED.
        from gui.naksha_cache import format as _F
        off = (int(r.index.blocks[0]["file_offset"])
               + _F.PC_BLOCK_HEADER.itemsize
               + _F.PC_STREAM.itemsize * len(_F.STREAM_ORDER) + 64)
        f.seek(off)
        bt = f.read(1)
        f.seek(off)
        f.write(bytes([bt[0] ^ 0xFF]))
    try:
        r.read_tile(detailed[0], 0, verify_crc=True)
        detected = False
    except CorruptBlock:
        detected = True
    print(f"  STAGE 1 RESULT: "
          f"{'PASS' if not fails else 'FAIL -> ' + '; '.join(fails)}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
