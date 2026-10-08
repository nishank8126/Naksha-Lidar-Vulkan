"""transforms.py - per-stream reversible preprocessing for NKPC compression.

Every transform is LOSSLESS and byte-exact round-trip, including the XYZ
coordinate case: transforms operate on the already-quantised tile-local int32
values stored in the cache, so no additional coordinate error is introduced.
That is the whole point - the cache's existing quantisation is the only
precision loss, and it is unchanged.

XYZ ladder (spec A-D):
    A raw int32 XYZ          -> Zstd
    B delta-coded X/Y/Z      -> Zstd
    C delta + zig-zag        -> Zstd
    D delta + zig-zag + byte shuffle -> Zstd

Other streams:
    classification : raw uint8 or RLE
    intensity      : raw uint16 or byte-shuffled uint16
    rgb            : interleaved or planar (R/G/B separate streams)
"""
from __future__ import annotations

import numpy as np

from . import codecs

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _zigzag_encode(v: np.ndarray) -> np.ndarray:
    """Map signed -> unsigned so small magnitudes become small bytes.
    (v<<1) ^ (v>>63) for int64, standard zig-zag."""
    return ((v.astype(np.int64) << 1) ^ (v.astype(np.int64) >> 63))


def _zigzag_decode(u: np.ndarray) -> np.ndarray:
    return (u.astype(np.int64) >> 1) ^ -(u.astype(np.int64) & 1)


def _byte_shuffle(a: np.ndarray) -> np.ndarray:
    """Transpose a (N,) uint array into byte planes: all byte0, then byte1, ...
    Grouping similar-magnitude bytes together is what Zstd's entropy coder
    likes. Exact inverse."""
    if a.dtype.itemsize == 1:
        return np.ascontiguousarray(a)
    b = np.ascontiguousarray(a).view(np.uint8).reshape(-1, a.dtype.itemsize)
    return np.ascontiguousarray(b.T).reshape(-1)


def _byte_unshuffle(a: np.ndarray, itemsize: int, dtype) -> np.ndarray:
    if itemsize == 1:
        return np.ascontiguousarray(a)
    b = np.ascontiguousarray(a).reshape(itemsize, -1).T
    return np.ascontiguousarray(b).view(dtype).reshape(-1)


# --------------------------------------------------------------------------
# XYZ - operates on (N,3) int32 tile-local coordinates
# --------------------------------------------------------------------------


def xyz_raw(xyz: np.ndarray) -> bytes:
    return np.ascontiguousarray(xyz, dtype=np.int32).tobytes()


def xyz_unpack_raw(buf: bytes, n: int) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.int32).reshape(n, 3)


def xyz_delta(xyz: np.ndarray) -> bytes:
    """Delta along each axis independently (Morton order is spatially coherent,
    so consecutive points share high bits -> small deltas)."""
    a = np.ascontiguousarray(xyz, dtype=np.int32).astype(np.int64)
    d = np.empty_like(a)
    d[0] = a[0]
    d[1:] = a[1:] - a[:-1]
    return np.ascontiguousarray(d, dtype=np.int32).tobytes()


def xyz_unpack_delta(buf: bytes, n: int) -> np.ndarray:
    d = np.frombuffer(buf, dtype=np.int32).reshape(n, 3).astype(np.int64)
    a = d.copy()
    # cumsum(d[1:]) alone drops d[0]; the running total must START at d[0].
    a[0] = d[0]
    a[1:] = d[0] + np.cumsum(d[1:], axis=0)
    return np.ascontiguousarray(a, dtype=np.int32)


def xyz_delta_zigzag(xyz: np.ndarray) -> bytes:
    a = np.ascontiguousarray(xyz, dtype=np.int32).astype(np.int64)
    d = np.empty_like(a)
    d[0] = a[0]
    d[1:] = a[1:] - a[:-1]
    z = _zigzag_encode(d)
    # back to int32 view; deltas fit in int32 by construction of the cache
    return np.ascontiguousarray(z, dtype=np.int32).tobytes()


def xyz_unpack_delta_zigzag(buf: bytes, n: int) -> np.ndarray:
    z = np.frombuffer(buf, dtype=np.int32).reshape(n, 3)
    d = _zigzag_decode(z)
    a = d.copy()
    # the running total starts at d[0], exactly as in xyz_unpack_delta
    a[0] = d[0]
    a[1:] = d[0] + np.cumsum(d[1:], axis=0)
    return np.ascontiguousarray(a, dtype=np.int32)


def xyz_delta_zigzag_shuffle(xyz: np.ndarray) -> bytes:
    a = np.ascontiguousarray(xyz, dtype=np.int32).astype(np.int64)
    d = np.empty_like(a)
    d[0] = a[0]
    d[1:] = a[1:] - a[:-1]
    z = _zigzag_encode(d)
    flat = np.ascontiguousarray(z, dtype=np.int32).reshape(-1)
    return _byte_shuffle(flat).tobytes()


def xyz_unpack_delta_zigzag_shuffle(buf: bytes, n: int) -> np.ndarray:
    flat = _byte_unshuffle(np.frombuffer(buf, dtype=np.uint8), 4, np.int32)
    z = flat.reshape(n, 3)
    d = _zigzag_decode(z)
    a = d.copy()
    a[0] = d[0]
    a[1:] = d[0] + np.cumsum(d[1:], axis=0)
    return np.ascontiguousarray(a, dtype=np.int32)


