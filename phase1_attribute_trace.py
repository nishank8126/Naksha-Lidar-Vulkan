"""PHASE 1A - [DATASET ATTRIBUTES]: what is ACTUALLY cached, not assumed.

READ ONLY. Opens the committed .nakshaidx + .nakshapc and reports, from the
REAL cache headers and REAL decoded block bytes:

    [DATASET ATTRIBUTES]
    XYZ: CLASS: INTENSITY: RGB:
    source_classification_dtype:
    source_intensity_dtype:
    class_range:
    intensity_range:
    elevation_range:

1A of PHASE 1 is "Do not assume a stream exists." Every claim about Class /
Intensity / Elevation support is derived from the cache itself, so the
downstream work cannot be built on a fabricated stream.

Usage:  python phase1_attribute_trace.py [dataset.las ...]
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gui.naksha_cache.format import (ATTR_CLASSIFICATION, ATTR_INTENSITY,  # noqa: E402
                                     ATTR_NAMES, ATTR_RGB, ATTR_XYZ)
from gui.naksha_cache.reader import NakshaPointCacheReader  # noqa: E402


def _mask_names(mask: int):
    return [n for b, n in ATTR_NAMES.items() if mask & b]


def _sample_blocks(idx, dataset, limit=12):
    """Decode a bounded, deterministic sample of REAL blocks."""
    reader = NakshaPointCacheReader(dataset, verify_crc=False,
                                    load_edits=False)
    n = int(idx.blocks.size)
    rows = sorted(set(int(i) for i in np.linspace(
        0, max(n - 1, 0), num=min(n, limit)).astype(int)))
    cls_all, int_all, xyz_all = [], [], []
    ms = 0.0
    for row in rows:
        e = idx.blocks[row]
        try:
            t0 = time.perf_counter()
            t = reader.read_tile(int(e["node_id"]), int(e["lod"]),
                                 only_attrs=(ATTR_XYZ, ATTR_CLASSIFICATION,
                                             ATTR_INTENSITY, ATTR_RGB),
                                 apply_edits=False, verify_crc=False,
                                 render_space=True)
            ms += (time.perf_counter() - t0) * 1000.0
        except Exception as exc:                                  # noqa: BLE001
            print(f"  ! block row {row}: {exc!r}")
            continue
        if t is None:
            continue
        for key, sink in (("xyz", xyz_all), ("classification", cls_all),
                          ("intensity", int_all)):
            if t.get(key) is not None:
                sink.append(np.asarray(t[key]))
    reader.close()
    return rows, xyz_all, cls_all, int_all, ms


def trace(dataset: str) -> dict:
    from gui.naksha_cache.index import IndexReader

    idx_path, pc_path = dataset + ".nakshaidx", dataset + ".nakshapc"
    print("=" * 78)
    print(f"  source : {dataset}")
    for p in (idx_path, pc_path):
        sz = os.path.getsize(p) if os.path.isfile(p) else 0
        print(f"  file   : {os.path.basename(p)} ({sz:,} B)")
    print("=" * 78)

    idx = IndexReader(idx_path)
    hdr = idx.header
    mask = int(hdr["attribute_mask"])
    out = {"dataset": dataset, "attribute_mask": mask,
           "mask_names": _mask_names(mask),
           "total_points": int(idx.total_points),
           "nodes": int(hdr["node_count"]), "blocks": int(idx.blocks.size)}

    b = idx.blocks
    for key in ("attr_mask", "attribute_mask", "attrs"):
        if b.size and key in (b.dtype.names or ()):
            m = int(np.bitwise_or.reduce(b[key].astype(np.int64)))
            out["block_attr_mask_union"] = m
            out["block_attr_mask_names"] = _mask_names(m)
            break

    rows, xyz_l, cls_l, int_l, ms = _sample_blocks(idx, dataset)
    cls = np.concatenate(cls_l) if cls_l else None
    inten = np.concatenate(int_l) if int_l else None
    xyz = np.concatenate(xyz_l) if xyz_l else None

    out["blocks_sampled"] = len(rows)
    out["sample_points"] = int(0 if xyz is None else xyz.shape[0])
    out["sample_read_ms"] = round(ms, 2)
    out["XYZ"] = "PRESENT" if xyz is not None else "ABSENT"
    out["CLASS"] = "PRESENT" if cls is not None else "ABSENT"
    out["INTENSITY"] = "PRESENT" if inten is not None else "ABSENT"
    out["RGB"] = "PRESENT" if (mask & ATTR_RGB) else "ABSENT"
    out["source_classification_dtype"] = (str(cls.dtype) if cls is not None else "n/a")
    out["source_intensity_dtype"] = (str(inten.dtype) if inten is not None else "n/a")

    if cls is not None and cls.size:
        uniq, cnt = np.unique(cls.astype(np.int64), return_counts=True)
        out["class_range"] = (int(cls.min()), int(cls.max()))
        out["class_unique_ids"] = [int(v) for v in uniq[:32]]
        out["class_histogram"] = {int(u): int(c)
                                  for u, c in zip(uniq[:32], cnt[:32])}
    else:
        out["class_range"] = None
        out["class_unique_ids"] = []
        out["class_histogram"] = {}

    if inten is not None and inten.size:
        nz = inten[inten != 0]
        out["intensity_range"] = (float(inten.min()), float(inten.max()))
        out["intensity_nonzero_range"] = (
            (float(nz.min()), float(nz.max())) if nz.size else None)
        out["intensity_zero_fraction"] = round(float((inten == 0).mean()), 4)
    else:
        out["intensity_range"] = None
        out["intensity_nonzero_range"] = None
        out["intensity_zero_fraction"] = None

    if xyz is not None and xyz.size:
        z = xyz[:, 2]
        out["elevation_range"] = (float(z.min()), float(z.max()))
        out["xyz_range"] = ((float(xyz[:, 0].min()), float(xyz[:, 0].max())),
                            (float(xyz[:, 1].min()), float(xyz[:, 1].max())))
    else:
        out["elevation_range"] = None
        out["xyz_range"] = None
    idx.close()
    _print_report(out)
    return out


def _print_report(o: dict) -> None:
    print("[DATASET ATTRIBUTES]")
    print(f"  XYZ: {o['XYZ']}")
    print(f"  CLASS: {o['CLASS']}")
    print(f"  INTENSITY: {o['INTENSITY']}")
    print(f"  RGB: {o['RGB']}")
    print(f"  source_classification_dtype: {o['source_classification_dtype']}")
    print(f"  source_intensity_dtype: {o['source_intensity_dtype']}")
    print(f"  class_range: {o['class_range']}")
    print(f"  intensity_range: {o['intensity_range']}")
    print(f"  elevation_range: {o['elevation_range']}")
    print()
    print(f"  attribute_mask  : {o['attribute_mask']} "
          f"(0x{o['attribute_mask']:x}) -> {', '.join(o['mask_names'])}")
    if "block_attr_mask_union" in o:
        print(f"  block attr mask : {o['block_attr_mask_union']} "
              f"(0x{o['block_attr_mask_union']:x}) -> "
              f"{', '.join(o['block_attr_mask_names'])}")
    print(f"  index totals    : {o['total_points']:,} pts / {o['nodes']} nodes "
          f"/ {o['blocks']} blocks")
    print(f"  blocks sampled  : {o['blocks_sampled']} "
          f"({o['sample_points']:,} pts, {o['sample_read_ms']} ms)")
    if o["class_unique_ids"]:
        top = sorted(o["class_histogram"].items(), key=lambda kv: -kv[1])[:12]
        print("  class histogram : " + ", ".join(f"{k}:{v:,}" for k, v in top))
    if o.get("intensity_nonzero_range"):
        print(f"  intensity nz    : {o['intensity_nonzero_range']} "
              f"(zeros {o['intensity_zero_fraction'] * 100:.1f}%)")
    if o.get("xyz_range"):
        print(f"  xyz_range       : x{o['xyz_range'][0]} y{o['xyz_range'][1]}")
    print()


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv:
        argv = [os.path.join(ROOT, "123.las")]
    ok = []
    for ds in argv:
        try:
            ok.append(trace(ds))
        except Exception as exc:                                  # noqa: BLE001
            print(f"FAIL {ds}: {exc!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
