from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QWidget

import gui.plugin_manager as plugin_manager_module
from gui.menu_sidebar_system import PluginsRibbon


class _RibbonPlugin:
    def get_ribbon_button(self):
        return {
            "label": "Test",
            "emoji": "T",
            "callback": lambda: None,
            "toggleable": False,
        }


class PluginRibbonRefreshTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_active_plugins_ribbon_keeps_nonzero_height(self):
        window = QMainWindow()
        host = QWidget(window)
        host_layout = QHBoxLayout(host)
        window.setCentralWidget(host)
        window.plugin_manager = SimpleNamespace(
            loaded_plugins={
                "Test": {"instance": _RibbonPlugin()},
            }
        )
        ribbon = PluginsRibbon(host, app=window)
        host_layout.addWidget(ribbon)
        window.ribbon_manager = SimpleNamespace(
            current_ribbon="plugins",
            ribbons={"plugins": ribbon},
        )
        height_updates = []

        def update_height():
            height = max(
                ribbon.sizeHint().height(),
                ribbon.minimumSizeHint().height(),
                ribbon.minimumHeight(),
            )
            host.setFixedHeight(height)
            height_updates.append(height)

        window._update_ribbon_container_height = update_height
        window.show()
        ribbon.show()
        self.qt_app.processEvents()

        manager = plugin_manager_module.PluginManager(window)
        window.plugin_manager = manager
        manager.loaded_plugins["Test"] = {"instance": _RibbonPlugin()}
        manager._rebuild_plugins_ribbon()

        self.assertEqual(height_updates, [])
        self.qt_app.processEvents()
        self.assertTrue(ribbon.isVisible())
        self.assertGreater(host.height(), 0)
        self.assertGreater(height_updates[-1], 0)
        window.close()

    def test_uninstall_rebuilds_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            plugin_dir = Path(temp) / "test_plugin"
            plugin_dir.mkdir()
            instance = SimpleNamespace(on_unload=lambda: None)
            manager = plugin_manager_module.PluginManager(SimpleNamespace())
            manager.loaded_plugins["Test"] = {
                "instance": instance,
                "manifest": {"name": "Test"},
                "dir": plugin_dir,
            }
            private_module = "_naksha_test_private_plugin_module"
            sys.modules[private_module] = SimpleNamespace(
                __file__=str(plugin_dir / "helper.py")
            )
            rebuilds = []
            manager._rebuild_plugins_ribbon = lambda: rebuilds.append(True)

            try:
                ok, _ = manager.uninstall_plugin("Test")

                self.assertTrue(ok)
                self.assertEqual(len(rebuilds), 1)
                self.assertNotIn(private_module, sys.modules)
            finally:
                sys.modules.pop(private_module, None)

    def test_reinstall_rebuilds_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old_dir = root / "old"
            old_dir.mkdir()
            archive = root / "plugin.zip"
            manifest = {
                "name": "Test",
                "entry": "plugin.py",
                "class": "Plugin",
            }
            source = (
                "class Plugin:\n"
                "    def on_load(self, app): self.app = app\n"
                "    def on_unload(self): pass\n"
            )
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("manifest.json", json.dumps(manifest))
                package.writestr("plugin.py", source)

            original_dir = plugin_manager_module.PLUGINS_DIR
            plugin_manager_module.PLUGINS_DIR = root / "installed"
            try:
                manager = plugin_manager_module.PluginManager(
                    SimpleNamespace()
                )
                manager.loaded_plugins["Test"] = {
                    "instance": SimpleNamespace(on_unload=lambda: None),
                    "manifest": manifest,
                    "dir": old_dir,
                }
                rebuilds = []
                manager._rebuild_plugins_ribbon = (
                    lambda: rebuilds.append(True)
                )

                ok, _ = manager.install_plugin_from_zip(archive)

                self.assertTrue(ok)
                self.assertEqual(len(rebuilds), 1)
            finally:
                plugin_manager_module.PLUGINS_DIR = original_dir


if __name__ == "__main__":
    unittest.main()
