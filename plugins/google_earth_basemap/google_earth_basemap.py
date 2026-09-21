"""Universal basemap plugin for NakshaAI-LiDAR.

Esri World Imagery is the default no-key provider. Google satellite, roadmap,
and terrain remain available through the official Google Maps Platform Map
Tiles API when the user supplies an API key. All providers render directly
inside the existing VTK top-view canvas; the plugin never replaces the host
renderer or monkey-patches the main application.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import vtk
from pyproj import CRS, Transformer
from vtk.util import numpy_support

from PySide6.QtCore import (
    QByteArray, QEvent, QObject, QRunnable, QSettings, QThreadPool, QTimer,
    QUrl, Qt, Signal,
)
from PySide6.QtGui import QKeySequence, QShortcut
try:
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
except ImportError as exc:
    raise RuntimeError(
        "Basemap plugin requires PySide6.QtNetwork. "
        "For a PyInstaller build, include the hidden import PySide6.QtNetwork in the host once."
    ) from exc
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from tile_math import (
    choose_zoom,
    enumerate_tiles,
    max_zoom_for_center,
    tile_x_to_lon,
    tile_y_to_lat,
    lonlat_to_web_mercator,
    web_mercator_to_lonlat,
    WEB_MERCATOR_HALF_WORLD_M,
    MAX_MERCATOR_LAT,
)

PLUGIN_OBJECT_NAME = "NakshaGoogleEarthBasemapPluginSection"
SETTINGS_ORG = "NakshaAI"
SETTINGS_APP = "GoogleEarthBasemapPlugin"

PROVIDER_CHOICES = [
    ("Esri World Imagery (Free)", "esri_world_imagery"),
    ("OpenStreetMap (Free)", "openstreetmap"),
    ("Google Satellite", "google_satellite"),
    ("Google Roadmap", "google_roadmap"),
    ("Google Terrain", "google_terrain"),
]
PROVIDER_IDS = {value for _label, value in PROVIDER_CHOICES}

# Free, no-key providers. Each is a list of interchangeable tile servers; the
# plugin tries them in order and retries on a failed host (mirrors how the
# reference React/MapLibre project pulls OSM + Esri without any API key).
ESRI_TILE_URLS = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
)
ESRI_METADATA_URLS = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer?f=pjson",
    "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer?f=pjson",
)
ESRI_ATTRIBUTION_FALLBACK = "Esri | World Imagery"
ESRI_MAX_ZOOM_DEFAULT = 22

OSM_TILE_URLS = (
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
)
OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_MAX_ZOOM = 19

# Free providers share one refresh path. id -> (url templates, attribution, max zoom)
FREE_PROVIDERS = {
    "esri_world_imagery": (ESRI_TILE_URLS, ESRI_ATTRIBUTION_FALLBACK, ESRI_MAX_ZOOM_DEFAULT),
    "openstreetmap": (OSM_TILE_URLS, OSM_ATTRIBUTION, OSM_MAX_ZOOM),
}


class _ProjectedComposeSignals(QObject):
    """Queued hand-off from a background raster reprojection task to Qt UI.

    Retired as of v1.1.28 - GoogleEarthBasemapPlugin._start_projected_free_refresh()
    no longer calls into this class or the compose pipeline below it. Kept in
    the file (unused) as a reference/fallback rather than deleted outright;
    see _local_canvas_to_mercator_affine() for the replacement approach and
    its docstring for why the per-pixel composite this class performed was
    itself the cause of the reported "basemap becomes a wedge" symptom.
    """

    finished = Signal(object)


class _ProjectedComposeTask(QRunnable):
    """CPU-heavy inverse reprojection/composition, deliberately off the GUI thread.

    Unused as of v1.1.28 - see the note on _ProjectedComposeSignals above.

    QGIS uses background render jobs and a render cache while the user pans/zooms.
    The old plugin performed a pyproj transform for up to ~4 million pixels directly
    in the Qt GUI thread, which could block wheel interaction for a noticeable time.
    This worker keeps VTK/Qt actor creation on the GUI thread but moves all NumPy +
    PROJ pixel work off it.
    """

    def __init__(self, payload):
        super().__init__()
        self.payload = payload
        self.signals = _ProjectedComposeSignals()

    def run(self):
        try:
            result = self._compose(self.payload)
        except Exception as exc:  # never let a render worker kill the plugin
            result = {
                "token": self.payload.get("token"),
                "error": str(exc),
            }
        self.signals.finished.emit(result)

    @staticmethod
    def _compose(payload):
        width, height = map(int, payload["size"])
        xmin, ymin, xmax, ymax = map(float, payload["bounds"])
        available = payload.get("available", {})
        if width <= 0 or height <= 0 or not available:
            return None

        x_values = xmin + (np.arange(width, dtype=np.float64) + 0.5) * (
            (xmax - xmin) / width
        )
        y_values = ymax - (np.arange(height, dtype=np.float64) + 0.5) * (
            (ymax - ymin) / height
        )
        grid_x, grid_y = np.meshgrid(x_values, y_values)

        current_crs = CRS.from_wkt(payload["crs_wkt"])
        transform = Transformer.from_crs(
            current_crs, CRS.from_epsg(4326), always_xy=True
        )
        lon, lat = transform.transform(grid_x, grid_y)
        valid = (
            np.isfinite(lon)
            & np.isfinite(lat)
            & (lon >= -180.0)
            & (lon <= 180.0)
            & (lat >= -MAX_MERCATOR_LAT)
            & (lat <= MAX_MERCATOR_LAT)
        )
        area_bounds = payload.get("area_bounds")
        if area_bounds is not None:
            west, south, east, north = map(float, area_bounds)
            valid &= (
                (lon >= west)
                & (lon <= east)
                & (lat >= south)
                & (lat <= north)
            )

        # RGBA, not RGB. Pixels outside the mathematically valid CRS domain are
        # transparent, so they can never become black wedges/fans/amoebas.
        output = np.zeros((height, width, 4), dtype=np.uint8)
        remaining = valid.copy()
        safe_lon = np.where(valid, lon, 0.0)
        safe_lat = np.where(valid, lat, 0.0)

        levels = sorted({tile_id[0] for tile_id in available}, reverse=True)
        for level in levels:
            n = float(2 ** level)
            world_x = ((safe_lon + 180.0) / 360.0) * n
            latitude = np.radians(
                np.clip(safe_lat, -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT)
            )
            world_y = (
                1.0 - np.arcsinh(np.tan(latitude)) / math.pi
            ) * 0.5 * n
            tile_x = np.floor(world_x).astype(np.int64)
            tile_y = np.floor(world_y).astype(np.int64)

            for tile_id, rgb in available.items():
                if tile_id[0] != level:
                    continue
                mask = (
                    remaining
                    & (tile_x == tile_id[1])
                    & (tile_y == tile_id[2])
                )
                if not np.any(mask):
                    continue
                tile_height, tile_width = rgb.shape[:2]
                px = np.clip(
                    ((world_x[mask] - tile_id[1]) * tile_width).astype(np.int64),
                    0,
                    tile_width - 1,
                )
                py = np.clip(
                    ((world_y[mask] - tile_id[2]) * tile_height).astype(np.int64),
                    0,
                    tile_height - 1,
                )
                output[mask, :3] = rgb[py, px]
                output[mask, 3] = 255
                remaining[mask] = False

        valid_count = int(valid.sum())
        covered_count = int((valid & ~remaining).sum())
        coverage = float(covered_count) / valid_count if valid_count else 0.0
        return {
            "token": payload.get("token"),
            "image": output,
            "coverage": coverage,
            "valid_count": valid_count,
        }


class _SettingsDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("Basemap Settings")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        note = QLabel(
            "Esri World Imagery and OpenStreetMap both work without an API key. "
            "Google Satellite, Roadmap and Terrain use the official Google Maps "
            "Platform Map Tiles API and require a Google API key/billing."
        )
        note.setWordWrap(True)
        root.addWidget(note)

        form = QFormLayout()
        self.provider = QComboBox()
        for label, value in PROVIDER_CHOICES:
            self.provider.addItem(label, value)
        idx = self.provider.findData(plugin._provider())
        self.provider.setCurrentIndex(max(0, idx))

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.api_key.setPlaceholderText("Optional - only needed for Google providers")
        self.api_key.setText(plugin._settings.value("api_key", "", type=str) or "")

        self.region = QLineEdit(plugin._settings.value("region", "IN", type=str) or "IN")
        self.region.setMaxLength(2)
        self.language = QLineEdit(plugin._settings.value("language", "en-US", type=str) or "en-US")

        self.opacity = QSpinBox()
        self.opacity.setRange(10, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(plugin._settings.value("opacity", 100, type=int))

        self.max_tiles = QSpinBox()
        self.max_tiles.setRange(9, 64)
        self.max_tiles.setValue(plugin._settings.value("max_tiles", 36, type=int))
        self.max_tiles.setToolTip("Maximum visible tiles requested for one viewport refresh")

        form.addRow("Provider:", self.provider)
        form.addRow("Google API key:", self.api_key)
        form.addRow("Google region:", self.region)
        form.addRow("Google language:", self.language)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Tile budget:", self.max_tiles)
        root.addLayout(form)

        key_note = QLabel(
            "For Google, GOOGLE_MAPS_API_KEY can be set in the environment instead "
            "of saving the key here. The environment variable takes priority. "
            "Esri World Imagery needs no key, but network use and attribution still "
            "remain subject to Esri's service terms."
        )
        key_note.setWordWrap(True)
        root.addWidget(key_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self):
        old_provider = self.plugin._provider()
        self.plugin._settings.setValue("provider", self.provider.currentData())
        self.plugin._settings.setValue("api_key", self.api_key.text().strip())
        self.plugin._settings.setValue("region", self.region.text().strip().upper() or "IN")
        self.plugin._settings.setValue("language", self.language.text().strip() or "en-US")
        self.plugin._settings.setValue("opacity", int(self.opacity.value()))
        self.plugin._settings.setValue("max_tiles", int(self.max_tiles.value()))
        self.plugin._settings.sync()
        new_provider = self.plugin._provider()
        if new_provider != old_provider:
            self.plugin._provider_changed(clear_tiles=True)
        self.plugin._sync_provider_combo()
        self.plugin._apply_opacity()
        if self.plugin._active:
            self.plugin.schedule_refresh(0)
        super().accept()


class _GoToDialog(QDialog):
    def __init__(self, plugin, parent=None):
        super().__init__(parent)
        self.plugin = plugin
        self.setWindowTitle("Basemap - Go To")
        self.setMinimumWidth(360)
        form = QFormLayout(self)

        self.lat = QDoubleSpinBox()
        self.lat.setRange(-85.0, 85.0)
        self.lat.setDecimals(7)
        self.lat.setValue(plugin._settings.value("center_lat", 20.5937, type=float))

        self.lon = QDoubleSpinBox()
        self.lon.setRange(-180.0, 180.0)
        self.lon.setDecimals(7)
        self.lon.setValue(plugin._settings.value("center_lon", 78.9629, type=float))

        self.zoom = QSpinBox()
        self.zoom.setRange(1, 22)
        self.zoom.setValue(plugin._settings.value("center_zoom", 5, type=int))

        form.addRow("Latitude:", self.lat)
        form.addRow("Longitude:", self.lon)
        form.addRow("Zoom:", self.zoom)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def accept(self):
        lat = float(self.lat.value())
        lon = float(self.lon.value())
        zoom = int(self.zoom.value())
        self.plugin._settings.setValue("center_lat", lat)
        self.plugin._settings.setValue("center_lon", lon)
        self.plugin._settings.setValue("center_zoom", zoom)
        self.plugin._settings.sync()
        self.plugin.go_to_lonlat(lon, lat, zoom)
        super().accept()


class GoogleEarthBasemapPlugin(QObject):
    """Externally installable Esri + optional Google basemap plugin."""

    def __init__(self):
        super().__init__()
        self.app = None
        self._settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self._nam = None
        self._active = False
        self._unloaded = False
        self._ribbon_widget = None
        self._toggle_button = None
        self._provider_combo = None
        self._plugins_menu_button = None
        self._shortcut = None
        self._refresh_timer = None
        self._state_timer = None
        self._reply_meta = {}
        self._tile_retry_counts = {}
        self._session_reply = None
        self._session_token = None
        self._session_expiry = 0
        self._session_signature = None
        self._esri_metadata_reply = None
        self._esri_metadata_url_index = 0
        self._esri_attribution_loaded = False
        self._esri_max_zoom = 22
        self._refresh_generation = 0
        self._pending_refresh_after_session = False
        self._tile_actors = {}
        self._viewport_actor = None
        self._viewport_signature = None
        self._projected_tile_images = {}
        self._projected_view = None
        self._projected_compose_timer = None
        self._projected_extent_cache = {}
        self._tile_geometry_cache = {}
        self._refresh_mesh_subdivisions = 1
        self._domain_rejected = 0
        self._projected_compose_running = False
        self._projected_compose_pending = False
        self._projected_compose_task = None
        self._projected_compose_serial = 0
        self._tile_actor_signature = None
        self._current_z_plane = None
        self._attribution_actor = None
        # v1.1.27: world-space camera width captured at the moment the
        # projected viewport actor currently on screen was placed. Used to
        # detect "this texture is now stale relative to the live camera" so
        # it can be cleared proactively instead of sitting there looking like
        # a wrong/shrunken patch while a slow-to-arrive replacement is still
        # being composed. See _viewport_is_stale_for_camera().
        # v1.1.28: the single-texture viewport-actor path this supported is
        # retired (see _local_canvas_to_mercator_affine / the rewritten
        # _start_projected_free_refresh); kept only because _viewport_actor
        # itself stays around as a defensive no-op for old saved state / any
        # leftover call site, and this field travels with it.
        self._viewport_camera_width = None
        # Anti-blink architecture (v1.1.4):
        # - _tile_actors is now the UNIFIED set of every tile currently drawn
        #   (any zoom). _active_ids marks the tiles wanted at the current zoom;
        #   every other displayed tile is a lower-resolution "placeholder" kept
        #   on screen (drawn underneath) until its higher-res replacement lands,
        #   exactly like web maps. This is what stops the blank flash on zoom.
        # - _tile_cache_dir / on-disk cache makes re-viewed areas appear
        #   instantly instead of after a network round-trip.
        # - _coalesce_timer batches renders to one per ~120ms instead of one
        #   per tile, eliminating per-tile pop-in flicker.
        self._active_ids = set()
        # Tiles confirmed to be the provider's own "no imagery at this zoom"
        # placeholder (keyed (provider, z, x, y)) - never requested again this
        # session. See _tile_looks_unavailable().
        self._dead_tile_ids = set()
        self._tile_active_sig = None
        self._tile_cache_dir = None
        self._coalesce_timer = None
        self._coalesce_reset_clip = False
        self._placeholder_sep = 1e-3
        self._tile_clip_planes = None
        self._camera_observer_tags = []
        self._last_project_signature = None
        self._standalone_view_initialized = False
        self._last_error_text = ""
        # Signature of the camera transform (focal point, parallel scale, Z) used
        # to suppress redundant auto-refresh triggers coming from our own
        # ResetCameraClippingRange() during render, while still catching every
        # genuine user pan/zoom (including programmatic zoom done by the host app).
        self._last_camera_sig = None
        self._enforcing_2d_plan_view = False

    # ------------------------------------------------------------------
    # Plugin lifecycle expected by gui.plugin_manager.PluginManager
    # ------------------------------------------------------------------
    def on_load(self, app_window):
        self.app = app_window
        self._unloaded = False
        self._nam = QNetworkAccessManager(self)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_now)

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(1000)
        self._state_timer.timeout.connect(self._watch_project_state)
        self._state_timer.start()

        self._install_canvas_hooks()
        self._install_shortcut()
        self._connect_plugins_tab_hook()

        # PluginManager rebuilds the Plugins ribbon after on_load returns.
        # Queue our UI insertion so it runs after that rebuild.
        QTimer.singleShot(0, self._ensure_ribbon_ui)
        self._status("Basemap plugin loaded - Esri + OpenStreetMap ready without an API key (Ctrl+Alt+G)", 3000)
        print("Naksha Basemap plugin loaded")

    def on_unload(self):
        self._unloaded = True
        self.deactivate(silent=True)

        if self._state_timer is not None:
            self._state_timer.stop()
        if self._refresh_timer is not None:
            self._refresh_timer.stop()

        self._abort_all_replies()
        self._remove_canvas_hooks()
        self._remove_shortcut()
        self._disconnect_plugins_tab_hook()
        self._remove_ribbon_ui()

        if self._nam is not None:
            self._nam.deleteLater()
            self._nam = None

        self.app = None
        print("Naksha Basemap plugin unloaded cleanly")

    # ------------------------------------------------------------------
    # UI integration - injected only into the existing Plugins ribbon
    # ------------------------------------------------------------------
    def _connect_plugins_tab_hook(self):
        try:
            btn = getattr(self.app, "menu_buttons", {}).get("plugins")
            if btn is not None:
                self._plugins_menu_button = btn
                btn.clicked.connect(self._queue_ensure_ribbon_ui)
        except Exception:
            pass

    def _disconnect_plugins_tab_hook(self):
        if self._plugins_menu_button is not None:
            try:
                self._plugins_menu_button.clicked.disconnect(self._queue_ensure_ribbon_ui)
            except Exception:
                pass
        self._plugins_menu_button = None

    def _queue_ensure_ribbon_ui(self, *args):
        QTimer.singleShot(0, self._ensure_ribbon_ui)

    def _ensure_ribbon_ui(self):
        if self._unloaded or self.app is None:
            return
        try:
            manager = getattr(self.app, "ribbon_manager", None)
            ribbon = getattr(manager, "ribbons", {}).get("plugins") if manager else None
            if ribbon is None or ribbon.layout() is None:
                return

            existing = ribbon.findChild(QWidget, PLUGIN_OBJECT_NAME)
            if existing is not None:
                self._ribbon_widget = existing
                toggle = existing.findChild(QPushButton, "GoogleBasemapToggle")
                combo = existing.findChild(QComboBox, "BasemapProviderCombo")
                if toggle is not None:
                    self._toggle_button = toggle
                    toggle.blockSignals(True)
                    toggle.setChecked(bool(self._active))
                    toggle.blockSignals(False)
                if combo is not None:
                    self._provider_combo = combo
                    self._sync_provider_combo()
                return

            panel = QFrame(ribbon)
            panel.setObjectName(PLUGIN_OBJECT_NAME)
            panel.setFrameShape(QFrame.StyledPanel)
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(6, 3, 6, 3)
            outer.setSpacing(2)

            title = QLabel("Basemap")
            title.setAlignment(Qt.AlignCenter)
            outer.addWidget(title)

            provider_combo = QComboBox()
            provider_combo.setObjectName("BasemapProviderCombo")
            for label, value in PROVIDER_CHOICES:
                provider_combo.addItem(label, value)
            provider_combo.setToolTip("Choose free Esri/OpenStreetMap or an optional Google provider")
            outer.addWidget(provider_combo)

            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(4)

            toggle = QPushButton("Basemap")
            toggle.setObjectName("GoogleBasemapToggle")
            toggle.setCheckable(True)
            toggle.setChecked(bool(self._active))
            toggle.setToolTip("Toggle basemap in the main VTK canvas (Ctrl+Alt+G)")
            toggle.toggled.connect(self.set_active)

            refresh = QPushButton("Refresh")
            refresh.clicked.connect(lambda: self.schedule_refresh(0))

            goto = QPushButton("Go To")
            goto.clicked.connect(self.open_go_to_dialog)

            settings = QPushButton("Settings")
            settings.clicked.connect(self.open_settings)

            row.addWidget(toggle)
            row.addWidget(refresh)
            row.addWidget(goto)
            row.addWidget(settings)
            outer.addLayout(row)

            layout = ribbon.layout()
            insert_at = max(0, layout.count() - 1)
            layout.insertWidget(insert_at, panel)
            self._ribbon_widget = panel
            self._toggle_button = toggle
            self._provider_combo = provider_combo
            self._sync_provider_combo()
            provider_combo.currentIndexChanged.connect(self._on_provider_combo_changed)

            panel.show()
            panel.updateGeometry()
            update_height = getattr(self.app, "_update_ribbon_container_height", None)
            if callable(update_height):
                QTimer.singleShot(0, update_height)
        except RuntimeError:
            pass
        except Exception as exc:
            print(f"Basemap ribbon UI warning: {exc}")

    def _remove_ribbon_ui(self):
        widget = self._ribbon_widget
        self._ribbon_widget = None
        self._toggle_button = None
        self._provider_combo = None
        if widget is not None:
            try:
                widget.setParent(None)
                widget.deleteLater()
            except Exception:
                pass

    def _install_shortcut(self):
        try:
            self._shortcut = QShortcut(QKeySequence("Ctrl+Alt+G"), self.app)
            self._shortcut.activated.connect(self.toggle)
        except Exception:
            self._shortcut = None

    def _remove_shortcut(self):
        if self._shortcut is not None:
            try:
                self._shortcut.setEnabled(False)
                self._shortcut.deleteLater()
            except Exception:
                pass
            self._shortcut = None

    # ------------------------------------------------------------------
    # Activation / settings
    # ------------------------------------------------------------------
    def toggle(self):
        self.set_active(not self._active)

    def set_active(self, enabled: bool):
        if enabled:
            self.activate()
        else:
            self.deactivate()
        if self._toggle_button is not None:
            try:
                self._toggle_button.blockSignals(True)
                self._toggle_button.setChecked(bool(self._active))
                self._toggle_button.blockSignals(False)
            except Exception:
                pass

    def activate(self):
        if self._active or self.app is None:
            return
        if self._uses_google() and not self._api_key():
            QMessageBox.information(
                self.app,
                "Google Basemap",
                "The selected Google provider needs a Google Maps Platform API key.\n\n"
                "Choose Esri World Imagery for no-key satellite imagery, or add a Google key in Settings.",
            )
            self.open_settings()
            if self._uses_google() and not self._api_key():
                return

        if not self._is_top_view():
            QMessageBox.warning(
                self.app,
                "Basemap",
                "The basemap is designed for the 2D Top/Plan view. Switch to Top View, then activate it again.",
            )
            return

        self._enforce_2d_plan_view()
        self._active = True
        self._capture_camera_sig()

        if not self._project_crs() and not self._has_project_data():
            if not self._standalone_view_initialized:
                lon = self._settings.value("center_lon", 78.9629, type=float)
                lat = self._settings.value("center_lat", 20.5937, type=float)
                zoom = self._settings.value("center_zoom", 5, type=int)
                self.go_to_lonlat(lon, lat, zoom, schedule=False)
                self._standalone_view_initialized = True

        self.schedule_refresh(0)
        self._status(f"{self._provider_label()} enabled", 2200)

    def deactivate(self, silent=False):
        if (
            not self._active
            and not self._tile_actors
            and self._viewport_actor is None
            and self._attribution_actor is None
        ):
            return
        self._active = False
        if self._coalesce_timer is not None:
            try:
                self._coalesce_timer.stop()
            except Exception:
                pass
            self._coalesce_timer = None
        if self._projected_compose_timer is not None:
            self._projected_compose_timer.stop()
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
        self._abort_non_session_replies()
        self._clear_tiles(render=False)
        self._remove_attribution_actor()
        self._render(reset_clipping=True)
        if not silent:
            self._status("Basemap disabled", 2000)

    def open_settings(self):
        if self.app is None:
            return
        dlg = _SettingsDialog(self, self.app)
        dlg.exec()

    def open_go_to_dialog(self):
        if self.app is None:
            return
        dlg = _GoToDialog(self, self.app)
        dlg.exec()

    def _api_key(self) -> str:
        return (os.getenv("GOOGLE_MAPS_API_KEY") or self._settings.value("api_key", "", type=str) or "").strip()

    def _provider(self) -> str:
        value = (self._settings.value("provider", "esri_world_imagery", type=str) or "esri_world_imagery").strip().lower()
        return value if value in PROVIDER_IDS else "esri_world_imagery"

    def _provider_label(self) -> str:
        current = self._provider()
        for label, value in PROVIDER_CHOICES:
            if value == current:
                return label
        return "Esri World Imagery"

    def _uses_google(self) -> bool:
        return self._provider().startswith("google_")

    def _google_map_type(self) -> str:
        return {
            "google_satellite": "satellite",
            "google_roadmap": "roadmap",
            "google_terrain": "terrain",
        }.get(self._provider(), "satellite")

    def _session_settings_signature(self):
        return (
            self._provider(),
            self._api_key(),
            self._google_map_type(),
            self._settings.value("language", "en-US", type=str) or "en-US",
            self._settings.value("region", "IN", type=str) or "IN",
        )

    def _provider_changed(self, clear_tiles=True):
        self._refresh_generation += 1
        self._abort_non_session_replies()
        self._invalidate_session(clear_tiles=False)
        self._remove_attribution_actor()
        self._tile_actor_signature = None
        if clear_tiles:
            self._clear_tiles(render=False)
        if self._provider() == "esri_world_imagery":
            self._esri_attribution_loaded = False
        self._render(reset_clipping=True)

    def _sync_provider_combo(self):
        combo = self._provider_combo
        if combo is None:
            return
        try:
            idx = combo.findData(self._provider())
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, idx))
            combo.blockSignals(False)
        except Exception:
            pass

    def _on_provider_combo_changed(self, _index):
        combo = self._provider_combo
        if combo is None:
            return
        value = combo.currentData()
        if value not in PROVIDER_IDS or value == self._provider():
            return
        self._settings.setValue("provider", value)
        self._settings.sync()
        self._provider_changed(clear_tiles=True)
        if self._active:
            self.schedule_refresh(0)
        self._status(f"Basemap source: {self._provider_label()}", 2200)

    # ------------------------------------------------------------------
    # Camera / project monitoring
    # ------------------------------------------------------------------
    def _install_canvas_hooks(self):
        if self.app is None:
            return
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            interactor = getattr(vtk_widget, "interactor", None)
            if interactor is not None:
                interactor.installEventFilter(self)
                for event_name in (
                    "EndInteractionEvent",
                    "MouseWheelForwardEvent",
                    "MouseWheelBackwardEvent",
                    "MiddleButtonReleaseEvent",
                    "LeftButtonReleaseEvent",
                ):
                    tag = interactor.AddObserver(event_name, self._on_vtk_camera_event, -5.0)
                    self._camera_observer_tags.append((interactor, tag))

            # The host LiDAR app performs most pan/zoom programmatically (it sets
            # the camera directly from wheel/pan handlers), so interactor-style
            # events are unreliable. Observing the camera's ModifiedEvent catches
            # EVERY camera change regardless of input source. The handler ignores
            # modifications that do not change the transform (e.g. our own
            # ResetCameraClippingRange) so it never loops.
            renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
            camera = getattr(renderer, "GetActiveCamera", lambda: None)() if renderer is not None else None
            if camera is not None:
                self._add_camera_hook(camera)
        except Exception as exc:
            print(f"Basemap camera hook warning: {exc}")

    def _remove_canvas_hooks(self):
        try:
            interactor = getattr(getattr(self.app, "vtk_widget", None), "interactor", None) if self.app else None
            if interactor is not None:
                interactor.removeEventFilter(self)
        except Exception:
            pass
        for interactor, tag in self._camera_observer_tags:
            try:
                interactor.RemoveObserver(tag)
            except Exception:
                pass
        self._camera_observer_tags.clear()

    def eventFilter(self, obj, event):
        if self._active and event.type() in (QEvent.Resize, QEvent.Show):
            self.schedule_refresh(250)
        return False

    def _current_camera_sig(self):
        """Return the camera transform signature, or None if not readable.

        Captures the complete camera transform except clipping range. Position,
        view-up and projection mode are included so orbit/roll is detected even
        when focal point and zoom did not change.
        """
        try:
            vtk_widget = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vtk_widget, "renderer", None) if vtk_widget is not None else None
            if renderer is None:
                return None
            cam = renderer.GetActiveCamera()
            fp = tuple(float(v) for v in cam.GetFocalPoint())
            pos = tuple(float(v) for v in cam.GetPosition())
            view_up = tuple(float(v) for v in cam.GetViewUp())
            ps = float(cam.GetParallelScale())
            parallel = bool(cam.GetParallelProjection())
        except Exception:
            return None
        return (
            round(fp[0], 6),
            round(fp[1], 6),
            round(fp[2], 6),
            round(ps, 9),
            *(round(value, 6) for value in pos),
            *(round(value, 6) for value in view_up),
            parallel,
        )

    def _capture_camera_sig(self):
        """Record the current camera signature so our own renders are ignored."""
        self._last_camera_sig = self._current_camera_sig()

    def _on_vtk_camera_event(self, obj=None, event=None):
        if not self._active:
            return
        repaired = self._enforce_2d_plan_view()
        sig = self._current_camera_sig()
        if sig is None or sig == self._last_camera_sig:
            return
        self._last_camera_sig = sig
        if repaired:
            self._render(reset_clipping=True)
        self.schedule_refresh(300)

    def _enforce_2d_plan_view(self):
        if self._enforcing_2d_plan_view or not self._is_top_view():
            return False
        self._enforcing_2d_plan_view = True
        try:
            changed = self._repair_plan_camera()
            return self._repair_plan_interactor() or changed
        finally:
            self._enforcing_2d_plan_view = False

    def _repair_plan_camera(self):
        camera = self.app.vtk_widget.renderer.GetActiveCamera()
        focal = tuple(map(float, camera.GetFocalPoint()))
        position = tuple(map(float, camera.GetPosition()))
        distance = math.dist(position, focal)
        if not math.isfinite(distance) or distance < 1e-9:
            distance = 1.0
        target = (focal[0], focal[1], focal[2] + distance)
        tilted = math.dist(position, target) > max(1e-9, distance * 1e-10)
        rolled = math.dist(tuple(camera.GetViewUp()), (0.0, 1.0, 0.0)) > 1e-9
        changed = not camera.GetParallelProjection() or tilted or rolled
        if not changed:
            return False
        camera.ParallelProjectionOn()
        camera.SetPosition(*target)
        camera.SetViewUp(0.0, 1.0, 0.0)
        camera.OrthogonalizeViewUp()
        return True

    def _repair_plan_interactor(self):
        interactor = getattr(self.app.vtk_widget, "interactor", None)
        if interactor is None:
            return False
        style = interactor.GetInteractorStyle()
        if style is not None and style.GetClassName() == "vtkInteractorStyleImage":
            return False
        from vtkmodules.vtkInteractionStyle import vtkInteractorStyleImage
        interactor.SetInteractorStyle(vtkInteractorStyleImage())
        return True

    def _watch_project_state(self):
        if not self._active or self.app is None:
            return
        self._ensure_live_camera_hook()
        self._enforce_2d_plan_view()
        sig = (
            self._canonical_crs_signature(self._project_crs()),
            bool(getattr(self.app, "is_3d_mode", False)),
            str(getattr(self.app, "current_view", "top") or "top").lower(),
        )
        if sig != self._last_project_signature:
            self._last_project_signature = sig
            self._capture_camera_sig()
            self._tile_actor_signature = None
            self._clear_tiles(render=False)
            if not self._is_top_view():
                self._remove_attribution_actor()
                self._render(reset_clipping=True)
            self.schedule_refresh(100)

    def _ensure_live_camera_hook(self):
        widget = getattr(self.app, "vtk_widget", None)
        renderer = getattr(widget, "renderer", None)
        camera = renderer.GetActiveCamera() if renderer is not None else None
        if camera is None:
            return
        if any(obj is camera for obj, _tag in self._camera_observer_tags):
            return
        self._add_camera_hook(camera)

    def _add_camera_hook(self, camera):
        # Run before the host raster-camera mirror (default VTK priority 0).
        # If another tool briefly tilts/rolls the main camera, we repair it first,
        # so the raster renderer never copies that transient bad orientation.
        tag = camera.AddObserver("ModifiedEvent", self._on_vtk_camera_event, 1.0)
        self._camera_observer_tags.append((camera, tag))
        self._capture_camera_sig()

    def go_to_lonlat(self, lon: float, lat: float, zoom: int, schedule=True):
        if self.app is None:
            return
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return
        try:
            rw = self._get_render_window(vtk_widget)
            if rw is None:
                raise RuntimeError("VTK render window is not available yet")
            _width, height = rw.GetSize()
            height = max(1, int(height))

            # Define the requested view in Web Mercator (the slippy-map/Web Mercator
            # coordinate system), then transform that camera extent into the
            # application's project CRS. This keeps Go To correct for UTM and
            # other projected/geographic CRSs instead of silently moving the
            # app camera into EPSG:3857 coordinates.
            merc_x, merc_y = lonlat_to_web_mercator(lon, lat)
            lat_rad = math.radians(max(-85.0, min(85.0, lat)))
            meters_per_pixel = (
                156543.03392804097
                * max(0.01, math.cos(lat_rad))
                / (2 ** int(zoom))
            )
            visible_height_merc = max(1e-6, meters_per_pixel * height)

            target_crs = self._project_crs()
            if target_crs is None:
                if self._has_project_data():
                    # Same rule as the viewport refresh: do not move the camera
                    # using a guessed EPSG:3857 assumption while real survey/GIS
                    # data with an unresolved CRS is loaded.
                    self._status("CRS required before basemap alignment.", 4500)
                    return
                target_crs = CRS.from_epsg(3857)

            from_merc = Transformer.from_crs(
                CRS.from_epsg(3857), target_crs, always_xy=True
            )
            center_x, center_y = from_merc.transform(merc_x, merc_y)
            _x0, y0 = from_merc.transform(
                merc_x, merc_y - visible_height_merc / 2.0
            )
            _x1, y1 = from_merc.transform(
                merc_x, merc_y + visible_height_merc / 2.0
            )
            parallel_scale = abs(float(y1) - float(y0)) / 2.0
            if not all(
                math.isfinite(float(v))
                for v in (center_x, center_y, parallel_scale)
            ):
                raise RuntimeError("Target coordinate transform returned invalid values")

            cam = vtk_widget.renderer.GetActiveCamera()
            old_focal_z = float(cam.GetFocalPoint()[2])
            old_pos_z = float(cam.GetPosition()[2])
            z_distance = old_pos_z - old_focal_z
            if abs(z_distance) < 1e-9:
                z_distance = 1.0

            cam.ParallelProjectionOn()
            cam.SetFocalPoint(float(center_x), float(center_y), old_focal_z)
            cam.SetPosition(float(center_x), float(center_y), old_focal_z + z_distance)
            cam.SetViewUp(0.0, 1.0, 0.0)
            cam.SetParallelScale(max(1e-9, parallel_scale))
            self._enforce_2d_plan_view()
            vtk_widget.renderer.ResetCameraClippingRange()
            self._capture_camera_sig()
            self._render()
            self._standalone_view_initialized = True
            if not self._active:
                # Go To should actually show the place, so enable the basemap on
                # the fly when we are in the 2D Top view.
                if self._is_top_view():
                    self._active = True
                    self._status("Basemap enabled - showing location", 2200)
                    if schedule:
                        self.schedule_refresh(0)
                else:
                    self._status(
                        "Camera moved. Switch to Top View to see the basemap here.",
                        4000,
                    )
            elif schedule:
                self.schedule_refresh(0)
        except Exception as exc:
            self._show_error_once(f"Could not move basemap camera: {exc}")

    # ------------------------------------------------------------------
    # Refresh / provider networking
    # ------------------------------------------------------------------
    def schedule_refresh(self, delay_ms=250):
        if not self._active or self._refresh_timer is None:
            return
        self._refresh_timer.stop()
        self._refresh_timer.start(max(0, int(delay_ms)))

    def on_canvas_crs_changed(self):
        """Entry point used by gui.crs_manager.notify_plugins_crs_changed().

        The 1s _watch_project_state() poll would catch this too, but the
        authoritative canvas CRS notification is instant - no reason to wait.
        """
        new_sig = self._canonical_crs_signature(self._project_crs())
        old_sig = self._tile_active_sig[0] if self._tile_active_sig else None
        self._tile_actor_signature = None
        if new_sig is None and old_sig is not None:
            try:
                from gui.crs_manager import scene_has_spatial_content
                empty = not scene_has_spatial_content(self.app)
            except Exception:
                empty = not self._has_project_data()
            if empty:
                view = getattr(self.app, "_released_canvas_view", None) or {}
                lon = float(view.get("lon", self._settings.value("center_lon", 0.0, type=float)))
                lat = float(view.get("lat", self._settings.value("center_lat", 0.0, type=float)))
                zoom = int(view.get("zoom", self._settings.value("center_zoom", 3, type=int)))
                self._active_ids = set()
                self._abort_unwanted_tile_replies()
                self._clear_tiles(render=False)
                self._tile_clip_planes = None
                self._projected_extent_cache.clear()
                self._tile_geometry_cache.clear()
                self._tile_active_sig = None
                self._standalone_view_initialized = False
                self.go_to_lonlat(lon, lat, zoom, schedule=False)
                print("BASEMAP_MODE_CHANGE old_mode=projected "
                      "new_mode=standalone_webmercator "
                      f"old_crs={old_sig} geographic_center=({lon:.8f},{lat:.8f}) zoom={zoom}")
                self.schedule_refresh(0)
                return
        if old_sig is not None and old_sig != new_sig:
            self._clear_tiles(render=False)
        self.schedule_refresh(0)

    def _refresh_now(self):
        if not self._active or self.app is None or self._unloaded:
            return
        if not self._is_top_view():
            self._status("Basemap paused outside 2D Top/Plan view", 1500)
            return

        self._refresh_generation += 1
        generation = self._refresh_generation
        # Only cancel tiles that actually scrolled out of view. The old code
        # aborted EVERY in-flight tile here, so a wide (up to 36-tile) view -
        # which takes longer to download than the 300ms debounce window - was
        # killed and restarted by the next camera tick before it could ever
        # finish. That is what made zoomed-out loading loop forever while
        # zoomed-in (few tiles) resolved fine.
        self._abort_unwanted_tile_replies()

        provider = self._provider()
        if provider == "esri_world_imagery":
            self._start_esri_refresh(generation)
            return
        if provider == "openstreetmap":
            self._start_osm_refresh(generation)
            return

        key = self._api_key()
        if not key:
            self._show_error_once(
                "Selected Google basemap requires an API key. "
                "Choose Esri World Imagery or OpenStreetMap for no-key maps."
            )
            return

        sig = self._session_settings_signature()
        if (
            not self._session_token
            or self._session_signature != sig
            or time.time() >= (self._session_expiry - 60)
        ):
            self._pending_refresh_after_session = True
            self._start_session_request(generation)
            return

        self._start_google_viewport_request(generation)

    def _start_esri_refresh(self, generation):
        self._ensure_esri_attribution()
        target_crs = self._project_crs()
        if target_crs is not None and not self._is_web_mercator(target_crs):
            if self._start_projected_free_refresh(
                generation,
                target_crs,
                "esri_world_imagery",
                int(self._esri_max_zoom),
            ):
                return
        self._start_free_refresh(
            generation, "esri_world_imagery", int(self._esri_max_zoom)
        )

    def _start_osm_refresh(self, generation):
        self._set_attribution_text(OSM_ATTRIBUTION)
        target_crs = self._project_crs()
        if target_crs is not None and not self._is_web_mercator(target_crs):
            if self._start_projected_free_refresh(
                generation, target_crs, "openstreetmap", OSM_MAX_ZOOM
            ):
                return
        self._start_free_refresh(generation, "openstreetmap", OSM_MAX_ZOOM)

    def _start_free_refresh(self, generation, provider, max_zoom):
        self._projected_view = None
        self._clear_viewport_actor(render=False)
        view = self._compute_viewport_request()
        if view is None:
            return
        west, south, east, north, _zoom, target_crs, z_plane, width, height = view
        max_tiles = self._settings.value("max_tiles", 36, type=int)
        zoom = choose_zoom(
            west, south, east, north, width, height,
            max_zoom=int(max_zoom),
            max_tiles=max_tiles, margin=0,
        )
        tiles = enumerate_tiles(west, south, east, north, zoom, margin=0)
        if len(tiles) > max_tiles:
            tiles = tiles[:max_tiles]
        self._prepare_tile_set(generation, tiles, target_crs, z_plane, provider)

    def _start_projected_esri_refresh(self, generation, target_crs):
        """Backward-compatible wrapper retained for older host/plugin hooks."""
        return self._start_projected_free_refresh(
            generation,
            target_crs,
            "esri_world_imagery",
            int(self._esri_max_zoom),
        )

    def _start_projected_free_refresh(
        self, generation, target_crs, provider, max_zoom
    ):
        """Render an XYZ basemap into a non-Web-Mercator project CRS.

        v1.1.29: every tile vertex is transformed exactly from WGS84 into the
        authoritative canvas CRS. Camera state controls only deterministic
        mesh density, never the location of a geographic sample.

        Which lon/lat area to fetch, and at what zoom, still uses the exact
        CRS-domain-clipped math below (unchanged from 1.1.25/1.1.27) - only
        *placement* of each already-selected tile changed. That keeps the
        plugin from ever requesting imagery for a region the project CRS
        cannot meaningfully represent, while placement itself stays cheap
        and always renders a plain rectangle/parallelogram, never a wedge.
        """
        if provider not in FREE_PROVIDERS:
            return False

        # Small overscan gives QGIS-style preview coverage during a short pan,
        # but it is clipped immediately to the valid projected CRS footprint.
        view = self._canvas_view_state(target_crs, overscan=1.12)
        if view is None:
            return True
        camera_bounds, z_plane, width, height = view

        # EPSG area_of_use is an accuracy hint, not a mathematical clip.
        # Actual non-finite/overflow/singularity rejection happens per mesh.
        self._tile_clip_planes = None
        bounds = camera_bounds

        geographic = self._projected_lonlat_bounds(bounds, target_crs)
        if geographic is None:
            self._projected_view = None
            self._active_ids = set()
            self._abort_unwanted_tile_replies()
            self._clear_tile_actors_only(render=False)
            self._render(reset_clipping=True)
            return True
        west, south, east, north = geographic

        output_size = self._projected_output_size(
            camera_bounds, bounds, width, height
        )
        max_tiles = self._settings.value("max_tiles", 36, type=int)
        zoom = choose_zoom(
            west,
            south,
            east,
            north,
            output_size[0],
            output_size[1],
            max_zoom=int(max_zoom),
            max_tiles=max_tiles,
            margin=0,
        )
        tiles = enumerate_tiles(west, south, east, north, zoom, margin=0)
        before_validity = len(tiles)
        tiles = [t for t in tiles if self._tile_intersects_crs_area(t, target_crs)]
        self._domain_rejected = before_validity - len(tiles)
        if len(tiles) > max_tiles:
            tiles = tiles[:max_tiles]

        # The old single-texture viewport actor is retired for this path;
        # make sure nothing left over from it (or from a previous CRS/mode)
        # is still on screen.
        self._clear_viewport_actor(render=False)
        self._projected_view = None

        if not tiles:
            self._clear_tile_actors_only(render=True)
            return True

        # Reuses the same per-tile pipeline as the plain Web-Mercator-canvas
        # path (cache, placeholder-while-loading, retry, center-out ordering,
        # purge); projected placement uses exact cached tessellated geometry.
        world_per_pixel = max(
            abs(camera_bounds[2] - camera_bounds[0]) / max(1, width),
            abs(camera_bounds[3] - camera_bounds[1]) / max(1, height),
        )
        self._refresh_mesh_subdivisions = self._choose_common_subdivisions(
            tiles, target_crs, world_per_pixel
        )
        self._prepare_tile_set(generation, tiles, target_crs, z_plane, provider)
        return True

    def _inflight_tile_ids(self, provider):
        ids = set()
        for meta in self._reply_meta.values():
            context = meta[2] if meta and len(meta) > 2 else None
            if isinstance(context, dict) and context.get("provider") == provider:
                tile_id = context.get("tile_id")
                if tile_id is not None:
                    ids.add(tile_id)
        return ids

    @staticmethod
    def _coarse_parent_ids(tiles):
        parents = set()
        for z, x, y in tiles:
            parent_z = max(0, z - 2)
            shift = z - parent_z
            parent = (parent_z, x >> shift, y >> shift)
            if parent != (z, x, y):
                parents.add(parent)
        return parents

    def _schedule_tile_retry(self, context):
        provider = context.get("provider")
        tile_id = context.get("tile_id")
        key = (provider, tile_id)
        attempt = self._tile_retry_counts.get(key, 0) + 1
        if tile_id is None or attempt > 4:
            return
        self._tile_retry_counts[key] = attempt
        retry = dict(context)
        retry.pop("request_url", None)
        retry["esri_url_index"] = 0
        retry["osm_url_index"] = 0
        delay = min(5000, 500 * (2 ** (attempt - 1)))
        QTimer.singleShot(delay, lambda c=retry: self._retry_tile_if_needed(c))

    def _retry_tile_if_needed(self, context):
        tile_id = context.get("tile_id")
        provider = context.get("provider")
        key = (provider, tile_id)
        if not self._active or tile_id not in self._active_ids:
            self._tile_retry_counts.pop(key, None)
            return
        if (
            tile_id in self._tile_actors
            or tile_id in self._projected_tile_images
        ):
            self._tile_retry_counts.pop(key, None)
            return
        if tile_id not in self._inflight_tile_ids(provider):
            self._start_tile_request(self._refresh_generation, context)

    def _ensure_esri_attribution(self):
        self._set_attribution_text(ESRI_ATTRIBUTION_FALLBACK)
        if self._esri_attribution_loaded or self._esri_metadata_reply is not None or self._nam is None:
            return
        self._esri_metadata_url_index = 0
        self._request_esri_metadata()

    def _request_esri_metadata(self):
        if self._nam is None or self._esri_metadata_url_index >= len(ESRI_METADATA_URLS):
            return
        url = QUrl(ESRI_METADATA_URLS[self._esri_metadata_url_index])
        reply = self._nam.get(self._make_request(url))
        self._esri_metadata_reply = reply
        self._reply_meta[reply] = ("esri_meta", self._refresh_generation, {"url_index": self._esri_metadata_url_index})
        reply.finished.connect(lambda r=reply: self._on_esri_metadata_finished(r))

    def _on_esri_metadata_finished(self, reply):
        meta = self._reply_meta.pop(reply, None)
        self._esri_metadata_reply = None
        if self._unloaded:
            reply.deleteLater()
            return
        try:
            status = self._http_status(reply)
            body = bytes(reply.readAll())
            if status < 200 or status >= 300 or self._net_error_code(reply) != 0:
                raise RuntimeError(self._network_error_message(reply, body, status))
            data = json.loads(body.decode("utf-8"))
            text = str(data.get("copyrightText", "")).strip()
            if text and self._provider() == "esri_world_imagery":
                self._set_attribution_text(f"Esri | {text}")
            lods = ((data.get("tileInfo") or {}).get("lods") or [])
            levels = []
            for lod in lods:
                try:
                    levels.append(int(lod.get("level")))
                except Exception:
                    pass
            if levels:
                old_max_zoom = int(self._esri_max_zoom)
                self._esri_max_zoom = max(levels)
                if self._active and self._provider() == "esri_world_imagery" and self._esri_max_zoom != old_max_zoom:
                    self.schedule_refresh(0)
            self._esri_attribution_loaded = True
        except Exception as exc:
            idx = 0
            if meta and isinstance(meta[2], dict):
                idx = int(meta[2].get("url_index", 0))
            if idx + 1 < len(ESRI_METADATA_URLS):
                self._esri_metadata_url_index = idx + 1
                print(f"Basemap: Esri metadata host failed ({exc}); trying fallback host")
                QTimer.singleShot(0, self._request_esri_metadata)
            else:
                print(f"Basemap: Esri metadata warning: {exc}")
        finally:
            reply.deleteLater()

    def _start_session_request(self, generation):
        if self._session_reply is not None:
            self._pending_refresh_after_session = True
            return
        requested_signature = self._session_settings_signature()
        _provider, key, map_type, language, region = requested_signature
        payload = {
            "mapType": map_type,
            "language": language,
            "region": region.upper(),
            "imageFormat": "jpeg" if map_type == "satellite" else "png",
        }
        if map_type == "terrain":
            payload["layerTypes"] = ["layerRoadmap"]
        url = QUrl("https://tile.googleapis.com/v1/createSession?" + urlencode({"key": key}))
        request = self._make_request(url)
        try:
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        except AttributeError:
            request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        reply = self._nam.post(request, QByteArray(json.dumps(payload).encode("utf-8")))
        self._session_reply = reply
        self._reply_meta[reply] = ("session", generation, {"signature": requested_signature})
        reply.finished.connect(lambda r=reply: self._on_session_finished(r))
        self._status("Google Basemap: starting map session...", 1500)

    def _on_session_finished(self, reply):
        meta = self._reply_meta.pop(reply, None)
        self._session_reply = None
        if self._unloaded:
            reply.deleteLater()
            return
        try:
            status = self._http_status(reply)
            body = bytes(reply.readAll())
            if status < 200 or status >= 300 or self._net_error_code(reply) != 0:
                raise RuntimeError(self._network_error_message(reply, body, status))
            data = json.loads(body.decode("utf-8"))
            self._session_token = str(data["session"])
            self._session_expiry = int(data.get("expiry", int(time.time()) + 3600))
            requested_signature = None
            if meta and isinstance(meta[2], dict):
                requested_signature = meta[2].get("signature")
            self._session_signature = tuple(requested_signature) if requested_signature else None
            self._pending_refresh_after_session = False
            self.schedule_refresh(0)
        except Exception as exc:
            self._session_token = None
            self._session_expiry = 0
            self._show_error_once(f"Google Map Tiles session failed: {exc}")
        finally:
            reply.deleteLater()

    def _start_google_viewport_request(self, generation):
        view = self._compute_viewport_request()
        if view is None:
            return
        west, south, east, north, zoom, target_crs, z_plane, width, height = view
        params = {
            "session": self._session_token,
            "key": self._api_key(),
            "zoom": int(zoom),
            "north": f"{north:.10f}",
            "south": f"{south:.10f}",
            "east": f"{east:.10f}",
            "west": f"{west:.10f}",
        }
        url = QUrl("https://tile.googleapis.com/tile/v1/viewport?" + urlencode(params))
        reply = self._nam.get(self._make_request(url))
        context = {
            "bounds": (west, south, east, north),
            "zoom": int(zoom),
            "target_crs": target_crs,
            "z_plane": float(z_plane),
            "size": (int(width), int(height)),
        }
        self._reply_meta[reply] = ("viewport", generation, context)
        reply.finished.connect(lambda r=reply: self._on_google_viewport_finished(r))

    def _on_google_viewport_finished(self, reply):
        meta = self._reply_meta.pop(reply, None)
        if not meta:
            reply.deleteLater()
            return
        _, generation, context = meta
        if self._unloaded or generation != self._refresh_generation or not self._active:
            reply.deleteLater()
            return
        try:
            status = self._http_status(reply)
            body = bytes(reply.readAll())
            if status < 200 or status >= 300 or self._net_error_code(reply) != 0:
                raise RuntimeError(self._network_error_message(reply, body, status))
            data = json.loads(body.decode("utf-8"))
            copyright_text = str(data.get("copyright", "")).strip()
            text = "Google Maps" + (("   " + copyright_text) if copyright_text else "")
            self._set_attribution_text(text)

            west, south, east, north = context["bounds"]
            center_lon = (west + east) / 2.0
            center_lat = (south + north) / 2.0
            max_zoom = max_zoom_for_center(
                data.get("maxZoomRects", []), center_lon, center_lat, fallback=22
            )
            zoom = min(int(context["zoom"]), int(max_zoom))
            width, height = context["size"]
            max_tiles = self._settings.value("max_tiles", 36, type=int)
            zoom = choose_zoom(
                west, south, east, north, width, height,
                max_zoom=zoom, max_tiles=max_tiles, margin=0,
            )
            tiles = enumerate_tiles(west, south, east, north, zoom, margin=0)
            if len(tiles) > max_tiles:
                tiles = tiles[:max_tiles]
            self._prepare_tile_set(
                generation, tiles, context["target_crs"], context["z_plane"], self._provider()
            )
        except Exception as exc:
            self._show_error_once(f"Google viewport request failed: {exc}")
        finally:
            reply.deleteLater()

    def _prepare_tile_set(self, generation, tiles, target_crs, z_plane, provider):
        crs_sig = self._canonical_crs_signature(target_crs)
        # A hard clear only when the CRS or provider changes - those make old
        # tiles geometrically wrong. A pure zoom/pan keeps the previous tiles on
        # screen as lower-res placeholders, which is what removes the blink.
        if (crs_sig, provider) != self._tile_active_sig:
            self._clear_tiles(render=False)
            self._tile_active_sig = (crs_sig, provider)
        self._tile_actor_signature = (crs_sig, round(float(z_plane), 3), provider)
        self._current_z_plane = float(z_plane)
        self._active_ids = set(tiles)
        # The wanted set just changed; drop anything in flight that is no
        # longer part of it before queueing the new requests.
        self._abort_unwanted_tile_replies()
        sep = self._basemap_z_sep()

        # Active (current-zoom) tiles sit exactly on the basemap plane. Still-needed
        # older tiles stay on screen as lower-resolution placeholders pushed clearly
        # BEHIND (negative Z, large vs depth precision) so the sharper current-zoom
        # tile always wins the depth test - no z-fighting / flicker. In a top/plan
        # view the Z offset is depth-only, so there is no screen parallax.
        for tid, actor in self._tile_actors.items():
            if tid in self._active_ids:
                actor.SetPosition(0.0, 0.0, 0.0)
            else:
                actor.SetPosition(0.0, 0.0, -sep)

        missing = [
            tile_id for tile_id in tiles
            if tile_id not in self._tile_actors
            and (provider,) + tile_id not in self._dead_tile_ids
        ]
        # Request/display center-out, not scanline order. Network replies
        # complete out of order regardless, so without this the tiles that
        # happen to land first look scattered at random across the view
        # while loading; center-out makes it read as one steadily expanding
        # image (how every slippy map loads) instead of patchy pop-in.
        if len(missing) > 1:
            xs = [t[1] for t in tiles]
            ys = [t[2] for t in tiles]
            cx = (min(xs) + max(xs)) / 2.0
            cy = (min(ys) + max(ys)) / 2.0
            missing.sort(key=lambda t: (t[1] - cx) ** 2 + (t[2] - cy) ** 2)
        for tile_id in missing:
            # Instant hit from the on-disk cache = no network, no flash.
            cached = self._read_cache(provider, *tile_id)
            if cached is not None:
                if self._ingest_tile(
                    tile_id, cached, target_crs, z_plane, provider,
                    from_cache=True,
                ):
                    continue
                if (provider,) + tile_id in self._dead_tile_ids:
                    # Confirmed placeholder from a stale cache entry - already
                    # deleted from disk by _ingest_tile(); no point re-fetching
                    # the same dead tile from the network.
                    continue
            context = {
                "tile_id": tile_id,
                "target_crs": target_crs,
                "z_plane": float(z_plane),
                "provider": provider,
                "canvas_crs_signature": crs_sig,
            }
            if provider == "esri_world_imagery":
                # Split requests across Esri's two mirror hostnames (domain
                # sharding). Different hostnames get independent per-host
                # connection pools, so a big batch downloads with roughly
                # double the effective concurrency instead of queueing behind
                # one host's connection limit. Failure fallback (advancing
                # this index) is unchanged - only the STARTING host varies.
                context["esri_url_index"] = (tile_id[1] + tile_id[2]) % len(ESRI_TILE_URLS)
            elif provider == "openstreetmap":
                context["osm_url_index"] = 0
            self._start_tile_request(generation, context)

        self._maybe_purge_retired()
        self._render_coalesced()
        self._debug_refresh_record(generation, provider, crs_sig, tiles)

    def _debug_refresh_record(self, generation, provider, crs_sig, tiles):
        if not self._settings.value("debug", False, type=bool):
            return
        camera = self.app.vtk_widget.renderer.GetActiveCamera()
        raster = getattr(self.app, "_raster_renderer", None)
        clip = raster.GetActiveCamera().GetClippingRange() if raster is not None else (None, None)
        inflight = len(self._inflight_tile_ids(provider))
        active = len(set(tiles) & set(self._tile_actors))
        placeholders = len(set(self._tile_actors) - set(tiles))
        print(
            "BASEMAP_REFRESH "
            f"generation={generation} provider={provider} canvas_crs={crs_sig} "
            f"camera_center={tuple(round(float(v), 3) for v in camera.GetFocalPoint()[:2])} "
            f"parallel_scale={float(camera.GetParallelScale()):.6g} "
            f"xyz_zoom={tiles[0][0] if tiles else None} requested_tiles={len(tiles)} "
            f"cached_tiles={len(self._tile_actors)} inflight_tiles={inflight} "
            f"active_tiles={active} placeholder_tiles={placeholders} "
            f"mesh_subdivisions={self._refresh_mesh_subdivisions} "
            f"domain_rejected={self._domain_rejected} z_plane={self._current_z_plane} "
            f"raster_clip_near={clip[0]} raster_clip_far={clip[1]}"
        )

    def _start_tile_request(self, generation, context):
        if self._nam is None or self._unloaded or not self._active:
            return
        z, x, y = context["tile_id"]
        provider = context.get("provider")
        if provider == "esri_world_imagery":
            idx = int(context.get("esri_url_index", 0))
            if idx >= len(ESRI_TILE_URLS):
                return
            url = QUrl(ESRI_TILE_URLS[idx].format(z=z, y=y, x=x))
        elif provider == "openstreetmap":
            idx = int(context.get("osm_url_index", 0))
            if idx >= len(OSM_TILE_URLS):
                return
            url = QUrl(OSM_TILE_URLS[idx].format(z=z, x=x, y=y))
        else:
            params = {"session": self._session_token, "key": self._api_key()}
            url = QUrl(
                f"https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?" + urlencode(params)
            )
        context = dict(context)
        context["request_url"] = url.toString()
        reply = self._nam.get(self._make_request(url))
        self._reply_meta[reply] = ("tile", generation, context)
        reply.finished.connect(lambda r=reply: self._on_tile_finished(r))

    def _on_tile_finished(self, reply):
        meta = self._reply_meta.pop(reply, None)
        if not meta:
            reply.deleteLater()
            return
        _kind, generation, context = meta
        if self._unloaded or not self._active:
            reply.deleteLater()
            return
        # Drop a tile only when it is genuinely no longer wanted (scrolled out
        # of view / zoomed past). Matching against _refresh_generation here
        # threw away valid tiles whenever a newer refresh ticked mid-download.
        tile_id = context.get("tile_id") if isinstance(context, dict) else None
        if tile_id is not None and tile_id not in (self._active_ids or set()):
            reply.deleteLater()
            return
        if context.get("provider") != self._provider():
            reply.deleteLater()
            return
        if context.get("canvas_crs_signature") != self._canonical_crs_signature(self._project_crs()):
            reply.deleteLater()
            return
        try:
            status = self._http_status(reply)
            body = bytes(reply.readAll())
            if status < 200 or status >= 300 or self._net_error_code(reply) != 0:
                raise RuntimeError(self._network_error_message(reply, body, status))
        except Exception as exc:
            # Network-level failure: try the next host for any free provider, then report.
            provider = context.get("provider")
            if provider in FREE_PROVIDERS:
                urls, _attrib, _mz = FREE_PROVIDERS[provider]
                idx_key = "esri_url_index" if provider == "esri_world_imagery" else "osm_url_index"
                idx = int(context.get(idx_key, 0))
                if idx + 1 < len(urls):
                    retry_context = dict(context)
                    retry_context[idx_key] = idx + 1
                    retry_context.pop("request_url", None)
                    print(
                        f"Basemap: {provider} tile host {idx + 1} failed for {context.get('tile_id')}: {exc}; "
                        "trying fallback host"
                    )
                    QTimer.singleShot(0, lambda g=generation, c=retry_context: self._start_tile_request(g, c))
                else:
                    url = context.get("request_url", "")
                    self._show_error_once(
                        f"{provider} tile load failed after all hosts: {exc}"
                        + (f" | {url}" if url else "")
                    )
                    self._schedule_tile_retry(context)
            else:
                self._show_error_once(f"{self._provider_label()} tile load failed: {exc}")
                self._schedule_tile_retry(context)
            reply.deleteLater()
            return

        # Network OK - ingest: cache + decode + add, unless it's the provider's
        # own "no imagery at this zoom" placeholder (see _ingest_tile), in
        # which case the previous coarser tile stays on screen instead.
        try:
            tile_id = context["tile_id"]
            provider = context.get("provider")
            if context.get("render_mode") == "projected_viewport":
                if self._ingest_projected_tile(tile_id, body, from_cache=False):
                    self._tile_retry_counts.pop((provider, tile_id), None)
                return
            if tile_id in self._tile_actors:
                reply.deleteLater()
                return
            self._ingest_tile(
                tile_id, body, context["target_crs"], context["z_plane"], provider,
                from_cache=False,
            )
        except Exception as exc:
            self._show_error_once(f"Basemap tile decode failed: {exc}")
            self._schedule_tile_retry(context)
        finally:
            reply.deleteLater()

    @staticmethod
    def _make_request(url):
        request = QNetworkRequest(url)
        try:
            request.setHeader(
                QNetworkRequest.KnownHeaders.UserAgentHeader,
                "NakshaAI-LiDAR-Basemap/1.1.30",
            )
        except AttributeError:
            try:
                request.setHeader(
                    QNetworkRequest.UserAgentHeader,
                    "NakshaAI-LiDAR-Basemap/1.1.30",
                )
            except Exception:
                pass
        return request

    @staticmethod
    def _net_error_code(reply):
        """Return the QNetworkReply error as an int, Qt5/Qt6 safe.

        In Qt6/PySide6 ``reply.error()`` returns a ``QNetworkReply.NetworkError``
        enum; calling ``int()`` on it raises ``TypeError``. Use ``.value`` when
        present and fall back to the raw object otherwise.
        """
        err = reply.error()
        return int(getattr(err, "value", err))

    @staticmethod
    def _get_render_window(vtk_widget):
        """Resolve the host VTK render window without raising if interactor is None.

        pyvistaqt's ``QtInteractor`` exposes both ``.interactor.GetRenderWindow()``
        and ``.GetRenderWindow()`` directly. During some init orderings the
        interactor is not yet wired, so fall back gracefully instead of raising
        ``AttributeError`` ("'NoneType' object has no attribute 'GetRenderWindow'").
        """
        if vtk_widget is None:
            return None
        for getter in (
            lambda: vtk_widget.interactor.GetRenderWindow(),
            lambda: vtk_widget.GetRenderWindow(),
            lambda: getattr(vtk_widget, "render_window", None),
        ):
            try:
                rw = getter()
                if rw is not None:
                    return rw
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Viewport math / CRS handling
    # ------------------------------------------------------------------
    def _has_project_data(self):
        data = getattr(self.app, "data", None) if self.app else None
        if isinstance(data, dict):
            xyz = data.get("xyz")
            try:
                return xyz is not None and len(xyz) > 0
            except Exception:
                return bool(xyz is not None)
        return data is not None

    def _project_crs(self):
        if self.app is None:
            return None
        # Prefer the host app's single authoritative canvas CRS object
        # (gui.crs_manager) when available. It is kept in sync with the
        # legacy project_crs_* fields below, but going straight to the
        # pyproj.CRS object avoids a WKT/EPSG round-trip and always reflects
        # whichever dataset actually established the canvas CRS first.
        try:
            from gui.crs_manager import get_canvas_crs
            crs = get_canvas_crs(self.app)
            if crs is not None:
                return crs
        except Exception:
            pass
        try:
            wkt = getattr(self.app, "project_crs_wkt", None)
            if wkt:
                return CRS.from_wkt(wkt)
        except Exception:
            pass
        try:
            epsg = getattr(self.app, "project_crs_epsg", None)
            if epsg:
                return CRS.from_epsg(int(str(epsg).split(":")[-1]))
        except Exception:
            pass
        try:
            crs = getattr(self.app, "crs", None)
            if crs:
                return CRS.from_user_input(crs)
        except Exception:
            pass
        # Fallback: discover the CRS from any loaded file's adjacent WKT .prj.
        # This is essential for SNT/DGN attachments because the host loader
        # does not currently set app.project_crs_wkt for SNT-rendered data.
        # Without this, the basemap reprojects local-UTM coords as if they were
        # Web Mercator and centers on the wrong place (often the open ocean).
        try:
            crs = self._discover_crs_from_app_files()
            if crs is not None:
                return crs
        except Exception:
            pass
        return None

    @staticmethod
    def _canonical_crs_signature(crs):
        """Stable authority/WKT-equivalent signature for actor validity."""
        if crs is None:
            return None
        try:
            return crs.to_json()
        except Exception:
            return CRS.from_user_input(crs).to_wkt()

    def _attached_snt_paths(self):
        """Return the de-duplicated paths of SNT/DGN geometry on the canvas."""
        candidates = []

        def _add_record(record):
            if not isinstance(record, dict):
                return
            for key in ("full_path", "filename", "path", "source_file"):
                val = record.get(key)
                if val:
                    p = os.path.normcase(os.path.abspath(str(val)))
                    if p not in candidates:
                        candidates.append(p)
                    break

        try:
            for actor_data in getattr(self.app, "snt_actors", []) or []:
                _add_record(actor_data)
        except Exception:
            pass
        try:
            for att in getattr(self.app, "snt_attachments", []) or []:
                _add_record(att)
        except Exception:
            pass
        return candidates

    def _cached_crs_for_path(self, path):
        if not hasattr(self, "_snt_crs_cache"):
            self._snt_crs_cache = {}
        key = os.path.normcase(os.path.abspath(str(path)))
        if key in self._snt_crs_cache:
            return self._snt_crs_cache[key]
        crs = self._resolve_crs_for_path(path)
        self._snt_crs_cache[key] = crs
        return crs

    def _resolve_crs_for_path(self, path):
        try:
            from gui.crs_manager import resolve_snt_crs
            crs, _ = resolve_snt_crs(path)
            return crs
        except Exception:
            return None

    def _attached_snt_crs(self):
        """Return (has_snt, common_crs); unresolved/disagreeing SNT => None."""
        paths = self._attached_snt_paths()
        if not paths:
            return False, None

        common = None
        for path in paths:
            crs = self._cached_crs_for_path(path)
            if crs is None:
                return True, None
            if common is None:
                common = crs
                continue
            try:
                agrees = bool(common.equals(crs))
            except Exception:
                agrees = common == crs
            if not agrees:
                return True, None
        return True, common

    def _current_project_signature(self):
        """Stable signature that invalidates tile cache if project data or CRS changes."""
        snt_paths = tuple(sorted(self._attached_snt_paths()))
        p_crs = None
        try:
            c = self._project_crs()
            p_crs = c.to_wkt() if c else None
        except Exception:
            p_crs = None
        loaded = getattr(self.app, "loaded_file", None)
        return (loaded, snt_paths, p_crs)

    def _discover_crs_from_app_files(self):
        """Find a CRS by scanning the app for any loaded .snt/.dgn/.laz/.las/.ply
        file path and reading the adjacent OGC WKT .prj (NOT TerraScan block .prj).

        Sources, in priority order:
          1. app.loaded_file / last_save_path / current_file / project_file
          2. app.snt_attachments / attachments / loaded_items / snt_items dicts/lists
          3. VTK actors tagged with _naksha_snt_filename by snt_attachment.py
        The first path with an adjacent parseable WKT .prj wins. Result is cached
        back onto the app so we never re-scan.
        """
        candidates = []
        try:
            for attr in (
                "loaded_file",
                "last_save_path",
                "current_file",
                "project_file",
                "snt_path",
                "snt_filename",
                "loaded_laz_path",
                "laz_path",
                "las_path",
                "point_cloud_path",
            ):
                v = getattr(self.app, attr, None)
                if v:
                    candidates.append(str(v))
        except Exception:
            pass

        def _harvest_dict(item):
            if not isinstance(item, dict):
                return None
            for k in (
                "full_path",
                "filename",
                "snt_path",
                "path",
                "file_path",
                "source_path",
                "attached_file",
            ):
                v = item.get(k)
                if v:
                    return str(v)
            return None

        try:
            for attr in ("snt_attachments", "attachments", "loaded_items",
                         "snt_items", "loaded_attachments", "project_files"):
                v = getattr(self.app, attr, None)
                if v is None:
                    continue
                if isinstance(v, dict):
                    for vv in v.values():
                        fp = _harvest_dict(vv) if isinstance(vv, dict) else (
                            str(vv) if isinstance(vv, str) else None)
                        if fp:
                            candidates.append(fp)
                elif isinstance(v, (list, tuple, set)):
                    for item in v:
                        fp = _harvest_dict(item) if isinstance(item, dict) else (
                            str(item) if isinstance(item, str) else None)
                        if fp:
                            candidates.append(fp)
        except Exception:
            pass

        # Actors stamped by snt_attachment._render_snt_in_vtk
        try:
            vw = getattr(self.app, "vtk_widget", None)
            renderer = getattr(vw, "renderer", None)
            if renderer is not None:
                actors = renderer.GetActors()
                n = actors.GetNumberOfItems() if actors is not None else 0
                for i in range(n):
                    prop = actors.GetItemAsObject(i)
                    if prop is None:
                        continue
                    fn = getattr(prop, "_naksha_snt_filename", None)
                    if fn:
                        candidates.append(str(fn))
        except Exception:
            pass

        seen = set()
        for c in candidates:
            c = (c or "").strip()
            if not c or c in seen:
                continue
            seen.add(c)
            crs = self._resolve_crs_for_path(c)
            if crs is not None:
                try:
                    wkt = crs.to_wkt()
                    try:
                        self.app.project_crs_wkt = wkt
                    except Exception:
                        pass
                    try:
                        self.app.crs = crs
                    except Exception:
                        pass
                    try:
                        epsg = crs.to_epsg()
                        if epsg:
                            self.app.project_crs_epsg = int(epsg)
                    except Exception:
                        pass
                except Exception:
                    pass
                return crs
        return None

    def _read_adjacent_wkt_prj(self, file_path):
        """Read the .prj next to file_path. Supports:
          - OGC WKT (starts with PROJCS/GEOGCS/GEOCCS/...).
          - TerraScan project file (starts with ``[TerraScan project]``)
            with a ``ProjectionSystem=<EPSG>`` line — the TerraScan EPSG code
            for the LiDAR project / SNT blocks.
        Falls back to scanning the same folder for a uniquely-named
        differently-stemmed ``.prj`` (TerraScan delivery convention).
        Returns CRS or None.
        """
        try:
            from pathlib import Path
            p = Path(str(file_path))
            if p.suffix.lower() not in (".snt", ".dgn", ".laz", ".las", ".ply"):
                return None
            # Primary: same-stem .prj
            prj = p.with_suffix(".prj")
            if prj.exists() and prj.is_file():
                crs = self._try_read_prj(prj)
                if crs is not None:
                    return crs
            # Fallback: a single differently-named .prj in the same folder
            try:
                siblings = [q for q in p.parent.glob("*.prj") if q.is_file()]
            except Exception:
                siblings = []
            if len(siblings) == 1:
                return self._try_read_prj(siblings[0])
            return None
        except Exception:
            return None

    def _try_read_prj(self, prj):
        """Parse a single .prj file: WKT or TerraScan ProjectionSystem."""
        try:
            from pathlib import Path
            prj = Path(str(prj))
            with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(2048).lstrip()
            if not head:
                return None
            upper = head.upper()
            if head[:32].lstrip().upper().startswith("[TERRASCAN"):
                import re as _re
                with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                m = _re.search(r"ProjectionSystem\s*=\s*(\d+)", text, _re.IGNORECASE)
                if m:
                    epsg = int(m.group(1))
                    if epsg > 0:
                        return CRS.from_epsg(epsg)
                return None
            looks_like_wkt = any(upper.startswith(k) for k in (
                "PROJCS", "GEOGCS", "GEOCCS", "COMPD_CS", "VERT_CS",
                "LOCAL_CS", "ENGINEERINGCRS", "PARAMETRICCRS",
                "TIMECRS", "DERIVEDPROJCRS",
            ))
            if not looks_like_wkt:
                if "PROJCS[" not in upper and "GEOGCS[" not in upper:
                    return None
            with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                wkt_text = f.read()
            return CRS.from_wkt(wkt_text)
        except Exception:
            return None

    def _read_laz_crs(self, laz_path):
        """Read CRS from a LAZ/LAS file's own projection VLRs.
        Tries, in order:
          1. OGC WKT VLR (record_id 2112 / 2113, user_id ``LASF_Projection``)
          2. GeoKey Directory VLR (record_id 34735) — looks for keys 3072
             (ProjectedCSTypeGeoKey) or 2048 (GeographicTypeGeoKey).
        The LAZ header is stored uncompressed so the raw bytes can be parsed
        directly without invoking laspy or decompressing point data.
        """
        try:
            import struct as _st
            with open(laz_path, "rb") as f:
                sig = f.read(4)
                if sig != b"LASF":
                    return None
                f.seek(94)
                header_size = _st.unpack_from("<H", f.read(2))[0]
                offset_to_pt = _st.unpack_from("<I", f.read(4))[0]
                n_vlr = _st.unpack_from("<I", f.read(4))[0]
                pos = header_size
                for _ in range(n_vlr):
                    if pos + 22 > offset_to_pt:
                        break
                    f.seek(pos)
                    f.read(2)  # reserved
                    uid_raw = f.read(16)
                    uid = uid_raw.split(b"\x00", 1)[0].decode("ascii", errors="ignore")
                    record_id = _st.unpack_from("<H", f.read(2))[0]
                    record_len = _st.unpack_from("<H", f.read(2))[0]
                    if pos + 22 + record_len > offset_to_pt:
                        break
                    if uid.startswith("LASF_Proj"):
                        data = f.read(record_len)
                        # OGC WKT VLR (LAS 1.4)
                        if record_id in (2112, 2113) and record_len > 0:
                            try:
                                wkt = data.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
                                if wkt and ("PROJCS" in wkt or "GEOGCS" in wkt
                                            or "GEOCCS" in wkt or "COMPD_CS" in wkt
                                            or "VERT_CS" in wkt or "LOCAL_CS" in wkt):
                                    return CRS.from_wkt(wkt)
                            except Exception:
                                pass
                        # GeoKey Directory VLR
                        elif record_id == 34735 and record_len >= 8:
                            try:
                                n_short = record_len // 2
                                shorts = _st.unpack_from("<" + "H" * n_short, data)
                                nkeys = shorts[3]
                                for i in range(nkeys):
                                    base = 4 + i * 4
                                    if base + 3 >= n_short:
                                        break
                                    key_id = shorts[base]
                                    tiff_loc = shorts[base + 1]
                                    count = shorts[base + 2]
                                    val_off = shorts[base + 3]
                                    if key_id in (3072, 2048) and tiff_loc == 0 and count == 1:
                                        epsg = int(val_off)
                                        if epsg > 0:
                                            return CRS.from_epsg(epsg)
                            except Exception:
                                pass
                    pos += 22 + record_len
        except Exception:
            return None
        return None

    def _snt_referenced_laz_paths(self, snt_path):
        """If a TerraScan ``.prj`` exists for snt_path (same-stem or a unique
        differently-named one in the folder), return the ``.laz`` / ``.las``
        file paths referenced by its ``Block <name>`` lines (only those that
        actually exist on disk). Used to recover the CRS when the SNT itself
        has no ``ProjectionSystem`` — the embedded LAZ usually carries it in
        its own header.
        """
        try:
            from pathlib import Path
            import re as _re
            p = Path(str(snt_path))
            prj_candidates = []
            same_stem = p.with_suffix(".prj")
            if same_stem.exists():
                prj_candidates.append(same_stem)
            try:
                siblings = [q for q in p.parent.glob("*.prj") if q.is_file()]
            except Exception:
                siblings = []
            if len(siblings) == 1 and siblings[0] not in prj_candidates:
                prj_candidates.append(siblings[0])
            for prj in prj_candidates:
                try:
                    with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(2048).lstrip()
                except Exception:
                    continue
                if not head[:32].lstrip().upper().startswith("[TERRASCAN"):
                    continue
                try:
                    with open(prj, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                except Exception:
                    continue
                found = []
                seen = set()
                for ln in lines:
                    m = _re.match(r"\s*Block\s+(.+?)\s*$", ln)
                    if not m:
                        continue
                    block_name = m.group(1).strip()
                    base_name = block_name
                    for ext in (".laz", ".las", ".LAZ", ".LAS"):
                        if base_name.lower().endswith(ext.lower()):
                            base_name = base_name[: -len(ext)]
                            break
                    for ext in (".laz", ".las"):
                        candidate = prj.parent / f"{base_name}{ext}"
                        if candidate.exists() and str(candidate).lower() not in seen:
                            seen.add(str(candidate).lower())
                            found.append(str(candidate))
                if found:
                    return found
            return []
        except Exception:
            return []

    def _resolve_crs_for_path(self, path):
        """Return CRS for *path* or None. Tries WKT/TerraScan .prj, then
        direct LAZ VLRs, then SNT -> referenced-LAZ chain."""
        try:
            from pathlib import Path
            p = Path(str(path))
            suf = p.suffix.lower()
            crs = self._read_adjacent_wkt_prj(path)
            if crs is not None:
                return crs
            if suf in (".laz", ".las"):
                return self._read_laz_crs(path)
            if suf in (".snt", ".dgn"):
                for laz in self._snt_referenced_laz_paths(path):
                    crs = self._read_laz_crs(laz)
                    if crs is not None:
                        return crs
            return None
        except Exception:
            return None

    def _canvas_view_state(self, target_crs=None, overscan=1.0):
        """Return exact axis-aligned canvas bounds in the current canvas CRS."""
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return None
        try:
            rw = self._get_render_window(vtk_widget)
            if rw is None:
                return None
            width, height = rw.GetSize()
            width = max(1, int(width))
            height = max(1, int(height))
            renderer = vtk_widget.renderer
            cam = renderer.GetActiveCamera()
            if not cam.GetParallelProjection():
                self._status("Basemap requires 2D parallel Top View", 1800)
                return None
            fx, fy, _fz = map(float, cam.GetFocalPoint())
            half_h = max(1e-9, float(cam.GetParallelScale())) * max(
                1.0, float(overscan)
            )
            half_w = half_h * width / float(height)
            bounds = (fx - half_w, fy - half_h, fx + half_w, fy + half_h)
            if not all(math.isfinite(value) for value in bounds):
                return None
            if target_crs is None:
                target_crs = self._project_crs()
            if target_crs is None:
                return None
            z_plane = self._choose_background_z(renderer, cam)
            return bounds, z_plane, width, height
        except Exception as exc:
            self._show_error_once(f"Could not calculate 2D canvas bounds: {exc}")
            return None

    def _viewport_is_stale_for_camera(self, camera_bounds, ratio=1.6):
        """True if the on-screen viewport actor no longer matches the live
        camera scale closely enough to keep showing while a replacement
        composes (see _start_projected_free_refresh)."""
        if self._viewport_actor is None or self._viewport_camera_width is None:
            return False
        try:
            live_width = abs(float(camera_bounds[2]) - float(camera_bounds[0]))
        except Exception:
            return False
        if not math.isfinite(live_width) or live_width <= 0:
            return False
        prior_width = float(self._viewport_camera_width)
        if prior_width <= 0:
            return False
        scale_change = max(live_width / prior_width, prior_width / live_width)
        return scale_change > float(ratio)

    @staticmethod
    def _intersect_bounds(a, b):
        ax0, ay0, ax1, ay1 = map(float, a)
        bx0, by0, bx1, by1 = map(float, b)
        xmin = max(ax0, bx0)
        ymin = max(ay0, by0)
        xmax = min(ax1, bx1)
        ymax = min(ay1, by1)
        if not all(map(math.isfinite, (xmin, ymin, xmax, ymax))):
            return None
        if xmax <= xmin or ymax <= ymin:
            return None
        return (xmin, ymin, xmax, ymax)

    def _projected_crs_valid_extent(self, target_crs):
        """Projected envelope of the CRS's official geographic area-of-use.

        Densifying all four geographic edges avoids the classic mistake of
        projecting only four corners of a curved projection domain. The result is
        cached because a project's CRS normally stays fixed for the session.
        """
        try:
            signature = target_crs.to_wkt()
            if signature in self._projected_extent_cache:
                return self._projected_extent_cache[signature]
            area = getattr(target_crs, "area_of_use", None)
            if area is None:
                self._projected_extent_cache[signature] = None
                return None
            west = float(area.west)
            south = float(area.south)
            east = float(area.east)
            north = float(area.north)
            if not all(map(math.isfinite, (west, south, east, north))):
                self._projected_extent_cache[signature] = None
                return None
            if east <= west or north <= south:
                self._projected_extent_cache[signature] = None
                return None

            n = 129
            lon_axis = np.linspace(west, east, n, dtype=np.float64)
            lat_axis = np.linspace(south, north, n, dtype=np.float64)
            lons = np.concatenate(
                (lon_axis, lon_axis, np.full(n, west), np.full(n, east))
            )
            lats = np.concatenate(
                (np.full(n, south), np.full(n, north), lat_axis, lat_axis)
            )
            forward = Transformer.from_crs(
                CRS.from_epsg(4326), target_crs, always_xy=True
            )
            xs, ys = forward.transform(lons, lats)
            xs = np.asarray(xs, dtype=np.float64)
            ys = np.asarray(ys, dtype=np.float64)
            valid = np.isfinite(xs) & np.isfinite(ys)
            # PROJ can return enormous finite values near a projection
            # singularity. They are not useful map-domain coordinates.
            valid &= (np.abs(xs) < 1.0e12) & (np.abs(ys) < 1.0e12)
            if not np.any(valid):
                self._projected_extent_cache[signature] = None
                return None
            extent = (
                float(xs[valid].min()),
                float(ys[valid].min()),
                float(xs[valid].max()),
                float(ys[valid].max()),
            )
            if extent[2] <= extent[0] or extent[3] <= extent[1]:
                extent = None
            self._projected_extent_cache[signature] = extent
            return extent
        except Exception:
            return None

    @staticmethod
    def _projected_output_size(camera_bounds, render_bounds, width, height):
        """Pixel budget based on the *visible CRS overlap*, not full canvas.

        If Estonia occupies 25 pixels after a huge zoom-out, requesting an
        1800-pixel texture for the whole camera is both wrong LOD and wasted CPU.
        A 1024-pixel edge cap keeps settled imagery crisp while allowing the
        background reprojection job to finish quickly.
        """
        cx0, cy0, cx1, cy1 = map(float, camera_bounds)
        rx0, ry0, rx1, ry1 = map(float, render_bounds)
        camera_w = max(1e-12, cx1 - cx0)
        camera_h = max(1e-12, cy1 - cy0)
        fraction_x = max(0.0, min(1.0, (rx1 - rx0) / camera_w))
        fraction_y = max(0.0, min(1.0, (ry1 - ry0) / camera_h))
        pixel_w = max(1.0, float(width) * fraction_x)
        pixel_h = max(1.0, float(height) * fraction_y)
        supersample = 1.12
        max_edge = 1024.0
        scale = min(
            supersample,
            max_edge / max(1.0, pixel_w),
            max_edge / max(1.0, pixel_h),
        )
        return (
            max(32, int(round(pixel_w * scale))),
            max(32, int(round(pixel_h * scale))),
        )

    def _projected_lonlat_bounds(self, bounds, target_crs):
        """Dense inverse footprint for a *domain-clipped* projected rectangle.

        The v1.1.24 9x9 grid degenerates to one valid center point for EPSG:3301
        once the camera is zoomed far enough out. Dense vectorized sampling on a
        rectangle already clipped to the official CRS domain guarantees enough
        valid samples and prevents a zero-area geographic bbox / z22 tile burst.
        """
        xmin, ymin, xmax, ymax = map(float, bounds)
        try:
            values = np.linspace(0.0, 1.0, 33, dtype=np.float64)
            xs = xmin + (xmax - xmin) * values
            ys = ymin + (ymax - ymin) * values
            grid_x, grid_y = np.meshgrid(xs, ys)
            transform = Transformer.from_crs(
                target_crs, CRS.from_epsg(4326), always_xy=True
            )
            lon, lat = transform.transform(grid_x, grid_y)
            lon = np.asarray(lon, dtype=np.float64)
            lat = np.asarray(lat, dtype=np.float64)
            valid = (
                np.isfinite(lon)
                & np.isfinite(lat)
                & (lon >= -180.0)
                & (lon <= 180.0)
                & (lat >= -MAX_MERCATOR_LAT)
                & (lat <= MAX_MERCATOR_LAT)
            )
            if not np.any(valid):
                return None
            lons = lon[valid]
            lats = lat[valid]
            result = (
                float(lons.min()), float(lats.min()),
                float(lons.max()), float(lats.max()),
            )
            # A truly tiny view can legitimately have a tiny geographic span;
            # what is forbidden is an exact zero-area bbox caused by one sparse
            # sample surviving. Add a sub-pixel epsilon only in that rare case.
            west, south, east, north = result
            eps = 1e-10
            if east - west < eps:
                west -= eps
                east += eps
            if north - south < eps:
                south -= eps
                north += eps
            return west, south, east, north
        except Exception:
            return None

    # ------------------------------------------------------------------
    # v1.1.28: locally-linear project-CRS <-> Web Mercator alignment.
    #
    # The old projected-basemap path (still present below, unused - see the
    # note on _ProjectedComposeTask) rendered a basemap into a non-Web-
    # Mercator project CRS by transforming every output pixel through pyproj
    # and compositing the result into one texture. That is exact, but "exact"
    # is exactly the problem at global scale: a CRS like EPSG:3301 (Estonia,
    # Lambert Conformal Conic) truly IS a wedge once you plot it outside its
    # area of use, so zooming out made the basemap visibly shrink into a
    # wedge/patch no matter how correct the math was. It was also a
    # synchronous NumPy/PROJ sweep over up to ~4 million pixels on the Qt GUI
    # thread (forced there in 1.1.26 after a background attempt
    # access-violated inside PROJ on Windows).
    #
    # This replaces that with per-tile placement using a *local* linear
    # approximation of the project CRS around the current view's focal
    # point. Both the project CRS and Web Mercator are conformal projections
    # (they preserve local angles/shape by construction), so within one
    # screen's worth of view the map between them is, to very good
    # approximation, a single rotation+scale - no shear, no curvature. That
    # local map is cheap (2-3 point transforms, not a pixel sweep) and is
    # recomputed every refresh from the live focal point, so the basemap is
    # always a plain rectangle/parallelogram on screen: never a wedge,
    # regardless of what the CRS's true global shape looks like. Accuracy is
    # excellent near the view center and degrades gracefully toward the
    # edges of a very wide view or at extreme global zoom-out - the same
    # trade-off every web map already makes by using Web Mercator at all.
    # ------------------------------------------------------------------
    def _local_canvas_to_mercator_affine(self, target_crs, fx, fy, probe):
        """Return the local canvas_crs -> Web Mercator map at (fx, fy).

        The result lets a caller take any point already expressed in Web
        Mercator meters (e.g. a basemap tile corner) and place it into
        canvas-CRS world space with one multiply-add, instead of a real
        pyproj transform per point. Returns None if the CRS's own transform
        is not usable at this point (e.g. exactly on a projection
        singularity) - callers should skip this refresh and retry once the
        camera has moved rather than reuse a stale/invalid map.
        """
        try:
            to_wgs84 = Transformer.from_crs(target_crs, CRS.from_epsg(4326), always_xy=True)
            lon0, lat0 = to_wgs84.transform(fx, fy)
            if not (math.isfinite(lon0) and math.isfinite(lat0)):
                return None
            mx0, my0 = lonlat_to_web_mercator(lon0, lat0)

            # Sample one probe step along each canvas axis. The full 2x2
            # Jacobian (not an assumed pure scale) is kept so any local
            # rotation - grid convergence between the project CRS's north and
            # Mercator's north - is captured for free, along with a small
            # amount of protection against numerical noise.
            lon1, lat1 = to_wgs84.transform(fx + probe, fy)
            lon2, lat2 = to_wgs84.transform(fx, fy + probe)
            if not all(math.isfinite(v) for v in (lon1, lat1, lon2, lat2)):
                return None
            mx1, my1 = lonlat_to_web_mercator(lon1, lat1)
            mx2, my2 = lonlat_to_web_mercator(lon2, lat2)

            a = (mx1 - mx0) / probe
            c = (my1 - my0) / probe
            b = (mx2 - mx0) / probe
            d = (my2 - my0) / probe
            det = a * d - b * c
            if not math.isfinite(det) or abs(det) < 1e-9:
                return None
            return {
                "fx": float(fx), "fy": float(fy),
                "mx0": float(mx0), "my0": float(my0),
                # Inverse Jacobian: maps a Web-Mercator-meter delta back to a
                # canvas-CRS delta, which is the direction tile placement
                # actually needs (tiles are naturally given in Mercator).
                "inv_a": d / det, "inv_b": -b / det,
                "inv_c": -c / det, "inv_d": a / det,
            }
        except Exception:
            return None

    @staticmethod
    def _affine_mercator_to_canvas(affine, mx, my):
        dmx = mx - affine["mx0"]
        dmy = my - affine["my0"]
        dcx = affine["inv_a"] * dmx + affine["inv_b"] * dmy
        dcy = affine["inv_c"] * dmx + affine["inv_d"] * dmy
        return affine["fx"] + dcx, affine["fy"] + dcy

    def _compute_viewport_request(self):
        vtk_widget = getattr(self.app, "vtk_widget", None)
        if vtk_widget is None:
            return None
        try:
            rw = self._get_render_window(vtk_widget)
            if rw is None:
                return None
            width, height = rw.GetSize()
            width = max(1, int(width))
            height = max(1, int(height))
            renderer = vtk_widget.renderer
            cam = renderer.GetActiveCamera()
            if not cam.GetParallelProjection():
                self._status("Basemap requires 2D parallel Top View", 1800)
                return None

            fx, fy, fz = cam.GetFocalPoint()
            parallel_scale = max(1e-9, float(cam.GetParallelScale()))
            aspect = width / float(height)
            half_w = parallel_scale * aspect
            minx, maxx = fx - half_w, fx + half_w
            miny, maxy = fy - parallel_scale, fy + parallel_scale
            z_plane = self._choose_background_z(renderer, cam)

            target_crs = self._project_crs()
            if target_crs is None:
                if self._has_project_data():
                    # A geospatial dataset (SNT/LAZ/GIS) is loaded but its CRS
                    # could not be resolved. NEVER assume EPSG:3857 here - the
                    # loaded data is real projected/survey coordinates, and
                    # Web Mercator tiles drawn under it would silently claim a
                    # geographic alignment that is not true (often placing the
                    # map over the wrong country/ocean). Refuse to render a
                    # knowingly misaligned basemap instead.
                    self._status("CRS required before basemap alignment.", 4000)
                    self._clear_tiles(render=True)
                    return None
                # Allowed case: nothing georeferenced is loaded yet, so there is
                # nothing to misalign. A standalone basemap may use Web Mercator
                # as its own temporary canvas CRS.
                target_crs = CRS.from_epsg(3857)

            is_mercator = (
                getattr(target_crs, "to_epsg", lambda: None)() in (3857, 900913)
                or "pseudo-mercator" in getattr(target_crs, "name", "").lower()
                or "web mercator" in getattr(target_crs, "name", "").lower()
            )

            half_world = float(WEB_MERCATOR_HALF_WORLD_M)
            max_lat = float(MAX_MERCATOR_LAT)

            if is_mercator:
                # Web Mercator world-extent clamping: when zoomed out to see the entire Earth
                # or past world boundaries, clamp coordinates so they never wrap into strips or amoebas.
                if (maxx - minx) >= 2.0 * half_world:
                    west, east = -180.0, 180.0
                else:
                    c_minx = max(-half_world, min(half_world, minx))
                    c_maxx = max(-half_world, min(half_world, maxx))
                    if c_minx >= c_maxx:
                        west, east = -180.0, 180.0
                    else:
                        west = (c_minx / half_world) * 180.0
                        east = (c_maxx / half_world) * 180.0

                if (maxy - miny) >= 2.0 * half_world:
                    south, north = -max_lat, max_lat
                else:
                    c_miny = max(-half_world, min(half_world, miny))
                    c_maxy = max(-half_world, min(half_world, maxy))
                    if c_miny >= c_maxy:
                        south, north = -max_lat, max_lat
                    else:
                        south = web_mercator_to_lonlat(0.0, c_miny)[1]
                        north = web_mercator_to_lonlat(0.0, c_maxy)[1]
            else:
                # Projected local CRS (UTM, Lambert Conformal, State Plane, etc.):
                # Sample grid across camera view and validate with round-trip inversion.
                to_wgs84 = Transformer.from_crs(target_crs, CRS.from_epsg(4326), always_xy=True)
                from_wgs84 = Transformer.from_crs(CRS.from_epsg(4326), target_crs, always_xy=True)
                c_lon, c_lat = to_wgs84.transform(fx, fy)
                if not (math.isfinite(c_lon) and math.isfinite(c_lat)):
                    return None

                valid_lons = [c_lon]
                valid_lats = [c_lat]
                grid_n = 5
                for gi in range(grid_n):
                    gx = minx + (maxx - minx) * (gi / float(grid_n - 1))
                    for gj in range(grid_n):
                        gy = miny + (maxy - miny) * (gj / float(grid_n - 1))
                        lon, lat = to_wgs84.transform(gx, gy)
                        if not (math.isfinite(lon) and math.isfinite(lat)):
                            continue
                        if not (-180.0 <= lon <= 180.0 and -max_lat <= lat <= max_lat):
                            continue
                        bx, by = from_wgs84.transform(lon, lat)
                        if not (math.isfinite(bx) and math.isfinite(by)):
                            continue
                        if math.hypot(bx - gx, by - gy) > 5000.0:
                            continue
                        if abs(lon - c_lon) > 40.0:
                            continue
                        valid_lons.append(lon)
                        valid_lats.append(lat)

                if len(valid_lons) <= 1:
                    dlon = min(45.0, max(5.0, (parallel_scale / 111000.0)))
                    dlat = min(35.0, max(5.0, (parallel_scale / 111000.0)))
                    west = max(-180.0, c_lon - dlon)
                    east = min(180.0, c_lon + dlon)
                    south = max(-max_lat, c_lat - dlat)
                    north = min(max_lat, c_lat + dlat)
                else:
                    west = max(-180.0, min(valid_lons))
                    east = min(180.0, max(valid_lons))
                    south = max(-max_lat, min(valid_lats))
                    north = min(max_lat, max(valid_lats))

            if (east - west) < 0.001:
                west -= 0.001
                east += 0.001
            if (north - south) < 0.001:
                south -= 0.001
                north += 0.001

            zoom = choose_zoom(
                west,
                south,
                east,
                north,
                width,
                height,
                max_zoom=22,
                max_tiles=self._settings.value("max_tiles", 36, type=int),
                margin=0,
            )
            return west, south, east, north, zoom, target_crs, z_plane, width, height
        except Exception as exc:
            self._show_error_once(f"Could not calculate basemap viewport: {exc}")
            return None

    def _is_top_view(self):
        if self.app is None:
            return False
        if bool(getattr(self.app, "is_3d_mode", False)):
            return False
        view = str(getattr(self.app, "current_view", "top") or "top").lower()
        return view in {"top", "plan", "2d", "plan_view", "top_view"}

    @staticmethod
    def _crs_area_bounds(crs):
        area = getattr(crs, "area_of_use", None)
        if area is None:
            return None
        bounds = (area.west, area.south, area.east, area.north)
        if not all(math.isfinite(float(value)) for value in bounds):
            return None
        west, south, east, north = map(float, bounds)
        return bounds if west < east and south < north else None

    @staticmethod
    def _inside_area(lon, lat, bounds):
        if bounds is None:
            return True
        west, south, east, north = bounds
        return west <= lon <= east and south <= lat <= north

    @staticmethod
    def _is_web_mercator(crs):
        epsg = getattr(crs, "to_epsg", lambda: None)()
        name = getattr(crs, "name", "").lower()
        return (
            epsg in (3857, 900913)
            or "pseudo-mercator" in name
            or "web mercator" in name
        )

    def _project_view_bounds(self, bounds, target_crs):
        if not bounds or self._is_web_mercator(target_crs):
            return None
        west, south, east, north = map(float, bounds)
        lons = (west, (west + east) / 2.0, east)
        lats = (south, (south + north) / 2.0, north)
        transform = Transformer.from_crs(
            CRS.from_epsg(4326), target_crs, always_xy=True
        )
        points = [
            transform.transform(lon, lat) for lon in lons for lat in lats
        ]
        points = [
            (x, y)
            for x, y in points
            if math.isfinite(x) and math.isfinite(y)
        ]
        if not points:
            return None
        xs, ys = zip(*points)
        return min(xs), max(xs), min(ys), max(ys)

    def _make_tile_clip_planes(self, bounds, target_crs):
        projected = self._project_view_bounds(bounds, target_crs)
        if projected is None:
            return None
        xmin, xmax, ymin, ymax = projected
        planes = vtk.vtkPlaneCollection()
        specs = (
            ((xmin, 0.0, 0.0), (1.0, 0.0, 0.0)),
            ((xmax, 0.0, 0.0), (-1.0, 0.0, 0.0)),
            ((0.0, ymin, 0.0), (0.0, 1.0, 0.0)),
            ((0.0, ymax, 0.0), (0.0, -1.0, 0.0)),
        )
        for origin, normal in specs:
            plane = vtk.vtkPlane()
            plane.SetOrigin(*origin)
            plane.SetNormal(*normal)
            planes.AddItem(plane)
        return planes

    def _apply_tile_clip(self, actor):
        mapper = actor.GetMapper() if actor is not None else None
        if mapper is None:
            return
        mapper.RemoveAllClippingPlanes()
        if self._tile_clip_planes is not None:
            mapper.SetClippingPlanes(self._tile_clip_planes)

    def _choose_background_z(self, renderer, cam):
        """Place the basemap just below the loaded 3D data without affecting fit bounds.

        The data Z extent is computed from NON-basemap actors only. Previously this
        used ``renderer.ComputeVisiblePropBounds()``, which included the basemap
        tiles themselves, so every refresh pushed the basemap further below the data
        (a runaway). Once the host re-framed the point cloud, the camera far clip no
        longer reached the basemap and it vanished - exactly the "map disappears when
        I load a LAZ/LAS" symptom.

        The margin below ``zmin`` must dwarf depth-buffer precision, not just the
        data's own Z span: at real-world projected coordinates (UTM/Lambert, often
        hundreds of thousands of meters) a flat/near-flat dataset (e.g. a 2D-mode
        SNT) has near-zero span, so a tiny absolute epsilon here is smaller than
        the depth buffer can resolve and the two actors z-fight - the basemap can
        then win the coin flip and render in front of SNT/DXF/GIS data instead of
        under it. Reuse the same parallel-scale-based separation already used for
        placeholder tiles (_basemap_z_sep) so the gap scales with the view instead
        of being swamped by coordinate magnitude.
        """
        sep = self._basemap_z_sep()
        try:
            zb = self._data_z_bounds(renderer)
            if zb is not None:
                zmin, zmax = zb
                if math.isfinite(zmin) and math.isfinite(zmax) and zmax >= zmin:
                    span = max(1e-6, abs(zmax - zmin))
                    return zmin - max(sep, span * 0.01)
        except Exception:
            pass
        # No other geometry to sit under: keep the basemap just below the world origin.
        return -sep

    def _data_z_bounds(self, renderer):
        """Return (zmin, zmax) of every visible 3D actor EXCEPT the basemap tiles."""
        zmin = zmax = None
        try:
            actors = renderer.GetActors()
            n = actors.GetNumberOfItems() if actors is not None else 0
            for i in range(n):
                prop = actors.GetItemAsObject(i)
                if (
                    prop is None
                    or prop in self._tile_actors.values()
                    or prop is self._viewport_actor
                ):
                    continue
                bb = prop.GetBounds()
                if not bb or bb[0] > bb[1] or bb[4] > bb[5]:
                    continue
                z0, z1 = float(bb[4]), float(bb[5])
                if not (math.isfinite(z0) and math.isfinite(z1)):
                    continue
                if zmin is None or z0 < zmin:
                    zmin = z0
                if zmax is None or z1 > zmax:
                    zmax = z1
        except Exception:
            return None
        if zmin is None or zmax is None:
            return None
        return (zmin, zmax)

    def _extend_clipping_for_basemap(self):
        """Keep the non-bounding basemap plane (active + placeholder) inside the far clip."""
        if self.app is None or self._current_z_plane is None:
            return
        try:
            cam = self.app.vtk_widget.renderer.GetActiveCamera()
            near, far = map(float, cam.GetClippingRange())
            pz = float(cam.GetPosition()[2])
            # Lowest basemap depth includes placeholder tiles pushed behind by sep.
            lowest = float(self._current_z_plane) - getattr(self, "_placeholder_sep", 0.0)
            distance = abs(pz - lowest)
            if math.isfinite(distance) and distance > 0.0 and distance >= far:
                cam.SetClippingRange(max(1e-9, near), distance * 1.02)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Anti-blink layer: on-disk cache, placeholder tiles, coalesced render
    # ------------------------------------------------------------------
    _COALESCE_MS = 120
    _TILE_CACHE_MAX_AGE = 7 * 24 * 3600
    _RETIRE_MAX_COUNT = 400

    # Overzoom handling (v1.1.7): Esri World Imagery resolution varies by
    # region - past a location's real resolution the tile endpoint returns
    # HTTP 200 with a "Map data not yet available" graphic instead of a 404.
    # Below this zoom we never bother checking (world/region/city views are
    # always covered, and a flat ocean/desert tile there is real imagery, not
    # a placeholder - checking would only add false-positive risk for zero
    # benefit). At/above it, a tile whose sampled pixels are this uniform is
    # treated as "no imagery here" and rejected; _maybe_purge_retired() then
    # naturally keeps the last real (coarser) tile on screen, which the VTK
    # camera stretches to cover the gap - exactly the "keep the good tile
    # instead of a blank one" behaviour a slippy map gives you for free.
    _OVERZOOM_DETECT_MIN_ZOOM = 15
    _OVERZOOM_DOMINANT_FRACTION = 0.92

    def _cache_dir(self):
        if self._tile_cache_dir is not None:
            return self._tile_cache_dir
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or tempfile.gettempdir()
        d = Path(base) / "NakshaAI" / "basemap_cache"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            d = Path(tempfile.gettempdir()) / "naksha_basemap_cache"
            try:
                d.mkdir(parents=True, exist_ok=True)
            except Exception:
                d = None
        self._tile_cache_dir = d
        return d

    def _cache_path(self, provider, z, x, y):
        d = self._cache_dir()
        if d is None:
            return None
        return d / str(provider) / str(int(z)) / str(int(x)) / f"{int(y)}.tile"

    def _read_cache(self, provider, z, x, y):
        path = self._cache_path(provider, z, x, y)
        if path is None or not path.exists():
            return None
        try:
            age = time.time() - path.stat().st_mtime
            if age > self._TILE_CACHE_MAX_AGE:
                return None
            return path.read_bytes()
        except Exception:
            return None

    def _write_cache(self, provider, z, x, y, data):
        path = self._cache_path(provider, z, x, y)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except Exception:
            pass

    def _basemap_z_sep(self):
        """Negative-Z separation pushed onto placeholder (lower-res) tiles.

        This build's VTK actor has no SetRenderOrder / polygon-offset, so ordering
        is done with a real Z offset. We use a fraction of the view's parallel scale
        (not a tiny epsilon) so the gap dwarfs depth-buffer precision and the
        sharper current-zoom tile reliably wins the depth test. The value is stored
        so _extend_clipping_for_basemap can keep placeholders inside the far clip.
        """
        try:
            cam = self.app.vtk_widget.renderer.GetActiveCamera()
            ps = float(cam.GetParallelScale())
            sep = max(1e-6, abs(ps) * 0.1)
        except Exception:
            sep = 1e-3
        self._placeholder_sep = sep
        return sep

    def _tile_looks_unavailable(self, image_bytes, zoom):
        """True if *image_bytes* looks like a provider "no imagery at this
        zoom" placeholder rather than real photographic tile content.

        Heuristic (no reference image needed, works for any provider that
        does this): real satellite/aerial imagery always carries per-pixel
        photographic/compression noise, even over visually flat terrain
        (ocean, desert). A "not yet available" placeholder is a rendered
        graphic - overwhelmingly one flat background colour plus a little
        text - so a very high fraction of sampled pixels land in a single
        colour bucket. Gated to _OVERZOOM_DETECT_MIN_ZOOM+ (see there).
        """
        if zoom < self._OVERZOOM_DETECT_MIN_ZOOM:
            return False
        try:
            image_data = self._decode_via_vtk_reader(bytes(image_bytes or b""))
            if image_data is None:
                return False
            scalars = image_data.GetPointData().GetScalars()
            if scalars is None:
                return False
            dims = image_data.GetDimensions()
            n_comp = scalars.GetNumberOfComponents()
            if dims[0] <= 0 or dims[1] <= 0 or n_comp < 3:
                return False
            arr = numpy_support.vtk_to_numpy(scalars).reshape(dims[1], dims[0], n_comp)[:, :, :3]
            sample = arr[::4, ::4].reshape(-1, 3)
            if len(sample) < 16:
                return False
            # Bucket to absorb JPEG noise before counting the dominant colour.
            buckets = (sample.astype(np.int32) // 8) * 8
            _, counts = np.unique(buckets, axis=0, return_counts=True)
            dominant_fraction = float(counts.max()) / float(len(sample))
            return dominant_fraction > self._OVERZOOM_DOMINANT_FRACTION
        except Exception:
            return False

    def _ingest_tile(self, tile_id, image_bytes, target_crs, z_plane, provider, from_cache):
        """Decode+add one tile, unless it's the provider's own "no imagery at
        this zoom" placeholder - then reject it, remember not to ask again
        (and drop any stale cached copy), and leave whatever coarser tile is
        already on screen there. Returns True if a real tile was added.

        Geometry is an exact, camera-independent function of CRS and tile id.
        """
        z = tile_id[0]
        if provider == "esri_world_imagery" and self._tile_looks_unavailable(image_bytes, z):
            self._dead_tile_ids.add((provider,) + tile_id)
            if from_cache:
                try:
                    p = self._cache_path(provider, *tile_id)
                    if p is not None and p.exists():
                        p.unlink()
                except Exception:
                    pass
            self._maybe_purge_retired()
            return False
        if not from_cache:
            self._write_cache(provider, *tile_id, image_bytes)
        return self._add_tile_actor(
            tile_id, image_bytes, target_crs, z_plane, provider,
            from_cache=from_cache,
        )

    def _add_tile_actor(self, tile_id, image_bytes, target_crs, z_plane, provider, from_cache=False):
        """Decode + add one tile. Returns True if it was added.

        Current-zoom (active) tiles are drawn slightly in front of any kept
        lower-resolution placeholder so they always win the depth test.
        """
        if tile_id in self._tile_actors:
            return True
        actor = self._build_tile_actor(tile_id, image_bytes, target_crs, z_plane)
        if actor is None:
            if from_cache:
                try:
                    p = self._cache_path(provider, *tile_id)
                    if p is not None and p.exists():
                        p.unlink()
                except Exception:
                    pass
            return False
        # Active tiles sit on the basemap plane (depth ordering vs placeholders is
        # handled by the negative Z offset applied to placeholders in _prepare_tile_set).
        actor.SetPosition(0.0, 0.0, 0.0)
        if not self._pipeline_add(self.app, actor):
            self.app.vtk_widget.renderer.AddActor(actor)
        self._apply_tile_clip(actor)
        self._tile_actors[tile_id] = actor
        self._try_remove_parent(tile_id)
        self._maybe_purge_retired()
        self._render_coalesced()
        return True

    def _render_coalesced(self, reset_clip=False):
        self._coalesce_reset_clip = self._coalesce_reset_clip or reset_clip
        if self._coalesce_timer is None:
            self._coalesce_timer = QTimer(self)
            self._coalesce_timer.setSingleShot(True)
            self._coalesce_timer.timeout.connect(self._do_coalesced_render)
        self._coalesce_timer.start(self._COALESCE_MS)

    def _do_coalesced_render(self):
        self._coalesce_timer = None
        reset = self._coalesce_reset_clip
        self._coalesce_reset_clip = False
        self._render(reset_clipping=reset)

    def _try_remove_parent(self, tile_id):
        z, x, y = tile_id
        if z <= 0:
            return
        pz, px, py = z - 1, x // 2, y // 2
        parent = (pz, px, py)
        if parent not in self._tile_actors:
            return
        children = (
            (z, 2 * px, 2 * py),
            (z, 2 * px + 1, 2 * py),
            (z, 2 * px, 2 * py + 1),
            (z, 2 * px + 1, 2 * py + 1),
        )
        if all(c in self._tile_actors for c in children):
            self._remove_actor(self._tile_actors.pop(parent))
            self._try_remove_parent(parent)

    def _nearest_displayed_ancestor(self, tile_id):
        z, x, y = tile_id
        while z > 0:
            z -= 1
            x //= 2
            y //= 2
            cand = (z, x, y)
            if cand in self._tile_actors:
                return cand
        return None

    def _maybe_purge_retired(self):
        missing_active = [t for t in self._active_ids if t not in self._tile_actors]
        needed = set()
        for tid in missing_active:
            anc = self._nearest_displayed_ancestor(tid)
            if anc is not None:
                needed.add(anc)
        for tid in [t for t in self._tile_actors if t not in self._active_ids]:
            if tid not in needed:
                self._remove_actor(self._tile_actors.pop(tid))
        if len(self._tile_actors) > self._RETIRE_MAX_COUNT:
            placeholders = [t for t in self._tile_actors if t not in self._active_ids]
            excess = len(self._tile_actors) - self._RETIRE_MAX_COUNT
            for tid in placeholders[:excess]:
                self._remove_actor(self._tile_actors.pop(tid))

    def _decode_tile_rgb(self, image_bytes):
        from PySide6.QtGui import QImage

        image = QImage()
        if not image.loadFromData(bytes(image_bytes or b"")):
            return None
        image = image.convertToFormat(QImage.Format.Format_RGB888)
        width = int(image.width())
        height = int(image.height())
        stride = int(image.bytesPerLine())
        raw = np.frombuffer(bytes(image.constBits()), dtype=np.uint8)
        return raw.reshape(height, stride)[:, : width * 3].reshape(
            height, width, 3
        ).copy()

    def _ingest_projected_tile(self, tile_id, image_bytes, from_cache):
        view = self._projected_view
        if (
            view is None
            or tile_id not in view.get("wanted", set())
        ):
            return False
        provider = view.get("provider")
        zoom = int(tile_id[0])
        if (
            provider == "esri_world_imagery"
            and self._tile_looks_unavailable(image_bytes, zoom)
        ):
            self._dead_tile_ids.add((provider,) + tile_id)
            return False
        rgb = self._decode_tile_rgb(image_bytes)
        if rgb is None:
            return False
        if not from_cache:
            self._write_cache(provider, *tile_id, image_bytes)
        self._projected_tile_images[tile_id] = rgb
        wanted = view.get("wanted", set())
        if len(self._projected_tile_images) > 160:
            self._projected_tile_images = {
                key: value
                for key, value in self._projected_tile_images.items()
                if key in wanted
            }
        self._schedule_projected_compose()
        return True

    def _schedule_projected_compose(self):
        if self._projected_compose_running:
            self._projected_compose_pending = True
            return
        if self._projected_compose_timer is None:
            self._projected_compose_timer = QTimer(self)
            self._projected_compose_timer.setSingleShot(True)
            self._projected_compose_timer.timeout.connect(
                self._compose_projected_view
            )
        # 90ms (was 55ms in 1.1.25/1.1.26): _compose_projected_view() runs the
        # NumPy/PROJ work synchronously on the Qt GUI thread (see the safety
        # note there), so every extra compose pass during a burst of tiles
        # landing close together is time the UI can't process camera/mouse
        # events. Widening the debounce trades a little first-paint latency
        # for noticeably fewer synchronous stalls during rapid zoom.
        self._projected_compose_timer.start(90)

    def _compose_projected_view(self):
        view = self._projected_view
        if not self._active or not isinstance(view, dict):
            return
        if self._projected_compose_running:
            self._projected_compose_pending = True
            return
        current_crs = self._project_crs()
        if (
            current_crs is None
            or current_crs.to_string() != view.get("crs_signature")
            or self._provider() != view.get("provider")
        ):
            return
        wanted = view.get("wanted", set())
        available = {
            tile_id: self._projected_tile_images[tile_id]
            for tile_id in wanted
            if tile_id in self._projected_tile_images
        }
        if not available:
            return

        self._projected_compose_serial += 1
        token = (view.get("token"), int(self._projected_compose_serial))
        payload = {
            "token": token,
            "crs_wkt": view["crs_wkt"],
            "area_bounds": view.get("area_bounds"),
            "bounds": tuple(view["bounds"]),
            "size": tuple(view["size"]),
            "available": available,
        }
        # pyproj/PROJ in the supported Windows runtime can access-violate when a
        # background transform overlaps CRS work performed by the host during a
        # GIS import. Windows reports the fault in proj_9-*.dll (0xc0000005), so
        # Python exception handling cannot contain it. Keep composition on the
        # Qt thread until the host and plugin can share a process-wide PROJ lock.
        # The v1.1.25 1024 px cap still bounds the cost of this safe path.
        self._projected_compose_running = True
        self._projected_compose_pending = False
        self._projected_compose_task = None
        try:
            result = _ProjectedComposeTask._compose(payload)
        except Exception as exc:
            # Pre-existing bug fixed in passing (was `{token: token, error: str(exc)}`,
            # referencing an undefined name `error` - a NameError waiting to
            # happen). Harmless in practice only because this whole method is
            # unreachable as of v1.1.28; fixed anyway for anyone who re-enables it.
            result = {"token": token, "error": str(exc)}
        self._on_projected_compose_finished(result)

    def _on_projected_compose_finished(self, result):
        self._projected_compose_running = False
        self._projected_compose_task = None
        try:
            if not result or not self._active:
                return
            if result.get("error"):
                print(f"Basemap: projected compose warning: {result['error']}")
                return
            token = result.get("token")
            view_token = token[0] if isinstance(token, tuple) and token else None
            view = self._projected_view
            if not isinstance(view, dict) or view.get("token") != view_token:
                return  # stale background job after a newer pan/zoom
            if self._provider() != view.get("provider"):
                return
            coverage = float(result.get("coverage", 0.0))
            # Keep the last complete preview while exact/parent tiles arrive -
            # but only when there IS a usable last preview to keep. On a cold
            # start (no viewport actor yet, e.g. right after activate()) the
            # "keep the old one" rule has nothing to keep, so it was silently
            # showing nothing at all until coverage crossed 98.5%. Accept a
            # much lower first-paint bar so *something* correct appears fast,
            # then let later compose passes (still gated at 0.985) refine it.
            required_coverage = 0.985 if self._viewport_actor is not None else 0.20
            if coverage < required_coverage:
                return
            image_data = self._rgb_array_to_vtk(result["image"])
            self._replace_viewport_actor(
                image_data, view["bounds"], view["z_plane"]
            )
            self._viewport_camera_width = abs(
                float(view["camera_bounds"][2]) - float(view["camera_bounds"][0])
            )
            self._viewport_signature = (
                view["crs_signature"],
                tuple(round(value, 4) for value in view["bounds"]),
                int(view["zoom"]),
                round(coverage, 6),
            )
        finally:
            if self._projected_compose_pending and self._active:
                self._projected_compose_pending = False
                QTimer.singleShot(20, self._schedule_projected_compose)

    @staticmethod
    def _rgb_array_to_vtk(rgb):
        height, width = rgb.shape[:2]
        channels = int(rgb.shape[2]) if rgb.ndim == 3 else 1
        if channels not in (3, 4):
            raise ValueError("Basemap texture must be RGB or RGBA")
        image = vtk.vtkImageData()
        image.SetDimensions(int(width), int(height), 1)
        image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, channels)
        vtk_array = numpy_support.vtk_to_numpy(
            image.GetPointData().GetScalars()
        ).reshape(height, width, channels)
        vtk_array[:] = np.ascontiguousarray(rgb[::-1])
        image.Modified()
        return image

    def _build_viewport_actor(self, image_bytes, bounds, z_plane):
        image_data = (
            image_bytes
            if isinstance(image_bytes, vtk.vtkImageData)
            else self._image_bytes_to_vtk(image_bytes)
        )
        if image_data is None:
            raise RuntimeError("Esri export image could not be decoded")

        texture = vtk.vtkTexture()
        texture.SetInputData(image_data)
        texture.InterpolateOn()
        texture.RepeatOff()

        xmin, ymin, xmax, ymax = map(float, bounds)
        plane = vtk.vtkPlaneSource()
        plane.SetOrigin(xmin, ymin, float(z_plane))
        plane.SetPoint1(xmax, ymin, float(z_plane))
        plane.SetPoint2(xmin, ymax, float(z_plane))
        plane.SetXResolution(1)
        plane.SetYResolution(1)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(plane.GetOutputPort())

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetTexture(texture)
        actor.GetProperty().LightingOff()
        actor.GetProperty().SetOpacity(self._opacity())
        actor.SetPickable(False)
        try:
            actor.SetUseBounds(False)
        except Exception:
            pass
        actor._naksha_basemap = True
        actor._naksha_basemap_provider = self._provider()
        actor._naksha_basemap_viewport = True
        actor._naksha_basemap_bounds = tuple(bounds)
        return actor

    def _replace_viewport_actor(self, image_bytes, bounds, z_plane):
        actor = self._build_viewport_actor(image_bytes, bounds, z_plane)
        old_actor = self._viewport_actor
        if not self._pipeline_add(self.app, actor):
            self.app.vtk_widget.renderer.AddActor(actor)
        self._viewport_actor = actor
        self._viewport_signature = (
            tuple(round(float(value), 6) for value in bounds),
            self._project_crs().to_string(),
        )
        self._current_z_plane = float(z_plane)
        if old_actor is not None:
            self._remove_actor(old_actor)
        self._render(reset_clipping=True)

    @staticmethod
    def _tile_intersects_crs_area(tile_id, target_crs):
        """Actual finite-transform safety check; area_of_use is not a clip."""
        z, x, y = tile_id
        transform = Transformer.from_crs(CRS.from_epsg(4326), target_crs, always_xy=True)
        points = [transform.transform(tile_x_to_lon(x + fx, z), tile_y_to_lat(y + fy, z))
                  for fy in (0.0, 0.5, 1.0) for fx in (0.0, 0.5, 1.0)]
        if not all(math.isfinite(v) and abs(v) < 1.0e12 for p in points for v in p):
            return False
        xs, ys = zip(*points)
        return max(xs) - min(xs) <= 2.5e7 and max(ys) - min(ys) <= 2.5e7

    def _exact_tile_vertices(self, tile_id, target_crs, subdivisions):
        """Return exact projected regular-grid vertices, cached by stable inputs."""
        z, x, y = tile_id
        subdivisions = int(subdivisions)
        key = (self._canonical_crs_signature(target_crs), z, x, y, subdivisions)
        cached = self._tile_geometry_cache.get(key)
        if cached is not None:
            return cached
        fractions = np.linspace(0.0, 1.0, subdivisions + 1)
        lons = np.tile([tile_x_to_lon(x + f, z) for f in fractions], subdivisions + 1)
        lats = np.repeat([tile_y_to_lat(y + f, z) for f in fractions], subdivisions + 1)
        transform = Transformer.from_crs(CRS.from_epsg(4326), target_crs, always_xy=True)
        xs, ys = transform.transform(lons, lats)
        result = tuple(
            (float(wx), float(wy), float(i) / subdivisions, 1.0 - float(j) / subdivisions)
            for j in range(subdivisions + 1)
            for i, (wx, wy) in enumerate(zip(
                xs[j * (subdivisions + 1):(j + 1) * (subdivisions + 1)],
                ys[j * (subdivisions + 1):(j + 1) * (subdivisions + 1)],
            ))
        )
        self._tile_geometry_cache[key] = result
        return result

    @staticmethod
    def _tile_interpolation_error(tile_id, target_crs, subdivisions):
        """Maximum exact midpoint error against bilinear cell interpolation."""
        z, x, y = tile_id
        transform = Transformer.from_crs(CRS.from_epsg(4326), target_crs, always_xy=True)
        maximum = 0.0
        for j in range(subdivisions):
            for i in range(subdivisions):
                f0, f1 = i / subdivisions, (i + 1) / subdivisions
                g0, g1 = j / subdivisions, (j + 1) / subdivisions
                samples = ((f0, g0), (f1, g0), (f0, g1), (f1, g1),
                           ((f0 + f1) / 2.0, (g0 + g1) / 2.0))
                projected = [transform.transform(tile_x_to_lon(x + fx, z), tile_y_to_lat(y + fy, z)) for fx, fy in samples]
                if not all(math.isfinite(v) for point in projected for v in point):
                    return float("inf")
                predicted = ((projected[0][0] + projected[1][0] + projected[2][0] + projected[3][0]) / 4.0,
                             (projected[0][1] + projected[1][1] + projected[2][1] + projected[3][1]) / 4.0)
                maximum = max(maximum, math.dist(predicted, projected[4]))
        return maximum

    def _choose_common_subdivisions(self, tiles, target_crs, world_per_pixel):
        """One crack-free grid density for all visible tiles at an XYZ zoom."""
        threshold = max(1e-12, float(world_per_pixel)) * 0.35
        allowed = (1, 2, 4, 8, 16, 32)
        configured = max(1, min(32, self._settings.value("mesh_max_subdivisions", 32, type=int)))
        max_subdivisions = max(v for v in allowed if v <= configured)
        for subdivisions in allowed:
            if subdivisions >= max_subdivisions:
                return max_subdivisions
            if all(self._tile_interpolation_error(t, target_crs, subdivisions) <= threshold for t in tiles):
                return subdivisions
        return 32

    def _build_tile_actor(self, tile_id, image_bytes, target_crs, z_plane, subdivisions=None):
        z, x, y = tile_id
        image_data = self._image_bytes_to_vtk(image_bytes)
        if image_data is None:
            raise RuntimeError("Basemap tile image could not be decoded")

        texture = vtk.vtkTexture()
        texture.SetInputData(image_data)
        texture.InterpolateOn()
        texture.RepeatOff()

        subdivisions = int(subdivisions or self._refresh_mesh_subdivisions or 1)
        raw_pts = self._exact_tile_vertices(tile_id, target_crs, subdivisions)

        # Check for non-finite numbers or extreme projective distortion (amoeba rejection)
        xs = [p[0] for p in raw_pts if math.isfinite(p[0])]
        ys = [p[1] for p in raw_pts if math.isfinite(p[1])]
        if len(xs) != len(raw_pts) or len(ys) != len(raw_pts):
            return None
        span_x = max(xs) - min(xs)
        span_y = max(ys) - min(ys)
        # In any realistic projection, a single slippy-map tile should never exceed 2.5e7 meters.
        # Singularity tiles in conic/transverse projections produce spans of 100M-200M meters.
        if span_x > 2.5e7 or span_y > 2.5e7:
            return None

        points = vtk.vtkPoints()
        tcoords = vtk.vtkFloatArray()
        tcoords.SetNumberOfComponents(2)
        tcoords.SetName("TextureCoordinates")
        for wx, wy, fx, tc_y in raw_pts:
            points.InsertNextPoint(float(wx), float(wy), float(z_plane))
            tcoords.InsertNextTuple2(fx, tc_y)

        polys = vtk.vtkCellArray()
        stride = subdivisions + 1
        for j in range(subdivisions):
            for i in range(subdivisions):
                p0 = j * stride + i
                p1 = p0 + 1
                p2 = p1 + stride
                p3 = p0 + stride
                quad = vtk.vtkQuad()
                quad.GetPointIds().SetId(0, p0)
                quad.GetPointIds().SetId(1, p1)
                quad.GetPointIds().SetId(2, p2)
                quad.GetPointIds().SetId(3, p3)
                polys.InsertNextCell(quad)

        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(polys)
        poly.GetPointData().SetTCoords(tcoords)

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)

        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.SetTexture(texture)
        actor.GetProperty().LightingOff()
        actor.GetProperty().SetOpacity(self._opacity())
        actor.SetPickable(False)
        try:
            actor.SetUseBounds(False)
        except Exception:
            pass
        actor._naksha_basemap = True
        actor._naksha_basemap_provider = self._provider()
        actor._naksha_basemap_tile_id = tile_id
        actor._naksha_canvas_crs_signature = self._canonical_crs_signature(target_crs)
        actor._naksha_basemap_subdivision = subdivisions
        actor._naksha_geometry_signature = (
            actor._naksha_canvas_crs_signature, z, x, y, subdivisions
        )
        return actor

    def _image_bytes_to_vtk(self, image_bytes):
        """Decode JPEG/PNG in memory to a vtkImageData.

        Several decode strategies are attempted so the plugin keeps working
        across VTK builds:
          * vtkPNGReader / vtkJPEGReader with SetMemoryBuffer (VTK >= 9)
          * the older vtkMemoryResourceStream + SetStream API
          * QImage.loadFromData as a last-resort fallback.

        A silent decode failure here is exactly what previously left the
        basemap canvas black, so every path is guarded and reported.
        """
        data = bytes(image_bytes or b"")
        if not data:
            return None

        image = self._decode_via_vtk_reader(data)
        if image is not None:
            return image

        image = self._decode_via_qimage(data)
        if image is not None:
            return image

        print("Basemap: VTK image decode failed for tile payload")
        return None

    def _decode_via_vtk_reader(self, data):
        is_png = data.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg = data.startswith(b"\xff\xd8")
        reader = (
            vtk.vtkPNGReader() if is_png
            else vtk.vtkJPEGReader() if is_jpeg
            else vtk.vtkJPEGReader()  # Esri World Imagery is normally JPEG
        )
        try:
            reader.SetMemoryBuffer(data)
            reader.SetMemoryBufferLength(len(data))
            reader.Update()
        except Exception:
            # Older VTK builds exposed a memory stream instead of a raw buffer.
            try:
                stream = vtk.vtkMemoryResourceStream()
                stream.SetBuffer(data, len(data), True)
                reader.SetStream(stream)
                reader.Update()
            except Exception:
                return None
        output = reader.GetOutput()
        dims = output.GetDimensions() if output is not None else (0, 0, 0)
        if not output or int(dims[0]) <= 0 or int(dims[1]) <= 0:
            return None
        image = vtk.vtkImageData()
        image.DeepCopy(output)
        return image

    def _decode_via_qimage(self, data):
        try:
            from PySide6.QtGui import QImage
        except Exception:
            return None
        qimg = QImage()
        if not qimg.loadFromData(data):
            return None
        if qimg.format() not in (
            QImage.Format.Format_RGB32,
            QImage.Format.Format_ARGB32,
            QImage.Format.Format_RGB16,
        ):
            qimg = qimg.convertToFormat(QImage.Format.Format_RGB32)
        width = qimg.width()
        height = qimg.height()
        bits = qimg.constBits()
        try:
            import numpy as np
            arr = np.frombuffer(bytes(bits), dtype=np.uint8).reshape(height, width, 4)
            rgb = arr[:, :, :3].copy()
            rgb = rgb[::-1, :, ::-1].copy()  # flip Y (VTK bottom-up) and BGR->RGB
            flat = np.ascontiguousarray(rgb)
        except Exception:
            return None
        image = vtk.vtkImageData()
        image.SetDimensions(width, height, 1)
        image.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 3)
        out = np.frombuffer(bytes(image.GetScalarPointer()), dtype=np.uint8)
        out[:] = flat.reshape(-1)
        return image

    def _opacity(self):
        value = self._settings.value("opacity", 100, type=int)
        return max(0.1, min(1.0, float(value) / 100.0))

    def _apply_opacity(self):
        opacity = self._opacity()
        actors = list(self._tile_actors.values())
        if self._viewport_actor is not None:
            actors.append(self._viewport_actor)
        for actor in actors:
            try:
                actor.GetProperty().SetOpacity(opacity)
            except Exception:
                pass
        self._render()

    def _ensure_attribution_actor(self):
        if self._attribution_actor is not None or self.app is None:
            return
        ann = vtk.vtkCornerAnnotation()
        ann.SetText(1, self._provider_label())
        ann.SetMaximumFontSize(14)
        ann.SetMinimumFontSize(10)
        prop = ann.GetTextProperty()
        prop.SetColor(1.0, 1.0, 1.0)
        try:
            prop.SetBackgroundColor(0.0, 0.0, 0.0)
            prop.SetBackgroundOpacity(0.55)
        except Exception:
            pass
        ann.SetPickable(False)
        self.app.vtk_widget.renderer.AddActor2D(ann)
        self._attribution_actor = ann

    def _set_attribution_text(self, text):
        self._ensure_attribution_actor()
        if self._attribution_actor is None:
            return
        self._attribution_actor.SetText(1, str(text or self._provider_label()))

    def _remove_attribution_actor(self):
        if self._attribution_actor is not None:
            try:
                self.app.vtk_widget.renderer.RemoveActor2D(self._attribution_actor)
            except Exception:
                pass
            self._attribution_actor = None

    def _clear_tile_actors_only(self, render=True):
        for actor in list(self._tile_actors.values()):
            self._remove_actor(actor)
        self._tile_actors.clear()
        self._active_ids = set()
        self._tile_active_sig = None
        if render:
            self._render(reset_clipping=True)

    def _clear_viewport_actor(self, render=True):
        if self._viewport_actor is not None:
            self._remove_actor(self._viewport_actor)
        self._viewport_actor = None
        self._viewport_signature = None
        self._viewport_camera_width = None
        if render:
            self._render(reset_clipping=True)

    def _clear_tiles(self, render=True):
        self._clear_tile_actors_only(render=False)
        self._clear_viewport_actor(render=False)
        self._projected_view = None
        self._projected_tile_images.clear()
        self._projected_compose_pending = False
        self._tile_retry_counts.clear()
        if self._projected_compose_timer is not None:
            self._projected_compose_timer.stop()
        self._current_z_plane = None
        if render:
            self._render(reset_clipping=True)

    # --- scene routing -------------------------------------------------
    # Newer app builds expose gui.scene_render_pipeline, which keeps raster
    # (basemap) actors in their own renderer/layer so they cannot reorder
    # themselves above LiDAR/vector data.  Older builds have no such module,
    # so fall back to the plain renderer rather than dropping tiles on add
    # or leaking actors on remove.

    @staticmethod
    def _pipeline_add(app, actor):
        try:
            from gui.scene_render_pipeline import add_raster_actor
        except Exception:
            return False
        try:
            add_raster_actor(app, actor)
            return True
        except Exception:
            return False

    @staticmethod
    def _pipeline_remove(app, actor):
        try:
            from gui.scene_render_pipeline import remove_actor_from_pipeline
        except Exception:
            return False
        try:
            remove_actor_from_pipeline(app, actor)
            return True
        except Exception:
            return False

    def _remove_actor(self, actor):
        if self.app is None or actor is None:
            return
        try:
            if not self._pipeline_remove(self.app, actor):
                self.app.vtk_widget.renderer.RemoveActor(actor)
        except Exception:
            pass

    def _render(self, reset_clipping=False):
        if self.app is None:
            return
        try:
            if reset_clipping:
                self.app.vtk_widget.renderer.ResetCameraClippingRange()
            self._extend_clipping_for_basemap()
            manager = getattr(self.app, "gpu_render_manager", None)
            if manager is not None and callable(getattr(manager, "request_render", None)):
                manager.request_render(self.app.vtk_widget)
            else:
                self.app.vtk_widget.render()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Reply/session cleanup and errors
    # ------------------------------------------------------------------
    def _invalidate_session(self, clear_tiles=False):
        self._session_token = None
        self._session_expiry = 0
        self._session_signature = None
        if clear_tiles:
            self._clear_tiles(render=True)

    def _abort_non_session_replies(self):
        for reply, meta in list(self._reply_meta.items()):
            if meta and meta[0] in {"session", "esri_meta"}:
                continue
            try:
                reply.abort()
                reply.deleteLater()
            except Exception:
                pass
            self._reply_meta.pop(reply, None)

    def _abort_unwanted_tile_replies(self):
        """Cancel only the in-flight tiles that are no longer wanted.

        Staleness is decided by *"is this tile still in the current view"*
        (membership in ``_active_ids``), NOT by *"which refresh cycle asked for
        it"*. The old rule - abort everything, and drop any reply whose
        generation tag is not the newest - meant a single camera tick landing
        mid-download invalidated a whole batch of perfectly good tiles. On a
        wide view (up to 36 tiles) that happens almost every time, so loading
        restarted from zero forever; zoomed in with only a handful of tiles it
        usually finished inside one debounce window and looked fine.
        """
        active = self._active_ids or set()
        for reply, meta in list(self._reply_meta.items()):
            if not meta:
                continue
            kind = meta[0]
            if kind in {"session", "esri_meta"}:
                continue
            context = meta[2] if len(meta) > 2 else None
            tile_id = context.get("tile_id") if isinstance(context, dict) else None
            if tile_id is not None and tile_id in active:
                # Still on screen - let it finish instead of restarting it.
                continue
            try:
                reply.abort()
                reply.deleteLater()
            except Exception:
                pass
            self._reply_meta.pop(reply, None)

    def _abort_all_replies(self):
        for reply in list(self._reply_meta):
            try:
                reply.abort()
                reply.deleteLater()
            except Exception:
                pass
        self._reply_meta.clear()
        self._session_reply = None
        self._esri_metadata_reply = None

    @staticmethod
    def _http_status(reply):
        try:
            try:
                attr = QNetworkRequest.Attribute.HttpStatusCodeAttribute
            except AttributeError:
                attr = QNetworkRequest.HttpStatusCodeAttribute
            value = reply.attribute(attr)
            return int(value) if value is not None else 0
        except Exception:
            return 0

    @staticmethod
    def _network_error_message(reply, body, status):
        text = ""
        try:
            if body:
                parsed = json.loads(body.decode("utf-8", errors="replace"))
                error = parsed.get("error") if isinstance(parsed, dict) else None
                if isinstance(error, dict):
                    text = str(error.get("message", ""))
                elif error:
                    text = str(error)
        except Exception:
            pass
        if not text:
            try:
                text = reply.errorString()
            except Exception:
                text = "Network request failed"
        return f"HTTP {status}: {text}" if status else text

    def _show_error_once(self, text):
        text = str(text)
        if text == self._last_error_text:
            self._status(text, 3000)
            return
        self._last_error_text = text
        print(f"Basemap: {text}")
        self._status(text, 5000)
        self._log_error(text)

    def _log_error(self, text):
        """Persist errors to a log file so frozen-build failures are debuggable."""
        try:
            log_path = Path(__file__).resolve().parent / "basemap_error.log"
            with open(log_path, "a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except Exception:
            pass

    def _status(self, text, timeout=2000):
        try:
            self.app.statusBar().showMessage(str(text), int(timeout))
        except Exception:
            pass
