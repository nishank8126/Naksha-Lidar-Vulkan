"""Single-file base storage with independent, checksummed ZSTD-1 streams.

Hierarchy tables remain mmapable. Only selected streams of selected blocks are
read and decoded; no temporary extraction is needed on runtime open.
"""
from __future__ import annotations

import csv
import hashlib
import os
from pathlib import Path
import struct
import zlib

import numpy as np

from . import codecs
from .derived_container import DerivedContainer, HEADER_SIZE, MAGIC
from .format import (PC_BLOCK_HEADER, PC_STREAM, STREAM_ORDER, STREAM_DTYPE,
                     ATTR_RGB, CODEC_ZSTD, rgb_dtype, stream_component_count)

STREAM_V2 = np.dtype(PC_STREAM.descr + [("raw_bytes", "<u8"),
                                      ("checksum", "<u4"),
                                      ("codec_level", "<u2")])
PAYLOAD_VERSION = 2


def encode_stream_block(header, arrays, report=None):
    header = np.array([header], dtype=PC_BLOCK_HEADER)
    header["version"] = PAYLOAD_VERSION
    header["codec"] = CODEC_ZSTD
    directory = np.zeros(len(STREAM_ORDER), dtype=STREAM_V2)
    offset = PC_BLOCK_HEADER.itemsize + directory.nbytes
    payloads = []
    for i, attr in enumerate(STREAM_ORDER):
        array = arrays.get(attr)
        if array is None:
            continue
        raw = np.ascontiguousarray(array).tobytes()
        stored = codecs.compress(raw, CODEC_ZSTD, level=1)
        item = directory[i]
        item["attr"], item["offset"], item["bytes"] = attr, offset, len(stored)
        item["raw_bytes"], item["checksum"] = len(raw), zlib.crc32(stored)
        item["codec"], item["codec_level"] = CODEC_ZSTD, 1
        item["elem_bits"] = array.dtype.itemsize * 8
        if report is not None:
            report(dict(block_id=int(header["block_id"][0]), stream_type=attr,
                        encoding="CANONICAL_SOA", codec="ZSTD", codec_level=1,
                        raw_bytes=len(raw), compressed_bytes=len(stored),
                        compression_ratio=len(raw) / max(len(stored), 1)))
        offset += len(stored)
        payloads.append(stored)
    payload = b"".join(payloads)
    header["stream_count"] = len(payloads)
    header["payload_crc"] = zlib.crc32(payload)
    header["header_crc"] = 0
    header["header_crc"] = zlib.crc32(header.tobytes() + directory.tobytes())
    return header.tobytes() + directory.tobytes() + payload