# --------------------------------------------------------------------------
# classification (uint8, must stay BIT EXACT)
# --------------------------------------------------------------------------


def cls_raw(a: np.ndarray) -> bytes:
    return np.ascontiguousarray(a, dtype=np.uint8).tobytes()


def cls_unpack_raw(buf: bytes, n: int) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.uint8)[:n]


def cls_rle(a: np.ndarray) -> bytes:
    """Run-length encode. LiDAR classification is extremely repetitive inside a
    spatial block, so this is usually a large win. Header is a u32 run count,
    then (u32 runlen, u8 value) per run."""
    a = np.ascontiguousarray(a, dtype=np.uint8)
    if a.size == 0:
        return b""
    change = np.flatnonzero(a[1:] != a[:-1]) + 1
    starts = np.concatenate(([0], change))
    runs = np.diff(np.concatenate((starts, [a.size]))).astype("<u4")
    vals = a[starts].astype(np.uint8)
    out = bytearray(4 + runs.size * 5)
    out[0:4] = np.uint32(runs.size).tobytes()
    p = 4
    for i in range(runs.size):
        out[p:p + 4] = runs[i].tobytes()
        out[p + 4] = int(vals[i])
        p += 5
    return bytes(out)


def cls_unpack_rle(buf: bytes, n: int) -> np.ndarray:
    if len(buf) < 4:
        return np.zeros(n, dtype=np.uint8)
    nruns = int(np.frombuffer(buf[:4], dtype="<u4")[0])
    out = np.zeros(n, dtype=np.uint8)
    p = 4
    w = 0
    for i in range(nruns):
        if p + 5 > len(buf):
            break
        rl = int(np.frombuffer(buf[p:p + 4], dtype="<u4")[0])
        v = buf[p + 4]
        p += 5
        w2 = min(w + rl, n)
        out[w:w2] = v
        w = w2
        if w >= n:
            break
    return out


# --------------------------------------------------------------------------
# intensity (uint16, BIT EXACT)
# --------------------------------------------------------------------------


def int_raw(a: np.ndarray) -> bytes:
    return np.ascontiguousarray(a, dtype=np.uint16).tobytes()


def int_unpack_raw(buf: bytes, n: int) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.uint16)[:n]


def int_shuffle(a: np.ndarray) -> bytes:
    return _byte_shuffle(np.ascontiguousarray(a, dtype=np.uint16)).tobytes()


def int_unpack_shuffle(buf: bytes, n: int) -> np.ndarray:
    return _byte_unshuffle(np.frombuffer(buf, dtype=np.uint8), 2, np.uint16)[:n]


# --------------------------------------------------------------------------
# RGB16 (must stay full uint16, NO truncation)
# --------------------------------------------------------------------------


def rgb_interleaved(a: np.ndarray) -> bytes:
    return np.ascontiguousarray(a, dtype=np.uint16).tobytes()


def rgb_unpack_interleaved(buf: bytes, n: int) -> np.ndarray:
    return np.frombuffer(buf, dtype=np.uint16).reshape(n, 3)


def rgb_planar(a: np.ndarray):
    arr = np.ascontiguousarray(a, dtype=np.uint16).reshape(-1, 3)
    return [np.ascontiguousarray(arr[:, i]).tobytes() for i in range(3)]


def rgb_unpack_planar(bufs, n: int) -> np.ndarray:
    cols = [np.frombuffer(b, dtype=np.uint16)[:n] for b in bufs]
    return np.ascontiguousarray(np.column_stack(cols), dtype=np.uint16)


# --------------------------------------------------------------------------
# stable identity: block/source file id + uint32 source point index
# --------------------------------------------------------------------------


def sid_raw32(a: np.ndarray) -> bytes:
    """Only the within-block source point index (uint32); the source file id
    travels in the block header, so a uint64 per point is unnecessary."""
    return np.ascontiguousarray(a, dtype=np.uint32).tobytes()


def sid_unpack_raw32(buf: bytes, n: int, source_id: int = 0) -> np.ndarray:
    idx = np.frombuffer(buf, dtype=np.uint32)[:n]
    return (idx.astype(np.uint64) + np.uint64(source_id)).astype(np.uint64)


def sid_delta32(a: np.ndarray) -> bytes:
    v = np.ascontiguousarray(a, dtype=np.uint64).astype(np.int64)
    d = np.empty_like(v)
    d[0] = v[0]
    d[1:] = v[1:] - v[:-1]
    z = _zigzag_encode(d)
    return np.ascontiguousarray(z, dtype=np.uint32).tobytes()


def sid_unpack_delta32(buf: bytes, n: int, source_id: int = 0) -> np.ndarray:
    z = np.frombuffer(buf, dtype=np.uint32)[:n]
    d = _zigzag_decode(z)
    v = d.copy()
    # same correction as the XYZ delta decoder: the running total starts at d[0]
    v[0] = d[0]
    v[1:] = d[0] + np.cumsum(d[1:])
    return (np.ascontiguousarray(v, dtype=np.uint64)
            + np.uint64(source_id)).astype(np.uint64)