"""codecs.py - NAKSHA NKPC stream codecs (Phase: compression R&D).

CODEC_NONE = 0, CODEC_LZ4 = 1, CODEC_ZSTD = 2.

Both codecs are bound through ctypes to the zstd 1.5.7 and lz4 1.9.4 shared
libraries already present on the workstation, because this environment has no
package index and no zstandard/lz4 wheels. The C ABIs used here (ZSTD_compress,
ZSTD_decompress, ZSTD_isError, LZ4_compress_default, LZ4_decompress_safe,
LZ4_compressBound) are stable and versioned.

Everything here is INDEPENDENT-BLOCK oriented: one call compresses or
decompresses exactly one stream of one block. Nothing produces or consumes a
single monolithic stream across the whole NKPC, so every block stays
independently seekable.
"""
from __future__ import annotations

import ctypes
import os
from typing import Optional

CODEC_NONE = 0
CODEC_LZ4 = 1
CODEC_ZSTD = 2

CODEC_NAMES = {CODEC_NONE: "NONE", CODEC_LZ4: "LZ4", CODEC_ZSTD: "ZSTD"}

# Candidate DLL locations. The conda env on this workstation ships both; the
# project's own native tree is checked too so a future bundled copy wins.
_CANDIDATE_DIRS = (
    r"H:\conda_stuff\envs\urban_k2_test\Library\bin",
    r"H:\conda_stuff\envs\urban_k2_test\Library\lib",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "native"),
)

_zstd = None
_lz4 = None


def _find(names, env=None):
    """Locate a shared library. NAKSHA_CODEC_DIR wins, then the known candidate
    dirs, then anything already loadable by name (system path)."""
    dirs = []
    if env:
        dirs.append(env)
    dirs.extend(_CANDIDATE_DIRS)
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    return None


def zstd_lib():
    """Lazily bind libzstd. Returns None when unavailable (caller degrades)."""
    global _zstd
    if _zstd is not None:
        return _zstd
    p = _find(("libzstd.dll", "zstd.dll", "libzstd-1.dll"),
              os.environ.get("NAKSHA_ZSTD_DLL"))
    if not p:
        # last resort: let the OS loader resolve it
        try:
            p = "libzstd.dll"
        except Exception:
            return None
    try:
        lib = ctypes.CDLL(p)
        lib.ZSTD_compress.restype = ctypes.c_size_t
        lib.ZSTD_compress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_void_p, ctypes.c_size_t,
                                      ctypes.c_int]
        lib.ZSTD_decompress.restype = ctypes.c_size_t
        lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                        ctypes.c_void_p, ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_versionNumber.restype = ctypes.c_uint
        lib.ZSTD_compressBound.restype = ctypes.c_size_t
        lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        _zstd = lib
    except Exception:
        _zstd = None
    return _zstd


def lz4_lib():
    global _lz4
    if _lz4 is not None:
        return _lz4
    p = _find(("liblz4.dll", "lz4.dll"))
    if not p:
        return None
    try:
        lib = ctypes.CDLL(p)
        lib.LZ4_compressBound.restype = ctypes.c_int
        lib.LZ4_compressBound.argtypes = [ctypes.c_int]
        lib.LZ4_compress_default.restype = ctypes.c_int
        lib.LZ4_compress_default.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                              ctypes.c_int, ctypes.c_int]
        lib.LZ4_decompress_safe.restype = ctypes.c_int
        lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_char_p,
                                            ctypes.c_int, ctypes.c_int]
        _lz4 = lib
    except Exception:
        _lz4 = None
    return _lz4


def available_codecs():
    out = [CODEC_NONE]
    if lz4_lib() is not None:
        out.append(CODEC_LZ4)
    if zstd_lib() is not None:
        out.append(CODEC_ZSTD)
    return out


def versions():
    v = {}
    z = zstd_lib()
    if z is not None:
        v["zstd"] = "%d.%d.%d" % (z.ZSTD_versionNumber() // 10000,
                                  (z.ZSTD_versionNumber() // 100) % 100,
                                  z.ZSTD_versionNumber() % 100)
    l = lz4_lib()
    if l is not None:
        v["lz4"] = "present"
    return v


def compress(data: bytes, codec: int, level: int = 1) -> bytes:
    """Compress ONE stream of ONE block. Never spans blocks."""
    if codec == CODEC_NONE or not data:
        return bytes(data)
    if codec == CODEC_ZSTD:
        lib = zstd_lib()
        if lib is None:
            return bytes(data)
        bound = lib.ZSTD_compressBound(len(data))
        dst = ctypes.create_string_buffer(bound)
        n = lib.ZSTD_compress(dst, bound, data, len(data), int(level))
        if lib.ZSTD_isError(n):
            raise RuntimeError(f"zstd compress failed: "
                               f"{lib.ZSTD_getErrorName(n).decode()}")
        return dst.raw[:n]
    if codec == CODEC_LZ4:
        lib = lz4_lib()
        if lib is None:
            return bytes(data)
        bound = lib.LZ4_compressBound(len(data))
        src = ctypes.create_string_buffer(bytes(data), len(data))
        dst = ctypes.create_string_buffer(bound)
        n = lib.LZ4_compress_default(src, dst, len(data), bound)
        if n <= 0:
            raise RuntimeError("lz4 compress failed")
        return dst.raw[:n]
    raise ValueError(f"unknown codec {codec}")


def decompress(data: bytes, raw_size: int, codec: int) -> bytes:
    """Decompress ONE stream, given its UNCOMPRESSED size from the directory.

    raw_size is authoritative because it is stored per stream in the block
    directory, so no frame header needs to be parsed.
    """
    if codec == CODEC_NONE or raw_size <= 0:
        return bytes(data)
    dst = ctypes.create_string_buffer(raw_size)
    if codec == CODEC_ZSTD:
        lib = zstd_lib()
        if lib is None:
            raise RuntimeError("zstd unavailable")
        n = lib.ZSTD_decompress(dst, raw_size, data, len(data))
        if lib.ZSTD_isError(n) or n != raw_size:
            raise RuntimeError(f"zstd decompress failed: "
                               f"{lib.ZSTD_getErrorName(n).decode()}")
        return dst.raw[:raw_size]
    if codec == CODEC_LZ4:
        lib = lz4_lib()
        if lib is None:
            raise RuntimeError("lz4 unavailable")
        src = ctypes.create_string_buffer(bytes(data), len(data))
        n = lib.LZ4_decompress_safe(src, dst, len(data), raw_size)
        if n != raw_size:
            raise RuntimeError(f"lz4 decompress failed: got {n} want {raw_size}")
        return dst.raw[:raw_size]
    raise ValueError(f"unknown codec {codec}")


def per_block_fallback(raw_size: int, comp_size: int, threshold: float = 0.95) -> bool:
    """PER-BLOCK FALLBACK (spec): if compression bought essentially nothing,
    store that stream as CODEC_NONE so the runtime never wastes CPU on
    incompressible data. Operates per stream, so one bad stream cannot poison
    the rest of the block."""
    return comp_size >= raw_size * threshold