"""Diagnose CorruptBlock('buffer is smaller than requested size').

Stream-level (not block-level) bounds audit of the real NKPC, plus a
per-attribute direct read of any offending block. READ-ONLY: nothing here
writes, rebuilds or modifies the cache.
"""
import os
import sys
from collections import Counter

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from gui.naksha_cache.index import IndexReader, project_paths   # noqa: E402
from gui.naksha_cache.format import (                            # noqa: E402
    ATTR_NAMES, ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_INTENSITY,
    ATTR_RGB, ATTR_SOURCE_ID, PC_BLOCK_HEADER, PC_STREAM, STREAM_ORDER)
from gui.naksha_cache.readblock_new import read_block_once        # noqa: E402

SRC = os.path.join(ROOT, "test_classified_highprecision.laz")


def main():
    idx_path, pc_path, _ed, _ = project_paths(SRC)
    idx = IndexReader(idx_path)
    fs = os.path.getsize(pc_path)
    blocks = idx.blocks
    hdr_size = PC_BLOCK_HEADER.itemsize
    dir_size = PC_STREAM.itemsize * len(STREAM_ORDER)
    pstart = hdr_size + dir_size

    print("=" * 74)
    print("NKPC CORRUPT BLOCK DIAGNOSIS (read-only)")
    print("=" * 74)
    print(f"NKPC       : {os.path.basename(pc_path)}")
    print(f"file_size  : {fs:,}")
    print(f"blocks     : {blocks.size}")
    print(f"hdr {hdr_size} B + dir {dir_size} B -> pstart {pstart} B")

    fh = open(pc_path, "rb")
    offenders, zero_len = [], []
    for r in blocks:
        bid = int(r["block_id"])
        off, stored, upts = (int(r["file_offset"]), int(r["stored_bytes"]),
                             int(r["uncompressed_bytes"]))
        fh.seek(off)
        raw = fh.read(stored)
        if len(raw) < stored:
            offenders.append((bid, "SHORT_BLOCK_READ", 0, 0, 0, 0,
                              int(r["point_count"]), upts))
            continue
        directory = np.frombuffer(raw[hdr_size:pstart], dtype=PC_STREAM,
                                  count=len(STREAM_ORDER))
        for i in range(len(STREAM_ORDER)):
            s = directory[i]
            attr, nb, a = int(s["attr"]), int(s["bytes"]), int(s["offset"])
            if attr == 0:
                continue
            if nb == 0:
                zero_len.append((bid, ATTR_NAMES.get(attr, attr)))
                continue
            if a + nb > len(raw):
                offenders.append((bid, ATTR_NAMES.get(attr, attr), a, nb,
                                  len(raw), a + nb, int(r["point_count"]),
                                  upts))
    fh.close()

    print(f"\nstream-level overruns : {len(offenders)}")
    print(f"zero-length streams   : {len(zero_len)}")
    if zero_len:
        print("   by attr:", Counter(a for _b, a in zero_len).most_common())

    if not offenders:
        print("\nNO stream-level overrun found; reading EVERY block directly.")
        from gui.naksha_cache.readblock_new import read_block_once
        fails = []
        fh = open(pc_path, "rb")
        for r in blocks:
            try:
                read_block_once(fh, r, only_attrs=None, verify_crc=False)
            except Exception as exc:
                fails.append((int(r["block_id"]), int(r["node_id"]),
                              int(r["lod"]), int(r["point_count"]),
                              type(exc).__name__, str(exc)))
        fh.close()
        print(f"blocks read OK : {blocks.size - len(fails)}")
        print(f"blocks FAILED  : {len(fails)}")
        tp = 0
        for f in fails:
            tp += f[3]
            print(f"  block_id={f[0]} node_id={f[1]} lod={f[2]} "
                  f"points={f[3]:,} {f[4]}: {f[5]}")
        print(f"sum failed points = {tp:,}   "
              f"(selected-submitted = {1628096 - 1614867:,})")
        print("=" * 74)
        return 0

    print("\n[CORRUPT BLOCK] detail")
    for o in offenders:
        bid, attr, a, nb, avail, end, pts, upts = o
        print(f"  block_id={bid} attr={attr} offset={a} bytes={nb} "
              f"available={avail} offset+bytes={end} "
              f"OVERRUN={end - avail} point_count={pts:,} "
              f"uncompressed={upts:,}")

    bad_bids = sorted({o[0] for o in offenders})
    print(f"\nfailed_block_ids = {bad_bids}")
    total_pts = 0
    for bid in bad_bids:
        rows = blocks[blocks["block_id"] == bid]
        if rows.size == 0:
            continue
        r = rows[0]
        pts = int(r["point_count"])
        total_pts += pts
        print(f"  block_id={bid} node_id={int(r['node_id'])} "
              f"lod={int(r['lod'])} point_count={pts:,} "
              f"mask=0x{int(r['attribute_mask']):x} "
              f"streams={int(r['stream_count'])}")
        j = int(np.flatnonzero(blocks["block_id"] == bid)[0])
        for k in (j - 1, j, j + 1):
            if 0 <= k < blocks.size:
                q = blocks[k]
                print(f"      nb block_id={int(q['block_id'])} "
                      f"off={int(q['file_offset']):,} "
                      f"stored={int(q['stored_bytes']):,} "
                      f"upts={int(q['uncompressed_bytes']):,} "
                      f"pts={int(q['point_count']):,}")

    gap = 1628096 - 1614867
    print(f"\nsum failed point_count = {total_pts:,}")
    print(f"selected - submitted  = {gap:,}")
    print(f"do they correspond    = "
          f"{'YES' if total_pts == gap else 'NO'}")

    print("\n" + "=" * 74)
    print("PER-ATTRIBUTE DIRECT READ (production reader)")
    print("=" * 74)
    fh = open(pc_path, "rb")
    for bid in bad_bids:
        entry = blocks[blocks["block_id"] == bid][0]
        pts = int(entry["point_count"])
        print(f"\nblock_id={bid} node_id={int(entry['node_id'])} "
              f"lod={int(entry['lod'])} point_count={pts:,}")
        for label, attrs in (("XYZ", (ATTR_XYZ,)),
                             ("CLASS", (ATTR_CLASSIFICATION,)),
                             ("INTENSITY", (ATTR_INTENSITY,)),
                             ("RGB", (ATTR_RGB,)),
                             ("SOURCE_ID", (ATTR_SOURCE_ID,))):
            try:
                _hdr, streams, _nb, _raw = read_block_once(
                    fh, entry, only_attrs=attrs, verify_crc=False)
                got = {ATTR_NAMES.get(k, k): (v.shape, str(v.dtype))
                       for k, v in streams.items()}
                print(f"   {label:<11} OK   {got}")
            except Exception as exc:
                print(f"   {label:<11} FAIL {type(exc).__name__}: {exc}")
    fh.close()
    print("\n" + "=" * 74)
    print("EVERY-BLOCK DIRECT READ (production reader, full attrs)")
    print("=" * 74)
    from gui.naksha_cache.readblock_new import read_block_once        # noqa: E402
    fails = []
    fh = open(pc_path, "rb")
    for r in blocks:
        bid = int(r["block_id"])
        try:
            read_block_once(fh, r, only_attrs=None, verify_crc=False)
        except Exception as exc:
            fails.append((bid, int(r["node_id"]), int(r["lod"]),
                          int(r["point_count"]), type(exc).__name__, str(exc)))
    fh.close()
    print(f"blocks read OK : {blocks.size - len(fails)}")
    print(f"blocks FAILED  : {len(fails)}")
    for f in fails:
        print(f"  block_id={f[0]} node_id={f[1]} lod={f[2]} "
              f"points={f[3]:,} {f[4]}: {f[5]}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main())