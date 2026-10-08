"""Append-only project sections, with two independently checked commit slots.

The base cache is imported once, byte for byte. Derived commits append payloads
and a checked directory, fsync, then publish one small inactive header slot.
Interrupted appends cannot invalidate the previous directory or base bytes.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import struct
import threading
import zlib

MAGIC = b"NKSHAC01"
HEADER_SIZE = 128
SLOT = struct.Struct("<QQI12x")
FOOTER = struct.Struct("<8sQQ32s")
FOOT_MAGIC = b"NKSHADIR"
_locks = {}
_locks_guard = threading.Lock()


def project_container_path(dataset):
    path = Path(dataset)
    return path if path.suffix.lower() == ".naksha" else path.with_suffix(".naksha")


@contextlib.contextmanager
def exclusive_writer(path):
    key = os.path.normcase(os.path.abspath(path))
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    with lock, open(str(path) + ".lock", "a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class DerivedContainer:
    def __init__(self, path):
        self.path = Path(path)
        self.directory, self.generation = self._read_directory()

    def _read_directory(self):
        with self.path.open("rb") as handle:
            header = handle.read(HEADER_SIZE)
            if len(header) != HEADER_SIZE or header[:8] != MAGIC:
                raise ValueError("invalid .naksha header")
            if struct.unpack_from("<I", header, 8)[0] != 1:
                raise ValueError("unsupported .naksha section version")
            size = os.fstat(handle.fileno()).st_size
            candidates = []
            for offset in (16, 48):
                generation, footer_at, checksum = SLOT.unpack_from(header, offset)
                if generation and zlib.crc32(struct.pack("<QQ", generation, footer_at)) == checksum:
                    candidates.append((generation, footer_at))
            for generation, footer_at in sorted(candidates, reverse=True):
                if not HEADER_SIZE <= footer_at <= size - FOOTER.size:
                    continue
                handle.seek(footer_at)
                magic, at, length, digest = FOOTER.unpack(handle.read(FOOTER.size))
                if magic != FOOT_MAGIC or at < HEADER_SIZE or at + length != footer_at or length > 64 << 20:
                    continue
                handle.seek(at)
                data = handle.read(length)
                if hashlib.sha256(data).digest() != digest:
                    continue
                directory = json.loads(data)
                if directory.get("generation") != generation:
                    continue
                for section in directory["sections"].values():
                    for block in section.get("blocks", {}).values():
                        if not HEADER_SIZE <= block["offset"] <= at or block["offset"] + block["size"] > at:
                            raise ValueError("section block outside committed payload")
                return directory, generation
        raise ValueError("no valid committed .naksha directory")

    def read_block(self, section, key):
        if section == "BASE_POINTS" and self.directory["sections"][section]["version"] == 2:
            raise ValueError("BASE_DATA must be read through the random-access point-stream reader")
        item = self.directory["sections"][section]["blocks"][str(key)]
        with self.path.open("rb") as handle:
            handle.seek(item["offset"])
            data = handle.read(item["size"])
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["checksum"]:
            raise ValueError("derived block checksum mismatch")
        codec = item.get("codec", "NONE")
        if codec == "ZLIB1":
            data = zlib.decompress(data)
        elif codec in ("ZSTD", "LZ4"):
            from .codecs import decompress
            from .format import CODEC_ZSTD, CODEC_LZ4
            data = decompress(data, int(item["uncompressed_size"]),
                              CODEC_ZSTD if codec == "ZSTD" else CODEC_LZ4)
        elif codec != "NONE":
            raise ValueError("unsupported derived stream codec")
        if len(data) != item.get("uncompressed_size", len(data)):
            raise ValueError("derived block decoded size mismatch")
        return data, item

    @staticmethod
    def _commit(handle, directory, fail_hook=None):
        generation = directory["generation"]
        handle.seek(0, 2)
        at = handle.tell()
        data = json.dumps(directory, sort_keys=True, separators=(",", ":")).encode()
        handle.write(data)
        footer_at = handle.tell()
        handle.write(FOOTER.pack(FOOT_MAGIC, at, len(data), hashlib.sha256(data).digest()))
        handle.flush()
        os.fsync(handle.fileno())
        if fail_hook:
            fail_hook("before_publish")
        checksum = zlib.crc32(struct.pack("<QQ", generation, footer_at))
        handle.seek(16 if generation % 2 else 48)
        handle.write(SLOT.pack(generation, footer_at, checksum))
        handle.flush()
        os.fsync(handle.fileno())

    @classmethod
    def import_base(cls, path, index_path, points_path, identity, cancel=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_writer(path):
            if path.exists():
                current = cls(path)
                if current.directory["identity"] != identity:
                    raise ValueError("STALE .naksha base; use a new project path for a rebuilt layout")
                return current
            temporary = Path(str(path) + ".import.tmp")
            try:
                with temporary.open("w+b") as handle:
                    header = bytearray(HEADER_SIZE)
                    header[:8] = MAGIC
                    struct.pack_into("<I", header, 8, 1)
                    handle.write(header)
                    directory = {"generation": 1, "identity": identity, "sections": {}}
                    for name, source in (("BASE_INDEX", index_path), ("BASE_POINTS", points_path)):
                        at = handle.tell()
                        digest = hashlib.sha256()
                        with open(source, "rb") as input_file:
                            while chunk := input_file.read(4 << 20):
                                if cancel is not None and cancel.is_set():
                                    raise InterruptedError("project import cancelled")
                                handle.write(chunk)
                                digest.update(chunk)
                        directory["sections"][name] = {"version": 1, "blocks": {"base": {
                            "offset": at, "size": handle.tell() - at,
                            "checksum": digest.hexdigest(), "codec": "NONE"}}}
                    cls._commit(handle, directory)
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return cls(path)

    def append(self, section, metadata, blocks, *, fail_hook=None):
        """Publish one bounded batch; base ranges are never rewritten."""
        with exclusive_writer(self.path):
            current = DerivedContainer(self.path)
            directory = current.directory
            old = directory["sections"].get(section)
            if old is None or old["metadata"] != metadata:
                old = {"version": 1, "metadata": metadata, "blocks": {}}
            directory["sections"][section] = old
            with self.path.open("r+b") as handle:
                handle.seek(0, 2)
                for key, data, attributes in blocks:
                    from .codecs import compress
                    from .format import CODEC_ZSTD
                    stored = compress(data, CODEC_ZSTD, level=1)
                    codec = "ZSTD"
                    item = dict(attributes, offset=handle.tell(), size=len(stored),
                                uncompressed_size=len(data), codec=codec, codec_level=1,
                                checksum=hashlib.sha256(stored).hexdigest())
                    handle.write(stored)
                    old["blocks"][str(key)] = item
                directory["generation"] = current.generation + 1
                self._commit(handle, directory, fail_hook)
            self.directory, self.generation = directory, directory["generation"]
