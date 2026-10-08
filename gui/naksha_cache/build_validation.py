"""All-block pre-commit validation with bounded RAM and exact core-ID proof."""
from __future__ import annotations

import os
import time
import zlib

import numpy as np

from . import format as F
from .build_safety import BuildSafetyError
from .global_stats import GlobalStats
from .lod_ladder import occupied_area, area_spacing
from .readblock_new import read_block_once, decode_world_f64
from .source_catalog import resolve_global_ids, verify_dense_bases


def require(ok, message):
    if not ok:
        raise BuildSafetyError("VALIDATION FAILED: " + message)


def validate_index(path, builder, overview_bid, pc_path):
    from .index import IndexReader
    with open(path, "rb") as f:
        raw = f.read(F.IDX_HEADER.itemsize)
    require(len(raw) == F.IDX_HEADER.itemsize, "index temp header truncated")
    hdr = np.frombuffer(raw, F.IDX_HEADER).copy()
    saved_crc = int(hdr["header_crc"][0])
    hdr["header_crc"] = 0
    require(F.crc32_bytes(hdr.tobytes()) == saved_crc, "index temp header CRC")
    idx = IndexReader(path)
    try:
        require(idx.cache_status == F.CACHE_CURRENT_VALID, "index semantic versions")
        require(idx.total_points == builder.total_points, "index source point count")
        require(np.array_equal(idx.sources, builder.sources), "index source identity table")
        require(np.array_equal(idx.nodes, builder.nodes), "index node table")
        require(np.array_equal(idx.blocks, builder.blocks), "index block directory")
        require(idx.point_cache_name == os.path.basename(pc_path), "index payload generation")
        require(int(idx.header["pc_file_size"]) == os.path.getsize(builder.pc_path), "index NKPC size")
        require(idx.overview_block_id == (-1 if overview_bid is None else overview_bid), "index overview id")
        require(idx.stats is not None and idx.stats.n_points == builder.total_points, "index statistics")
        expected_size = int(idx.header["stats_offset"]) + int(idx.header["stats_bytes"])
        require(os.path.getsize(path) == expected_size, "index file extent/trailing bytes")
        expected_fp = F.layout_fingerprint(versions=idx.semantic_versions,
            shard_depth=int(idx.header["shard_depth"]), max_depth=int(idx.header["max_depth"]),
            lod_count=int(idx.header["lod_count"]), node_count=len(idx.nodes), block_count=len(idx.blocks),
            attribute_mask=int(idx.header["attribute_mask"]), bounds_min=idx.bounds_min,
            bounds_max=idx.bounds_max)
        require(idx.layout_fingerprint == expected_fp, "index layout fingerprint")
    finally:
        idx.close()


