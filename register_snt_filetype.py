"""
Register .snt file type association in Windows.

This script sets up the Windows registry so that .snt files display
the custom snt_file.ico icon in Explorer — similar to how MicroStation
registers .dgn files.

Usage:
    Run as Administrator:
        python register_snt_filetype.py --install

    To uninstall:
        python register_snt_filetype.py --uninstall

This is also called automatically by the installer (post-install hook).
"""

import os
import sys
import ctypes
import winreg
import argparse
from pathlib import Path


# ── Configuration ─────────────────────────────────────────────────────────────
FILE_EXTENSION = ".snt"
PROG_ID = "Naksha.SNTFile"
FILE_TYPE_NAME = "Naksha SNT File"
ICON_FILENAME = "snt_file.ico"


def _is_admin():
    """Check if running with administrator privileges."""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _get_icon_path():
    """
    Resolve the icon path. Works in dev, PyInstaller --onedir, and --onefile modes.

    IMPORTANT: For registry file association, we MUST use a PERMANENT path
    (not _MEIPASS which is a temp folder deleted on exit).
    The icon must live next to the exe in the install directory.
    """
    if getattr(sys, 'frozen', False):
        # ALWAYS use the exe's directory for the registry icon path.
        # _MEIPASS is temporary and gets cleaned up — registry needs a permanent path.
        exe_dir = Path(sys.executable).parent

        # Standard location: <install_dir>/icons/snt_file.ico
        ico = exe_dir / "icons" / ICON_FILENAME
        if ico.exists():
            return str(ico)

        # Legacy / alternate layout: <install_dir>/gui/icons/snt_file.ico
        ico = exe_dir / "gui" / "icons" / ICON_FILENAME
        if ico.exists():
            return str(ico)

        # Fallback: <install_dir>/snt_file.ico (flat layout)
        ico = exe_dir / ICON_FILENAME
        if ico.exists():
            return str(ico)

        # PyInstaller --onedir: check _internal folder
        ico = exe_dir / "_internal" / "icons" / ICON_FILENAME
        if ico.exists():
            return str(ico)

        ico = exe_dir / "_internal" / "gui" / "icons" / ICON_FILENAME
        if ico.exists():
            return str(ico)

    # Development mode: relative to this script
    ico = Path(__file__).parent / "icons" / ICON_FILENAME
    if ico.exists():
        return str(ico)

    ico = Path(__file__).parent / "gui" / "icons" / ICON_FILENAME
    if ico.exists():
        return str(ico)

    return None


def _notify_shell():
    """Tell Windows Explorer to refresh file icons."""
    try:
        from ctypes import windll, c_int, c_void_p, c_wchar_p
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None
        )
        print("   🔄 Explorer icon cache refreshed")
    except Exception as e:
        print(f"   ⚠️  Could not refresh icon cache: {e}")
        print("      You may need to restart Explorer or log out/in.")


def install_file_association(icon_path=None):
    """
    Register .snt file type with custom icon in Windows registry.

    Creates:
      HKCR\\.snt  → (Default) = "Naksha.SNTFile"
      HKCR\\Naksha.SNTFile → (Default) = "Naksha SNT File"
      HKCR\\Naksha.SNTFile\\DefaultIcon → (Default) = "<path_to_ico>,0"
    """
    if icon_path is None:
        icon_path = _get_icon_path()

    if icon_path is None:
        print("ERROR: snt_file.ico not found!")
        print("       Run 'python create_snt_ico.py' first to generate it.")
        return False

    icon_path = os.path.abspath(icon_path)
    if not os.path.isfile(icon_path):
        print(f"ERROR: Icon file does not exist: {icon_path}")
        return False

    print(f"📁 Registering .snt file type association...")
    print(f"   Icon: {icon_path}")

    try:
        # 1. Create .snt extension key → points to ProgID
        with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, FILE_EXTENSION) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, PROG_ID)
            # Also set Content Type
            winreg.SetValueEx(key, "Content Type", 0, winreg.REG_SZ, "application/x-snt")
        print(f"   ✅ HKCR\\{FILE_EXTENSION} → {PROG_ID}")

        # 2. Create ProgID key with friendly name
        with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, PROG_ID) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, FILE_TYPE_NAME)
        print(f"   ✅ HKCR\\{PROG_ID} → \"{FILE_TYPE_NAME}\"")

        # 3. Set the DefaultIcon
        icon_value = f"{icon_path},0"
        with winreg.CreateKey(winreg.HKEY_CLASSES_ROOT, f"{PROG_ID}\\DefaultIcon") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon_value)
        print(f"   ✅ HKCR\\{PROG_ID}\\DefaultIcon → \"{icon_value}\"")

        # 4. Optionally register "Open with Naksha" shell command
        exe_path = sys.executable if getattr(sys, 'frozen', False) else None
        if exe_path:
            open_cmd = f'"{exe_path}" "%1"'
            with winreg.CreateKey(
                winreg.HKEY_CLASSES_ROOT, f"{PROG_ID}\\shell\\open\\command"
            ) as key:
                winreg.SetValueEx(key, "", 0, winreg.REG_SZ, open_cmd)
            print(f"   ✅ Shell open command registered")

        # 5. Notify Explorer
        _notify_shell()

        print("\n✅ Done! .snt files will now show the Naksha icon in Explorer.")
        return True

    except PermissionError:
        print("\n❌ ERROR: Administrator privileges required!")
        print("   Right-click → 'Run as administrator'")
        return False
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return False


