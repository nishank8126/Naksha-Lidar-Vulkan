#!/usr/bin/env python3
"""Apply the NakshaAI Shaded-Classification sharpness-state integration.

This installer is deliberately surgical. It changes only the two supplied
files and keeps all unrelated rendering/classification/surface behaviour intact.

Default usage from the NakshaAI-Lidar repository root:
    python apply_to_current_files.py

Custom paths:
    python apply_to_current_files.py --shading gui/shading_display.py --app gui/app_window.py
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


def _read_source(path: Path) -> tuple[str, bool, str]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if text.count("\r\n") > text.count("\n") / 2 else "\n"
    # Patch against one normalized representation, then restore the file's
    # original dominant newline style on write.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, has_bom, newline


def _write_source(path: Path, text: str, has_bom: bool, newline: str) -> None:
    if newline != "\n":
        text = text.replace("\n", newline)
    raw = text.encode("utf-8")
    if has_bom:
        raw = b"\xef\xbb\xbf" + raw
    path.write_bytes(raw)


def _backup(path: Path) -> Path:
    base = path.with_name(path.name + ".before_sharpness_sync.bak")
    candidate = base
    serial = 1
    while candidate.exists():
        candidate = path.with_name(path.name + f".before_sharpness_sync.{serial}.bak")
        serial += 1
    shutil.copy2(path, candidate)
    return candidate


def patch_app_window(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    # Keep last_shade_angle as the internal physical elevation, but create a
    # separate persistent user-facing Sharpness state next to it.
    if "self.shading_sharpness_angle = 45.0" not in text:
        init_anchor = (
            "        self.last_shade_angle = 45.0\n"
            "        self.shade_coverage_target = 0.70\n"
        )
        init_repl = (
            "        self.last_shade_angle = 45.0\n"
            "        self.shading_sharpness_angle = 45.0\n"
            "        self.shade_coverage_target = 0.70\n"
        )
        count = text.count(init_anchor)
        if count != 1:
            raise RuntimeError(
                "app_window.py safety check failed: expected exactly one "
                f"shading-state initializer anchor, found {count}."
            )
        text = text.replace(init_anchor, init_repl, 1)
        notes.append("initialized app.shading_sharpness_angle")

    legacy = 'getattr(self, "last_shade_angle", 45.0)'
    replacement = 'getattr(self, "shading_sharpness_angle", 45.0)'
    legacy_count = text.count(legacy)
    current_count = text.count(replacement)

    if legacy_count:
        # The supplied 2026-08-31 app_window has seven user-facing Shading
        # re-entry/rebuild call sites. Refuse unexpected layouts rather than
        # performing a broad unsafe rewrite.
        if legacy_count != 7:
            raise RuntimeError(
                "app_window.py safety check failed: expected 7 legacy "
                f"Shading angle call sites, found {legacy_count}."
            )
        text = text.replace(legacy, replacement)
        notes.append("updated 7 Shading re-entry calls to user Sharpness")
    elif current_count < 7:
        raise RuntimeError(
            "app_window.py safety check failed: neither the expected legacy "
            "nor already-patched Sharpness call layout was found."
        )
    else:
        notes.append("app_window.py Sharpness call sites already integrated")

    return text, notes


def patch_shading_display(text: str) -> tuple[str, list[str]]:
    notes: list[str] = []

    helper_name = "def _resolve_requested_multiclass_sharpness(app, requested_angle) -> float:"
    if helper_name not in text:
        anchor = (
            "    except Exception:\n"
            "        return 45.0\n\n\n"
            "def _shading_sharpness_response(sharpness_value):\n"
        )
        helper = (
            "    except Exception:\n"
            "        return 45.0\n\n\n"
            "def _resolve_requested_multiclass_sharpness(app, requested_angle) -> float:\n"
            "    \"\"\"Resolve user Sharpness without confusing it with light elevation.\n\n"
            "    Multi-class Shaded Classification stores two different values:\n"
            "    ``shading_sharpness_angle`` is the user-facing 0..999 facet\n"
            "    Sharpness, while ``last_shade_angle`` is the internal physical\n"
            "    light elevation used by cached/local shading calculations.\n\n"
            "    Older callers can still pass ``last_shade_angle`` back as the\n"
            "    ``angle`` argument to ``update_shaded_class``. Treat that exact\n"
            "    internal value as a legacy re-entry only when it differs from\n"
            "    the stored user Sharpness. Popup/full-rebuild requests remain\n"
            "    authoritative because they store ``shading_sharpness_angle``\n"
            "    before invoking the shading update.\n"
            "    \"\"\"\n"
            "    if requested_angle is None:\n"
            "        return _shading_sharpness_angle(app)\n\n"
            "    try:\n"
            "        explicit = float(requested_angle)\n"
            "    except Exception:\n"
            "        return _shading_sharpness_angle(app)\n\n"
            "    stored_sharpness = getattr(app, 'shading_sharpness_angle', None)\n"
            "    stored_physical = getattr(app, 'last_shade_angle', None)\n"
            "    if stored_sharpness is not None and stored_physical is not None:\n"
            "        try:\n"
            "            sharpness_value = float(stored_sharpness)\n"
            "            physical_value = float(stored_physical)\n"
            "            if (\n"
            "                abs(explicit - physical_value) <= 1e-9\n"
            "                and abs(sharpness_value - physical_value) > 1e-9\n"
            "            ):\n"
            "                return _shading_sharpness_angle(app, sharpness_value)\n"
            "        except Exception:\n"
            "            pass\n\n"
            "    return _shading_sharpness_angle(app, explicit)\n\n\n"
            "def _shading_sharpness_response(sharpness_value):\n"
        )
        count = text.count(anchor)
        if count != 1:
            raise RuntimeError(
                "shading_display.py safety check failed: expected exactly one "
                f"Sharpness helper insertion anchor, found {count}."
            )
        text = text.replace(anchor, helper, 1)
        notes.append("added legacy-caller Sharpness/elevation resolver")

    old_call = "        sharpness_angle = _shading_sharpness_angle(app, requested_angle)\n"
    new_call = "        sharpness_angle = _resolve_requested_multiclass_sharpness(app, requested_angle)\n"
    old_count = text.count(old_call)
    new_count = text.count(new_call)
    if old_count:
        if old_count != 1:
            raise RuntimeError(
                "shading_display.py safety check failed: expected exactly one "
                f"multi-class requested-angle call, found {old_count}."
            )
        text = text.replace(old_call, new_call, 1)
        notes.append("routed multi-class update_shaded_class through resolver")
    elif new_count != 1:
        raise RuntimeError(
            "shading_display.py safety check failed: requested-angle call site "
            "does not match the supplied current implementation."
        )
    else:
        notes.append("shading_display.py resolver call already integrated")

    return text, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shading", default="gui/shading_display.py")
    parser.add_argument("--app", default="gui/app_window.py")
    args = parser.parse_args()

    shading_path = Path(args.shading).resolve()
    app_path = Path(args.app).resolve()
    for path in (shading_path, app_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    app_text, app_bom, app_newline = _read_source(app_path)
    shading_text, shading_bom, shading_newline = _read_source(shading_path)

    patched_app, app_notes = patch_app_window(app_text)
    patched_shading, shading_notes = patch_shading_display(shading_text)

    # Create backups only after every safety check has succeeded.
    app_backup = _backup(app_path)
    shading_backup = _backup(shading_path)

    _write_source(app_path, patched_app, app_bom, app_newline)
    _write_source(shading_path, patched_shading, shading_bom, shading_newline)

    print("NakshaAI Shading Sharpness integration applied.")
    for note in app_notes + shading_notes:
        print(f"  - {note}")
    print(f"  app backup:     {app_backup}")
    print(f"  shading backup: {shading_backup}")
    print("Run: python -m py_compile gui/shading_display.py gui/app_window.py")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