def validate_output(b, overview_bid):
    start = time.perf_counter()
    require(verify_dense_bases(b.sources), "canonical source bases are not dense")
    require(int(b.sources["point_count"].sum()) == b.total_points, "source total count")
    boundaries = [0, b.total_points - 1]
    for r in b.sources:
        boundaries.extend((int(r["point_base"]), int(r["point_base"] + r["point_count"] - 1)))
    fid, local = resolve_global_ids(b.sources, np.asarray(boundaries, np.uint64))
    require(np.array_equal(b.sources["point_base"][fid] + local.astype(np.uint64), boundaries),
            "global ID source transitions")
    require(np.array_equal(b.blocks["block_id"], np.arange(len(b.blocks))), "block IDs not unique/dense")
    total_size = os.path.getsize(b.pc_path)
    previous_end = 0
    total_read = core_count = overview_count = 0
    seen_path = os.path.join(b._session.m["work_dir"], "validate-core-ids.bits")
    seen = np.memmap(seen_path, mode="w+", dtype=np.uint8, shape=((b.total_points + 7) // 8,))
    seen[:] = 0
    stats = b.stats_obj
    require(stats.n_points == b.total_points, "statistics point count")
    for name in ("inten_hist", "z_hist", "class_counts", "source_points"):
        require(int(getattr(stats, name).sum()) == b.total_points, "statistics total: " + name)
    require(np.array_equal(stats.source_points, b.sources["point_count"]), "per-source statistics points")
    require(np.array_equal(stats.source_class.sum(axis=1), b.sources["point_count"]), "per-source class totals")
    require(np.array_equal(stats.source_class.sum(axis=0), stats.class_counts), "global/per-source classes")
    require(np.isfinite([stats.z_min, stats.z_max]).all() and stats.z_min <= stats.z_max,
            "statistics Z range")
    inten_hist = np.zeros(65536, np.uint64)
    class_hist = np.zeros(256, np.uint64)
    source_class = np.zeros_like(stats.source_class)
    previous_node = None
    prev_ids = None
    leaf_area = 0
    crc_start = time.perf_counter()
    try:
        with open(b.pc_path, "rb") as f:
            for entry in b.blocks:
                bid, nid, lod, n = (int(entry[k]) for k in ("block_id", "node_id", "lod", "point_count"))
                off, length = int(entry["file_offset"]), int(entry["stored_bytes"])
                require(off == previous_end and length > 0 and off + length <= total_size,
                        f"block {bid} offset/length outside NKPC or overlapping")
                hdr, streams, _, raw = read_block_once(f, entry, verify_crc=True)
                total_read += len(raw)
                require(zlib.crc32(raw) & 0xffffffff == int(entry["checksum"]), f"block {bid} full CRC")
                header = np.array([hdr], dtype=F.PC_BLOCK_HEADER)
                hcrc = int(header["header_crc"][0])
                header["header_crc"] = 0
                require(F.crc32_bytes(header.tobytes()) == hcrc, f"block {bid} header CRC")
                for field in ("block_id", "node_id", "lod", "point_count", "attribute_mask", "stream_count", "codec"):
                    require(int(hdr[field]) == int(entry[field]), f"block {bid} header/directory {field}")
                require(n > 0 and int(hdr["codec"]) == F.CODEC_NONE, f"block {bid} count/codec")
                require(int(hdr["attribute_mask"]) & (F.ATTR_XYZ | F.ATTR_SOURCE_ID)
                        == F.ATTR_XYZ | F.ATTR_SOURCE_ID, f"block {bid} required attributes")
                directory = F.read_block_directory(f, off)
                stream_end = F.PC_BLOCK_HEADER.itemsize + F.PC_STREAM.itemsize * len(F.STREAM_ORDER)
                mask = 0
                active_streams = 0
                for slot, s in enumerate(directory):
                    attr, size = int(s["attr"]), int(s["bytes"])
                    if not attr:
                        require(size == 0, f"block {bid} empty stream has bytes")
                        continue
                    require(attr == F.STREAM_ORDER[slot], f"block {bid} stream attribute slot")
                    require(int(s["offset"]) == stream_end and size == n * F.stream_itemsize(attr, int(hdr["rgb_bits"])),
                            f"block {bid} stream bounds/count")
                    stream_end += size
                    mask |= attr
                    active_streams += 1
                require(stream_end == length and mask == int(hdr["attribute_mask"])
                        and active_streams == int(hdr["stream_count"]), f"block {bid} stream extent/flags")
                xyz = decode_world_f64(hdr, streams[F.ATTR_XYZ])
                tolerance = np.maximum(np.abs(hdr["scale"]) * 2, 1e-5)
                require(np.isfinite(xyz).all() and (xyz >= b.gmin - tolerance).all()
                        and (xyz <= b.gmax + tolerance).all(), f"block {bid} dataset bounds")
                gids = streams[F.ATTR_SOURCE_ID]
                require(len(gids) == n and int(gids.max()) < b.total_points, f"block {bid} source IDs outside dataset")
                sorted_ids = np.sort(gids)
                require(n == 1 or (np.diff(sorted_ids) != 0).all(), f"block {bid} duplicate IDs")
                if bid == overview_bid:
                    overview_count = n
                    require(n <= b.overview_target, "overview size exceeds bound")
                else:
                    require(nid < len(b.nodes), f"block {bid} node reference")
                    node = b.nodes[nid]
                    require(lod < len(node["lod_block"]) and int(node["lod_block"][lod]) == bid
                            and int(node["lod_point_count"][lod]) == n, f"block {bid} node LOD directory")
                    require((xyz >= node["bounds_min"] - tolerance).all()
                            and (xyz <= node["bounds_max"] + tolerance).all(), f"block {bid} leaf bounds")
                    if lod == 0:
                        core_count += n
                        byte_ids = gids >> np.uint64(3)
                        flags = np.left_shift(np.uint8(1), (gids & np.uint64(7)).astype(np.uint8))
                        unique_bytes, inv = np.unique(byte_ids, return_inverse=True)
                        masks = np.zeros(len(unique_bytes), np.uint8)
                        np.bitwise_or.at(masks, inv, flags)
                        require(not (seen[unique_bytes] & masks).any(), f"duplicate canonical core ID in block {bid}")
                        seen[unique_bytes] |= masks
                        require(int(node["point_count"]) == n, f"node {nid} core count")
                        leaf_area = occupied_area(xyz)
                        inten_hist += np.bincount(streams[F.ATTR_INTENSITY], minlength=65536).astype(np.uint64)
                        classes = streams[F.ATTR_CLASSIFICATION]
                        class_hist += np.bincount(classes, minlength=256).astype(np.uint64)
                        sf, _ = resolve_global_ids(b.sources, gids)
                        np.add.at(source_class, (sf, classes), np.uint64(1))
                    else:
                        require(previous_node == nid and prev_ids is not None, f"LOD {bid} missing parent")
                        require(n < len(prev_ids) * .9 and np.isin(sorted_ids, prev_ids, assume_unique=True).all(),
                                f"LOD {bid} rung nestedness/count ratio")
                    expected_spacing = area_spacing(leaf_area, n)
                    if lod:
                        expected_spacing = max(expected_spacing, 1e-3)
                    require(np.isclose(float(node["lod_spacing"][lod]), expected_spacing, rtol=.03, atol=1e-5),
                            f"LOD {bid} spacing semantics")
                    previous_node, prev_ids = nid, sorted_ids
                previous_end = off + length
        require(previous_end == total_size, "NKPC trailing/unindexed bytes")
        require(core_count == b.total_points, "canonical core count/missing IDs")
        require(np.array_equal(inten_hist, stats.inten_hist), "intensity statistics differ from core")
        require(np.array_equal(class_hist, stats.class_counts), "class statistics differ from core")
        require(np.array_equal(source_class, stats.source_class), "per-source class statistics differ from core")
        if b._session.config["with_overview"]:
            require(overview_count > 0, "overview missing")
        xy_width = b.nodes["bounds_max"][:, :2] - b.nodes["bounds_min"][:, :2]
        areas = xy_width[:, 0] * xy_width[:, 1]
        dataset_area = max(float(b.span[0] * b.span[1]), 1e-9)
        max_width = xy_width.max(axis=1)
        median_width = max(float(np.median(max_width)), 1e-9)
        geometry = {"nodes": len(b.nodes), "overlap_ratio": float(areas.sum() / dataset_area),
                    "wide_fraction": float((max_width > 3 * median_width).mean())}
        require(geometry["overlap_ratio"] <= 1.05, "leaf overlap ratio exceeds partition bound")
        # Width distributions are diagnostic; sparse floor exceptions are
        # legitimate. Membership is proven against original shards separately.
        widths = b.nodes["bounds_max"][:, :2] - b.nodes["bounds_min"][:, :2]
        geometry["width_percentiles"] = np.percentile(widths.max(axis=1), [0, 50, 95, 100]).tolist()
        return {"milliseconds": (time.perf_counter() - start) * 1000,
                "crc_policy": "ALL blocks, header + full encoded + payload CRC",
                "crc_bytes": total_read, "crc_mib_s": total_read / (1 << 20) / max(time.perf_counter() - crc_start, 1e-9),
                "core_points": core_count, "duplicate_ids": 0, "missing_ids": 0,
                "overview_points": overview_count, "leaf_geometry": geometry,
                "shard_membership": "FULL original shard proof before finalize"}
    finally:
        seen.flush()
        seen._mmap.close()
