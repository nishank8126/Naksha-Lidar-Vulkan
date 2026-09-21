"""Universal GIS Catalog Browser for Naksha.

The catalog answers a different question from the Overlay Control Center:
Catalog = what exists on disk/in containers; Overlay Control Center = what is
currently rendered.  FileGDB is therefore one datasource among SHP, GeoJSON,
GeoPackage, raster and other GDAL-backed formats.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QSettings, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QDockWidget, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QSplitter, QTextBrowser, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .catalog_inspector import inspect_source, list_folder_items
from .capabilities import capability_text, priority_capabilities, runtime_summary
from .catalog_events import catalog_events
from .gis_style import (
    apply_gis_dock_style, compact_layout, compact_text_browser, compact_view,
    configure_header,
)
from .icons import icon as gis_icon

_PANEL_ATTR = "_gis_catalog_dock"
_ROLE_PATH = Qt.UserRole
_ROLE_KIND = Qt.UserRole + 1
_ROLE_LAYER = Qt.UserRole + 2
_ROLE_META = Qt.UserRole + 3


def _theme(widget):
    apply_gis_dock_style(widget)


def _fmt_int(value):
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value if value is not None else "-")


def _yesno(value):
    if value is True: return "Yes"
    if value is False: return "No"
    return "-"


def _inspect_html(meta: dict) -> str:
    if not meta:
        return "<i>Select a GIS datasource or layer.</i>"
    if meta.get("error"):
        return f"<h3>Inspection failed</h3><p>{html.escape(str(meta['error']))}</p>"
    if meta.get("empty"):
        return (
            "<h3>Empty File Geodatabase</h3>"
            f"<p>{html.escape(str(meta.get('path') or ''))}</p>"
            "<table cellspacing='0' cellpadding='0'>"
            "<tr><td><b>Feature classes</b></td><td>0</td></tr>"
            "<tr><td><b>Tables</b></td><td>0</td></tr>"
            "<tr><td><b>Feature datasets</b></td><td>0</td></tr></table>"
            "<p>Create a Feature Dataset, Feature Class, Table, Domain, or import data.</p>"
        )

    rows = []
    def add(k, v):
        if v not in (None, "", [], {}):
            rows.append(f"<tr><td><b>{html.escape(str(k))}</b></td><td>{html.escape(str(v))}</td></tr>")

    add("Path", meta.get("path"))
    add("Kind", meta.get("kind"))
    add("Driver", meta.get("driver"))
    add("GDAL", meta.get("gdal_version"))
    cap = meta.get("capabilities") or {}
    if cap:
        ops = []
        for key, label in (("open","Read"),("create","Create"),("update","Update"),("field_domains","Domains"),("relationships","Relationships"),("measured_geometries","M"),("z_geometries","Z")):
            if cap.get(key): ops.append(label)
        add("Capabilities", ", ".join(ops) or "Installed")

    if meta.get("kind") == "raster":
        add("Raster size", f"{meta.get('width','-')} × {meta.get('height','-')}")
        add("Bands", meta.get("bands"))
        crs = meta.get("crs") or {}
        add("CRS", f"EPSG:{crs.get('epsg')} — {crs.get('name')}" if crs.get("epsg") else crs.get("name"))
    else:
        layers = meta.get("layers") or []
        add("Layers / tables", len(layers))
        add("Feature Datasets", len(meta.get("feature_datasets") or []))
        add("Domains", len(meta.get("domains") or []))
        add("Relationships", len(meta.get("relationships") or []))

    body = "<table cellspacing='0' cellpadding='0'>" + "".join(rows) + "</table>"
    warnings = meta.get("warnings") or []
    if warnings:
        body += "<h4>Warnings</h4><ul>" + "".join(f"<li>{html.escape(str(w))}</li>" for w in warnings) + "</ul>"

    layers = meta.get("layers") or []
    if layers:
        body += "<h4>Layers / tables</h4><table width='100%' cellspacing='0' cellpadding='0'><tr><th align='left'>Name</th><th>Type</th><th>Count</th><th>Z</th><th>M</th><th>CRS</th></tr>"
        for layer in layers[:300]:
            crs = layer.get("crs") or {}
            body += (
                "<tr>"
                f"<td>{html.escape(str(layer.get('name','')))}</td>"
                f"<td>{html.escape(str(layer.get('geometry_type') or layer.get('kind') or ''))}</td>"
                f"<td>{html.escape(_fmt_int(layer.get('feature_count')))}</td>"
                f"<td>{_yesno(layer.get('has_z'))}</td><td>{_yesno(layer.get('has_m'))}</td>"
                f"<td>{html.escape('EPSG:'+str(crs.get('epsg')) if crs.get('epsg') else str(crs.get('name') or '-'))}</td>"
                "</tr>"
            )
        body += "</table>"
    return body


def _layer_html(container_meta: dict, layer_name: str) -> str:
    for lyr in container_meta.get("layers") or []:
        if lyr.get("name") != layer_name:
            continue
        rows = []
        def add(k,v):
            if v not in (None,"",[],{}): rows.append(f"<tr><td><b>{html.escape(str(k))}</b></td><td>{html.escape(str(v))}</td></tr>")
        add("Layer", layer_name)
        add("Type", lyr.get("geometry_type") or lyr.get("kind"))
        add("Feature count", _fmt_int(lyr.get("feature_count")))
        add("Feature Dataset", lyr.get("group"))
        add("Catalog path", lyr.get("catalog_path"))
        add("Z", _yesno(lyr.get("has_z")))
        add("M", _yesno(lyr.get("has_m")))
        crs = lyr.get("crs") or {}
        add("CRS", f"EPSG:{crs.get('epsg')} — {crs.get('name')}" if crs.get("epsg") else crs.get("name"))
        add("Metadata extent", lyr.get("extent"))
        add("Verified extent", lyr.get("verified_extent"))
        fields = lyr.get("fields") or []
        text = "<table cellspacing='0' cellpadding='0'>"+"".join(rows)+"</table>"
        if fields:
            text += "<h4>Fields</h4><table width='100%' cellspacing='0' cellpadding='0'><tr><th>Name</th><th>Type</th><th>Width</th><th>Domain</th></tr>"
            for f in fields:
                text += f"<tr><td>{html.escape(str(f.get('name','')))}</td><td>{html.escape(str(f.get('type','')))}</td><td>{html.escape(str(f.get('width','')))}</td><td>{html.escape(str(f.get('domain','') or ''))}</td></tr>"
            text += "</table>"
        return text
    return "<i>Layer metadata not available.</i>"


class _CatalogDetailsBrowser(QTextBrowser):
    """Catalog metadata view with styling isolated from the app theme."""

    _STYLE = """
    <style>
      body { font-family: 'Segoe UI'; font-size: 11px; color: #1d2730; background: #fff; margin: 0; }
      h3 { font-size: 12px; color: #17354a; margin: 2px 0 3px 0; font-weight: 600; }
      h4 { font-size: 11px; color: #17354a; margin: 5px 0 1px 0; font-weight: 600; }
      p { margin: 1px 0; }
      table { border-collapse: collapse; margin: 0; }
      th { font-size: 11px; color: #17354a; background: #d9e5ee; font-weight: 600; text-align: left; }
      td { font-size: 11px; background: #fff; }
      th, td { padding: 0 4px 0 2px; border-bottom: 1px solid #d8dee3; }
      ul { margin: 2px 0 2px 12px; padding: 0; }
      li { margin: 0; padding: 0; }
      pre { font-family: Consolas; font-size: 11px; margin: 2px 0; }
    </style>
    """

    def setHtml(self, text: str) -> None:
        super().setHtml(f"<html><head>{self._STYLE}</head><body>{text}</body></html>")

class GisCatalogDock(QDockWidget):
    def __init__(self, app):
        super().__init__("GIS Catalog", app)
        self.app = app
        self.setObjectName("NakshaGisCatalogDock")
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.resize(920, 650)
        self._source_cache = {}
        self._rebuilding = False
        self._settings = QSettings("NakshaAI", "LidarApp")
        self._current_folder = self._settings.value(
            "gis/catalog/last_folder", str(Path.home()), type=str
        )
        if not os.path.isdir(self._current_folder):
            self._current_folder = str(Path.home())

        host = QWidget(self)
        self.setWidget(host)
        root = QVBoxLayout(host)
        compact_layout(root)

        top = QHBoxLayout()
        self.path_edit = QLineEdit(self._current_folder)
        self.path_edit.returnPressed.connect(lambda: self.set_folder(self.path_edit.text()))
        browse = QPushButton("...")
        browse.setToolTip("Browse for catalog folder")
        browse.clicked.connect(self._browse)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        caps = QPushButton("Drivers")
        caps.clicked.connect(self._show_drivers)
        top.addWidget(QLabel("Folder:")); top.addWidget(self.path_edit,1); top.addWidget(browse); top.addWidget(refresh); top.addWidget(caps)
        root.addLayout(top)

        # Discoverable primary actions. Right-click remains a shortcut, not the
        # only way to create/manage GIS data.
        actions = QHBoxLayout()
        self.new_btn = QPushButton("+ New")
        self.new_btn.setToolTip("Create folders, Shapefile, GeoJSON, GeoPackage, FlatGeobuf, dBASE, FileGDB, or schema items")
        self.new_btn.clicked.connect(self._new_button_menu)
        self.import_btn = QPushButton("Import / Convert")
        self.import_btn.clicked.connect(self._import_convert_button)
        self.project_crs_btn = QPushButton("Project CRS")
        self.project_crs_btn.setToolTip("Select/view the unified Naksha project coordinate reference system")
        self.project_crs_btn.clicked.connect(self._project_crs_button)
        actions.addWidget(self.new_btn)
        actions.addWidget(self.import_btn)
        actions.addWidget(self.project_crs_btn)
        actions.addStretch(1)
        root.addLayout(actions)

        splitter = QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setObjectName("gisCatalogTree")
        self.tree.setIndentation(13)
        self.tree.setHeaderLabels(["Catalog Item", "Type"])
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemSelectionChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.itemExpanded.connect(self._expanded)
        compact_view(self.tree)
        configure_header(self.tree.header(), stretch_column=0)
        self.tree.header().resizeSection(1, 125)
        self.details = _CatalogDetailsBrowser()
        self.details.setObjectName("gisCatalogDetails")
        self.details.setOpenExternalLinks(False)
        compact_text_browser(self.details)
        splitter.addWidget(self.tree); splitter.addWidget(self.details)
        saved_sizes = self._settings.value("gis/catalog/splitter_sizes")
        splitter.setSizes(
            [int(x) for x in saved_sizes]
            if isinstance(saved_sizes, (list, tuple)) and len(saved_sizes) == 2
            else [390, 530]
        )
        splitter.splitterMoved.connect(
            lambda _pos, _index: self._settings.setValue(
                "gis/catalog/splitter_sizes", splitter.sizes()
            )
        )
        root.addWidget(splitter,1)

        status = QLabel("Catalog shows data on disk. Use Overlay Control Center for layers currently loaded in the canvas.")
        status.setWordWrap(True)
        status.setProperty("secondary", True)
        root.addWidget(status)
        _theme(host)
        events = catalog_events()
        events.datasourceCreated.connect(self._datasource_created)
        events.datasetCreated.connect(self._dataset_created)
        events.layerCreated.connect(self._layer_created)
        events.schemaChanged.connect(self._schema_changed)
        self.refresh()

    def _dataset_created(self, path, name):
        self._refresh_container(path, "feature_dataset", name)

    def _layer_created(self, path, _group, name):
        self._refresh_container(path, "layer", name)

    def _schema_changed(self, path):
        self._refresh_container(path)

    def set_folder(self, folder: str):
        folder = os.path.abspath(os.path.expanduser(folder or ""))
        if not os.path.isdir(folder):
            QMessageBox.warning(self, "GIS Catalog", f"Folder does not exist:\n{folder}")
            return
        self._current_folder = folder
        self._settings.setValue("gis/catalog/last_folder", folder)
        self.path_edit.setText(folder)
        self.refresh()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "GIS Catalog Folder", self._current_folder)
        if folder: self.set_folder(folder)

    def _item_key(self, item):
        if item is None:
            return None
        path = item.data(0, _ROLE_PATH)
        return (
            os.path.normcase(os.path.abspath(path)) if path else "",
            str(item.data(0, _ROLE_KIND) or ""),
            str(item.data(0, _ROLE_LAYER) or ""),
        )

    def _iter_items(self):
        pending = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while pending:
            item = pending.pop(0)
            if item is None:
                continue
            yield item
            pending[0:0] = [item.child(i) for i in range(item.childCount())]

    def _find_item(self, key):
        return next((item for item in self._iter_items() if self._item_key(item) == key), None)

    def refresh(self, select_key=None):
        # Reentrancy guard: refresh() is reachable from a button click, from
        # set_folder(), and from the process-wide catalog_events() bus (which
        # can fire while a nested QMenu/QMessageBox event loop from this same
        # dock is still on the stack). Rebuilding the tree from inside itself
        # frees items a caller further up may still hold - the same native
        # access-violation class fixed in the GIS layer panel.
        if self._rebuilding:
            return
        self._rebuilding = True
        try:
            expanded = {self._item_key(i) for i in self._iter_items() if i.isExpanded()}
            selected = select_key or self._item_key(self.tree.currentItem())
            scroll = self.tree.verticalScrollBar().value()
            self.tree.setUpdatesEnabled(False)
            try:
                self.tree.setCurrentItem(None)
                self.tree.clear(); self._source_cache.clear()
                parent_item = QTreeWidgetItem([self._current_folder, "Folder"])
                parent_item.setData(0,_ROLE_PATH,self._current_folder); parent_item.setData(0,_ROLE_KIND,"folder_root")
                parent_item.setIcon(0, gis_icon("folder", "#b07818"))
                self.tree.addTopLevelItem(parent_item); parent_item.setExpanded(True)
                for info in list_folder_items(self._current_folder):
                    self._add_fs_item(parent_item, info)
                # Restore expanded datasource branches lazily, without collapsing
                # unrelated work after a schema operation.
                for key in list(expanded):
                    item = self._find_item(key)
                    if item is not None:
                        item.setExpanded(True)
                target = self._find_item(selected)
                if target is not None:
                    self.tree.setCurrentItem(target)
                    self.tree.scrollToItem(target)
                else:
                    self.tree.verticalScrollBar().setValue(scroll)
            finally:
                self.tree.setUpdatesEnabled(True)
        finally:
            self._rebuilding = False
        self.details.setHtml(
            "<h3>GIS Catalog</h3>"
            "<p><b>+ New</b> creates GIS files with an explicit coordinate system, or FileGDB schema items.</p>"
            "<p><b>Project CRS</b> selects the one Naksha world coordinate system used by LiDAR, SNT, GIS and CAD display.</p>"
            "<p><b>Import / Convert</b> transfers GIS data between supported formats.</p>"
            "<p>Select an item to inspect its schema and runtime driver capabilities.</p>"
        )

    def _add_fs_item(self, parent, info):
        label = {"folder":"Folder","gdb":"File Geodatabase","vector":"Vector","raster":"Raster"}.get(info.get("kind"), info.get("kind","GIS"))
        item = QTreeWidgetItem(parent, [info.get("name",""), label])
        item.setData(0,_ROLE_PATH,info.get("path")); item.setData(0,_ROLE_KIND,info.get("kind")); item.setData(0,_ROLE_META,info)
        icons = {
            "folder": ("folder", "#b07818"), "gdb": ("database", "#8b5d20"),
            "vector": ("vector-polygon", "#26734d"), "raster": ("raster-layer", "#6b4c9a"),
        }
        icon_name, color = icons.get(info.get("kind"), ("globe", "#4f6b7a"))
        item.setIcon(0, gis_icon(icon_name, color))
        if info.get("expandable") and info.get("kind") != "folder":
            dummy = QTreeWidgetItem(item,["Loading...",""]); dummy.setData(0,_ROLE_KIND,"dummy")
        return item

    def _expanded(self, item):
        kind = item.data(0,_ROLE_KIND); path = item.data(0,_ROLE_PATH)
        if kind not in {"gdb","vector","raster"} or not path: return
        if item.childCount() and item.child(0).data(0,_ROLE_KIND) != "dummy": return
        item.takeChildren()
        meta = self._inspect(path, include_fields=False)

        # Physical Feature Datasets come from GDAL inspection.  OpenFileGDB
        # builds that cannot persist an empty Feature Dataset are supplemented
        # with explicit session-only PENDING descriptors from the writer.
        physical_fds = [str(fd) for fd in (meta.get("feature_datasets") or [])]
        pending_fds = []
        if kind == "gdb":
            try:
                from gui.gis.gdb.pending import list_pending
                pending_fds = list_pending(path)
            except Exception:
                pending_fds = []

        physical_keys = {name.casefold() for name in physical_fds}
        for fd in physical_fds:
            fd_item = QTreeWidgetItem(item,[fd,"Feature Dataset"])
            fd_item.setData(0,_ROLE_PATH,path); fd_item.setData(0,_ROLE_KIND,"feature_dataset"); fd_item.setData(0,_ROLE_LAYER,fd)
            fd_item.setData(0,_ROLE_META,{"pending": False})
            fd_item.setIcon(0, gis_icon("folder", "#9a6b22"))

        for rec in pending_fds:
            fd = str(rec.get("name") or "").strip()
            if not fd or fd.casefold() in physical_keys:
                continue
            fd_item = QTreeWidgetItem(item,[fd,"Feature Dataset (pending)"])
            fd_item.setData(0,_ROLE_PATH,path); fd_item.setData(0,_ROLE_KIND,"feature_dataset"); fd_item.setData(0,_ROLE_LAYER,fd)
            fd_item.setData(0,_ROLE_META,dict(rec))
            fd_item.setToolTip(0, "Pending: this OpenFileGDB runtime materializes the Feature Dataset when its first Feature Class is created.")
            fd_item.setIcon(0, gis_icon("folder", "#b8862d"))

        fd_map = {item.child(i).text(0): item.child(i) for i in range(item.childCount()) if item.child(i).data(0,_ROLE_KIND)=="feature_dataset"}
        table_parent = None
        if kind == "gdb" or str(path).lower().endswith(".gpkg"):
            table_parent = QTreeWidgetItem(item, ["Tables", "Container"])
            table_parent.setData(0, _ROLE_PATH, path); table_parent.setData(0, _ROLE_KIND, "tables")
            table_parent.setIcon(0, gis_icon("table", "#5c6870"))
        for lyr in meta.get("layers") or []:
            group = str(lyr.get("group") or "")
            parent = table_parent if lyr.get("kind") == "table" and table_parent is not None else fd_map.get(group, item)
            typ = "Table" if lyr.get("kind")=="table" else (lyr.get("geometry_type") or lyr.get("kind") or "Layer")
            li = QTreeWidgetItem(parent,[str(lyr.get("name")),str(typ)])
            li.setData(0,_ROLE_PATH,path); li.setData(0,_ROLE_KIND,"layer"); li.setData(0,_ROLE_LAYER,lyr.get("name")); li.setData(0,_ROLE_META,lyr)
            icon_name = {"point": "vector-point", "line": "vector-line", "polygon": "vector-polygon", "table": "table"}.get(lyr.get("kind"), "vector-polygon")
            li.setIcon(0, gis_icon(icon_name, "#26734d"))
        if kind == "gdb":
            domains = QTreeWidgetItem(item,[f"Domains ({len(meta.get('domains') or [])})","Schema"])
            domains.setData(0,_ROLE_PATH,path); domains.setData(0,_ROLE_KIND,"domains")
            domains.setIcon(0, gis_icon("database", "#6b5b35"))
        if meta.get("relationships"):
            rels = QTreeWidgetItem(item,[f"Relationships ({len(meta['relationships'])})","Schema"])
            rels.setData(0,_ROLE_PATH,path); rels.setData(0,_ROLE_KIND,"relationships")

    def _inspect(self, path, *, include_fields: bool = False):
        key_path = os.path.abspath(path)
        key = (key_path, bool(include_fields))
        if key not in self._source_cache:
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                self._source_cache[key] = inspect_source(
                    key_path,
                    include_fields=bool(include_fields),
                    include_internal=False,
                    verify_extents=False,
                )
            finally:
                QApplication.restoreOverrideCursor()
        return self._source_cache[key]

    def _selected(self):
        item = self.tree.currentItem()
        if item is None: return
        path = item.data(0,_ROLE_PATH); kind = item.data(0,_ROLE_KIND); layer = item.data(0,_ROLE_LAYER)
        if kind in {"folder","folder_root"}:
            self.details.setHtml(f"<h3>Folder</h3><p>{html.escape(str(path or ''))}</p>"); return
        if kind == "feature_dataset":
            item_meta = item.data(0, _ROLE_META) or {}
            if item_meta.get("pending"):
                crs_value = item_meta.get("crs")
                crs_text = ""
                if crs_value:
                    try:
                        from gui.projection_engine import parse_crs, crs_identifier
                        parsed_crs = parse_crs(crs_value)
                        crs_text = (
                            "<p><b>Coordinate system:</b> "
                            + html.escape(f"{parsed_crs.name} ({crs_identifier(parsed_crs)})")
                            + "</p>"
                        )
                    except Exception:
                        crs_text = "<p><b>Coordinate system:</b> " + html.escape(str(crs_value)) + "</p>"
                self.details.setHtml(
                    "<h3>Feature Dataset (pending)</h3>"
                    f"<p><b>{html.escape(str(layer or ''))}</b></p>"
                    f"<p>{html.escape(str(path or ''))}</p>"
                    + crs_text
                    + "<p>The installed OpenFileGDB runtime does not persist an empty Feature Dataset. "
                      "Naksha will materialize it on disk when the first Feature Class is created.</p>"
                )
            else:
                self.details.setHtml(f"<h3>Feature Dataset</h3><p>{html.escape(str(layer or ''))}</p><p>{html.escape(str(path or ''))}</p>")
            return
        if not path: return
        meta = self._inspect(path, include_fields=(kind == "layer" and bool(layer)))
        if kind == "layer" and layer:
            self.details.setHtml(_layer_html(meta, layer))
        elif kind == "domains":
            doms = meta.get("domains") or []
            text = "<h3>Domains</h3><ul>"+"".join(f"<li>{html.escape(str(d.get('name','')))}</li>" for d in doms)+"</ul>"
            self.details.setHtml(text)
        elif kind == "relationships":
            rels = meta.get("relationships") or []
            self.details.setHtml("<h3>Relationships</h3><pre>"+html.escape(json.dumps(rels,indent=2,default=str))+"</pre>")
        else:
            self.details.setHtml(_inspect_html(meta))

    def _double_clicked(self, item, column):
        kind = item.data(0,_ROLE_KIND); path = item.data(0,_ROLE_PATH); layer = item.data(0,_ROLE_LAYER)
        if kind == "folder": self.set_folder(path); return
        if kind in {"gdb", "feature_dataset", "tables", "domains", "relationships"}:
            item.setExpanded(not item.isExpanded())
            return
        if kind in {"vector","raster","layer"}:
            self._import_item(path, layer)

    def _datasource_created(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        if os.path.normcase(folder) != os.path.normcase(self._current_folder):
            self.set_folder(folder)
        self.refresh((os.path.normcase(os.path.abspath(path)), "gdb" if path.lower().endswith(".gdb") else "vector", ""))
        item = self._find_item((
            os.path.normcase(os.path.abspath(path)),
            "gdb" if path.lower().endswith(".gdb") else "vector",
            "",
        ))
        if item is not None:
            item.setExpanded(True)

    def _refresh_container(self, path, target_kind=None, target_name=""):
        # Reached from the process-wide catalog_events() bus, which can fire
        # while this same tree is already mid-rebuild (refresh() or another
        # _refresh_container() call) - e.g. a queued schemaChanged landing
        # while a nested QMessageBox from that same operation is still open.
        # container.takeChildren() below frees items; skip rather than race.
        if self._rebuilding:
            return
        key_path = os.path.normcase(os.path.abspath(path))
        container = next(
            (i for i in self._iter_items()
             if self._item_key(i)[0] == key_path and i.data(0, _ROLE_KIND) in {"gdb", "vector"}),
            None,
        )
        _abs = os.path.abspath(path)
        for _key in [k for k in self._source_cache if (k[0] if isinstance(k, tuple) else k) == _abs]:
            self._source_cache.pop(_key, None)
        if container is None:
            self.refresh()
            return
        self._rebuilding = True
        try:
            was_expanded = container.isExpanded()
            container.takeChildren()
            QTreeWidgetItem(container, ["Loading...", ""]).setData(0, _ROLE_KIND, "dummy")
            container.setExpanded(True)
            self._expanded(container)
            if target_kind and target_name:
                target = next(
                    (i for i in self._iter_items()
                     if i.data(0, _ROLE_PATH) == path
                     and i.data(0, _ROLE_KIND) == target_kind
                     and str(i.data(0, _ROLE_LAYER) or "") == target_name),
                    None,
                )
                if target is not None:
                    target.parent().setExpanded(True)
                    self.tree.setCurrentItem(target)
                    self.tree.scrollToItem(target)
            elif not was_expanded:
                container.setExpanded(False)
        finally:
            self._rebuilding = False

    def _import_item(self, path, layer=None):
        if not path:
            return
        try:
            from gui.gis.gis_layers import _import_one, show_gis_layers_panel
            entry = None
            ok = False

            if layer and str(path).lower().endswith(".gdb"):
                # A specific Catalog child is already an unambiguous layer/table;
                # load it directly instead of reopening the whole GDB picker.
                from gui.gis.gdb.reader import import_gdb_layer
                entry = import_gdb_layer(self.app, path, layer)
                ok = entry is not None
            elif layer:
                # Same direct-child behavior for GeoPackage/SQLite and other
                # multi-layer OGR containers.
                from gui.gis.native_vector import import_native_vector_layer
                entry = import_native_vector_layer(self.app, path, layer_name=layer)
                ok = entry is not None
            elif str(path).lower().endswith(".gdb"):
                from gui.gis.gdb.gdb_picker import pick_and_import_gdb
                ok = bool(pick_and_import_gdb(self.app, path))
            else:
                ok = bool(_import_one(self.app, path))

            if not ok:
                return

            show_gis_layers_panel(self.app)

            # Catalog table leaves mean "Open Table", not "render a layer".
            # Keep the datasource-backed table registered in Layers, then open
            # its paged Attribute Table immediately.
            if isinstance(entry, dict) and entry.get("kind") == "table":
                from gui.gis.gis_layers import AttributeTableDialog
                if not hasattr(self.app, "_attribute_tables"):
                    self.app._attribute_tables = {}
                table_key = (entry.get("id"), None)
                existing = self.app._attribute_tables.get(table_key)
                if existing is not None:
                    existing.show(); existing.raise_(); existing.activateWindow()
                else:
                    dlg = AttributeTableDialog(self.app, entry)
                    self.app._attribute_tables[table_key] = dlg
                    dlg.finished.connect(
                        lambda *_args, key=table_key: self.app._attribute_tables.pop(key, None)
                    )
                    dlg.show(); dlg.raise_(); dlg.activateWindow()
        except Exception as exc:
            QMessageBox.critical(self, "Import GIS Data", str(exc))

    def _project_crs_button(self):
        opener = getattr(self.app, "open_project_crs_dialog", None)
        if callable(opener):
            opener()
        else:
            QMessageBox.information(self, "Project CRS", "Project CRS selector is not available in this build.")

    def _import_convert_button(self):
        try:
            from gui.gis.conversion_dialog import show_batch_conversion_dialog
            show_batch_conversion_dialog(self)
        except Exception as exc:
            QMessageBox.warning(self, "Import / Convert GIS Data", str(exc))

    def _create_folder(self, parent_folder):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New Folder", "Name:", text="New Folder")
        name = name.strip()
        if not ok or not name:
            return
        if Path(name).name != name:
            QMessageBox.warning(self, "New Folder", "Enter a folder name without a path.")
            return
        target = os.path.join(parent_folder, name)
        try:
            os.mkdir(target)
        except FileExistsError:
            QMessageBox.warning(self, "New Folder", "A folder with this name already exists.")
            return
        except Exception as exc:
            QMessageBox.warning(self, "New Folder", str(exc))
            return
        self.refresh((os.path.normcase(os.path.abspath(target)), "folder", ""))

    def _new_button_menu(self):
        """Context-sensitive visible New menu, ArcCatalog-style."""
        item = self.tree.currentItem()
        kind = item.data(0, _ROLE_KIND) if item is not None else "folder_root"
        path = item.data(0, _ROLE_PATH) if item is not None else self._current_folder
        layer = item.data(0, _ROLE_LAYER) if item is not None else None

        menu = QMenu(self)
        if kind in {"folder_root", "folder", None}:
            a_folder = menu.addAction("Folder...")
            menu.addSeparator()
            a_shp = menu.addAction("Shapefile...")
            a_geojson = menu.addAction("GeoJSON...")
            a_gpkg = menu.addAction("GeoPackage Layer...")
            a_fgb = menu.addAction("FlatGeobuf...")
            a_dbf = menu.addAction("dBASE Table...")
            menu.addSeparator()
            a_gdb = menu.addAction("File Geodatabase...")
            a_empty_gpkg = menu.addAction("Empty GeoPackage...")
            chosen = menu.exec(self.new_btn.mapToGlobal(self.new_btn.rect().bottomLeft()))
            target_folder = path if path and os.path.isdir(path) else self._current_folder
            vector_formats = {
                a_shp: "shapefile",
                a_geojson: "geojson",
                a_gpkg: "geopackage",
                a_fgb: "flatgeobuf",
                a_dbf: "dbase",
            }
            if chosen == a_folder:
                self._create_folder(target_folder)
            elif chosen in vector_formats:
                from gui.gis.vector_create_dialog import create_vector_dataset_dialog
                create_vector_dataset_dialog(self, target_folder, vector_formats[chosen])
            elif chosen == a_gdb:
                from gui.gis.gdb.schema_dialogs import create_file_gdb_dialog
                create_file_gdb_dialog(self, target_folder)
            elif chosen == a_empty_gpkg:
                from PySide6.QtWidgets import QInputDialog
                name, ok = QInputDialog.getText(self, "New Empty GeoPackage", "Name:", text="New GeoPackage")
                if ok and name.strip():
                    from gui.gis.container_writer import create_geopackage
                    filename = name.strip() if name.strip().lower().endswith(".gpkg") else name.strip()+".gpkg"
                    result = create_geopackage(os.path.join(target_folder, filename))
                    if result.success:
                        QMessageBox.information(self, "New Empty GeoPackage", f"Created:\n{result.path}")
                        catalog_events().datasourceCreated.emit(result.path)
                    else:
                        QMessageBox.warning(self, "New Empty GeoPackage", result.error or "Creation failed.")
            return

        if str(path or "").lower().endswith(".gdb"):
            a_fd = menu.addAction("Feature Dataset...")
            a_fc = menu.addAction("Feature Class...")
            a_table = menu.addAction("Table...")
            chosen = menu.exec(self.new_btn.mapToGlobal(self.new_btn.rect().bottomLeft()))
            if chosen == a_fd:
                from gui.gis.gdb.schema_dialogs import create_feature_dataset_dialog
                create_feature_dataset_dialog(self, path)
            elif chosen == a_fc:
                from gui.gis.gdb.schema_dialogs import create_feature_class_dialog
                create_feature_class_dialog(self, path, feature_dataset=layer if kind == "feature_dataset" else None)
            elif chosen == a_table:
                from gui.gis.gdb.schema_dialogs import create_table_dialog
                create_table_dialog(self, path)
            else:
                return
            return

        QMessageBox.information(
            self, "New GIS Data",
            "Select a folder to create a GIS file, or select a FileGDB to create schema items."
        )

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None: return
        path = item.data(0,_ROLE_PATH); kind = item.data(0,_ROLE_KIND); layer = item.data(0,_ROLE_LAYER)
        menu = QMenu(self)
        if kind in {"folder_root","folder"}:
            a_new_folder = menu.addAction("New Folder...")
            menu.addSeparator()
            a_new_shp = menu.addAction("New Shapefile...")
            a_new_geojson = menu.addAction("New GeoJSON...")
            a_new_gpkg_layer = menu.addAction("New GeoPackage Layer...")
            a_new_fgb = menu.addAction("New FlatGeobuf...")
            a_new_dbf = menu.addAction("New dBASE Table...")
            menu.addSeparator()
            a_new_gdb = menu.addAction("New File Geodatabase...")
            a_new_empty_gpkg = menu.addAction("New Empty GeoPackage...")
            a_convert = menu.addAction("Import / Convert GIS Data...")
            menu.addSeparator()
            a_refresh = menu.addAction("Refresh")
            chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
            vector_formats = {
                a_new_shp: "shapefile",
                a_new_geojson: "geojson",
                a_new_gpkg_layer: "geopackage",
                a_new_fgb: "flatgeobuf",
                a_new_dbf: "dbase",
            }
            if chosen == a_new_folder:
                self._create_folder(path)
            elif chosen in vector_formats:
                from gui.gis.vector_create_dialog import create_vector_dataset_dialog
                create_vector_dataset_dialog(self, path, vector_formats[chosen])
            elif chosen == a_new_gdb:
                from gui.gis.gdb.schema_dialogs import create_file_gdb_dialog
                create_file_gdb_dialog(self, path)
            elif chosen == a_new_empty_gpkg:
                from PySide6.QtWidgets import QInputDialog
                name, ok = QInputDialog.getText(self, "New Empty GeoPackage", "Name:", text="New GeoPackage")
                if ok and name.strip():
                    from gui.gis.container_writer import create_geopackage
                    target = os.path.join(path, name.strip() if name.strip().lower().endswith(".gpkg") else name.strip()+".gpkg")
                    result = create_geopackage(target)
                    if result.success:
                        QMessageBox.information(self, "New Empty GeoPackage", f"Created:\n{result.path}")
                        catalog_events().datasourceCreated.emit(result.path)
                    else:
                        QMessageBox.warning(self, "New Empty GeoPackage", result.error or "Creation failed.")
            elif chosen == a_convert:
                from gui.gis.conversion_dialog import show_batch_conversion_dialog
                show_batch_conversion_dialog(self)
            elif chosen == a_refresh:
                self.refresh()
            return
        a_inspect = menu.addAction("Inspect")
        a_import = menu.addAction("Add to Map")
        item_meta = item.data(0, _ROLE_META) or {}
        is_table_leaf = kind == "layer" and str(item_meta.get("kind") or "") == "table"
        a_import.setText("Open Table" if is_table_leaf else "Add to Map")
        a_import.setEnabled(kind in {"layer", "vector", "raster"})
        a_convert = menu.addAction("Convert / Import to GDB or GeoPackage...")
        a_convert.setEnabled(kind in {"gdb", "vector", "layer"})
        if str(path or "").lower().endswith(".gdb"):
            menu.addSeparator()
            a_fd = menu.addAction("New Feature Dataset...")
            a_fc = menu.addAction("New Feature Class...")
            a_table = menu.addAction("New Table...")
            a_domains = menu.addAction("Domains...")
            a_repack = menu.addAction("Repack Geodatabase")
        else:
            a_fd = a_fc = a_table = a_domains = a_repack = None
        chosen = menu.exec(self.tree.viewport().mapToGlobal(pos))
        if chosen == a_inspect:
            abs_path = os.path.abspath(path)
            for cache_key in [k for k in self._source_cache if (k[0] if isinstance(k, tuple) else k) == abs_path]:
                self._source_cache.pop(cache_key, None)
            self._selected()
        elif chosen == a_import: self._import_item(path, layer)
        elif chosen == a_convert:
            from gui.gis.conversion_dialog import show_batch_conversion_dialog
            from gui.gis.conversion_engine import SourceLayer
            show_batch_conversion_dialog(self, sources=[SourceLayer(path=path, layer_name=layer) if layer else SourceLayer(path=path)])
        elif chosen == a_fd:
            from gui.gis.gdb.schema_dialogs import create_feature_dataset_dialog
            create_feature_dataset_dialog(self,path)
        elif chosen == a_fc:
            from gui.gis.gdb.schema_dialogs import create_feature_class_dialog
            # menu.exec() above pumps the event loop; a queued catalog_events()
            # emit landing during it can refresh() this tree and free `item`.
            # `layer`/`kind` were captured as plain values before exec(), so
            # use those instead of touching the possibly-dangling item.
            fd = layer if kind == "feature_dataset" else None
            create_feature_class_dialog(self,path,feature_dataset=fd)
        elif chosen == a_table:
            from gui.gis.gdb.schema_dialogs import create_table_dialog
            create_table_dialog(self,path)
        elif chosen == a_domains:
            try:
                from gui.gis.gdb import open_domains_dialog
                open_domains_dialog(self.app,path)
            except Exception as exc: QMessageBox.warning(self,"Domains",str(exc))
        elif chosen == a_repack:
            if QMessageBox.question(self,"Repack Geodatabase","Repack this FileGDB to reclaim free space?") == QMessageBox.Yes:
                from gui.gis.gdb.writer import repack
                result = repack(path)
                if result.success: QMessageBox.information(self,"Repack","Geodatabase repacked.")
                else: QMessageBox.warning(self,"Repack",result.error or "Repack failed.")

    def _show_drivers(self):
        summary = runtime_summary()
        lines = [f"GDAL: {summary.get('gdal_version')}", ""]
        for cap in priority_capabilities():
            lines.append(f"{cap.name:20s} {capability_text(cap)}")
        QMessageBox.information(self,"GIS Driver Capabilities","\n".join(lines))


def show_catalog_panel(app, folder: str | None = None):
    dock = getattr(app,_PANEL_ATTR,None)
    if dock is None:
        dock = GisCatalogDock(app)
        setattr(app,_PANEL_ATTR,dock)
        # Deferred: the dock's __init__ just built a fresh toolbar/tree
        # subtree (new QPushButtons, tooltips, icons) whose Qt-style-driven
        # setup may still be settling. Reparenting it into the main window
        # in the same tick raced a style animation's teardown against the
        # reparent's ChildRemoved/ChildAdded events elsewhere in this app
        # (0xc0000005 in Qt6Widgets.dll) - defer one event-loop tick.
        from PySide6.QtCore import QTimer

        def _deferred_dock(_dock=dock):
            try:
                app.addDockWidget(Qt.LeftDockWidgetArea, _dock)
            except Exception:
                pass
            if folder and os.path.isdir(folder):
                _dock.set_folder(folder)
            _dock.show(); _dock.raise_(); _dock.activateWindow()

        QTimer.singleShot(0, _deferred_dock)
        return dock
    if folder and os.path.isdir(folder): dock.set_folder(folder)
    dock.show(); dock.raise_(); dock.activateWindow()
    return dock


def toggle_catalog_panel(app):
    dock = getattr(app,_PANEL_ATTR,None)
    if dock is not None and dock.isVisible():
        dock.hide(); return dock
    return show_catalog_panel(app)
