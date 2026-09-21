
###################################################################################################
import os
import sys
import traceback
from pathlib import Path
 
 
def _configure_console_utf8():
    """Force UTF-8 console output on Windows to avoid mojibake in logs."""
    if os.name != "nt":
        return
 
    try:
        import ctypes
 
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleOutputCP(65001)
        kernel32.SetConsoleCP(65001)
    except Exception:
        pass
 
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
 
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            try:
                stream = open(os.devnull, "w", encoding="utf-8", buffering=1)
                setattr(sys, stream_name, stream)
            except Exception:
                continue
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
 
 
_configure_console_utf8()


def _configure_gdal_proj_env():
    """Point GDAL/PROJ at rasterio's own bundled data, process-scoped only.

    Some machines have PROJ_LIB/GDAL_DATA set system-wide by an unrelated
    PostgreSQL/PostGIS install. That copy of proj.db can be an older schema
    version than what this app's rasterio/GDAL build expects, which makes
    every CRS lookup fail (breaks GeoTIFF import/ortho rendering) with no
    obvious error to the user. Only override os.environ for this process
    when the existing value looks like that PostGIS path - never touches
    the persistent system/user environment variables, so other apps that
    rely on those vars (e.g. actual PostgreSQL/PostGIS tools) are unaffected.
    """
    try:
        import importlib.util
        spec = importlib.util.find_spec("rasterio")
        if spec is None or not spec.origin:
            return
        rasterio_dir = Path(spec.origin).parent
        proj_data = rasterio_dir / "proj_data"
        gdal_data = rasterio_dir / "gdal_data"
    except Exception:
        return

    def _looks_like_postgis(value: str | None) -> bool:
        return bool(value) and "postgis" in value.lower()

    if proj_data.is_dir() and _looks_like_postgis(os.environ.get("PROJ_LIB")):
        os.environ["PROJ_LIB"] = str(proj_data)
    if gdal_data.is_dir() and _looks_like_postgis(os.environ.get("GDAL_DATA")):
        os.environ["GDAL_DATA"] = str(gdal_data)


_configure_gdal_proj_env()


def _run_maintenance_command() -> int | None:
    """Handle installer maintenance commands before importing the GUI stack."""
    if "--unregister-snt" not in sys.argv[1:]:
        return None

    if getattr(sys, "frozen", False):
        internal_dir = Path(sys.executable).parent / "_internal"
        if str(internal_dir) not in sys.path:
            sys.path.insert(0, str(internal_dir))

    try:
        from register_snt_filetype import (
            _is_admin,
            uninstall_file_association,
            uninstall_for_current_user,
        )

        user_ok = uninstall_for_current_user()
        machine_ok = uninstall_file_association() if _is_admin() else True
        return 0 if user_ok and machine_ok else 1
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    _maintenance_exit_code = _run_maintenance_command()
    if _maintenance_exit_code is not None:
        raise SystemExit(_maintenance_exit_code)
 
import open3d as o3d
from PySide6.QtCore import qInstallMessageHandler, QTimer
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QApplication, QMessageBox
from gui.app_icon import apply_application_icon, set_windows_appusermodel_id
from gui.app_window import NakshaApp
from gui.crash_reporter import CrashReporter
# from license_client import require_valid_license, start_license_heartbeat
 
 
LOG_DIR = Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "NakshaTech" / "NakshaAI-LiDAR"
STARTUP_LOG_FILE = LOG_DIR / "startup_error.log"
 
_previous_qt_message_handler = None
 
 
def write_startup_log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with STARTUP_LOG_FILE.open("a", encoding="utf-8") as file:
            file.write(message + "\n")
    except Exception:
        pass
 
 
def show_startup_error(title: str, message: str) -> None:
    try:
        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)
 
        QMessageBox.critical(None, title, message)
    except Exception:
        pass
 
 
def install_qt_message_filter():
    """Filter a narrow set of noisy Qt/Windows lifecycle debug messages."""
    global _previous_qt_message_handler
    if _previous_qt_message_handler is not None:
        return
 
    noisy_prefixes = (
        "External WM_DESTROY received",
    )
 
    def message_handler(msg_type, context, message):
        if any(message.startswith(prefix) for prefix in noisy_prefixes):
            return
 
        if _previous_qt_message_handler is not None:
            _previous_qt_message_handler(msg_type, context, message)
        else:
            stream = sys.stderr
            stream.write(f"{message}\n")
            stream.flush()
 
    _previous_qt_message_handler = qInstallMessageHandler(message_handler)
 
 