def read_stream_block(fh, entry, only_attrs=None, stats=None):
    offset, stored = int(entry["file_offset"]), int(entry["stored_bytes"])
    fh.seek(offset)
    raw_header = fh.read(PC_BLOCK_HEADER.itemsize)
    if len(raw_header) != PC_BLOCK_HEADER.itemsize:
        raise ValueError("truncated point header")
    header = np.frombuffer(raw_header, dtype=PC_BLOCK_HEADER)[0]
    from .format import PC_MAGIC
    if bytes(header["magic"]).rstrip(b"\0") != PC_MAGIC or int(header["version"]) != PAYLOAD_VERSION:
        raise ValueError("unsupported single-file point block")
    directory_bytes = STREAM_V2.itemsize * len(STREAM_ORDER)
    raw_directory = fh.read(directory_bytes)
    if len(raw_directory) != directory_bytes:
        raise ValueError("truncated stream directory")
    directory = np.frombuffer(raw_directory, dtype=STREAM_V2)
    checked_header = np.array([header], dtype=PC_BLOCK_HEADER)
    checksum = int(checked_header["header_crc"][0])
    checked_header["header_crc"] = 0
    if zlib.crc32(checked_header.tobytes() + raw_directory) != checksum:
        raise ValueError("point stream directory checksum mismatch")
    if (int(header["point_count"]) != int(entry["point_count"])
            or int(header["block_id"]) != int(entry["block_id"])):
        raise ValueError("point block identity mismatch")
    want = None if only_attrs is None else set(only_attrs)
    out, bytes_read = {}, len(raw_header) + directory_bytes
    decoded_zstd = False
    for item in directory:
        attr, size = int(item["attr"]), int(item["bytes"])
        if not attr or (want is not None and attr not in want):
            continue
        dt = np.dtype(rgb_dtype(int(header["rgb_bits"])) if attr == ATTR_RGB
                      else STREAM_DTYPE[attr])
        components = stream_component_count(attr)
        expected = int(header["point_count"]) * components * dt.itemsize
        at = int(item["offset"])
        if (int(item["raw_bytes"]) != expected
                or at < len(raw_header) + directory_bytes or at + size > stored):
            raise ValueError("invalid stream extent or decoded size")
        fh.seek(offset + at)
        compressed = fh.read(size)
        if len(compressed) != size or zlib.crc32(compressed) != int(item["checksum"]):
            raise ValueError("point stream checksum mismatch")
        codec = int(item["codec"])
        raw = codecs.decompress(compressed, expected, codec)
        if len(raw) != expected:
            raise ValueError("point stream decoded size mismatch")
        array = np.frombuffer(raw, dtype=dt)
        out[attr] = array.reshape(-1, components) if components > 1 else array
        bytes_read += size
        if stats is not None and codec == CODEC_ZSTD:
            stats["zstd_decode_calls"] = stats.get("zstd_decode_calls", 0) + 1
            stats["decoded_zstd_bytes"] = stats.get("decoded_zstd_bytes", 0) + expected
            stats["zstd_compressed_bytes_read"] = stats.get("zstd_compressed_bytes_read", 0) + size
            decoded_zstd = True
    if stats is not None and decoded_zstd:
        stats["successful_random_zstd_block_reads"] = stats.get("successful_random_zstd_block_reads", 0) + 1
    return header, out, bytes_read, None


