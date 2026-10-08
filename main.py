
###################################################################################################
import os
import sys
import traceback
from pathlib import Path
# ---------------------------------------------------------------------------
# PROJECT RUNTIME (production truth)
# ---------------------------------------------------------------------------
# The acceptance command is `py main.py`. `py` usually resolves to whatever
# launcher is on PATH, which is NOT the interpreter this project is built and
# tested against. Re-exec ONCE under the project venv so every fix in this
# tree is the code that actually runs.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")


def _ensure_project_runtime():
    """Re-exec under the project venv, exactly once. Returns never (it execs)."""
    try:
        current = os.path.normcase(os.path.abspath(sys.executable))
        wanted = os.path.normcase(os.path.abspath(VENV_PYTHON))
        if current == wanted or os.environ.get("NAKSHA_VENV_REEXEC") == "1":
            return False
        if not os.path.isfile(VENV_PYTHON):
            print("=" * 70)
            print("FATAL: project virtual environment is missing.")
            print(f"  expected: {VENV_PYTHON}")
            print("  Refusing to continue under a different interpreter.")
            print("  Restore the venv (or run this project with its own python).")
            print("=" * 70)
            raise SystemExit(2)
        import subprocess
        env = dict(os.environ)
        env["NAKSHA_VENV_REEXEC"] = "1"
        # The project root first, so nothing can import another tree.
        env["PYTHONPATH"] = PROJECT_ROOT + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        print(f"[NAKSHA RUNTIME] relaunching under {VENV_PYTHON}")
        print(f"[NAKSHA RUNTIME] (was {sys.executable})")
        sys.stdout.flush()
        sys.stderr.flush()
        code = subprocess.call([VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:],
                               env=env, cwd=os.getcwd())
        raise SystemExit(code)
    except SystemExit:
        raise
    except Exception as _re_err:
        print(f"[NAKSHA RUNTIME] relaunch failed: {_re_err!r}")
        return False


_ensure_project_runtime()

# PROJECT_ROOT first on sys.path: the real tree must win over any other
# Naksha copy, site-packages shadow or reference checkout.
if PROJECT_ROOT in sys.path:
    sys.path.remove(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)