if __name__ == "__main__":
    try:
        write_startup_log("===== NakshaAI-LiDAR startup started =====")
 
        # Crash reporter: install Python excepthook first.
        crash_reporter = CrashReporter()
        crash_reporter.install()
        write_startup_log("CrashReporter installed")
 
        # Register .snt file type icon on first launch.
        try:
            import importlib.util
            import importlib

            # In a frozen build, post_install.py and register_snt_filetype.py are
            # bundled into _internal\ by the spec. PyInstaller does NOT add _internal
            # to sys.path automatically, so find_spec() returns None without this.
            if getattr(sys, 'frozen', False):
                _internal = Path(sys.executable).parent / "_internal"
                if str(_internal) not in sys.path:
                    sys.path.insert(0, str(_internal))

            if importlib.util.find_spec("post_install") is not None:
                post_install = importlib.import_module("post_install")
                if hasattr(post_install, "check_file_association_on_launch"):
                    post_install.check_file_association_on_launch()
                    write_startup_log("Post install check completed")
            else:
                write_startup_log("post_install module not found on sys.path")
        except Exception:
            write_startup_log("Post install check skipped")
 
         # IMPORTANT: license check must happen BEFORE opening main software.
        # write_startup_log("License check started")
        # if not require_valid_license():
        #      write_startup_log("License check failed")
        #      show_startup_error(
        #          "License Error",
        #          "License activation failed. Please check activation key or license server."
        #      )
        #      raise SystemExit("License activation failed")
 
        # write_startup_log("License check passed")
 
        # # # Start periodic license check only after valid activation.
        # start_license_heartbeat(interval_seconds=300)
        # write_startup_log("License heartbeat started")
 
        # # Initialize Open3D GUI.
        write_startup_log("Open3D initialization started")
        o3d.visualization.gui.Application.instance.initialize()
        write_startup_log("Open3D initialization completed")
 
        install_qt_message_filter()
        write_startup_log("Qt message filter installed")
 
        set_windows_appusermodel_id()
        app = QApplication(sys.argv)
        apply_application_icon(app)
 
        # ===== BLOCK Alt+F4 AT APPLICATION LEVEL =====
        from PySide6.QtCore import QObject, QEvent, Qt
        import ctypes
        
 
        class AltF4Blocker(QObject):
            """Application-level event filter that blocks Alt+F4 on ALL windows/dialogs."""
 
            VK_MENU = 0x12
            VK_F4 = 0x73
 
            def eventFilter(self, obj, event):
                if event.type() == QEvent.KeyPress:
                    if event.key() == Qt.Key_F4 and (event.modifiers() & Qt.AltModifier):
                        return True
 
                if event.type() == QEvent.ShortcutOverride:
                    if event.key() == Qt.Key_F4 and (event.modifiers() & Qt.AltModifier):
                        event.accept()
                        return True
 
                if event.type() == QEvent.Close and event.spontaneous():
                    if hasattr(obj, "_shutdown_in_progress") and obj._shutdown_in_progress:
                        return False
 
                    alt_down = ctypes.windll.user32.GetAsyncKeyState(self.VK_MENU) & 0x8000
                    f4_down = ctypes.windll.user32.GetAsyncKeyState(self.VK_F4) & 0x8000
 
                    if alt_down or f4_down:
                        event.ignore()
                        return True
                return False
 
        _alt_f4_blocker = AltF4Blocker(app)
        app.installEventFilter(_alt_f4_blocker)
 
        crash_reporter.install_qt_notify(app)
        write_startup_log("Qt crash reporter installed")
 
        write_startup_log("Creating NakshaApp window")
        win = NakshaApp()
        win.show()

        # Handle .snt file passed via Windows double-click / shell open
        # Use app.arguments() as backup — Qt may strip sys.argv entries
        _argv = sys.argv[1:]
        _app_args = app.arguments()[1:] if hasattr(app, 'arguments') else []
        _all_args = _argv + _app_args
        _open_snt_paths = [
            p for p in _all_args
            if os.path.isfile(p) and p.lower().endswith(".snt")
        ]
        if _open_snt_paths:
            QTimer.singleShot(500, lambda paths=_open_snt_paths: win.open_snt_files_from_shell(paths))

        sys.exit(app.exec())


 
    except SystemExit:
        raise
 
    except Exception:
        error_text = traceback.format_exc()
        write_startup_log(error_text)
        show_startup_error("NakshaAI-LiDAR Startup Error", error_text)
        raise