def uninstall_file_association():
    """Remove .snt file type registration from Windows registry."""
    print(f"🗑️  Removing .snt file type association...")

    errors = []

    # Remove ProgID subkeys first (must delete children before parent)
    for subkey in [
        f"{PROG_ID}\\shell\\open\\command",
        f"{PROG_ID}\\shell\\open",
        f"{PROG_ID}\\shell",
        f"{PROG_ID}\\DefaultIcon",
        PROG_ID,
    ]:
        try:
            winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, subkey)
            print(f"   ✅ Deleted HKCR\\{subkey}")
        except FileNotFoundError:
            pass
        except PermissionError:
            errors.append(subkey)
        except Exception as e:
            errors.append(f"{subkey}: {e}")

    # Remove extension key
    try:
        winreg.DeleteKey(winreg.HKEY_CLASSES_ROOT, FILE_EXTENSION)
        print(f"   ✅ Deleted HKCR\\{FILE_EXTENSION}")
    except FileNotFoundError:
        pass
    except PermissionError:
        errors.append(FILE_EXTENSION)

    if errors:
        print(f"\n⚠️  Could not delete (need admin): {errors}")
        return False

    _notify_shell()
    print("\n✅ .snt file association removed.")
    return True


def uninstall_for_current_user():
    """Remove the current user's .snt association without admin rights."""
    base = r"Software\Classes"
    errors = []

    for subkey in [
        f"{base}\\{PROG_ID}\\shell\\open\\command",
        f"{base}\\{PROG_ID}\\shell\\open",
        f"{base}\\{PROG_ID}\\shell",
        f"{base}\\{PROG_ID}\\DefaultIcon",
        f"{base}\\{PROG_ID}",
        f"{base}\\{FILE_EXTENSION}",
    ]:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
        except FileNotFoundError:
            pass
        except Exception as exc:
            errors.append(f"{subkey}: {exc}")

    _notify_shell()
    if errors:
        print(f"Could not remove some current-user association keys: {errors}")
        return False

    print("Current-user .snt association removed.")
    return True


def install_for_current_user(icon_path=None):
    """
    Register .snt file type for current user only (no admin required).

    Uses HKEY_CURRENT_USER\\Software\\Classes instead of HKEY_CLASSES_ROOT.
    """
    if icon_path is None:
        icon_path = _get_icon_path()

    if icon_path is None:
        print("ERROR: snt_file.ico not found!")
        print("       Run 'python create_snt_ico.py' first to generate it.")
        return False

    icon_path = os.path.abspath(icon_path)
    if not os.path.isfile(icon_path):
        print(f"ERROR: Icon file does not exist: {icon_path}")
        return False

    print(f"📁 Registering .snt file type (current user)...")
    print(f"   Icon: {icon_path}")

    base = r"Software\Classes"

    try:
        # 1. Extension → ProgID
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\{FILE_EXTENSION}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, PROG_ID)
        print(f"   ✅ HKCU\\{base}\\{FILE_EXTENSION} → {PROG_ID}")

        # 2. ProgID friendly name
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\{PROG_ID}") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, FILE_TYPE_NAME)
        print(f"   ✅ HKCU\\{base}\\{PROG_ID} → \"{FILE_TYPE_NAME}\"")

        # 3. DefaultIcon
        icon_value = f"{icon_path},0"
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, f"{base}\\{PROG_ID}\\DefaultIcon"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, icon_value)
        print(f"   ✅ DefaultIcon → \"{icon_value}\"")

        # 4. Register "Open with" shell command (so double-click works)
        exe_path = sys.executable if getattr(sys, 'frozen', False) else None
        if exe_path:
            open_cmd = f'"{exe_path}" "%1"'
            try:
                with winreg.CreateKey(
                    winreg.HKEY_CURRENT_USER, f"{base}\\{PROG_ID}\\shell\\open\\command"
                ) as key:
                    winreg.SetValueEx(key, "", 0, winreg.REG_SZ, open_cmd)
                print(f"   ✅ Shell open command registered (current user)")
            except Exception as e:
                print(f"   ⚠️  Could not register shell command: {e}")


        # 5. Notify Explorer
        _notify_shell()

        print("\n✅ Done! .snt files will now show the Naksha icon (current user).")
        return True


    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Register/unregister .snt file type icon in Windows"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--install", action="store_true",
                       help="Register .snt file association (system-wide, needs admin)")
    group.add_argument("--install-user", action="store_true",
                       help="Register .snt file association (current user only, no admin)")
    group.add_argument("--uninstall", action="store_true",
                       help="Remove .snt file association")
    parser.add_argument("--icon", type=str, default=None,
                        help="Path to .ico file (auto-detected if omitted)")

    args = parser.parse_args()

    if args.install:
        if not _is_admin():
            print("⚠️  Not running as admin. Attempting current-user registration...")
            install_for_current_user(args.icon)
        else:
            install_file_association(args.icon)
    elif args.install_user:
        install_for_current_user(args.icon)
    elif args.uninstall:
        if not _is_admin():
            print("⚠️  Admin required for full uninstall. Trying current-user keys...")
            # Try removing from HKCU
            base = r"Software\Classes"
            for subkey in [
                f"{base}\\{PROG_ID}\\shell\\open\\command",
                f"{base}\\{PROG_ID}\\shell\\open",
                f"{base}\\{PROG_ID}\\shell",
                f"{base}\\{PROG_ID}\\DefaultIcon",
                f"{base}\\{PROG_ID}",
                f"{base}\\{FILE_EXTENSION}",
            ]:
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, subkey)
                except (FileNotFoundError, PermissionError):
                    pass
            _notify_shell()
            print("✅ Current-user .snt association removed.")
        else:
            uninstall_file_association()


if __name__ == "__main__":
    main()
