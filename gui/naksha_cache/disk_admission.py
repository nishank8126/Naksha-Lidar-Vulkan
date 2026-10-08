"""Shared, per-filesystem build admission. No disk override exists.

Free space already excludes existing production generations. The new payload
and all shards coexist until commit; shards are never credited as deleted.
"""
from __future__ import annotations

import math
import os
import shutil
import time

GIB = 1024 ** 3
MEASURED_POINTS = 268_809_706
MEASURED_PC_BYTES = 9_113_670_032
MEASURED_INDEX_BYTES = 4_780_540
MEASURED_SHARD_BYTES = 12_365_246_476


class DiskAdmissionError(RuntimeError):
    def __init__(self, report):
        self.report = report
        super().__init__(format_report(report))


def existing_parent(path):
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            raise ValueError("no existing filesystem for " + path)
        p = parent
    return p


def filesystem(path):
    p = existing_parent(path)
    # st_dev identifies volumes on Windows too, including mounted volumes.
    return str(os.stat(p).st_dev), p


def measured_projection(points):
    """Historical basis for the standalone analysis, shared in one place."""
    return {"final_pc": math.ceil(points * MEASURED_PC_BYTES / MEASURED_POINTS),
            "index": math.ceil(points * MEASURED_INDEX_BYTES / MEASURED_POINTS),
            "shards": math.ceil(points * MEASURED_SHARD_BYTES / MEASURED_POINTS)}


def estimate_build(points, source_count, *, shard_itemsize=46, lod_levels=7,
                   overview_target=300_000, rgb_bits=16, have_rgb=True):
    from .format import (BLOCK_ENTRY, NODE_ENTRY, SOURCE_ENTRY,
                         PC_BLOCK_HEADER, PC_STREAM, STREAM_ORDER)
    from .microcells import MICROCELL_ENTRY
    n = int(points)
    # build_ladder refuses a rung >= .9 of its parent. Use that upper bound,
    # not the nominal .4 target: the search can miss its target on real clouds.
    factor = sum(0.9 ** k for k in range(max(1, lod_levels - 1)))
    stored = math.ceil(n * factor) + min(n, int(overview_target))
    bpp = 12 + 1 + 2 + 1 + 2 + 8 + (3 * (2 if rgb_bits == 16 else 1) if have_rgb else 0)
    # A shard has <=4096 cells, and each cell has at least one point. This is
    # deliberately a structural ceiling, including small/sparse leaves.
    from .builder import choose_shard_count
    nodes = min(n, choose_shard_count(n) * 4096)
    blocks = nodes * max(1, lod_levels - 1) + 1
    headers = blocks * (PC_BLOCK_HEADER.itemsize + PC_STREAM.itemsize * len(STREAM_ORDER))
    index = (4096 + source_count * SOURCE_ENTRY.itemsize + nodes * NODE_ENTRY.itemsize
             + blocks * BLOCK_ENTRY.itemsize + stored * MICROCELL_ENTRY.itemsize // 256
             + source_count * 8192 + (1 << 20))
    checkpoint = max(16 << 20, min(GIB, math.ceil(n * 1.0)))
    final_pc = stored * bpp + headers
    safety = max(64 << 20, min(20 * GIB, math.ceil(final_pc * .25)))
    return {"final_pc": final_pc, "index": index, "shards": n * shard_itemsize,
            "checkpoint": checkpoint, "metadata": index,
            "safety": safety, "lod_point_ceiling_factor": factor,
            "policy": "all shards retained; conservative .9 rung reduction ceiling"}


def admit_build(points, source_count, output_path, scratch_path, **kwargs):
    start = time.perf_counter()
    est = estimate_build(points, source_count, **kwargs)
    output_key, output_probe = filesystem(output_path)
    scratch_key, scratch_probe = filesystem(scratch_path)
    volumes = {}
    for key, probe, amount in (
            (output_key, output_probe, est["final_pc"] + 2 * est["index"]),
            (scratch_key, scratch_probe, est["shards"] + est["checkpoint"] + est["metadata"])):
        v = volumes.setdefault(key, {"path": probe, "required": 0,
                                     "available": shutil.disk_usage(probe).free})
        v["required"] += amount
    for v in volumes.values():
        v["required"] += est["safety"]
        v["short_by"] = max(0, v["required"] - v["available"])
    report = {"output_path": os.path.abspath(output_path),
              "scratch_path": os.path.abspath(scratch_path),
              "output_volume": output_key, "scratch_volume": scratch_key,
              "estimate": est, "volumes": volumes,
              "passed": all(v["short_by"] == 0 for v in volumes.values()),
              "milliseconds": (time.perf_counter() - start) * 1000}
    if not report["passed"]:
        raise DiskAdmissionError(report)
    return report


def format_report(r):
    e = r["estimate"]
    lines = ["DISK PREFLIGHT " + ("PASSED" if r["passed"] else "FAILED"),
             "Output drive: " + r["output_path"], "Scratch drive: " + r["scratch_path"],
             f"Estimated final cache: {e['final_pc'] / GIB:.2f} GiB",
             f"Estimated scratch peak: {(e['shards'] + e['checkpoint'] + e['metadata']) / GIB:.2f} GiB",
             f"Atomic overlap: retained production + new payload; new index {e['index'] / GIB:.3f} GiB",
             f"Safety margin per filesystem: {e['safety'] / GIB:.2f} GiB"]
    for key, v in r["volumes"].items():
        lines.append(f"Volume {key} ({v['path']}): Required {v['required'] / GIB:.2f} GiB; "
                     f"Available {v['available'] / GIB:.2f} GiB; Short by {v['short_by'] / GIB:.2f} GiB")
    if not r["passed"]:
        lines.append("Build NOT started.")
    return "\n".join(lines)
