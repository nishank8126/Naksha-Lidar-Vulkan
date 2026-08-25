import json
import zipfile
from pathlib import Path

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
    QLabel, QFileDialog, QMessageBox, QWidget, QStackedWidget,
    QLineEdit, QSplitter, QTextBrowser, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QGroupBox, QFormLayout, QFrame, QScrollArea
)


class PluginLoaderDialog(QDialog):
    def __init__(self, plugin_manager, parent=None):
        super().__init__(parent)
        self.pm = plugin_manager
        self.setWindowTitle("Plugins Manager")
        self.setMinimumSize(760, 500)
        self.resize(860, 560)
        self.setWindowFlags(self.windowFlags() | Qt.Dialog | Qt.WindowCloseButtonHint)
        self.discovered = []
        self._selected_plugin_name = None

        try:
            from gui.theme_manager import ThemeColors
            is_light = ThemeColors.is_light() if hasattr(ThemeColors, "is_light") else True
        except Exception:
            is_light = True

        # Color palette
        if is_light:
            self.c_bg          = "#ffffff"
            self.c_bg_alt      = "#f5f5f5"
            self.c_bg_sidebar  = "#3a3a3a"
            self.c_sidebar_sel = "#4a4a4a"
            self.c_sidebar_txt = "#cccccc"
            self.c_sidebar_txt_active = "#ffffff"
            self.c_border      = "#d0d5dd"
            self.c_text        = "#1b2838"
            self.c_text_muted  = "#6b7280"
            self.c_accent      = "#2c5f8a"
            self.c_accent_dark = "#245079"
            self.c_row_sel     = "#dce8f5"
            self.c_link        = "#2c7bca"
        else:
            self.c_bg          = "#1e1e2e"
            self.c_bg_alt      = "#2a2a3a"
            self.c_bg_sidebar  = "#181820"
            self.c_sidebar_sel = "#2a2a3a"
            self.c_sidebar_txt = "#aaaacc"
            self.c_sidebar_txt_active = "#e0e0ff"
            self.c_border      = "#3c3c5c"
            self.c_text        = "#e0e0f0"
            self.c_text_muted  = "#888899"
            self.c_accent      = "#4a90d9"
            self.c_accent_dark = "#3a7bc8"
            self.c_row_sel     = "#2a3a5a"
            self.c_link        = "#5b9bd5"

        self._build_ui()
        self._refresh()
        self.category_list.setCurrentRow(0)

    # ──────────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Title bar
        title_bar = QLabel("  Plugins Manager")
        title_bar.setFixedHeight(28)
        title_bar.setStyleSheet(
            f"background:{self.c_accent}; color:#ffffff; font-weight:bold;"
            "font-size:11pt; padding-left:8px;"
        )
        root.addWidget(title_bar)

        # ── Body splitter (sidebar | main)
        body = QSplitter(Qt.Horizontal)
        body.setHandleWidth(0)
        root.addWidget(body, 1)

        # ── Sidebar
        self.category_list = QListWidget()
        self.category_list.setFixedWidth(140)
        self.category_list.setObjectName("sidebar")
        for label, icon in [("Installed", "✦"), ("Install from ZIP", "⊞")]:
            item = QListWidgetItem(f"  {icon}  {label}")
            item.setSizeHint(QSize(140, 30))
            self.category_list.addItem(item)
        self.category_list.setStyleSheet(f"""
            QListWidget#sidebar {{
                background:{self.c_bg_sidebar};
                border:none;
                outline:none;
                font-size:9.5pt;
            }}
            QListWidget#sidebar::item {{
                color:{self.c_sidebar_txt};
                padding:6px 4px;
                border-left:3px solid transparent;
            }}
            QListWidget#sidebar::item:selected {{
                background:{self.c_sidebar_sel};
                color:{self.c_sidebar_txt_active};
                border-left:3px solid {self.c_accent};
                font-weight:bold;
            }}
            QListWidget#sidebar::item:hover:!selected {{
                background:#444444;
            }}
        """)
        body.addWidget(self.category_list)

        # ── Right container
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right.setStyleSheet(f"background:{self.c_bg};")

        self.stack = QStackedWidget()
        self.category_list.currentRowChanged.connect(self.stack.setCurrentIndex)
        right_layout.addWidget(self.stack, 1)

        # ── Page 0: Installed
        self.stack.addWidget(self._build_installed_page())

        # ── Page 1: ZIP install
        self.stack.addWidget(self._build_zip_page())

        # ── Bottom close bar
        close_bar = QHBoxLayout()
        close_bar.setContentsMargins(8, 6, 8, 6)
        close_bar.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFixedWidth(80)
        close_btn.clicked.connect(self.accept)
        close_bar.addWidget(close_btn)
        help_btn = QPushButton("Help")
        help_btn.setFixedWidth(80)
        close_bar.addWidget(help_btn)
        right_layout.addWidget(self._make_separator())
        right_layout.addLayout(close_bar)

        body.addWidget(right)
        body.setSizes([140, 720])

        self._apply_global_style()

    # ──────────────────────────────────────────────────────────────────
    def _build_installed_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Search bar
        search_row = QWidget()
        search_row.setFixedHeight(34)
        search_row.setStyleSheet(
            f"background:{self.c_bg_alt}; border-bottom:1px solid {self.c_border};"
        )
        sr_layout = QHBoxLayout(search_row)
        sr_layout.setContentsMargins(8, 4, 8, 4)
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("Search installed plugins…")
        self.search_bar.textChanged.connect(self._on_search)
        sr_layout.addWidget(self.search_bar)
        layout.addWidget(search_row)

        # Horizontal split: table | details
        h_split = QSplitter(Qt.Horizontal)
        h_split.setHandleWidth(1)
        h_split.setChildrenCollapsible(False)
        layout.addWidget(h_split, 1)

        # ── Plugin table (left)
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(0)

        self.plugin_table = QTableWidget(0, 3)
        self.plugin_table.setHorizontalHeaderLabels(["", "Plugin Name", "Version"])
        self.plugin_table.verticalHeader().setVisible(False)
        self.plugin_table.verticalHeader().setDefaultSectionSize(22)
        self.plugin_table.horizontalHeader().setFixedHeight(22)
        self.plugin_table.setAlternatingRowColors(True)
        self.plugin_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.plugin_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.plugin_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.plugin_table.setShowGrid(False)
        self.plugin_table.setColumnWidth(0, 24)
        self.plugin_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.plugin_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.plugin_table.setColumnWidth(2, 62)
        self.plugin_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Fixed)
        self.plugin_table.itemChanged.connect(self._on_check_toggled)
        self.plugin_table.currentItemChanged.connect(self._on_selection_changed)

        table_layout.addWidget(self.plugin_table)

        # Bottom: Upgrade All
        upgrade_bar = QHBoxLayout()
        upgrade_bar.setContentsMargins(6, 5, 6, 5)
        self.upgrade_all_btn = QPushButton("Upgrade All")
        self.upgrade_all_btn.setFixedWidth(90)
        upgrade_bar.addWidget(self.upgrade_all_btn)
        upgrade_bar.addStretch()
        table_layout.addWidget(self._make_separator())
        table_layout.addLayout(upgrade_bar)

        h_split.addWidget(table_widget)

        # ── Details pane (right)
        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.setSpacing(0)
        details_widget.setStyleSheet(
            f"background:{self.c_bg}; border-left:1px solid {self.c_border};"
        )

        self.details_browser = QTextBrowser()
        self.details_browser.setOpenExternalLinks(True)
        self.details_browser.setFrameShape(QFrame.NoFrame)
        self.details_browser.setStyleSheet(
            f"background:{self.c_bg}; color:{self.c_text};"
            "border:none; padding:14px 16px; font-size:9.5pt;"
        )
        details_layout.addWidget(self.details_browser, 1)

        # Bottom: Uninstall / Reinstall
        action_bar = QHBoxLayout()
        action_bar.setContentsMargins(8, 5, 8, 5)
        action_bar.addStretch()
        self.uninstall_btn = QPushButton("Uninstall Plugin")
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        self.uninstall_btn.setEnabled(False)
        self.uninstall_btn.setProperty("danger", True)
        self.reinstall_btn = QPushButton("Reinstall Plugin")
        self.reinstall_btn.setEnabled(False)
        action_bar.addWidget(self.uninstall_btn)
        action_bar.addWidget(self.reinstall_btn)
        details_layout.addWidget(self._make_separator())
        details_layout.addLayout(action_bar)

        h_split.addWidget(details_widget)
        h_split.setSizes([280, 440])

        return page

    # ──────────────────────────────────────────────────────────────────
    def _build_zip_page(self):
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(16, 16, 16, 16)
        page_layout.setSpacing(10)

        box = QGroupBox("Install Plugin from ZIP")
        form = QFormLayout(box)
        form.setContentsMargins(12, 16, 12, 12)
        form.setSpacing(8)

        hint = QLabel(
            "Select a plugin ZIP package to install. "
            "It must contain a valid <b>manifest.json</b> file."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{self.c_text_muted}; font-size:9pt; margin-bottom:4px;")
        form.addRow(hint)

        zip_row = QHBoxLayout()
        zip_row.setSpacing(4)
        self.zip_line_edit = QLineEdit()
        self.zip_line_edit.setPlaceholderText("Path to plugin ZIP file…")
        zip_row.addWidget(self.zip_line_edit)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._on_browse_zip)
        zip_row.addWidget(browse_btn)
        form.addRow("ZIP file:", zip_row)

        page_layout.addWidget(box)

        install_row = QHBoxLayout()
        install_row.addStretch()
        install_btn = QPushButton("Install Plugin")
        install_btn.setFixedWidth(130)
        install_btn.clicked.connect(self._on_install_zip)
        install_row.addWidget(install_btn)
        install_row.addStretch()
        page_layout.addLayout(install_row)
        page_layout.addStretch()

        return page

    # ──────────────────────────────────────────────────────────────────
    def _make_separator(self):
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color:{self.c_border}; background:{self.c_border}; max-height:1px;")
        return line

    # ──────────────────────────────────────────────────────────────────
    def _apply_global_style(self):
        self.setStyleSheet(f"""
            QDialog {{
                background:{self.c_bg};
            }}
            QTableWidget {{
                background:{self.c_bg};
                alternate-background-color:{self.c_bg_alt};
                color:{self.c_text};
                gridline-color:transparent;
                font-size:9pt;
                border:none;
                outline:none;
            }}
            QTableWidget::item {{
                padding:2px 4px;
                border:none;
            }}
            QTableWidget::item:selected {{
                background:{self.c_row_sel};
                color:{self.c_text};
            }}
            QHeaderView::section {{
                background:{self.c_bg_alt};
                color:{self.c_text_muted};
                border:none;
                border-bottom:1px solid {self.c_border};
                border-right:1px solid {self.c_border};
                padding:2px 4px;
                font-size:8.5pt;
                font-weight:bold;
            }}
            QLineEdit {{
                font-size:9pt;
                min-height:22px;
                max-height:22px;
                padding:2px 6px;
                border:1px solid {self.c_border};
                border-radius:3px;
                background:{self.c_bg};
                color:{self.c_text};
            }}
            QLineEdit:focus {{
                border-color:{self.c_accent};
            }}
            QPushButton {{
                font-size:9pt;
                padding:3px 12px;
                min-height:22px;
                max-height:22px;
                border:1px solid {self.c_border};
                border-radius:3px;
                background:{self.c_bg_alt};
                color:{self.c_text};
            }}
            QPushButton:hover {{
                background:{self.c_border};
            }}
            QPushButton:disabled {{
                color:{self.c_text_muted};
                background:{self.c_bg_alt};
            }}
            QPushButton[danger="true"]:hover {{
                background:#fee2e2;
                color:#991b1b;
                border-color:#ef4444;
            }}
            QGroupBox {{
                font-size:9.5pt;
                font-weight:bold;
                border:1px solid {self.c_border};
                border-radius:4px;
                margin-top:10px;
                color:{self.c_text};
            }}
            QGroupBox::title {{
                subcontrol-origin:margin;
                left:10px;
                padding:0 4px;
            }}
            QSplitter::handle {{
                background:{self.c_border};
            }}
            QScrollBar:vertical {{
                width:8px;
                background:{self.c_bg_alt};
            }}
            QScrollBar::handle:vertical {{
                background:{self.c_border};
                border-radius:4px;
                min-height:30px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height:0;
            }}
        """)

    # ──────────────────────────────────────────────────────────────────
    def _refresh(self):
        self.plugin_table.blockSignals(True)
        prev = self._selected_plugin_name
        self.plugin_table.setRowCount(0)

        self.discovered = self.pm.discover_plugins()
        target_row = -1

        for manifest, plugin_dir in self.discovered:
            name    = manifest.get("name", "Unknown")
            version = manifest.get("version", "—")
            active  = name in self.pm.loaded_plugins

            row = self.plugin_table.rowCount()
            self.plugin_table.insertRow(row)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if active else Qt.Unchecked)
            chk.setData(Qt.UserRole, name)
            self.plugin_table.setItem(row, 0, chk)

            name_item = QTableWidgetItem(name)
            name_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            name_item.setData(Qt.UserRole, name)
            self.plugin_table.setItem(row, 1, name_item)

            ver_item = QTableWidgetItem(version)
            ver_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            ver_item.setTextAlignment(Qt.AlignCenter)
            self.plugin_table.setItem(row, 2, ver_item)

            if name == prev:
                target_row = row

        self.plugin_table.blockSignals(False)

        if target_row >= 0:
            self.plugin_table.setCurrentCell(target_row, 1)
        elif self.plugin_table.rowCount() > 0:
            self.plugin_table.setCurrentCell(0, 1)
        else:
            self._clear_details()

    # ──────────────────────────────────────────────────────────────────
    def _clear_details(self):
        self.details_browser.setHtml(
            f'<p style="color:{self.c_text_muted}; font-style:italic; padding:8px;">'
            'Click on a plugin name to see details.</p>'
        )
        self.uninstall_btn.setEnabled(False)
        self.reinstall_btn.setEnabled(False)
        self._selected_plugin_name = None

    def _get_selected_name(self):
        row = self.plugin_table.currentRow()
        if row < 0:
            return None
        item = self.plugin_table.item(row, 1)
        return item.data(Qt.UserRole) if item else None

    # ──────────────────────────────────────────────────────────────────
    def _on_selection_changed(self, current=None, previous=None):
        name = self._get_selected_name()
        if not name:
            self._clear_details()
            return

        self._selected_plugin_name = name
        self.uninstall_btn.setEnabled(True)
        self.reinstall_btn.setEnabled(True)

        for manifest, pdir in self.discovered:
            if manifest.get("name") != name:
                continue

            version     = manifest.get("version", "—")
            author      = manifest.get("author", "Unknown")
            description = manifest.get("description", "No description provided.")
            tags        = manifest.get("tags", [])
            homepage    = manifest.get("homepage", "")
            tracker     = manifest.get("tracker", "")
            repo        = manifest.get("repository", "")

            # Build tags HTML
            if isinstance(tags, list):
                tags_html = ", ".join(
                    f'<a href="#" style="color:{self.c_link};">{t}</a>' for t in tags
                ) or "—"
            else:
                tags_html = f'<a href="#" style="color:{self.c_link};">{tags}</a>'

            # More info links
            links = []
            if homepage: links.append(f'<a href="{homepage}" style="color:{self.c_link};">homepage</a>')
            if tracker:  links.append(f'<a href="{tracker}" style="color:{self.c_link};">bug tracker</a>')
            if repo:     links.append(f'<a href="{repo}" style="color:{self.c_link};">code repository</a>')
            links_html = "&nbsp;&nbsp;".join(links) if links else "—"

            author_html = f'<a href="#" style="color:{self.c_link};">{author}</a>'

            # Description — if it has bullet lines starting with "-" split them
            desc_parts = description.split("\n")
            main_desc  = []
            bullets    = []
            in_bullets = False
            for line in desc_parts:
                ls = line.strip()
                if ls.startswith("- "):
                    in_bullets = True
                    bullets.append(f"<li>{ls[2:]}</li>")
                elif ls:
                    if in_bullets:
                        bullets.append(f"<li>{ls}</li>")
                    else:
                        main_desc.append(ls)

            main_html = "<br>".join(main_desc)
            bullet_html = (
                f'<ul style="margin:6px 0 8px 18px; padding:0; line-height:1.6;">'
                f'{"".join(bullets)}</ul>'
            ) if bullets else ""

            html = f"""
            <html><body style="
                font-family: sans-serif;
                font-size: 9.5pt;
                color: {self.c_text};
                background: {self.c_bg};
                margin: 0;
                padding: 0;
            ">
            <h2 style="
                font-size: 16pt;
                font-weight: bold;
                color: {self.c_text};
                margin: 0 0 8px 0;
            ">{name}</h2>

            <p style="
                font-size: 9.5pt;
                font-weight: bold;
                color: {self.c_text};
                margin: 0 0 6px 0;
                line-height: 1.5;
            ">{main_html}</p>

            {bullet_html}

            <table style="
                width: 100%;
                border-collapse: collapse;
                font-size: 9pt;
                margin-top: 12px;
            ">
              <tr>
                <td style="
                    text-align: right;
                    padding: 3px 10px 3px 0;
                    color: {self.c_text_muted};
                    font-weight: bold;
                    width: 130px;
                    vertical-align: top;
                ">Tags</td>
                <td style="padding:3px 0; color:{self.c_text}; line-height:1.6;">{tags_html}</td>
              </tr>
              <tr>
                <td style="
                    text-align: right;
                    padding: 3px 10px 3px 0;
                    color: {self.c_text_muted};
                    font-weight: bold;
                    vertical-align: top;
                ">More info</td>
                <td style="padding:3px 0;">{links_html}</td>
              </tr>
              <tr>
                <td style="
                    text-align: right;
                    padding: 3px 10px 3px 0;
                    color: {self.c_text_muted};
                    font-weight: bold;
                ">Author</td>
                <td style="padding:3px 0;">{author_html}</td>
              </tr>
              <tr>
                <td style="
                    text-align: right;
                    padding: 3px 10px 3px 0;
                    color: {self.c_text_muted};
                    font-weight: bold;
                ">Installed version</td>
                <td style="padding:3px 0; color:{self.c_link}; font-weight:bold;">{version}</td>
              </tr>
            </table>
            </body></html>
            """
            self.details_browser.setHtml(html)
            break

    # ──────────────────────────────────────────────────────────────────
    def _on_check_toggled(self, item):
        if item.column() != 0:
            return
        name       = item.data(Qt.UserRole)
        checked    = item.checkState() == Qt.Checked
        is_loaded  = name in self.pm.loaded_plugins

        if checked and not is_loaded:
            for manifest, pdir in self.discovered:
                if manifest.get("name") == name:
                    ok, msg = self.pm.load_plugin(manifest, pdir)
                    if not ok:
                        QMessageBox.critical(self, "Error", f"Failed to load '{name}':\n{msg}")
                        self.plugin_table.blockSignals(True)
                        item.setCheckState(Qt.Unchecked)
                        self.plugin_table.blockSignals(False)
                    break
        elif not checked and is_loaded:
            ok, msg = self.pm.unload_plugin(name)
            if not ok:
                QMessageBox.critical(self, "Error", f"Failed to unload '{name}':\n{msg}")
                self.plugin_table.blockSignals(True)
                item.setCheckState(Qt.Checked)
                self.plugin_table.blockSignals(False)

    def _on_search(self, text):
        term = text.strip().lower()
        for row in range(self.plugin_table.rowCount()):
            item = self.plugin_table.item(row, 1)
            name = (item.data(Qt.UserRole) or "").lower() if item else ""
            self.plugin_table.setRowHidden(row, bool(term) and term not in name)

    def _on_uninstall(self):
        name = self._get_selected_name()
        if not name:
            return
        ans = QMessageBox.question(
            self, "Uninstall Plugin",
            f"Are you sure you want to permanently uninstall '{name}'?\n"
            "This will delete its files from disk and cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if ans == QMessageBox.Yes:
            ok, msg = self.pm.uninstall_plugin(name)
            if ok:
                QMessageBox.information(self, "Success", msg)
                self._refresh()
            else:
                QMessageBox.critical(self, "Error", msg)

    # ──────────────────────────────────────────────────────────────────
    def _on_browse_zip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Plugin ZIP Archive", "", "ZIP Archives (*.zip)"
        )
        if path:
            self.zip_line_edit.setText(path)

    def _on_install_zip(self):
        zip_path = self.zip_line_edit.text().strip()
        if not zip_path:
            QMessageBox.warning(self, "Warning", "Please select a ZIP file first.")
            return

        ok, msg = self.pm.install_plugin_from_zip(Path(zip_path))
        if ok:
            QMessageBox.information(self, "Success", msg)
            self.zip_line_edit.clear()
            self.category_list.setCurrentRow(0)
            self._refresh()
            # Auto-select the newly installed plugin
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    for item_name in zf.namelist():
                        if item_name.endswith("manifest.json"):
                            manifest = json.loads(zf.read(item_name).decode("utf-8"))
                            new_name = manifest.get("name")
                            for r in range(self.plugin_table.rowCount()):
                                it = self.plugin_table.item(r, 1)
                                if it and it.data(Qt.UserRole) == new_name:
                                    self.plugin_table.setCurrentCell(r, 1)
                                    break
                            break
            except Exception:
                pass
        else:
            QMessageBox.critical(self, "Error", msg)