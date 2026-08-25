from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon


APP_USER_MODEL_ID = "NakshaTech.NakshaAI.LiDAR"


def _candidate_icon_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    project_root = here.parent

    candidates = [
        project_root / "icons" / "naksha.ico",
        project_root / "icons" / "Nakshatech_Logo-black.jpg",
        project_root / "icons" / "naksha.png",
        here / "icons" / "naksha.ico",
        here / "icons" / "Nakshatech_Logo-black.jpg",
        here / "icons" / "logo.png",
        here / "icons" / "naksha.png",
    ]

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            exe_dir / "_internal" / "gui" / "icons" / "naksha.ico",
            exe_dir / "_internal" / "gui" / "icons" / "Nakshatech_Logo-black.jpg",
            exe_dir / "_internal" / "gui" / "icons" / "logo.png",
            exe_dir / "_internal" / "gui" / "icons" / "naksha.png",
            exe_dir / "icons" / "naksha.ico",
            exe_dir / "icons" / "Nakshatech_Logo-black.jpg",
        ] + candidates

    return candidates


def resolve_app_icon_path() -> str | None:
    for path in _candidate_icon_paths():
        try:
            if path.exists():
                return str(path)
        except Exception:
            continue
    return None


def load_app_icon() -> QIcon:
    icon_path = resolve_app_icon_path()
    if not icon_path:
        return QIcon()

    icon = QIcon(icon_path)
    for size in (16, 24, 32, 48, 64, 128, 256):
        try:
            icon.addFile(icon_path, QSize(size, size))
        except Exception:
            pass
    return icon


def apply_window_icon(widget) -> bool:
    try:
        icon = load_app_icon()
        if icon.isNull():
            return False
        widget.setWindowIcon(icon)
        return True
    except Exception:
        return False


def apply_application_icon(app) -> bool:
    try:
        icon = load_app_icon()
        if icon.isNull():
            return False
        app.setWindowIcon(icon)
        return True
    except Exception:
        return False


def set_windows_appusermodel_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception:
        return False
