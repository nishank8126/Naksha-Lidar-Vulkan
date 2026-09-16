"""
crash_reporter.py - crash reporting helpers for Naksha.

Captures:
- Unhandled Python exceptions via sys.excepthook
- Unhandled Python thread exceptions via threading.excepthook
- Unraisable exceptions via sys.unraisablehook
- Qt event-loop exceptions via QApplication.notify wrapping

The reporter always writes a local crash log first, then attempts email only
when SMTP is fully configured.
"""

import datetime
import faulthandler
import json
import os
import platform
import smtplib
import socket
import sys
import textwrap
import threading
import traceback
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

try:
    import psutil

    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "crash_config.json"

_DEFAULT_CONFIG = {
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "tool.crash.report@gmail.com",
    # Never commit mail credentials. Configure this on deployment machines as
    # NAKSHA_SMTP_PASSWORD or supply it through crash_config.json.
    "smtp_password": os.getenv("NAKSHA_SMTP_PASSWORD", ""),
    "report_to": "software.team@nakshatech.com",
    "app_version": "2.8.0",
    "enabled": True,
    "smtp_timeout_seconds": 5,
}


def _stderr_write(message: str) -> None:
    """Write diagnostics when a console exists; stay silent in windowed EXEs."""
    stream = getattr(sys, "stderr", None)
    if stream is None or not hasattr(stream, "write"):
        return
    try:
        stream.write(str(message))
        stream.flush()
    except Exception:
        pass


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            return {**_DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(_DEFAULT_CONFIG)


def _has_value(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _smtp_is_configured(config: dict) -> bool:
    required = ("smtp_host", "smtp_port", "smtp_user", "smtp_password", "report_to")
    return all(_has_value(config.get(key)) for key in required)


def _smtp_timeout_seconds(config: dict) -> float:
    try:
        timeout = float(config.get("smtp_timeout_seconds", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    return max(1.0, min(timeout, 30.0))


def _safe_repr(value) -> str:
    try:
        return repr(value)
    except Exception:
        return "<unrepresentable object>"

def _log_dir() -> Path:
    program_data = os.getenv("PROGRAMDATA", "C:/ProgramData")

 
    log_dir = (
        Path(program_data)
        / "NakshaTech"
        / "NakshaAI-LiDAR"
        / "crash_logs"
    )
 
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

# --- Temporary notify-dispatch tracing (NAKSHA_NOTIFY_TRACE=<file>) ---
_notify_trace_handle = None
_notify_trace_skipped = {1, 12, 77, 110, 123, 183}  # timer/paint/hover-noise only; mouse events kept

def _notify_trace(receiver, event) -> None:
    """Log every non-noisy Qt event dispatch to diagnose native AVs."""
    global _notify_trace_handle, _notify_trace_skipped
    try:
        if _notify_trace_handle is None:
            trace_path = os.getenv("NAKSHA_NOTIFY_TRACE") or str(_log_dir() / "notify_trace.log")
            _notify_trace_handle = open(trace_path, "a", encoding="utf-8", buffering=1)
        etype = -1
        try:
            etype = event.type()
        except Exception:
            pass
        if etype in _notify_trace_skipped:
            return
        cls = "?"
        try:
            mo = receiver.metaObject()
            cls = mo.className() if mo is not None else type(receiver).__name__
        except Exception:
            try:
                cls = type(receiver).__name__
            except Exception:
                pass
        name = ""
        try:
            name = receiver.objectName() or ""
        except Exception:
            pass
        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _notify_trace_handle.write(f"{ts} {cls}[{name}] ev={etype} id={id(receiver):#x}\n")
    except Exception:
        pass

 
def _collect_platform_info() -> dict:
    return {
        "os": platform.system(),
        "os_version": platform.version(),
        "os_release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "hostname": socket.gethostname(),
    }


def _collect_memory_info() -> dict:
    if not _HAS_PSUTIL:
        return {"note": "psutil not available"}

    try:
        vm = psutil.virtual_memory()
        return {
            "total_ram_gb": round(vm.total / (1024**3), 2),
            "available_ram_gb": round(vm.available / (1024**3), 2),
            "used_ram_gb": round(vm.used / (1024**3), 2),
            "ram_percent_used": vm.percent,
        }
    except Exception as err:
        return {"error": str(err)}


def _collect_cpu_info() -> dict:
    if not _HAS_PSUTIL:
        return {"note": "psutil not available"}

    try:
        return {
            "cpu_count_logical": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
            "cpu_percent_at_crash": psutil.cpu_percent(interval=0.1),
        }
    except Exception as err:
        return {"error": str(err)}


def _collect_gpu_info() -> dict:
    try:
        from gui.gpu_support import get_gpu_info  # type: ignore

        return get_gpu_info() if callable(get_gpu_info) else {"note": "get_gpu_info not callable"}
    except Exception:
        pass

    try:
        import subprocess

        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            gpus = []
            for line in result.stdout.strip().splitlines():
                parts = [part.strip() for part in line.split(",")]
                gpus.append(
                    {
                        "name": parts[0] if len(parts) > 0 else "?",
                        "memory_total_mb": parts[1] if len(parts) > 1 else "?",
                        "memory_free_mb": parts[2] if len(parts) > 2 else "?",
                        "driver": parts[3] if len(parts) > 3 else "?",
                    }
                )
            return {"gpus": gpus}
    except Exception:
        pass

    return {"note": "GPU info unavailable"}


def _collect_process_info() -> dict:
    info = {"pid": os.getpid()}

    if not _HAS_PSUTIL:
        info["note"] = "psutil not available"
        return info

    try:
        proc = psutil.Process(os.getpid())
        mem_info = proc.memory_info()
        info.update(
            {
                "process_rss_mb": round(mem_info.rss / (1024**2), 2),
                "process_vms_mb": round(mem_info.vms / (1024**2), 2),
                "process_threads": proc.num_threads(),
                "process_open_files": len(proc.open_files()),
            }
        )
        return info
    except Exception as err:
        info["error"] = str(err)
        return info


class _StateTracker:
    """
    Lightweight global state tracker. The app can push breadcrumbs here so the
    crash report can say what the user was doing right before the exception.
    """

    _lock = threading.Lock()
    _current_tool: Optional[str] = None
    _loaded_file: Optional[str] = None
    _last_action: Optional[str] = None
    _point_count: Optional[int] = None
    _active_display_mode: Optional[str] = None
    _extra: dict = {}

    @classmethod
    def set_tool(cls, name: str):
        with cls._lock:
            cls._current_tool = name

    @classmethod
    def set_file(cls, path: str):
        with cls._lock:
            cls._loaded_file = path

    @classmethod
    def set_action(cls, description: str):
        with cls._lock:
            cls._last_action = description

    @classmethod
    def set_point_count(cls, count: int):
        with cls._lock:
            cls._point_count = count

    @classmethod
    def set_display_mode(cls, mode: str):
        with cls._lock:
            cls._active_display_mode = mode

    @classmethod
    def set_extra(cls, key: str, value):
        with cls._lock:
            cls._extra[key] = value

    @classmethod
    def snapshot(cls) -> dict:
        with cls._lock:
            return {
                "active_tool": cls._current_tool,
                "loaded_file": cls._loaded_file,
                "last_action": cls._last_action,
                "point_count": cls._point_count,
                "display_mode": cls._active_display_mode,
                **cls._extra,
            }


AppState = _StateTracker()


def _format_dict(data: dict, indent: int = 2) -> str:
    pad = " " * indent
    if not data:
        return f"{pad}note: none"
    return "\n".join(f"{pad}{key}: {value}" for key, value in data.items())


def _build_report(exc_type, exc_value, exc_tb, config: dict, context: Optional[dict] = None) -> str:
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ist_offset = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    now_ist = now_utc.astimezone(ist_offset)

    tb_lines = traceback.format_exception(exc_type, exc_value, exc_tb)
    tb_str = "".join(tb_lines)

    crash_location = "  Unknown"
    if exc_tb:
        frames = traceback.extract_tb(exc_tb)
        if frames:
            last = frames[-1]
            source_line = (last.line or "").strip()
            crash_location = "\n".join(
                [
                    f"  File: {last.filename}",
                    f"  Function: {last.name}",
                    f"  Line {last.lineno}: {source_line}",
                ]
            )

    context = context or {}
    crash_source = context.get("origin", "sys.excepthook")
    thread_name = context.get("thread_name", threading.current_thread().name)
    thread_ident = context.get("thread_ident", threading.get_ident())
    is_fatal = context.get("fatal", True)
    extra_context = context.get("extra_context") or {}

    sections = [
        "NAKSHA CRASH REPORT",
        "===================",
        "",
        "CRASH SUMMARY",
        "-------------",
        f"  App Version : {config.get('app_version', 'unknown')}",
        f"  Crash Time  : {now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"  UTC Time    : {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"  Exception   : {exc_type.__name__ if exc_type else 'Unknown'}",
        f"  Message     : {exc_value}",
        f"  Source      : {crash_source}",
        f"  Thread      : {thread_name} (id={thread_ident})",
        f"  Fatal       : {is_fatal}",
        "",
        "CRASH LOCATION",
        "--------------",
        crash_location,
        "",
        "LAST KNOWN APP STATE",
        "--------------------",
        _format_dict(AppState.snapshot()),
        "",
        "EXTRA CRASH CONTEXT",
        "-------------------",
        _format_dict(extra_context),
        "",
        "FULL TRACEBACK",
        "--------------",
        textwrap.indent(tb_str, "  ").rstrip(),
        "",
        "SYSTEM CONFIGURATION",
        "--------------------",
        _format_dict(_collect_platform_info()),
        "",
        "CPU",
        "---",
        _format_dict(_collect_cpu_info()),
        "",
        "MEMORY",
        "------",
        _format_dict(_collect_memory_info()),
        "",
        "GPU",
        "---",
        _format_dict(_collect_gpu_info()),
        "",
        "PROCESS",
        "-------",
        _format_dict(_collect_process_info()),
    ]
    return "\n".join(sections)


def _write_local_log(body: str) -> Optional[Path]:
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = _log_dir() / f"crash_{timestamp}_{os.getpid()}.txt"
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(body)
        _stderr_write(f"[CrashReporter] Crash log saved: {log_path}\n")
        return log_path
    except Exception:
        return None


def _send_email(subject: str, body: str, config: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config["smtp_user"]
    msg["To"] = config["report_to"]
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP(
        config["smtp_host"],
        config["smtp_port"],
        timeout=_smtp_timeout_seconds(config),
    ) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(config["smtp_user"], config["smtp_password"])
        server.sendmail(config["smtp_user"], [config["report_to"]], msg.as_string())


def _report_email_failure(err: Exception):
    try:
        message = str(err)
        if "Username and Password not accepted" in message or "535" in message:
            _stderr_write(
                "[CrashReporter] Email authentication failed.\n"
                "  -> Generate a Gmail App Password and place it in crash_config.json.\n"
            )
        else:
            _stderr_write(f"[CrashReporter] Email send failed: {err}\n")
    except Exception:
        pass


def _send_email_async(subject: str, body: str, config: dict):
    def _worker():
        try:
            _send_email(subject, body, config)
        except Exception as err:
            _report_email_failure(err)

    thread = threading.Thread(target=_worker, daemon=True, name="CrashReporterEmail")
    thread.start()
    return thread


class CrashReporter:
    """
    Install once at application startup.

    Covered crash paths:
    1. Unhandled main-thread Python exceptions
    2. Unhandled threading.Thread exceptions
    3. Unraisable exceptions (__del__, callback cleanup, etc.)
    4. Exceptions that propagate through Qt's event loop
    """

    def __init__(self):
        self._config = _load_config()
        self._original_excepthook = sys.excepthook
        self._original_threading_excepthook = getattr(threading, "excepthook", None)
        self._original_unraisablehook = getattr(sys, "unraisablehook", None)
        self._handler_lock = threading.Lock()
        self._handler_local = threading.local()
        self._faulthandler_stream = None
        self._installed = False

    def install(self):
        if self._installed:
            return

        if not self._config.get("enabled", True):
            _stderr_write("[CrashReporter] Disabled via config.\n")
            return

        sys.excepthook = self._handle_exception
        if self._original_threading_excepthook is not None:
            threading.excepthook = self._handle_thread_exception
        if self._original_unraisablehook is not None:
            sys.unraisablehook = self._handle_unraisable_exception

        self._enable_faulthandler()
        self._installed = True
        _stderr_write(
            "[CrashReporter] Installed (sys.excepthook + threading.excepthook + "
            "sys.unraisablehook).\n"
        )

    def install_qt_notify(self, app):
        """
        Call after QApplication is created, passing the app instance.
        Wraps QApplication.notify() so unhandled Qt exceptions are reported and
        treated as fatal instead of being silently swallowed.
        """

        if getattr(app, "_crash_reporter_notify_installed", False):
            return

        original_notify = app.notify

        def safe_notify(receiver, event):
            if os.getenv("NAKSHA_NOTIFY_TRACE"):
                _notify_trace(receiver, event)

            try:
                if getattr(app, "_shutdown_in_progress", False):
                    try:
                        from shiboken6 import isValid as _qt_object_is_valid
                    except Exception:
                        _qt_object_is_valid = lambda obj: obj is not None

                    if receiver is None or not _qt_object_is_valid(receiver):
                        return False

                    try:
                        from PySide6.QtCore import QEvent
                        shutdown_skip_events = {
                            QEvent.Paint,
                            QEvent.UpdateRequest,
                            QEvent.MouseMove,
                            QEvent.HoverMove,
                            QEvent.Enter,
                            QEvent.Leave,
                            QEvent.Wheel,
                            QEvent.MouseButtonPress,
                            QEvent.MouseButtonRelease,
                            QEvent.CursorChange,
                        }
                        if event is not None and event.type() in shutdown_skip_events:
                            return False
                    except Exception:
                        pass

                    try:
                        from PySide6.QtCore import QEvent
                        if (
                            receiver is not None
                            and getattr(receiver, "_naksha_skip_render", False)
                            and event is not None
                            and event.type() in {QEvent.Paint, QEvent.UpdateRequest}
                        ):
                            return False
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                return original_notify(receiver, event)
            except BaseException:
                exc_type, exc_value, exc_tb = sys.exc_info()
                if isinstance(exc_value, (KeyboardInterrupt, SystemExit)) and getattr(app, "_shutdown_in_progress", False):
                    return False
                self._handle_exception(
                    exc_type,
                    exc_value,
                    exc_tb,
                    origin="QApplication.notify",
                    fatal=True,
                    exit_after_report=True,
                )
                return False

        app.notify = safe_notify
        app._crash_reporter_notify_installed = True
        _stderr_write("[CrashReporter] Qt notify wrapper installed.\n")

    def _enable_faulthandler(self):
        try:
            log_path = _log_dir() / "faulthandler.log"
            self._faulthandler_stream = open(log_path, "a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=self._faulthandler_stream, all_threads=True)
        except Exception as err:
            _stderr_write(f"[CrashReporter] Failed to enable faulthandler: {err}\n")

    def _handle_thread_exception(self, args):
        self._handle_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            origin="threading.excepthook",
            fatal=False,
            thread=args.thread,
        )
        if self._original_threading_excepthook is not None:
            self._original_threading_excepthook(args)

    def _handle_unraisable_exception(self, unraisable):
        exc_type = unraisable.exc_type or RuntimeError
        exc_value = unraisable.exc_value or exc_type("Unraisable exception")
        self._handle_exception(
            exc_type,
            exc_value,
            unraisable.exc_traceback,
            origin="sys.unraisablehook",
            fatal=False,
            extra_context={
                "unraisable_object": _safe_repr(getattr(unraisable, "object", None)),
                "unraisable_err_msg": getattr(unraisable, "err_msg", None),
            },
        )
        if self._original_unraisablehook is not None:
            self._original_unraisablehook(unraisable)

    def _handle_exception(
        self,
        exc_type,
        exc_value,
        exc_tb,
        origin: str = "sys.excepthook",
        fatal: bool = True,
        exit_after_report: bool = False,
        thread=None,
        extra_context: Optional[dict] = None,
    ):
        if exc_type and issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            self._original_excepthook(exc_type, exc_value, exc_tb)
            return

        current_thread = thread or threading.current_thread()
        if fatal and current_thread is not threading.main_thread() and not exit_after_report:
            fatal = False

        if getattr(self._handler_local, "active", False):
            _stderr_write("[CrashReporter] Recursive crash inside reporter.\n")
            if exit_after_report:
                self._terminate_process(1)
            return

        try:
            with self._handler_lock:
                self._handler_local.active = True
                context = {
                    "origin": origin,
                    "fatal": fatal,
                    "thread_name": getattr(current_thread, "name", threading.current_thread().name),
                    "thread_ident": getattr(current_thread, "ident", threading.get_ident()),
                    "extra_context": extra_context or {},
                }
                report = _build_report(exc_type, exc_value, exc_tb, self._config, context=context)
                subject = (
                    f"[Naksha Crash] {exc_type.__name__ if exc_type else 'Unknown'} | "
                    f"{origin} | {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )

                _write_local_log(report)

                if _smtp_is_configured(self._config):
                    if fatal:
                        try:
                            _send_email(subject, report, self._config)
                        except Exception as err:
                            _report_email_failure(err)
                    else:
                        _send_email_async(subject, report, self._config)
                else:
                    _stderr_write(
                        "[CrashReporter] SMTP not fully configured; crash saved locally only.\n"
                        "  -> Fill smtp_host, smtp_port, smtp_user, smtp_password, and "
                        "report_to in crash_config.json to enable email reporting.\n"
                    )

                _stderr_write("\n" + "=" * 78 + "\n")
                _stderr_write(report)
                _stderr_write("\n" + "=" * 78 + "\n")

        except Exception as reporter_err:
            _stderr_write(f"[CrashReporter] Reporter itself failed: {reporter_err}\n")
            self._original_excepthook(exc_type, exc_value, exc_tb)
        finally:
            self._handler_local.active = False
            if exit_after_report:
                self._terminate_process(1)

    def _terminate_process(self, exit_code: int):
        try:
            if self._faulthandler_stream is not None:
                self._faulthandler_stream.flush()
        except Exception:
            pass
        try:
            sys.stdout.flush()
        except Exception:
            pass
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(exit_code)
