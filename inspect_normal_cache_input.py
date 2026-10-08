#!/usr/bin/env python
"""inspect_normal_cache_input.py - PART 1 GATE.

Prints the ACTUAL current cache structure before any normal code is written
against it. Normal storage must match real runtime block order, so this reads
the committed index/cache rather than assuming anything.

    python inspect_normal_cache_input.py [dataset.laz]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gui.naksha_cache.format import (  # noqa: E402
    ATTR_SOURCE_ID, ATTR_XYZ, ATTR_CLASSIFICATION, NODE_ENTRY, BLOCK_ENTRY)
from gui.naksha_cache.index import IndexReader, project_paths  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dataset", nargs="?",
                    default=str(ROOT / "test_classified_highprecision.laz"))
    args = ap.parse_args(argv)

    project = Path(args.dataset)
    idx_path, pc_path, _, _ = project_paths(str(project))
    if not Path(idx_path).is_file():
        print(f"[NORMAL CACHE INPUT] ERROR: no index at {idx_path}")
        return 3

    idx = IndexReader(idx_path)
    blocks = idx.blocks
    nodes = idx.nodes

    # ---- source points ----------------------------------------------------
    src_points = 0
    for s in getattr(idx, "sources", []):
        src_points += int(s["point_count"])

    # ---- stored points, per LOD ------------------------------------------
    per_lod_points = {}
    per_lod_blocks = {}
    for b in blocks:
        lod = int(b["lod"])
        per_lod_points[lod] = per_lod_points.get(lod, 0) + int(b["point_count"])
        per_lod_blocks[lod] = per_lod_blocks.get(lod, 0) + 1
    stored_points = int(sum(per_lod_points.values()))

    # ---- which blocks carry the stable identity stream? ------------------
    has_sid_blocks = 0
    sid_points = 0
    for b in blocks:
        if int(b["attribute_mask"]) & int(ATTR_SOURCE_ID):
            has_sid_blocks += 1
            sid_points += int(b["point_count"])

    # ---- leaves (level-0 spatial tiles) -----------------------------------
    leaves = nodes[nodes["level"] == 0] if "level" in nodes.dtype.names else nodes
    leaf_points = int(np.sum(leaves["point_count"])) if leaves.size else 0
    # Representative spacing per LOD is what the halo width must be derived
    # from, so report it rather than guessing a halo in metres.
    spacings = []
    for lvl in range(8):
        col = f"lod_spacing"
        if col in nodes.dtype.names and leaves.size:
            s = np.asarray(leaves[col][:, lvl], dtype=np.float64)
            s = s[s > 0]
            if s.size:
                spacings.append((lvl, float(np.median(s)), float(np.percentile(s, 95))))
    lvl0_spacing = None
    for lvl, med, p95 in spacings:
        if lvl == 0:
            lvl0_spacing = (med, p95)

    print("")
    print("[NORMAL CACHE INPUT]")
    print(f"source points: {src_points:,}")
    print(f"NKPC stored points including LOD copies: {stored_points:,}")
    print(f"block count: {len(blocks):,}")
    print(f"node count: {len(nodes):,}")
    print(f"LOD0 blocks: {per_lod_blocks.get(0, 0):,}")
    for lvl in range(1, 5):
        print(f"LOD{lvl} blocks: {per_lod_blocks.get(lvl, 0):,}")
    extra = sorted(l for l in per_lod_blocks if l >= 5)
    if extra:
        print(f"additional LOD levels present: {extra}")
    for lvl in sorted(per_lod_points):
        print(f"LOD{lvl} stored points: {per_lod_points[lvl]:,}")
    print(f"level-0 leaves (spatial tiles): {len(leaves):,}")
    print(f"level-0 leaf points: {leaf_points:,}")
    print(f"stable identity representation: "
          f"ATTR_SOURCE_ID = uint64 stored per point inside the block "
          f"(blocks carrying it: {has_sid_blocks:,} / {len(blocks):,}, "
          f"covering {sid_points:,} points)")
    print(f"block point-order source: "
          f"NKPC block payload, SoA streams in STREAM_ORDER; the runtime "
          f"concatenates tiles in NakshaStreamManager._flush_gpu key order")
    if lvl0_spacing:
        print(f"level-0 representative spacing (m): median={lvl0_spacing[0]:.4f} "
              f"P95={lvl0_spacing[1]:.4f}")
        print(f"=> halo width should be derived from this, not hardcoded")
    print(f"dup factor (stored / source): "
          f"{(stored_points / src_points) if src_points else float('nan'):.3f}")
    print(f"sidecar payload if block-aligned: {stored_points * 4 / 1048576:.1f} MiB")
    print(f"source-equivalent (one normal per source point): "
          f"{src_points * 4 / 1048576:.1f} MiB")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
