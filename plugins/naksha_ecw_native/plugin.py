"""Optional ECW reader plugin for NakshaAI; installed separately from a ZIP."""
from __future__ import annotations
import importlib.util
import os
from pathlib import Path
from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QFileDialog, QMessageBox
from gui.naksha_plugin_api import NakshaPlugin

PLUGIN_VERSION = "1.1.0"
_ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(__name__ + "_runtime", _ROOT / "ecw_runtime.py")
_runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_runtime)


class EcwNativePlugin(NakshaPlugin):
    def on_load(self, app_window):
        super().on_load(app_window)
        if getattr(self, "_enabled", False):
            return
        import rasterio
        from gui.gis import gis_layers
        self._client = None
        self._enabled = True
        self._importing = False
        original_open = rasterio.open
        original_import = gis_layers._import_one
        self._original_open = original_open
        self._original_import = original_import
        self._original_picker = gis_layers.unified_import_overlay
        self._original_raster_extensions = gis_layers._RASTER_EXTS
        self._original_extensions = getattr(app_window, "_GIS_DROP_EXTS", ())

        def open_raster(path, mode="r", *args, **kwargs):
            try:
                is_ecw = Path(os.fspath(path)).suffix.lower() == ".ecw"
            except TypeError:
                is_ecw = False
            if self._enabled and is_ecw and mode == "r":
                if args or kwargs:
                    raise ValueError("ECW native reads do not accept rasterio open options")
                return _runtime.NativeDataset(self._get_client(), path)
            return original_open(path, mode, *args, **kwargs)

        def import_one(app, path):
            if self._enabled and Path(path).suffix.lower() == ".ecw":
                return self.import_ecw(app, path)
            return original_import(app, path)

        self._open_wrapper = open_raster
        self._import_wrapper = import_one
        rasterio.open = open_raster
        gis_layers._import_one = import_one
        self._picker_wrapper = lambda app: self.choose_files(app)
        gis_layers.unified_import_overlay = self._picker_wrapper
        gis_layers._RASTER_EXTS = tuple(dict.fromkeys((*gis_layers._RASTER_EXTS, ".ecw")))
        app_window._GIS_DROP_EXTS = tuple(dict.fromkeys((*self._original_extensions, ".ecw")))
        qt = QCoreApplication.instance()
        if qt is not None:
            qt.aboutToQuit.connect(self._close_client)
        self._qt = qt
        for entry in gis_layers._registry(app_window):
            if entry.get("reader_plugin") == "Naksha ECW Native":
                for actor in entry.get("actors", []):
                    meta = getattr(actor, "_raster_lod_meta", {})
                    if "_ecw_lod_eligible" in meta:
                        meta["eligible"] = meta.pop("_ecw_lod_eligible")
        if getattr(app_window, "geotiff_actors", None):
            from gui.gis.raster_lod import kick
            kick(app_window)
        print("Naksha ECW Native enabled: direct window reads, isolated bundled runtime")

    def _get_client(self):
        if not self._enabled:
            raise RuntimeError("ECW plugin is disabled")
        if self._client is None:
            runtime = _runtime.prepare_runtime(_ROOT)
            self._client = _runtime.NativeClient(_ROOT, runtime)
        return self._client

    def _close_client(self):
        if self._client is not None:
            self._client.close()
            self._client = None

    def import_ecw(self, app, path):
        if self._importing:
            return False
        self._importing = True
        try:
            from gui.gis.gis_layers import _import_raster_layer, _registry
            # Keep the original .ecw path: all previews, restyles and zoom reads
            # return to this file, never a converted whole-file TIFF cache.
            corners = None
            with _runtime.NativeDataset(self._get_client(), path) as dataset:
                transform = dataset.transform
                if transform.b or transform.d:
                    corners = [transform * p for p in ((0, dataset.height),
                               (dataset.width, dataset.height), (0, 0))]
                    target = getattr(app, "project_crs_epsg", None)
                    if target and dataset.crs:
                        from rasterio.warp import transform as project
                        xs, ys = project(dataset.crs, f"EPSG:{target}",
                                         [p[0] for p in corners], [p[1] for p in corners])
                        corners = list(zip(xs, ys))
            ok = _import_raster_layer(app, str(path), Path(path).name, gcp_corners=corners)
            if ok:
                entry = next((e for e in reversed(_registry(app))
                              if e.get("path") == str(path) and e.get("kind") == "raster"), None)
                if entry is not None:
                    entry["source_path"] = str(path)
                    entry["fmt"] = "ecw"
                    entry["reader_plugin"] = "Naksha ECW Native"
            else:
                QMessageBox.warning(app, "ECW Import", "The ECW could not be loaded. See the console for the reader error.")
            return ok
        except Exception as exc:
            QMessageBox.warning(app, "ECW Import", str(exc))
            return False
        finally:
            self._importing = False

    def choose_files(self, app):
        from gui.gis import gis_layers
        filters = ("All supported (*.shp *.tif *.tiff *.ecw *.geojson *.json *.gdb);;"
                   "Shapefile (*.shp);;GeoTIFF (*.tif *.tiff);;ECW imagery (*.ecw);;"
                   "GeoJSON (*.geojson *.json);;ESRI File Geodatabase (*.gdb)")
        files, _ = QFileDialog.getOpenFileNames(app, "Import GIS overlay (multiple allowed)", "", filters)
        if not files:
            return
        failures = []
        gis_layers.suspend_layer_panel_refresh(app)
        try:
            for path in gis_layers.sort_imports_vectors_first(files):
                try:
                    if not gis_layers._import_one(app, path):
                        failures.append(Path(path).name)
                except Exception as exc:
                    failures.append(f"{Path(path).name}: {exc}")
        finally:
            gis_layers.resume_layer_panel_refresh(app)
        gis_layers.show_gis_layers_panel(app)
        if failures:
            QMessageBox.warning(app, "Import GIS overlay", "Could not import:\n" + "\n".join(failures))

    def on_unload(self):
        if not getattr(self, "_enabled", False):
            return
        self._enabled = False
        import rasterio
        from gui.gis import gis_layers
        if rasterio.open is self._open_wrapper:
            rasterio.open = self._original_open
        if gis_layers._import_one is self._import_wrapper:
            gis_layers._import_one = self._original_import
        if gis_layers.unified_import_overlay is self._picker_wrapper:
            gis_layers.unified_import_overlay = self._original_picker
        if ".ecw" not in self._original_raster_extensions:
            gis_layers._RASTER_EXTS = tuple(e for e in gis_layers._RASTER_EXTS if e != ".ecw")
        current = getattr(self.app_window, "_GIS_DROP_EXTS", ())
        if ".ecw" not in self._original_extensions:
            self.app_window._GIS_DROP_EXTS = tuple(e for e in current if e != ".ecw")
        # Disable future detail reads but keep already loaded imagery visible.
        for entry in gis_layers._registry(self.app_window):
            if entry.get("reader_plugin") == "Naksha ECW Native":
                for actor in entry.get("actors", []):
                    meta = getattr(actor, "_raster_lod_meta", None)
                    if meta is not None:
                        from gui.gis.raster_lod import _restore_overview
                        _restore_overview(actor)
                        meta["_ecw_lod_eligible"] = meta.get("eligible", False)
                        meta["eligible"] = False
        if self._qt is not None:
            try:
                self._qt.aboutToQuit.disconnect(self._close_client)
            except (RuntimeError, TypeError):
                pass
        self._close_client()
