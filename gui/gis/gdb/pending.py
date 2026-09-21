"""Session-only representation of FileGDB Feature Datasets that GDAL cannot
persist while empty.

OpenFileGDB materializes a feature dataset as soon as the first feature class is
created.  Until then Naksha keeps an explicit *pending* descriptor so the UI can
show the intended container without pretending it already exists on disk.
"""
from __future__ import annotations

import os

_PENDING: dict[str, dict[str, dict]] = {}


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def register_pending(gdb_path: str, name: str, crs=None) -> dict:
    rec = {"name": str(name), "crs": crs, "pending": True}
    _PENDING.setdefault(_key(gdb_path), {})[str(name).casefold()] = rec
    return rec


def clear_pending(gdb_path: str, name: str) -> None:
    bucket = _PENDING.get(_key(gdb_path))
    if not bucket:
        return
    bucket.pop(str(name).casefold(), None)
    if not bucket:
        _PENDING.pop(_key(gdb_path), None)


def get_pending(gdb_path: str, name: str):
    return (_PENDING.get(_key(gdb_path)) or {}).get(str(name).casefold())


def list_pending(gdb_path: str) -> list[dict]:
    return list((_PENDING.get(_key(gdb_path)) or {}).values())