def _runtime_self_check(win):
    """Startup self-check: runtime, source tree, native build, main view.

    Path/config sanity only - never loads a dataset. Prints the production
    identity block so it is always obvious whether `py main.py` is running the
    current tree, and STOPS if a module or the native DLL comes from anywhere
    other than this project.
    """
    import importlib
    import os as _os
    import sys as _sys

    failures = []

    def _mod(name):
        try:
            m = importlib.import_module(name)
            return _os.path.abspath(getattr(m, "__file__", "") or "")
        except Exception as _e:
            failures.append(f"import {name}: {_e!r}")
            return "<unavailable>"

    mods = {
        "app_window": _mod("gui.app_window"),
        "render_backend": _mod("gui.render_backend"),
        "stream_manager": _mod("gui.naksha_cache.stream_manager"),
        "app_streaming": _mod("gui.naksha_cache.app_streaming"),
        "zoom_navigation": _mod("gui.zoom_navigation"),
    }
    root_norm = os.path.normcase(PROJECT_ROOT)
    for _n, _p in mods.items():
        _pn = os.path.normcase(_p)
        if _pn == root_norm or _pn.startswith(root_norm + os.sep):
            continue
        failures.append(f"{_n} resolves OUTSIDE the project: {_p}")

    print("\n" + "=" * 70)
    print("[NAKSHA PRODUCTION RUNTIME]")
    print(f"  python:          {_sys.executable}")
    print(f"  project_root:    {PROJECT_ROOT}")
    print(f"  main_py:         {_os.path.abspath(__file__)}")
    for _n, _p in mods.items():
        print(f"  {_n + ':':<18}{_p}")

    # ---- native DLL: which one, and is it stale? (sections 9/10) ----------
    dll_path = "<not found>"
    dll_mtime = ""
    src_mtime = ""
    stale = False
    try:
        from gui import render_backend as _rb
        _bk = getattr(_rb, "VulkanRenderBackend", None)
        for _cand in (
            _os.path.join(PROJECT_ROOT, "native", "naksha_vulkan",
                          "build_msvc", "naksha_vulkan.dll"),
            _os.path.join(PROJECT_ROOT, "naksha_vulkan.dll"),
        ):
            if _os.path.isfile(_cand):
                dll_path = _cand
                break
        if dll_path != "<not found>":
            dll_mtime = _os.path.getmtime(dll_path)
            _latest = 0.0
            _ndir = _os.path.join(PROJECT_ROOT, "native", "naksha_vulkan")
            if _os.path.isdir(_ndir):
                for _dp, _dn, _fs in _os.walk(_ndir):
                    if "build" in _dp.replace("\\", "/").split("/"):
                        continue
                    for _f in _fs:
                        if _f.endswith((".cpp", ".hpp", ".h", ".glsl")):
                            try:
                                _latest = max(_latest,
                                              _os.path.getmtime(
                                                  _os.path.join(_dp, _f)))
                            except Exception:
                                pass
            src_mtime = _latest
            stale = bool(_latest and _latest > dll_mtime)
    except Exception as _e:
        failures.append(f"native dll probe: {_e!r}")

    print("[NAKSHA NATIVE BUILD]")
    print(f"  dll_path:                    {dll_path}")
    print(f"  dll_mtime:                   {dll_mtime}")
    print(f"  native_source_latest_mtime:  {src_mtime}")
    print(f"  stale:                       {'YES' if stale else 'NO'}")
    if stale:
        failures.append(
            "NAKSHA VULKAN DLL IS STALE - REBUILD REQUIRED. Rebuild with the "
            "existing native build (native\\naksha_vulkan\\build_msvc).")

    # ---- which main view is actually visible (section 4/14) --------------
    try:
        from gui import render_backend as _rb2
        _vk_on = _rb2.vulkan_viewport_enabled()
    except Exception:
        _vk_on = False
    _rbo = getattr(win, "render_backend", None)
    _vk_vis = False
    try:
        _vw = getattr(_rbo, "vulkan_widget", None)
        _vk_vis = bool(_vw is not None and _vw.isVisible())
    except Exception:
        pass
    print("[MAIN VIEW PRODUCTION PATH]")
    print(f"  backend:                "
          f"{'VULKAN' if (_vk_on and _vk_vis) else 'VTK'}")
    print(f"  Vulkan main viewport:   {'YES' if _vk_on else 'NO'}")
    print(f"  visible widget:         "
          f"{'render_backend._VulkanSurfaceWidget' if _vk_vis else 'app_window vtk_widget'}")
    print(f"  camera input owner:     gui/app_window.py")
    print(f"  camera mirror owner:    gui/render_backend.py")
    print(f"  stream manager:         gui/naksha_cache/stream_manager.py")

    print("[NAKSHA BUILD]")
    print(f"  python_build: {PROJECT_ROOT}")
    print(f"  native_build: {dll_path}")

    print("[NAKSHA RUNTIME SELF-CHECK] "
          + ("PASS" if not failures else "FAIL"))
    for f in failures:
        print(f"  - {f}")
    print("=" * 70 + "\n")
    if failures:
        raise SystemExit(
            "NAKSHA startup self-check FAILED:\n  - "
            + "\n  - ".join(failures))
 
 
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

        _runtime_self_check(win)

        # Start the device-targeted updater after the heavy GUI startup has
        # settled. Checks repeat every six hours and never block the UI.
        def _start_automatic_update_check():
            from datetime import datetime
            from PySide6.QtCore import QSettings
            from gui.update_manager import UpdateManager, UpdateWorker

            settings = QSettings("NakshaAI", "LidarApp")
            if not settings.value("updates/auto_check", True, type=bool):
                return
            manager = getattr(win, "_automatic_update_manager", None)
            if manager is None:
                manager = UpdateManager(settings)
                win._automatic_update_manager = manager
            if not manager.configured:
                return
            running = getattr(win, "_automatic_update_worker", None)
            if running is not None and running.isRunning():
                return

            worker = UpdateWorker(manager, "check", parent=win)
            win._automatic_update_worker = worker

            def _check_finished(info):
                settings.setValue(
                    "updates/last_checked", datetime.now().strftime("%Y-%m-%d %H:%M")
                )
                settings.sync()
                if info is None or getattr(win, "_shutdown_in_progress", False):
                    return
                win.open_global_settings()
                dialog = getattr(win, "_global_settings_dialog", None)
                if dialog is None:
                    return
                dialog.category_list.setCurrentRow(5)
                dialog._update_check_finished(info)
                if dialog.auto_download_updates_cb.isChecked():
                    dialog._pending_auto_download = False
                    dialog._download_update()

            def _check_failed(message):
                write_startup_log(f"Automatic update check skipped: {message}")

            def _check_cleanup():
                win._automatic_update_worker = None
                worker.deleteLater()

            worker.succeeded.connect(_check_finished)
            worker.failed.connect(_check_failed)
            worker.finished.connect(_check_cleanup)
            worker.start()

        win._automatic_update_timer = QTimer(win)
        win._automatic_update_timer.setInterval(6 * 60 * 60 * 1000)
        win._automatic_update_timer.timeout.connect(_start_automatic_update_check)
        win._automatic_update_timer.start()
        QTimer.singleShot(15_000, _start_automatic_update_check)

        # Handle .snt file passed via Windows double-click / shell open
        # Use app.arguments() as backup Ã¢â‚¬â€ Qt may strip sys.argv entries
        _argv = sys.argv[1:]
        _app_args = app.arguments()[1:] if hasattr(app, 'arguments') else []
        _all_args = _argv + _app_args
        _open_snt_paths = [
            p for p in _all_args
            if os.path.isfile(p) and p.lower().endswith(".snt")
        ]
        if _open_snt_paths:
            QTimer.singleShot(500, lambda paths=_open_snt_paths: win.open_snt_files_from_shell(paths))

        # Dev instrumentation only: close the Instant Shaded telemetry session
        # so the analyzer can distinguish a normal close from a cut-short log.
        # Fully guarded - a telemetry problem must never change the app's exit
        # code or block shutdown.
        try:
            _rc = int(app.exec())
        finally:
            try:
                from gui.instant_shaded_telemetry import emit as _emit_tel
                from gui.instant_shaded_telemetry import close as _close_tel
                try:
                    _emit_tel("application_shutdown",
                              exit_code=int(locals().get("_rc", -1)),
                              normal=locals().get("_rc", -1) == 0)
                finally:
                    _close_tel("normal" if locals().get("_rc", -1) == 0 else "abnormal")
            except Exception:
                pass
        sys.exit(locals().get("_rc", 0))

 
    except SystemExit:
        raise
 
    except Exception:
        error_text = traceback.format_exc()
        write_startup_log(error_text)
        show_startup_error("NakshaAI-LiDAR Startup Error", error_text)
        raise
