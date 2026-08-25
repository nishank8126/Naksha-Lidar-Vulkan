"""
Post-installation script for Naksha.

Registers .snt file type association so .snt files show the Naksha icon
in Windows Explorer (similar to how .dgn shows MicroStation icon).

This script is called:
  1. Automatically by the Inno Setup installer (if used)
  2. Manually after PyInstaller build: python post_install.py
  3. From within the app on first launch (see _check_file_association below)
"""

import os
import sys
import ctypes
from pathlib import Path


def run_post_install():
    """Register .snt file association using the bundled icon."""
    # Determine paths based on frozen/dev mode
    if getattr(sys, 'frozen', False):
        app_dir = Path(sys.executable).parent
    else:
        app_dir = Path(__file__).parent

    # Search for ico in multiple possible locations
    search_paths = [
        app_dir / "icons" / "snt_file.ico",
        app_dir / "gui" / "icons" / "snt_file.ico",
        app_dir / "_internal" / "gui" / "icons" / "snt_file.ico",
        app_dir / "snt_file.ico",
    ]

    ico_path = None
    for p in search_paths:
        if p.exists():
            ico_path = p
            break

    if ico_path is None:
        print(f"⚠️  Icon not found, skipping file association.")
        print(f"   Searched: {[str(p) for p in search_paths]}")
        return

    # Import the registration module
    sys.path.insert(0, str(app_dir))
    from register_snt_filetype import install_for_current_user, install_file_association

    # Try system-wide first (if admin), fall back to current user
    is_admin = False
    try:
        is_admin = ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        pass

    if is_admin:
        install_file_association(str(ico_path))
    else:
        install_for_current_user(str(ico_path))


def check_file_association_on_launch():
    """
    Call this from main app startup to silently ensure .snt is registered.
    Only registers for current user (no admin popup).

    Also REPAIRS the registration if the icon path in registry is stale
    (e.g., app was moved to a different folder or running on a new machine).
    """
    import winreg

    # Determine where the ico file actually is on THIS machine
    if getattr(sys, 'frozen', False):
        app_dir = Path(sys.executable).parent
    else:
        app_dir = Path(__file__).parent

    # Search multiple possible locations
    ico_path = None
    for candidate in [
        app_dir / "icons" / "snt_file.ico",
        app_dir / "gui" / "icons" / "snt_file.ico",
        app_dir / "_internal" / "gui" / "icons" / "snt_file.ico",
        app_dir / "snt_file.ico",
    ]:
        if candidate.exists():
            ico_path = candidate
            break

    if ico_path is None:
        return  # No icon available, nothing to register

    correct_icon_value = f"{ico_path},0"

    # Check if already registered AND icon path is correct
    needs_registration = True
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Classes\Naksha.SNTFile\DefaultIcon"
        ) as key:
            val, _ = winreg.QueryValueEx(key, "")
            if val == correct_icon_value:
                # Also verify the .snt extension points to our ProgID
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Software\Classes\.snt"
                ) as ext_key:
                    ext_val, _ = winreg.QueryValueEx(ext_key, "")
                    if ext_val == "Naksha.SNTFile":
                        needs_registration = False  # All good
    except (FileNotFoundError, OSError):
        pass

    # Also check system-wide (HKCR) if user-level not found
    if needs_registration:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CLASSES_ROOT, r"Naksha.SNTFile\DefaultIcon"
            ) as key:
                val, _ = winreg.QueryValueEx(key, "")
                if val == correct_icon_value:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ".snt") as ext_key:
                        ext_val, _ = winreg.QueryValueEx(ext_key, "")
                        if ext_val == "Naksha.SNTFile":
                            needs_registration = False
        except (FileNotFoundError, OSError):
            pass

    if not needs_registration:
        return

    # Register (or re-register with correct path) for current user
    try:
        from register_snt_filetype import install_for_current_user
        install_for_current_user(str(ico_path))
    except Exception:
        pass  # Non-critical, don't crash the app


if __name__ == "__main__":
    run_post_install()
