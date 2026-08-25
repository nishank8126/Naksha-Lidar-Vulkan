"""
Long-session soak telemetry for runtime stability verification.

Provides:
- Live dock panel with key health metrics
- Periodic sampling (default: every 5 seconds)
- CSV logging to a user-writable soak_telemetry folder
"""

from __future__ import annotations

import csv
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class SoakTelemetryController(QObject):
    SAMPLE_INTERVAL_MS = 5_000
    TREND_WINDOW_SAMPLES = 120  # 10 minutes at 5-second sampling

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self._started_at = time.monotonic()
        self._sample_count = 0

        self._csv_file = None
        self._csv_writer = None
        self._csv_path = None

        self._history = deque(maxlen=self.TREND_WINDOW_SAMPLES)

        self._timer = QTimer(self)
        self._timer.setInterval(self.SAMPLE_INTERVAL_MS)
        self._timer.timeout.connect(self._sample_once)

        self._dock = None
        self._status_label = None
        self._text = None
        self._pause_btn = None
        self._resume_btn = None

        self._build_ui()
        self._start_new_session()
        self.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def show(self) -> None:
        if self._dock is None:
            return
        self._dock.show()
        self._dock.raise_()
        self._dock.activateWindow()
        if not self._timer.isActive():
            self._timer.start()
        self._sample_once()

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def pause(self) -> None:
        self._timer.stop()
        self._set_status("Paused")

    def resume(self) -> None:
        if not self._timer.isActive():
            self._timer.start()
        self._set_status("Running")

    def shutdown(self) -> None:
        self._timer.stop()
        self._close_csv()
        if self._dock is not None:
            try:
                self._dock.close()
            except Exception:
                pass
            self._dock = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        dock = QDockWidget("Soak Telemetry", self.app)
        dock.setObjectName("SoakTelemetryDock")
        dock.setContextMenuPolicy(Qt.NoContextMenu)
        dock.setAllowedAreas(
            Qt.LeftDockWidgetArea
            | Qt.RightDockWidgetArea
            | Qt.BottomDockWidgetArea
        )

        body = QWidget(dock)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self._status_label = QLabel("Initializing...", body)
        layout.addWidget(self._status_label)

        controls = QHBoxLayout()
        new_btn = QPushButton("New Session", body)
        new_btn.clicked.connect(self._start_new_session)
        controls.addWidget(new_btn)

        self._pause_btn = QPushButton("Pause", body)
        self._pause_btn.clicked.connect(self.pause)
        controls.addWidget(self._pause_btn)

        self._resume_btn = QPushButton("Resume", body)
        self._resume_btn.clicked.connect(self.resume)
        controls.addWidget(self._resume_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        text = QPlainTextEdit(body)
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._text = text
        layout.addWidget(text, 1)

        dock.setWidget(body)
        self.app.addDockWidget(Qt.RightDockWidgetArea, dock)
        dock.hide()

        self._dock = dock

    def _set_status(self, state: str) -> None:
        if self._status_label is None:
            return
        csv_name = os.path.basename(self._csv_path) if self._csv_path else "none"
        self._status_label.setText(f"State: {state} | CSV: {csv_name}")

    def _resolve_log_dir(self) -> Path:
        """
        Pick a user-writable log folder.

        The legacy behavior wrote inside the app bundle, which works in a raw
        dist folder but fails once the app is installed under Program Files.
        """
        candidates = []

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "NakshaAI-LiDAR")

        candidates.append(Path.home() / ".naksha")
        candidates.append(Path(__file__).resolve().parents[1])

        errors = []
        for base_dir in candidates:
            log_dir = base_dir / "logs" / "soak_telemetry"
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                probe = log_dir / ".write_test"
                with open(probe, "a", encoding="utf-8"):
                    pass
                probe.unlink(missing_ok=True)
                return log_dir
            except Exception as exc:
                errors.append(f"{log_dir}: {exc}")

        raise OSError("Unable to create a writable soak telemetry log folder.\n" + "\n".join(errors))

    def _start_new_session(self) -> None:
        self._close_csv()
        self._sample_count = 0
        self._history.clear()

        log_dir = self._resolve_log_dir()

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = log_dir / f"soak_{stamp}.csv"

        self._csv_file = open(path, "w", newline="", encoding="utf-8")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_writer.writerow(
            [
                "timestamp",
                "uptime_sec",
                "ram_mb",
                "ram_pct",
                "undo_depth",
                "redo_depth",
                "actors",
                "observers",
                "active_timers",
                "total_timers",
                "pending_main_chunks",
                "pending_view_updates",
                "classify_interactors",
                "gc_last_freed",
                "ram_trend_mb_per_hour",
                "alerts",
            ]
        )
        self._csv_path = str(path)
        self._set_status("Running")
        self._sample_once()

    def _close_csv(self) -> None:
        if self._csv_file is not None:
            try:
                self._csv_file.flush()
                self._csv_file.close()
            except Exception:
                pass
        self._csv_file = None
        self._csv_writer = None

    def _collect_stats(self) -> dict:
        mem = {}
        try:
            guard = getattr(self.app, "_mem_guard", None)
            if guard is not None and hasattr(guard, "get_stats"):
                mem = guard.get_stats() or {}
        except Exception:
            mem = {}

        active_timers = 0
        total_timers = 0
        try:
            for timer in self.app.findChildren(QTimer):
                total_timers += 1
                if timer.isActive():
                    active_timers += 1
        except Exception:
            pass

        pending_chunks = getattr(self.app, "_pending_main_view_index_chunks", None)
        if isinstance(pending_chunks, list):
            pending_main_chunks = len(pending_chunks)
        else:
            pending_main_chunks = 0

        pending_view_updates = getattr(self.app, "_pending_view_updates", None)
        if isinstance(pending_view_updates, (set, list, tuple)):
            pending_view_count = len(pending_view_updates)
        else:
            pending_view_count = 0

        inter_map = getattr(self.app, "classify_interactors", None)
        if not getattr(self.app, "active_classify_tool", None):
            inter_count = 0
        elif isinstance(inter_map, dict):
            inter_count = len(inter_map)
        else:
            inter_count = 0

        now = time.monotonic()
        uptime_sec = int(now - self._started_at)

        return {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_sec": uptime_sec,
            "ram_mb": int(mem.get("ram_mb", 0) or 0),
            "ram_pct": float(mem.get("ram_pct", 0.0) or 0.0),
            "undo_depth": int(mem.get("undo_depth", 0) or 0),
            "redo_depth": int(mem.get("redo_depth", 0) or 0),
            "actors": int(mem.get("total_actors", 0) or 0),
            "observers": int(mem.get("live_observers", 0) or 0),
            "active_timers": active_timers,
            "total_timers": total_timers,
            "pending_main_chunks": pending_main_chunks,
            "pending_view_updates": pending_view_count,
            "classify_interactors": inter_count,
            "gc_last_freed": int(mem.get("last_gc_freed", 0) or 0),
        }

    def _compute_ram_trend_mb_per_hour(self, now_uptime: int, ram_mb: int) -> float:
        self._history.append((now_uptime, ram_mb))
        # Keep only the last 10 minutes (600s), matching the UI label.
        cutoff = max(0, int(now_uptime) - 600)
        while len(self._history) > 1 and self._history[0][0] < cutoff:
            self._history.popleft()
        if len(self._history) < 2:
            return 0.0

        first_t, first_ram = self._history[0]
        last_t, last_ram = self._history[-1]
        dt = max(1, last_t - first_t)
        delta_ram = last_ram - first_ram
        return (delta_ram * 3600.0) / float(dt)

    def _alerts_for(self, stats: dict, ram_trend: float) -> list[str]:
        alerts = []
        if stats["ram_pct"] >= 85.0:
            alerts.append("HIGH_RAM")
        # Require both slope and meaningful absolute growth over the kept window
        # to avoid false positives from startup/transient allocator behavior.
        abs_growth_mb = 0
        if len(self._history) >= 2:
            abs_growth_mb = int(self._history[-1][1] - self._history[0][1])
        if ram_trend >= 250.0 and abs_growth_mb >= 128:
            alerts.append("RAM_RISING_FAST")
        if stats["observers"] >= 500:
            alerts.append("OBSERVER_GROWTH")
        if stats["pending_main_chunks"] >= 96:
            alerts.append("MAIN_CHUNKS_HIGH")
        return alerts

    def _sample_once(self) -> None:
        stats = self._collect_stats()
        ram_trend = self._compute_ram_trend_mb_per_hour(
            stats["uptime_sec"], stats["ram_mb"]
        )
        alerts = self._alerts_for(stats, ram_trend)
        alerts_text = ",".join(alerts) if alerts else "OK"

        self._sample_count += 1

        if self._csv_writer is not None:
            self._csv_writer.writerow(
                [
                    stats["timestamp"],
                    stats["uptime_sec"],
                    stats["ram_mb"],
                    f"{stats['ram_pct']:.1f}",
                    stats["undo_depth"],
                    stats["redo_depth"],
                    stats["actors"],
                    stats["observers"],
                    stats["active_timers"],
                    stats["total_timers"],
                    stats["pending_main_chunks"],
                    stats["pending_view_updates"],
                    stats["classify_interactors"],
                    stats["gc_last_freed"],
                    f"{ram_trend:.1f}",
                    alerts_text,
                ]
            )
            self._csv_file.flush()

        if self._text is not None:
            uptime_h = stats["uptime_sec"] // 3600
            uptime_m = (stats["uptime_sec"] % 3600) // 60
            uptime_s = stats["uptime_sec"] % 60
            msg = (
                f"Samples: {self._sample_count}\n"
                f"Timestamp: {stats['timestamp']}\n"
                f"Uptime: {uptime_h:02d}:{uptime_m:02d}:{uptime_s:02d}\n"
                f"CSV: {self._csv_path or 'none'}\n"
                "\n"
                f"RAM: {stats['ram_mb']} MB ({stats['ram_pct']:.1f}%)\n"
                f"RAM trend (10m): {ram_trend:+.1f} MB/hour\n"
                f"Undo/Redo: {stats['undo_depth']} / {stats['redo_depth']}\n"
                f"Actors: {stats['actors']} | Observers: {stats['observers']}\n"
                f"Qt timers active/total: {stats['active_timers']} / {stats['total_timers']}\n"
                f"Pending main chunks: {stats['pending_main_chunks']}\n"
                f"Pending view updates: {stats['pending_view_updates']}\n"
                f"Classify interactors: {stats['classify_interactors']}\n"
                f"Last GC freed: {stats['gc_last_freed']}\n"
                "\n"
                f"Alerts: {alerts_text}\n"
            )
            self._text.setPlainText(msg)
