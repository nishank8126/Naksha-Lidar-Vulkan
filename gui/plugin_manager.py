import importlib.util
import json
import sys
import os
import shutil
import re
import tempfile
import zipfile
from pathlib import Path
from typing import Tuple, Optional

from PySide6.QtCore import QTimer


def _get_plugins_dir() -> Path:
    """
    Return a user-writable plugins directory.
    
    In frozen builds (PyInstaller), we use %LOCALAPPDATA%/NakshaAI-LiDAR/plugins
    to avoid permission issues with Program Files.
    
    In development, we use the local 'plugins' folder.
    """
    if getattr(sys, 'frozen', False):
        # Frozen build - use user's local appdata (always writable)
        local_appdata = os.getenv('LOCALAPPDATA')
        if local_appdata:
            plugins_dir = Path(local_appdata) / "NakshaAI-LiDAR" / "plugins"
        else:
            # Fallback to user home if LOCALAPPDATA not set
            plugins_dir = Path.home() / ".naksha" / "plugins"
    else:
        # Development mode - use local plugins folder
        plugins_dir = Path("plugins")
    
    return plugins_dir


# Initialize with proper user-writable path
PLUGINS_DIR = _get_plugins_dir()
INSTALL_CACHE_DIRNAME = ".install-cache"


class PluginManager:
    def __init__(self, app_window):
        self.app_window = app_window
        self.loaded_plugins = {}  # name -> {"instance": instance, "manifest": manifest, "dir": plugin_dir}
        
        # Ensure plugins directory exists with proper error handling
        self._ensure_plugins_dir()

    def _ensure_plugins_dir(self):
        """Create plugins directory with fallback handling for permission errors."""
        global PLUGINS_DIR
        
        try:
            PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            print(f"✅ Plugins directory ready: {PLUGINS_DIR}")
        except PermissionError as e:
            print(f"⚠️ Permission denied creating plugins directory: {PLUGINS_DIR}")
            print(f"   Error: {e}")
            
            # Fall back to temp directory
            fallback = Path(tempfile.gettempdir()) / "NakshaAI-LiDAR" / "plugins"
            try:
                fallback.mkdir(parents=True, exist_ok=True)
                PLUGINS_DIR = fallback
                print(f"✅ Using fallback plugins directory: {PLUGINS_DIR}")
            except Exception as e2:
                print(f"❌ Cannot create fallback plugins directory: {e2}")
                # Last resort: use a directory in user's home
                home_fallback = Path.home() / ".naksha_plugins"
                try:
                    home_fallback.mkdir(parents=True, exist_ok=True)
                    PLUGINS_DIR = home_fallback
                    print(f"✅ Using home fallback plugins directory: {PLUGINS_DIR}")
                except Exception as e3:
                    print(f"❌ All plugin directory creation attempts failed: {e3}")
        except Exception as e:
            print(f"⚠️ Unexpected error creating plugins directory: {e}")

    def discover_plugins(self):
        found = []
        if not PLUGINS_DIR.exists():
            return found
        for plugin_dir in PLUGINS_DIR.iterdir():
            manifest_path = plugin_dir / "manifest.json"
            if plugin_dir.is_dir() and manifest_path.exists():
                try:
                    with open(manifest_path, "r", encoding="utf-8") as f:
                        found.append((json.load(f), plugin_dir))
                except Exception as e:
                    print(f"Warning: Failed to parse manifest in {plugin_dir}: {e}")
        return found

    @staticmethod
    def _plugin_slug(name: str) -> str:
        return re.sub(r'[^a-zA-Z0-9_-]', '_', name).lower()

    def _cached_archive_path(self, name: str) -> Path:
        return PLUGINS_DIR / INSTALL_CACHE_DIRNAME / f"{self._plugin_slug(name)}.zip"

    @staticmethod
    def _remove_bytecode(plugin_dir: Path) -> None:
        for cache_dir in plugin_dir.rglob("__pycache__"):
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir, ignore_errors=True)

    @staticmethod
    def _validate_owned_plugin_dir(plugin_dir: Path) -> Path:
        root = PLUGINS_DIR.resolve()
        target = plugin_dir.resolve()
        if target == root or target.parent != root:
            raise ValueError(f"Refusing to delete plugin path outside '{root}': {target}")
        return target

    def load_plugin(self, manifest, plugin_dir):
        name = manifest["name"]
        if name in self.loaded_plugins:
            return False, f"'{name}' already loaded."
        
        entry = plugin_dir / manifest["entry"]
        if not entry.exists():
            return False, f"Entry file not found: {entry}"
            
        try:
            # Add plugin directory to sys.path to allow relative / local imports inside plugins
            dir_str = str(plugin_dir.resolve())
            if dir_str not in sys.path:
                sys.path.insert(0, dir_str)

            spec = importlib.util.spec_from_file_location(name, entry)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            
            cls = getattr(module, manifest["class"])
            instance = cls()
            instance.on_load(self.app_window)
            
            self.loaded_plugins[name] = {
                "instance": instance,
                "manifest": manifest,
                "dir": plugin_dir
            }

            # Refresh plugins ribbon UI dynamically if available
            self._rebuild_plugins_ribbon()
            
            return True, f"'{name}' loaded."
        except Exception as e:
            # Clean up sys.path and sys.modules on failure
            dir_str = str(plugin_dir.resolve())
            if dir_str in sys.path:
                sys.path.remove(dir_str)
            sys.modules.pop(name, None)
            return False, f"Failed: {e}"

    def unload_plugin(self, name, *, rebuild_ribbon=True):
        if name not in self.loaded_plugins:
            return False, f"'{name}' not loaded."

        info = self.loaded_plugins[name]
        hook_error = None
        try:
            info["instance"].on_unload()
        except Exception as exc:
            # A faulty plugin hook must not make the plugin impossible to
            # uninstall or repair. Continue with manager-owned cleanup.
            hook_error = exc

        try:
            plugin_dir = Path(info["dir"])
            dir_str = str(plugin_dir.resolve())
            while dir_str in sys.path:
                sys.path.remove(dir_str)

            # Remove the entry module and every private submodule imported
            # from this plugin directory. Otherwise a same-session reinstall
            # can reuse modules whose files belonged to the deleted version.
            plugin_root = plugin_dir.resolve()
            for module_name, module in list(sys.modules.items()):
                module_file = getattr(module, "__file__", None)
                if not module_file:
                    continue
                try:
                    module_path = Path(module_file).resolve()
                    if module_path == plugin_root or plugin_root in module_path.parents:
                        sys.modules.pop(module_name, None)
                except (OSError, RuntimeError, TypeError, ValueError):
                    continue

            self.loaded_plugins.pop(name, None)
            sys.modules.pop(name, None)
            importlib.invalidate_caches()

            if rebuild_ribbon:
                self._rebuild_plugins_ribbon()

            if hook_error is not None:
                print(f"Warning: Plugin '{name}' on_unload failed; forced cleanup completed: {hook_error}")
                return True, f"'{name}' unloaded with a plugin cleanup warning: {hook_error}"
            return True, f"'{name}' unloaded."
        except Exception as exc:
            return False, f"Unload cleanup failed: {exc}"

    def uninstall_plugin(self, name):
        """Unload and permanently delete a plugin from the disk."""
        plugin_dir = None
        if name in self.loaded_plugins:
            plugin_dir = self.loaded_plugins[name]["dir"]
            ok, msg = self.unload_plugin(name, rebuild_ribbon=False)
            if not ok:
                return False, msg
        else:
            # Search discovered plugins to locate the directory
            for manifest, pdir in self.discover_plugins():
                if manifest.get("name") == name:
                    plugin_dir = pdir
                    break
        
        if not plugin_dir:
            return False, f"Plugin '{name}' not found."

        delete_errors = []
        try:
            plugin_dir = self._validate_owned_plugin_dir(Path(plugin_dir))
            if plugin_dir.exists():
                shutil.rmtree(plugin_dir)
        except Exception as exc:
            delete_errors.append(f"plugin directory: {exc}")

        cached_archive = self._cached_archive_path(name)
        try:
            if cached_archive.exists():
                cached_archive.unlink()
            try:
                cached_archive.parent.rmdir()
            except OSError:
                pass
        except Exception as exc:
            delete_errors.append(f"installer cache: {exc}")

        self._rebuild_plugins_ribbon()
        if delete_errors:
            return False, "Failed to delete " + "; ".join(delete_errors)
        return True, f"Plugin '{name}' successfully uninstalled."

    def load_all(self):
        for manifest, plugin_dir in self.discover_plugins():
            ok, msg = self.load_plugin(manifest, plugin_dir)
            status_str = "SUCCESS" if ok else "ERROR"
            print(f"[{status_str}] Plugin: {msg}")

    def install_plugin_from_zip(self, zip_path: Path) -> Tuple[bool, str]:
        """
        Installs a plugin from a ZIP archive.
        Supports QGIS-style installations: scans for manifest.json, unloads any existing
        version of the same plugin, cleans up old directories, and unzips & loads it.
        """
        try:
            if not zip_path.exists():
                return False, f"ZIP file does not exist: {zip_path}"
                
            if not zipfile.is_zipfile(zip_path):
                return False, "Selected file is not a valid ZIP archive."

            with zipfile.ZipFile(zip_path, 'r') as zf:
                # 1. Locate manifest.json anywhere in the ZIP file (root or nested subfolder)
                manifest_in_zip = None
                for name in zf.namelist():
                    if name.endswith("manifest.json"):
                        manifest_in_zip = name
                        break
                        
                if not manifest_in_zip:
                    return False, "Invalid plugin zip structure: manifest.json not found."

                # 2. Read and parse manifest.json
                try:
                    manifest_content = zf.read(manifest_in_zip).decode("utf-8-sig")
                    manifest = json.loads(manifest_content)
                except Exception as e:
                    return False, f"Failed to parse manifest.json inside zip: {e}"

                # 3. Validate required fields
                required = ["name", "entry", "class"]
                missing = [f for f in required if f not in manifest]
                if missing:
                    return False, f"Invalid manifest: missing required fields {missing}"

                plugin_name = manifest["name"]

                # 4. Check if already loaded, unload if necessary
                if plugin_name in self.loaded_plugins:
                    ok, msg = self.unload_plugin(
                        plugin_name, rebuild_ribbon=False
                    )
                    if not ok:
                        return False, msg

                # Determine target directory under plugins/ (slugify name to make it safe)
                slug = self._plugin_slug(plugin_name)
                dest_dir = PLUGINS_DIR / slug

                # If destination directory already exists, attempt to remove it
                if dest_dir.exists():
                    try:
                        shutil.rmtree(dest_dir)
                    except Exception as e:
                        return False, f"Could not remove old directory '{dest_dir.name}': {e}"

                # 5. Extract to temporary folder first
                with tempfile.TemporaryDirectory() as tmpdir:
                    tmp_path = Path(tmpdir)
                    zf.extractall(tmp_path)

                    # Find the folder containing the manifest.json
                    manifest_abs = tmp_path / manifest_in_zip
                    plugin_root_src = manifest_abs.parent

                    # Copy contents to final destination
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(plugin_root_src, dest_dir, dirs_exist_ok=True)

                # 6. Load the installed plugin immediately
                ok, msg = self.load_plugin(manifest, dest_dir)
                if not ok:
                    return False, f"Installed successfully but failed to load: {msg}"

                # Retain the original package outside the live plugin directory.
                # Reinstall can then perform the same clean extraction again.
                cache_path = self._cached_archive_path(plugin_name)
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                if zip_path.resolve() != cache_path.resolve():
                    temp_cache = cache_path.with_suffix(".zip.tmp")
                    shutil.copy2(zip_path, temp_cache)
                    temp_cache.replace(cache_path)

                return True, f"Plugin '{plugin_name}' successfully installed and loaded."

        except Exception as e:
            return False, f"Plugin installation failed: {e}"

    def reinstall_plugin(self, name):
        """Re-extract a ZIP install, or cleanly reload a bundled plugin."""
        cached_archive = self._cached_archive_path(name)
        if cached_archive.exists():
            return self.install_plugin_from_zip(cached_archive)

        plugin_info = self.loaded_plugins.get(name)
        manifest = plugin_info.get("manifest") if plugin_info else None
        plugin_dir = Path(plugin_info["dir"]) if plugin_info else None
        if manifest is None or plugin_dir is None:
            for discovered_manifest, discovered_dir in self.discover_plugins():
                if discovered_manifest.get("name") == name:
                    manifest = discovered_manifest
                    plugin_dir = discovered_dir
                    break
        if manifest is None or plugin_dir is None:
            return False, f"Plugin '{name}' not found."

        if name in self.loaded_plugins:
            ok, msg = self.unload_plugin(name, rebuild_ribbon=False)
            if not ok:
                return False, msg
        self._remove_bytecode(plugin_dir)
        importlib.invalidate_caches()
        ok, msg = self.load_plugin(manifest, plugin_dir)
        if not ok:
            self._rebuild_plugins_ribbon()
            return False, f"Reinstall failed: {msg}"
        return True, f"Plugin '{name}' successfully reinstalled and loaded."

    def _rebuild_plugins_ribbon(self):
        """Rebuild once, then resize after Qt has laid out the new sections."""
        try:
            if hasattr(self.app_window, "ribbon_manager"):
                manager = self.app_window.ribbon_manager
                ribbon = manager.ribbons.get("plugins")
                if ribbon and hasattr(ribbon, "rebuild_ribbon"):
                    plugins_was_open = manager.current_ribbon == "plugins"
                    ribbon.rebuild_ribbon()
                    if plugins_was_open:
                        ribbon.show()

                    def finish_layout():
                        try:
                            layout = ribbon.layout()
                            if layout is not None:
                                layout.invalidate()
                                layout.activate()
                            ribbon.updateGeometry()
                            if manager.current_ribbon == "plugins":
                                ribbon.show()
                            update_height = getattr(
                                self.app_window,
                                "_update_ribbon_container_height",
                                None,
                            )
                            if callable(update_height):
                                update_height()
                        except RuntimeError:
                            pass

                    QTimer.singleShot(0, finish_layout)
        except Exception as e:
            print(f"Warning: Failed to rebuild plugins ribbon: {e}")
    
    def get_plugins_directory(self) -> Path:
        """Return the current plugins directory path (useful for UI display)."""
        return PLUGINS_DIR
