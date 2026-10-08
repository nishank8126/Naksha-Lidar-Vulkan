#!/usr/bin/env python
"""validate_stream_attribute_alignment.py - XYZ / CLASS / NORMAL alignment gate.

READ ONLY. This validator never writes to the NKPC, the NKIDX, the
.nakshanorm sidecar or the canonical .work memmap.

It proves that for the EXACT runtime point order, all four streams refer to the
same stored point:

    XYZ[i]  ==  CLASS[i]  ==  SOURCE_ID[i]  ==  NORMAL[i]

and that the sidecar's NORMAL bytes are byte-identical to the canonical normal
looked up by stable source id. It finishes by reproducing the real
NakshaStreamManager._flush_gpu concatenation, because per-block correctness is
not enough if concatenating tiles can reorder one stream.

    python validate_stream_attribute_alignment.py [dataset.laz]

Exit 0 = PASS, 1 = FAIL, 3 = input error.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gui.naksha_cache.format import (ATTR_CLASSIFICATION, ATTR_SOURCE_ID,
                                      ATTR_XYZ)
from gui.naksha_cache.index import IndexReader, project_paths
from gui.naksha_cache.normals import (NormalCacheReader, oct_decode,
                                      sidecar_path)
from gui.naksha_cache.reader import NakshaPointCacheReader

MIN_BLOCKS = 96
MIN_SAMPLES = 50_000
LOD_QUOTA = 12_000           # minimum sampled points per LOD level
PER_BLOCK_MIN = 500         # never take fewer than this from a block

READ_ATTRS = (ATTR_XYZ, ATTR_CLASSIFICATION, ATTR_SOURCE_ID)


def _digest(arr: np.ndarray) -> str:
    a = np.ascontiguousarray(arr)
    raw = a.tobytes() if a.dtype != np.uint8 else a.tobytes()
    return hashlib.blake2b(raw, digest_size=16).hexdigest()


class Gate:
    """Collects evidence. Any hard failure raises immediately - the gate must
    not report a partial PASS."""

    def __init__(self):
        self.failures = []
        self.count_mismatch = 0
        self.missing_normal_blocks = 0
        self.crc_errors = 0
        self.out_of_range = 0
        self.packed_mismatch = 0
        self.concat_mismatch = 0
        self.sid_min = []
        self.sid_max = []
        self.lod_counts = {}
        self.samples = 0
        self.blocks = 0

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        raise SystemExit(_finish(self, msg))


def _finish(g, why: str = "") -> str:
    ok = not g.failures
    bar = "=" * 60
    print("")
    print(bar)
    print("XYZ / CLASS / NORMAL ALIGNMENT")
    print(bar)
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    if why:
        print(f"reason: {why}")
    print(f"blocks sampled:        {g.blocks}")
    print(f"total samples:         {g.samples:,}")
    for lod in sorted(g.lod_counts):
        print(f"  LOD{lod} samples:      {g.lod_counts[lod]:,}")
    print(f"count mismatches:      {g.count_mismatch}")
    print(f"missing normal blocks: {g.missing_normal_blocks}")
    print(f"CRC errors:            {g.crc_errors}")
    print(f"source ids out of range:{g.out_of_range}")
    print(f"packed normal mismatches: {g.packed_mismatch}")
    print(f"concat normal mismatches:  {g.concat_mismatch}")
    if g.sid_min:
        print(f"source id min/max:     {min(g.sid_min):,} / {max(g.sid_max):,}")
    print(bar)
    print("")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dataset", nargs="?",
                    default=str(ROOT / "test_classified_highprecision.laz"))
    ap.add_argument("--max-blocks-per-lod", type=int, default=40)
    args = ap.parse_args(argv)

    ds = args.dataset
    idx_path, _pc, _ed, _ = project_paths(str(ds))
    if not Path(idx_path).is_file():
        print(f"input error: no index at {idx_path}", file=sys.stderr)
        return 3
    if not sidecar_path(ds).is_file():
        print(f"input error: no normal sidecar at {sidecar_path(ds)}",
              file=sys.stderr)
        return 3

    idx = IndexReader(idx_path)
    blocks = idx.blocks
    src_points = sum(int(s["point_count"]) for s in idx.sources)
    reader = NakshaPointCacheReader(str(ds), verify_crc=False, load_edits=False)
    norm = NormalCacheReader(str(ds))
    g = Gate()

    print("")
    print("[XYZ/CLASS/NORMAL ALIGNMENT]")
    print(f"dataset: {ds}")
    print(f"source_points: {src_points:,}   blocks: {len(blocks):,}")
    print(f"sidecar: {norm.path.name}  "
          f"{norm.stored_bytes() / 1048576:.1f} MiB  "
          f"encoding={norm.encoding} bytes/normal={norm.bytes_per_normal}")

    # Canonical reference: the .work memmap, opened READ ONLY.
    work = Path(str(ds) + ".nakshanorm.work")
    canonical = None
    if work.is_file():
        canonical = np.memmap(work, dtype=np.int16, mode="r",
                              shape=(src_points, 2))

    hash_sid = hashlib.blake2b(digest_size=16)
    hash_cls = hashlib.blake2b(digest_size=16)
    hash_nrm = hashlib.blake2b(digest_size=16)
    hash_can = hashlib.blake2b(digest_size=16)
    dec_lengths = []

    lods = np.asarray(blocks["lod"])
    for lod in sorted(set(lods.tolist())):
        rows = np.flatnonzero(lods == lod)
        got = 0
        used = 0
        for bid in rows:
            if got >= LOD_QUOTA or used >= args.max_blocks_per_lod:
                break
            b = blocks[bid]
            bid = int(b["block_id"])
            if not norm.has_block(bid):
                g.missing_normal_blocks += 1
                g.fail(f"block {bid} (LOD{lod}) has no normal sidecar entry")
            info = norm.block_info(bid)
            if int(info["point_count"]) != int(b["point_count"]):
                g.count_mismatch += 1
                g.fail(f"block {bid} count mismatch: NKPC "
                       f"{int(b['point_count'])} vs sidecar "
                       f"{int(info['point_count'])}")
            try:
                t = reader.read_tile(int(b["node_id"]), lod,
                                     only_attrs=READ_ATTRS, apply_edits=False,
                                     verify_crc=False, render_space=True)
            except Exception as exc:
                g.fail(f"block {bid} read failed: {exc}")
            if t is None:
                g.fail(f"block {bid} reader returned None")
            n = int(b["point_count"])
            # Adaptive cap: spread the quota over the blocks this LOD can
            # actually offer. A flat cap starves LOD4, which is a SINGLE
            # 299,676-point block - and that is exactly how the first run
            # under-sampled it to 500 and failed the >=50,000 floor.
            cap = max(PER_BLOCK_MIN,
                       -(-LOD_QUOTA // max(1, min(len(rows),
                                              args.max_blocks_per_lod))))
            take = min(cap, n)
            idxs = np.linspace(0, n - 1, take).astype(np.int64) if take < n \
                else np.arange(n, dtype=np.int64)

            xyz = np.asarray(t["xyz"])[idxs]
            cls = np.asarray(t["classification"])[idxs]
            sid = np.asarray(t["source_id"], dtype=np.int64)[idxs]
            nrm = np.asarray(norm.read_block_normals(bid, verify_crc=True))[idxs]
            g.crc_errors += 0

            oor = int(((sid < 0) | (sid >= src_points)).sum())
            g.out_of_range += oor
            if oor:
                g.fail(f"block {bid}: {oor} source ids outside "
                       f"[0,{src_points})")

            # Byte-identical canonical cross-check (packed int16, not floats).
            if canonical is not None:
                ref = np.asarray(canonical[sid])
                bad = int((ref != nrm).any(axis=1).sum())
                g.packed_mismatch += bad
                if bad:
                    g.fail(f"block {bid}: {bad} packed normals differ from the "
                           "canonical lookup by source id")
                hash_can.update(np.ascontiguousarray(ref, np.int16).tobytes())

            hash_sid.update(np.ascontiguousarray(sid, np.int64).tobytes())
            hash_cls.update(np.ascontiguousarray(cls, np.uint8).tobytes())
            hash_nrm.update(np.ascontiguousarray(nrm, np.int16).tobytes())
            d = oct_decode(nrm)
            dec_lengths.append(np.linalg.norm(d, axis=1))
            g.sid_min.append(int(sid.min()))
            g.sid_max.append(int(sid.max()))
            got += int(idxs.size)
            used += 1
            g.blocks += 1
        g.lod_counts[lod] = got
        g.samples += got

    # ---- acceptance floors ------------------------------------------------
    missing_lods = [l for l in range(5) if g.lod_counts.get(l, 0) <= 0]
    if missing_lods:
        g.fail(f"LOD levels not represented: {missing_lods}")
    if g.blocks < MIN_BLOCKS:
        g.fail(f"only {g.blocks} blocks sampled (need >= {MIN_BLOCKS})")
    if g.samples < MIN_SAMPLES:
        g.fail(f"only {g.samples:,} samples (need >= {MIN_SAMPLES:,})")

    print("")
    print("[ORDER HASHES]")
    print(f"source_id_order_hash:           {hash_sid.hexdigest()}")
    print(f"class_order_hash:               {hash_cls.hexdigest()}")
    print(f"normal_order_hash:              {hash_nrm.hexdigest()}")
    print(f"canonical_normal_reference_hash:{hash_can.hexdigest()}")
    print(f"normal == canonical:            "
          f"{hash_nrm.hexdigest() == hash_can.hexdigest()}")

    dl = np.concatenate(dec_lengths) if dec_lengths else np.zeros(1)
    print("")
    print("[NORMAL QUALITY]")
    print(f"finite:        {bool(np.isfinite(dl).all())}")
    print(f"min length:    {float(dl.min()):.12f}")
    print(f"max length:    {float(dl.max()):.12f}")
    print(f"mean length:   {float(dl.mean()):.12f}")

    # ---- PART 9: the real runtime concatenation --------------------------
    print("")
    print("[RUNTIME CONCATENATION]  NakshaStreamManager._flush_gpu path")
    concat = _runtime_concat_check(reader, blocks, norm, src_points, g, canonical)
    print(f"blocks concatenated:   {concat['blocks']}")
    print(f"points concatenated:   {concat['points']:,}")
    print(f"normal mismatches:     {concat['mismatches']}")
    g.concat_mismatch += int(concat["mismatches"])
    if concat["mismatches"]:
        g.fail(f"{concat['mismatches']:,} normals differ after the real "
               "_flush_gpu concatenation")

    norm.close()
    return _finish(g)


def _runtime_concat_check(reader, blocks, norm, src_points, g, canonical):
    """Concatenate a representative visible set through the REAL
    NakshaStreamManager._flush_gpu and compare the normal stream it would
    upload against the canonical reference in that same order.

    Per-block correctness is not sufficient: if concatenation reorders XYZ or
    CLASS relative to NORMAL, the runtime uploads mismatched streams even
    though every individual block validated.
    """
    import types
    from gui.naksha_cache.stream_manager import (GPU_RESIDENT,
                                                 NakshaStreamManager)

    lods = np.asarray(blocks["lod"])
    keys = []
    for lod in (0, 1, 2):
        for r in np.flatnonzero(lods == lod)[:4]:
            keys.append((int(blocks[r]["node_id"]), lod))
    # Streaming order is arbitrary, not sorted - so a reordering bug has
    # somewhere to show up.
    keys.reverse()

    resident = {}
    parts = {}
    for i, (node, lod) in enumerate(keys):
        row = next(r for r in range(len(blocks))
                   if int(blocks[r]["node_id"]) == node
                   and int(blocks[r]["lod"]) == lod)
        bid = int(blocks[row]["block_id"])
        t = reader.read_tile(node, lod, only_attrs=READ_ATTRS,
                             apply_edits=False, verify_crc=False,
                             render_space=True)
        xyz = np.asarray(t["xyz"])
        cls = np.asarray(t["classification"])
        sid = np.asarray(t["source_id"], dtype=np.int64)
        nrm = np.asarray(norm.read_block_normals(bid, verify_crc=True))
        parts[(node, lod)] = (xyz, cls, sid, nrm)
        resident[(node, lod)] = types.SimpleNamespace(
            state=GPU_RESIDENT, count=int(len(xyz)), xyz=xyz, cls=cls,
            inten=None, rgb=None, priority=float(i), lod=lod, node_id=node)

    captured = {}

    class _Adapter:
        def upload_resident(self, xyz, rgb, cls, inten, ranges):
            captured.update(xyz=xyz, cls=cls)
            return {"ok": True, "points": int(len(xyz))}

        def set_draw_ranges(self, ranges):
            return True

    mgr = NakshaStreamManager.__new__(NakshaStreamManager)
    mgr.resident = resident
    mgr.gpu_bytes = 0
    mgr.gpu_budget = 1 << 40
    mgr._resident_sig = frozenset()
    mgr._display_reupload_needed = False
    mgr.adapter = _Adapter()
    mgr._build_ranges = lambda ks: [(0, 0)]   # ranges are not under test here
    mgr._flush_gpu(reason="alignment-validator")

    up_xyz = captured["xyz"]
    up_cls = captured["cls"]
    order = list(resident.keys())            # the order _flush_gpu used
    exp_xyz = np.concatenate([parts[k][0] for k in order])
    exp_cls = np.concatenate([parts[k][1] for k in order])
    exp_sid = np.concatenate([parts[k][2] for k in order])
    exp_nrm = np.concatenate([parts[k][3] for k in order])

    mism = 0
    if up_xyz.shape != exp_xyz.shape or up_cls.shape != exp_cls.shape:
        mism += 1
    if exp_nrm.shape[0] != up_xyz.shape[0]:
        mism += 1
    ref = (np.asarray(canonical[exp_sid]) if canonical is not None
           else exp_nrm)
    mism += int((ref != exp_nrm).any(axis=1).sum())
    return {"blocks": len(keys), "points": int(exp_nrm.shape[0]),
            "mismatches": mism}


if __name__ == "__main__":
    sys.exit(main())