def repack_base(index_path, points_path, output, *, report_path=None, cancel=None):
    """Bounded conversion of staging blocks; atomic publication of one file."""
    from .index import IndexReader
    from .readblock_new import read_block_once
    index = IndexReader(str(index_path))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".base.tmp")
    report_handle = open(report_path, "w", newline="", encoding="utf-8") if report_path else None
    writer = None
    totals = dict(zstd_streams=0, lz4_streams=0, none_streams=0,
                  zstd_compression_calls=0, compressed_bytes=0, raw_bytes=0)
    entries = None
    def report(item):
        nonlocal writer
        if report_handle:
            if writer is None:
                writer = csv.DictWriter(report_handle, fieldnames=list(item))
                writer.writeheader()
            writer.writerow(item)
        totals["zstd_streams"] += 1
        totals["zstd_compression_calls"] += 1
        totals["compressed_bytes"] += item["compressed_bytes"]
        totals["raw_bytes"] += item["raw_bytes"]
    try:
        with temporary.open("w+b") as handle, open(points_path, "rb") as points:
            header = bytearray(HEADER_SIZE)
            header[:8] = MAGIC
            struct.pack_into("<I", header, 8, 1)
            handle.write(header)
            index_at = handle.tell()
            index_size = os.path.getsize(index_path)
            with open(index_path, "rb") as source_index:
                while chunk := source_index.read(4 << 20):
                    handle.write(chunk)
            handle.flush()
            source_bytes = len(index.sources) * index._src_dtype.itemsize
            block_start = index._hdr_dtype.itemsize + source_bytes + index.nodes.nbytes
            entries = np.memmap(temporary, mode="r+", dtype=index.blocks.dtype,
                shape=(len(index.blocks),), offset=index_at + block_start)
            point_start = handle.tell()
            digest = hashlib.sha256()
            for i, entry in enumerate(index.blocks):
                if cancel is not None and cancel.is_set():
                    raise InterruptedError("single-file compression cancelled")
                hdr, arrays, _, _ = read_block_once(points, entry, verify_crc=True)
                packed = encode_stream_block(hdr, arrays, report)
                entries[i]["file_offset"] = handle.tell() - point_start
                entries[i]["stored_bytes"] = len(packed)
                entries[i]["codec"] = CODEC_ZSTD
                entries[i]["checksum"] = zlib.crc32(packed)
                handle.write(packed)
                digest.update(packed)
            point_size = handle.tell() - point_start
            entries.flush()
            entries._mmap.close()
            handle.flush()
            handle.seek(index_at)
            embedded_header = np.frombuffer(handle.read(index._hdr_dtype.itemsize),
                dtype=index._hdr_dtype).copy()
            embedded_header["pc_file_size"] = point_size
            embedded_header["point_cache_name"] = output.name.encode("utf-8")[:63]
            handle.seek(index_at)
            handle.write(embedded_header.tobytes())
            handle.flush()
            handle.seek(index_at)
            index_digest = hashlib.sha256()
            remaining = index_size
            while remaining:
                chunk = handle.read(min(remaining, 4 << 20))
                index_digest.update(chunk)
                remaining -= len(chunk)
            identity = dict(dataset_uuid=index.uuid.hex(),
                            layout_fingerprint=index.layout_fingerprint,
                            source_set_fingerprint=hashlib.sha256(index.sources.tobytes()).hexdigest())
            directory = dict(generation=1, identity=identity, sections={
                "BASE_POINTS": dict(version=2, metadata=dict(compression=totals), blocks={"base":
                    dict(offset=point_start, size=point_size, codec="NONE", checksum=digest.hexdigest())}),
                "BASE_INDEX": dict(version=1, blocks={"base":
                    dict(offset=index_at, size=index_size, codec="NONE",
                         checksum=index_digest.hexdigest())})})
            sections = directory["sections"]
            sections["BASE_DATA"] = dict(version=2, metadata=dict(index="BASE_INDEX", points="BASE_POINTS"), blocks={})
            table_at = index_at + index._hdr_dtype.itemsize
            for name, table in (("SOURCE_CATALOG", index.sources), ("HIERARCHY", index.nodes),
                                ("POINT_BLOCK_DIRECTORY", index.blocks), ("OCCUPANCY", index.microcells)):
                sections[name] = dict(version=1, metadata=dict(count=len(table), record_bytes=table.dtype.itemsize),
                    blocks={"table": dict(offset=table_at, size=table.nbytes, codec="NONE")})
                handle.seek(table_at)
                table_digest = hashlib.sha256()
                remaining = table.nbytes
                while remaining:
                    chunk = handle.read(min(remaining, 4 << 20))
                    table_digest.update(chunk)
                    remaining -= len(chunk)
                sections[name]["blocks"]["table"]["checksum"] = table_digest.hexdigest()
                table_at += table.nbytes
            sections["LOD"] = dict(version=1, metadata=dict(hierarchy="HIERARCHY", levels=int(index.header["lod_count"])), blocks={})
            sections["OVERVIEW"] = dict(version=1, metadata=dict(block_id=index.overview_block_id), blocks={})
            sections["DATASET_METADATA"] = dict(version=1, metadata=dict(
                total_points=index.total_points, bounds_min=index.bounds_min.tolist(),
                bounds_max=index.bounds_max.tolist(), source_count=len(index.sources)), blocks={})
            DerivedContainer._commit(handle, directory)
        DerivedContainer(temporary)
        os.replace(temporary, output)
        return totals
    finally:
        if entries is not None and not entries._mmap.closed:
            entries._mmap.close()
        index.close()
        if report_handle:
            report_handle.close()
        if temporary.exists():
            temporary.unlink()
