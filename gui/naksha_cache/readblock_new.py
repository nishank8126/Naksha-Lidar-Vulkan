"""Single-pass block reader: one disk read, CRC over loaded bytes.

Replaces the earlier read_block, which read each stream separately and then
re-read the entire body to CRC it. That double read cost ~2 ms per block.

Measured breakdown for a 231K-point / 6.01 MB block (naksha_read_profile.py):
    seek                0.000 ms
    read (one pass)     1.584 ms
    parse               0.003 ms
    CRC (in memory)     1.660 ms
    decode              6.701 ms   <- the real bottleneck, not the CRC
"""
import numpy as np

from .format import (ATTR_RGB, ATTR_XYZ, PC_BLOCK_HEADER, PC_MAGIC, PC_STREAM,
                     PC_VERSION, STREAM_DTYPE, STREAM_ORDER, crc32_bytes,
                     rgb_dtype, stream_component_count)


def read_block_once(fh, entry, only_attrs=None, verify_crc=False):
    """Read a block with ONE disk read and ZERO-COPY stream views."""
    off = int(entry["file_offset"])
    stored = int(entry["stored_bytes"])
    hdr_size = PC_BLOCK_HEADER.itemsize
    dir_size = PC_STREAM.itemsize * len(STREAM_ORDER)
    pstart = hdr_size + dir_size

    fh.seek(off)
    raw = fh.read(stored)
    if len(raw) < stored:
        raise ValueError(f"short read at {off}: {len(raw)} of {stored} bytes")

    hdr = np.frombuffer(raw[:hdr_size], dtype=PC_BLOCK_HEADER, count=1)[0]
    magic = bytes(hdr["magic"]).rstrip(b"\x00")
    if magic != PC_MAGIC:
        raise ValueError(f"bad block magic {magic!r} at offset {off}")
    if int(hdr["version"]) != PC_VERSION:
        raise ValueError(f"unsupported block version {int(hdr['version'])}, "
                         f"expected {PC_VERSION}")

    if verify_crc:
        # CRC the bytes ALREADY LOADED. Re-reading the body is what made a
        # verified read cost two disk passes.
        if crc32_bytes(raw[pstart:]) != int(hdr["payload_crc"]):
            raise ValueError(
                f"block {int(entry['block_id'])} payload CRC mismatch")

    directory = np.frombuffer(raw[hdr_size:pstart], dtype=PC_STREAM,
                              count=len(STREAM_ORDER))
    want = None if only_attrs is None else set(only_attrs)
    out = {}
    bytes_used = 0
    for i in range(len(STREAM_ORDER)):
        s = directory[i]
        attr = int(s["attr"])
        nb = int(s["bytes"])
        if attr == 0 or nb == 0:
            continue
        if want is not None and attr not in want:
            continue
        comps = stream_component_count(attr)
        dt = (np.dtype(rgb_dtype(int(hdr["rgb_bits"]))) if attr == ATTR_RGB
              else np.dtype(STREAM_DTYPE[attr]))
        a = int(s["offset"])
        # Stream offsets are BLOCK-relative: the first stream starts exactly
        # at pstart (header + directory). Adding pstart again shifts every
        # payload by 444 bytes, so the LAST stream overruns the buffer and
        # every tile fails with 'buffer smaller than requested'.
        piece = raw[a: a + nb]
        arr = np.frombuffer(piece, dtype=dt, count=nb // dt.itemsize)
        out[attr] = arr.reshape(-1, comps) if comps > 1 else arr
        bytes_used += nb
    return hdr, out, bytes_used, raw


def decode_world_f32(hdr, local):
    """Tile-local render-space float32: the form the GPU actually wants.

    Dequantising to float64 world costs a fresh (N,3) float64 array (~5.5 MB
    for a 231K tile) and is only needed for export or editing. Rendering wants
    coordinates near the camera, so float32 relative to the TILE origin is both
    smaller and faster, and the tile origin travels in the node record.
    """
    o = np.asarray(hdr["origin"], dtype=np.float32)
    s = np.asarray(hdr["scale"], dtype=np.float32)
    return np.asarray(local, dtype=np.float32) * s


def decode_world_f64(hdr, local):
    """Authoritative float64 world coordinates (export / editing path)."""
    o = np.asarray(hdr["origin"], dtype=np.float64)
    s = np.asarray(hdr["scale"], dtype=np.float64)
    return np.asarray(local, dtype=np.float64) * s + o
