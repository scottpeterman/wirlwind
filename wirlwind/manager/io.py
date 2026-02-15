"""
Session import/export functionality.

Supports JSON format for portability.
Also supports importing from TerminalTelemetry YAML format.
Also supports simple CSV import for quick session lists.
"""

from __future__ import annotations
import csv
import json
import yaml
from io import StringIO
from pathlib import Path
from datetime import datetime
from typing import Optional
from dataclasses import asdict

from PyQt6.QtWidgets import (
    QWidget, QFileDialog, QMessageBox, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QPushButton, QDialogButtonBox, QTreeWidget,
    QTreeWidgetItem, QGroupBox, QComboBox, QTextEdit
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from .models import SessionStore, SavedSession, SessionFolder


# Export format version for future compatibility
EXPORT_VERSION = 1


def export_sessions(
    store: SessionStore,
    path: Path,
    include_stats: bool = False
) -> int:
    """
    Export all sessions to JSON file.

    Args:
        store: Session store instance
        path: Output file path
        include_stats: Include connect_count and last_connected

    Returns:
        Number of sessions exported
    """
    tree_data = store.get_tree()

    # Build export structure
    export_data = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now().isoformat(),
        "folders": [],
        "sessions": [],
    }

    # Export folders
    for folder in tree_data["folders"]:
        export_data["folders"].append({
            "id": folder.id,
            "name": folder.name,
            "parent_id": folder.parent_id,
            "position": folder.position,
        })

    # Export sessions
    for session in tree_data["sessions"]:
        session_data = {
            "name": session.name,
            "description": session.description,
            "hostname": session.hostname,
            "port": session.port,
            "credential_name": session.credential_name,
            "folder_id": session.folder_id,
            "position": session.position,
        }

        if session.extras:
            session_data["extras"] = session.extras

        if include_stats:
            session_data["connect_count"] = session.connect_count
            if session.last_connected:
                session_data["last_connected"] = str(session.last_connected)

        export_data["sessions"].append(session_data)

    # Write file
    with open(path, "w") as f:
        json.dump(export_data, f, indent=2)

    return len(export_data["sessions"])


def import_sessions(
    store: SessionStore,
    path: Path,
    merge: bool = True
) -> tuple[int, int]:
    """
    Import sessions from JSON file.

    Args:
        store: Session store instance
        path: Input file path
        merge: If True, merge with existing. If False, skip duplicates.

    Returns:
        Tuple of (sessions_imported, sessions_skipped)
    """
    with open(path) as f:
        data = json.load(f)

    version = data.get("version", 1)

    # Build folder ID mapping (old ID -> new ID)
    folder_map: dict[int, int] = {}

    # Import folders first
    if "folders" in data:
        # Sort by parent to ensure parents are created first
        folders = sorted(data["folders"], key=lambda f: (f.get("parent_id") or 0, f.get("position", 0)))

        for folder_data in folders:
            old_id = folder_data.get("id")
            parent_id = folder_data.get("parent_id")

            # Map parent ID if it was imported
            if parent_id and parent_id in folder_map:
                parent_id = folder_map[parent_id]
            elif parent_id:
                parent_id = None  # Parent not found, put at root

            # Check if folder with same name exists at same level
            existing = store.list_folders(parent_id)
            existing_folder = next(
                (f for f in existing if f.name == folder_data["name"]),
                None
            )

            if existing_folder:
                folder_map[old_id] = existing_folder.id
            else:
                new_id = store.add_folder(folder_data["name"], parent_id)
                folder_map[old_id] = new_id

    # Import sessions
    imported = 0
    skipped = 0

    existing_sessions = {s.hostname: s for s in store.list_all_sessions()}

    for session_data in data.get("sessions", []):
        hostname = session_data.get("hostname")
        name = session_data.get("name")

        # Check for duplicate by hostname
        if hostname in existing_sessions and not merge:
            skipped += 1
            continue

        # Map folder ID
        folder_id = session_data.get("folder_id")
        if folder_id and folder_id in folder_map:
            folder_id = folder_map[folder_id]
        else:
            folder_id = None

        session = SavedSession(
            name=name or hostname,
            description=session_data.get("description", ""),
            hostname=hostname,
            port=session_data.get("port", 22),
            credential_name=session_data.get("credential_name"),
            folder_id=folder_id,
            extras=session_data.get("extras", {}),
        )

        # Check if we're updating existing
        if hostname in existing_sessions and merge:
            existing = existing_sessions[hostname]
            session.id = existing.id
            store.update_session(session)
        else:
            store.add_session(session)

        imported += 1

    return imported, skipped


def import_sessions_csv(
    store: SessionStore,
    path: Path,
    merge: bool = True,
    folder_name: Optional[str] = None
) -> tuple[int, int, int]:
    """
    Import sessions from CSV file.

    Supports flexible column names:
    - name/display_name/hostname → session name
    - hostname/host/ip/address → connection hostname
    - port → port (default 22)
    - description/desc → description
    - folder/folder_name/group → folder assignment

    Args:
        store: Session store instance
        path: Input CSV file path
        merge: If True, merge with existing. If False, skip duplicates.
        folder_name: Override folder for all imported sessions (optional)

    Returns:
        Tuple of (folders_created, sessions_imported, sessions_skipped)
    """
    with open(path, newline='', encoding='utf-8-sig') as f:
        # Sniff dialect and read
        sample = f.read(4096)
        f.seek(0)

        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel  # Default to standard CSV

        reader = csv.DictReader(f, dialect=dialect)

        # Normalize header names (lowercase, strip whitespace)
        if reader.fieldnames:
            reader.fieldnames = [h.lower().strip() for h in reader.fieldnames]

        rows = list(reader)

    if not rows:
        return 0, 0, 0

    # Column name mappings (first match wins)
    name_cols = ['name', 'display_name', 'session_name', 'device_name', 'device']
    host_cols = ['hostname', 'host', 'ip', 'ip_address', 'address', 'mgmt_ip']
    port_cols = ['port', 'ssh_port']
    desc_cols = ['description', 'desc', 'notes', 'comment']
    folder_cols = ['folder', 'folder_name', 'group', 'site', 'location']

    def find_col(row: dict, candidates: list[str]) -> Optional[str]:
        """Find first matching column value."""
        for col in candidates:
            if col in row and row[col]:
                return row[col].strip()
        return None

    # Track folders and sessions
    folders_created = 0
    sessions_imported = 0
    sessions_skipped = 0

    existing_sessions = {s.hostname: s for s in store.list_all_sessions()}
    folder_cache: dict[str, int] = {}  # folder_name -> folder_id

    for row in rows:
        # Extract fields with fallbacks
        hostname = find_col(row, host_cols)
        if not hostname:
            continue  # Skip rows without hostname

        name = find_col(row, name_cols) or hostname
        port_str = find_col(row, port_cols)
        port = int(port_str) if port_str and port_str.isdigit() else 22
        description = find_col(row, desc_cols) or ""

        # Determine folder
        row_folder = folder_name or find_col(row, folder_cols)
        folder_id = None

        if row_folder:
            if row_folder in folder_cache:
                folder_id = folder_cache[row_folder]
            else:
                # Check if folder exists
                existing_folders = store.list_folders(None)
                existing = next((f for f in existing_folders if f.name == row_folder), None)

                if existing:
                    folder_id = existing.id
                else:
                    folder_id = store.add_folder(row_folder)
                    folders_created += 1

                folder_cache[row_folder] = folder_id

        # Check for duplicate
        if hostname in existing_sessions and not merge:
            sessions_skipped += 1
            continue

        session = SavedSession(
            name=name,
            description=description,
            hostname=hostname,
            port=port,
            credential_name=None,
            folder_id=folder_id,
        )

        # Update or insert
        if hostname in existing_sessions and merge:
            existing = existing_sessions[hostname]
            session.id = existing.id
            store.update_session(session)
        else:
            store.add_session(session)
            existing_sessions[hostname] = session

        sessions_imported += 1

    return folders_created, sessions_imported, sessions_skipped


def import_terminal_telemetry(
    store: SessionStore,
    path: Path,
    merge: bool = True
) -> tuple[int, int, int]:
    """
    Import sessions from TerminalTelemetry YAML format.

    Args:
        store: Session store instance
        path: Path to TerminalTelemetry sessions.yaml
        merge: If True, merge with existing. If False, skip duplicates.

    Returns:
        Tuple of (folders_created, sessions_imported, sessions_skipped)
    """
    with open(path) as f:
        data = yaml.safe_load(f)

    if not isinstance(data, list):
        raise ValueError("Invalid TerminalTelemetry format: expected list of folders")

    folders_created = 0
    sessions_imported = 0
    sessions_skipped = 0

    existing_sessions = {s.hostname: s for s in store.list_all_sessions()}

    for folder_entry in data:
        folder_name = folder_entry.get("folder_name", "Imported")
        sessions = folder_entry.get("sessions", [])

        if not sessions:
            continue  # Skip empty folders

        # Find or create folder
        existing_folders = store.list_folders(None)  # Root level
        folder = next((f for f in existing_folders if f.name == folder_name), None)

        if not folder:
            folder_id = store.add_folder(folder_name)
            folders_created += 1
        else:
            folder_id = folder.id

        # Import sessions in this folder
        for sess in sessions:
            hostname = sess.get("host", "")
            if not hostname:
                continue

            # Check for duplicate
            if hostname in existing_sessions and not merge:
                sessions_skipped += 1
                continue

            # Build description from DeviceType and Model
            device_type = sess.get("DeviceType", "")
            model = sess.get("Model", "")
            vendor = sess.get("Vendor", "")

            desc_parts = []
            if device_type:
                desc_parts.append(device_type)
            if model:
                desc_parts.append(model)
            description = " - ".join(desc_parts) if desc_parts else ""

            # Store extra metadata
            extras = {}
            if vendor:
                extras["vendor"] = vendor
            if device_type:
                extras["device_type"] = device_type
            if model:
                extras["model"] = model

            session = SavedSession(
                name=sess.get("display_name", hostname),
                description=description,
                hostname=hostname,
                port=int(sess.get("port", 22)),
                credential_name=None,  # Use agent auth by default
                folder_id=folder_id,
                extras=extras,
            )

            # Check if updating existing
            if hostname in existing_sessions and merge:
                existing = existing_sessions[hostname]
                session.id = existing.id
                store.update_session(session)
            else:
                store.add_session(session)
                existing_sessions[hostname] = session  # Track for duplicates

            sessions_imported += 1

    return folders_created, sessions_imported, sessions_skipped


class ExportDialog(QDialog):
    """Dialog for export options."""

    def __init__(self, store: SessionStore, parent: QWidget = None):
        super().__init__(parent)
        self.store = store

        self.setWindowTitle("Export Sessions")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # Info
        tree_data = store.get_tree()
        count = len(tree_data["sessions"])
        folder_count = len(tree_data["folders"])

        info = QLabel(f"Export {count} sessions and {folder_count} folders to JSON file.")
        layout.addWidget(info)

        # Options
        options_group = QGroupBox("Options")
        options_layout = QVBoxLayout(options_group)

        self._include_stats = QCheckBox("Include connection statistics")
        self._include_stats.setToolTip("Export connect count and last connected timestamp")
        options_layout.addWidget(self._include_stats)

        layout.addWidget(options_group)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save |
            QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save(self) -> None:
        """Handle save button."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Sessions",
            "wirlwind_sessions.json",
            "JSON Files (*.json)"
        )

        if path:
            try:
                count = export_sessions(
                    self.store,
                    Path(path),
                    include_stats=self._include_stats.isChecked()
                )
                QMessageBox.information(
                    self,
                    "Export Complete",
                    f"Exported {count} sessions to:\n{path}"
                )
                self.accept()
            except Exception as e:
                QMessageBox.critical(
                    self,
                    "Export Failed",
                    f"Failed to export sessions:\n{e}"
                )


# =============================================================================
# Format Help Text
# =============================================================================

CSV_HELP_TEXT = """\
<b>CSV Format</b><br><br>
Simple comma-separated format for quick imports from spreadsheets or other tools.<br><br>

<b>Supported Columns:</b>
<table cellspacing="4">
<tr><td><code>name</code></td><td>Session display name (falls back to hostname)</td></tr>
<tr><td><code>hostname</code></td><td><b>Required.</b> IP address or DNS name</td></tr>
<tr><td><code>port</code></td><td>SSH port (default: 22)</td></tr>
<tr><td><code>description</code></td><td>Optional notes</td></tr>
<tr><td><code>folder</code></td><td>Folder name (created if missing)</td></tr>
</table>
<br>
<b>Example:</b><br>
<code>name,hostname,port,folder<br>
core-rtr-01,10.0.0.1,22,Core<br>
core-rtr-02,10.0.0.2,22,Core<br>
edge-sw-01,10.1.0.1,22,Edge</code><br><br>

<i>Column names are flexible: "host", "ip", "address" also work for hostname.</i>
"""

JSON_HELP_TEXT = """\
<b>JSON Format</b><br><br>
Native wirlwind export format. Preserves folders, hierarchy, and all session metadata.<br><br>

<b>Structure:</b><br>
<code>{<br>
&nbsp;&nbsp;"version": 1,<br>
&nbsp;&nbsp;"folders": [{"id": 1, "name": "Site A", ...}],<br>
&nbsp;&nbsp;"sessions": [<br>
&nbsp;&nbsp;&nbsp;&nbsp;{"name": "router-01", "hostname": "10.0.0.1", "port": 22, "folder_id": 1}<br>
&nbsp;&nbsp;]<br>
}</code><br><br>

<b>Tip:</b> Use <i>Export Sessions</i> to create a template, then edit and re-import.
"""


class ImportDialog(QDialog):
    """Dialog for import options and preview."""

    def __init__(self, store: SessionStore, parent: QWidget = None):
        super().__init__(parent)
        self.store = store
        self._import_path: Optional[Path] = None
        self._import_data = None  # Can be dict (JSON) or list of rows (CSV)
        self._import_format: str = "json"

        self.setWindowTitle("Import Sessions")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)

        layout = QVBoxLayout(self)

        # Format selection row
        format_row = QHBoxLayout()
        format_row.addWidget(QLabel("Format:"))

        self._format_combo = QComboBox()
        self._format_combo.addItem("JSON (wirlwind native)", "json")
        self._format_combo.addItem("CSV (spreadsheet)", "csv")
        self._format_combo.currentIndexChanged.connect(self._on_format_changed)
        self._format_combo.setMinimumWidth(180)
        format_row.addWidget(self._format_combo)

        format_row.addStretch()

        # Help toggle
        self._help_btn = QPushButton("? Help")
        self._help_btn.setCheckable(True)
        self._help_btn.setMaximumWidth(80)
        self._help_btn.toggled.connect(self._toggle_help)
        format_row.addWidget(self._help_btn)

        layout.addLayout(format_row)

        # Help panel (hidden by default)
        self._help_panel = QTextEdit()
        self._help_panel.setReadOnly(True)
        self._help_panel.setMaximumHeight(180)
        self._help_panel.setHtml(JSON_HELP_TEXT)
        self._help_panel.hide()
        layout.addWidget(self._help_panel)

        # File selection
        file_row = QHBoxLayout()
        self._file_label = QLabel("No file selected")
        file_row.addWidget(self._file_label, 1)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(browse_btn)

        layout.addLayout(file_row)

        # Preview tree
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)

        self._preview_tree = QTreeWidget()
        self._preview_tree.setHeaderLabels(["Name", "Host", "Port"])
        self._preview_tree.setRootIsDecorated(True)
        self._preview_tree.setAlternatingRowColors(True)
        preview_layout.addWidget(self._preview_tree)

        layout.addWidget(preview_group)

        # Options
        options_group = QGroupBox("Import Options")
        options_layout = QVBoxLayout(options_group)

        self._merge_check = QCheckBox("Merge with existing (update duplicates)")
        self._merge_check.setChecked(True)
        self._merge_check.setToolTip(
            "If checked, sessions with matching hostnames will be updated.\n"
            "If unchecked, duplicates will be skipped."
        )
        options_layout.addWidget(self._merge_check)

        layout.addWidget(options_group)

        # Buttons
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.accepted.connect(self._on_import)
        self._button_box.rejected.connect(self.reject)
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        layout.addWidget(self._button_box)

    def _on_format_changed(self, index: int) -> None:
        """Handle format selection change."""
        self._import_format = self._format_combo.currentData()

        # Update help text
        if self._import_format == "csv":
            self._help_panel.setHtml(CSV_HELP_TEXT)
        else:
            self._help_panel.setHtml(JSON_HELP_TEXT)

        # Clear preview if format changed after file loaded
        if self._import_path:
            self._preview_tree.clear()
            self._import_path = None
            self._import_data = None
            self._file_label.setText("No file selected")
            self._button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _toggle_help(self, show: bool) -> None:
        """Show/hide help panel."""
        self._help_panel.setVisible(show)
        self._help_btn.setText("▼ Help" if show else "? Help")

    def _browse_file(self) -> None:
        """Browse for import file."""
        if self._import_format == "csv":
            filter_str = "CSV Files (*.csv);;All Files (*)"
        else:
            filter_str = "JSON Files (*.json);;All Files (*)"

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Sessions",
            "",
            filter_str
        )

        if path:
            self._load_preview(Path(path))

    def _load_preview(self, path: Path) -> None:
        """Load and preview import file."""
        try:
            self._preview_tree.clear()

            if self._import_format == "csv":
                self._load_csv_preview(path)
            else:
                self._load_json_preview(path)

            self._import_path = path
            self._file_label.setText(path.name)

            self._preview_tree.expandAll()
            for i in range(3):
                self._preview_tree.resizeColumnToContents(i)

            # Enable import button
            self._button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

        except Exception as e:
            QMessageBox.critical(
                self,
                "Load Failed",
                f"Failed to load file:\n{e}"
            )

    def _load_csv_preview(self, path: Path) -> None:
        """Load CSV file and populate preview."""
        with open(path, newline='', encoding='utf-8-sig') as f:
            sample = f.read(4096)
            f.seek(0)

            try:
                dialect = csv.Sniffer().sniff(sample)
            except csv.Error:
                dialect = csv.excel

            reader = csv.DictReader(f, dialect=dialect)
            if reader.fieldnames:
                reader.fieldnames = [h.lower().strip() for h in reader.fieldnames]

            rows = list(reader)

        self._import_data = rows

        # Column mappings
        name_cols = ['name', 'display_name', 'session_name', 'device_name', 'device']
        host_cols = ['hostname', 'host', 'ip', 'ip_address', 'address', 'mgmt_ip']
        port_cols = ['port', 'ssh_port']
        folder_cols = ['folder', 'folder_name', 'group', 'site', 'location']

        def find_col(row: dict, candidates: list[str]) -> Optional[str]:
            for col in candidates:
                if col in row and row[col]:
                    return row[col].strip()
            return None

        # Group by folder for preview
        folder_items: dict[str, QTreeWidgetItem] = {}
        root_sessions: list[QTreeWidgetItem] = []

        for row in rows:
            hostname = find_col(row, host_cols)
            if not hostname:
                continue

            name = find_col(row, name_cols) or hostname
            port = find_col(row, port_cols) or "22"
            folder = find_col(row, folder_cols)

            item = QTreeWidgetItem()
            item.setText(0, name)
            item.setText(1, hostname)
            item.setText(2, port)

            if folder:
                if folder not in folder_items:
                    folder_item = QTreeWidgetItem()
                    folder_item.setText(0, f"📁 {folder}")
                    self._preview_tree.addTopLevelItem(folder_item)
                    folder_items[folder] = folder_item
                folder_items[folder].addChild(item)
            else:
                root_sessions.append(item)

        # Add ungrouped sessions at root
        for item in root_sessions:
            self._preview_tree.addTopLevelItem(item)

    def _load_json_preview(self, path: Path) -> None:
        """Load JSON file and populate preview."""
        with open(path) as f:
            data = json.load(f)

        self._import_data = data

        # Create folder items
        folder_items: dict[int, QTreeWidgetItem] = {}
        for folder_data in data.get("folders", []):
            item = QTreeWidgetItem()
            item.setText(0, f"📁 {folder_data['name']}")
            folder_items[folder_data["id"]] = item

        # Parent folders
        for folder_data in data.get("folders", []):
            item = folder_items[folder_data["id"]]
            parent_id = folder_data.get("parent_id")
            if parent_id and parent_id in folder_items:
                folder_items[parent_id].addChild(item)
            else:
                self._preview_tree.addTopLevelItem(item)

        # Add sessions
        for session_data in data.get("sessions", []):
            item = QTreeWidgetItem()
            item.setText(0, session_data.get("name", ""))
            item.setText(1, session_data.get("hostname", ""))
            item.setText(2, str(session_data.get("port", 22)))

            folder_id = session_data.get("folder_id")
            if folder_id and folder_id in folder_items:
                folder_items[folder_id].addChild(item)
            else:
                self._preview_tree.addTopLevelItem(item)

    def _on_import(self) -> None:
        """Perform import."""
        if not self._import_path:
            return

        try:
            if self._import_format == "csv":
                folders, imported, skipped = import_sessions_csv(
                    self.store,
                    self._import_path,
                    merge=self._merge_check.isChecked()
                )
                msg = f"Created {folders} folders.\nImported {imported} sessions."
            else:
                imported, skipped = import_sessions(
                    self.store,
                    self._import_path,
                    merge=self._merge_check.isChecked()
                )
                msg = f"Imported {imported} sessions."

            if skipped:
                msg += f"\nSkipped {skipped} duplicates."

            QMessageBox.information(self, "Import Complete", msg)
            self.accept()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Import Failed",
                f"Failed to import sessions:\n{e}"
            )


class ImportTerminalTelemetryDialog(QDialog):
    """Dialog for importing TerminalTelemetry sessions.yaml."""

    def __init__(self, store: SessionStore, parent: QWidget = None):
        super().__init__(parent)
        self.store = store
        self._import_path: Optional[Path] = None
        self._import_data: Optional[list] = None

        self.setWindowTitle("Import from TerminalTelemetry")
        self.setMinimumWidth(500)
        self.setMinimumHeight(400)

        layout = QVBoxLayout(self)

        # Info
        info = QLabel(
            "Import sessions from TerminalTelemetry sessions.yaml file.\n"
            "Folders and sessions will be created automatically."
        )
        layout.addWidget(info)

        # File selection
        file_row = QHBoxLayout()
        self._file_label = QLabel("No file selected")
        file_row.addWidget(self._file_label, 1)

        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_file)
        file_row.addWidget(browse_btn)

        layout.addLayout(file_row)

        # Preview tree
        preview_group = QGroupBox("Preview")
        preview_layout = QVBoxLayout(preview_group)

        self._preview_tree = QTreeWidget()
        self._preview_tree.setHeaderLabels(["Name", "Host", "Description"])
        self._preview_tree.setRootIsDecorated(True)
        preview_layout.addWidget(self._preview_tree)

        layout.addWidget(preview_group)

        # Options
        options_group = QGroupBox("Import Options")
        options_layout = QVBoxLayout(options_group)

        self._merge_check = QCheckBox("Merge with existing (update duplicates)")
        self._merge_check.setChecked(True)
        options_layout.addWidget(self._merge_check)

        layout.addWidget(options_group)

        # Buttons
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel
        )
        self._button_box.accepted.connect(self._on_import)
        self._button_box.rejected.connect(self.reject)
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self._button_box.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        layout.addWidget(self._button_box)

    def _browse_file(self) -> None:
        """Browse for sessions.yaml file."""
        # Default to common TerminalTelemetry location
        default_path = Path.home() / ".terminaltelemetry" / "sessions.yaml"
        start_dir = str(default_path.parent) if default_path.parent.exists() else ""

        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select TerminalTelemetry sessions.yaml",
            start_dir,
            "YAML Files (*.yaml *.yml);;All Files (*)"
        )

        if path:
            self._load_preview(Path(path))

    def _load_preview(self, path: Path) -> None:
        """Load and preview the YAML file."""
        try:
            with open(path) as f:
                data = yaml.safe_load(f)

            if not isinstance(data, list):
                raise ValueError("Invalid format: expected list of folders")

            self._import_path = path
            self._import_data = data
            self._file_label.setText(path.name)

            # Build preview tree
            self._preview_tree.clear()

            for folder_entry in data:
                folder_name = folder_entry.get("folder_name", "Unknown")
                sessions = folder_entry.get("sessions", [])

                # Create folder item
                folder_item = QTreeWidgetItem()
                folder_item.setText(0, f"📁 {folder_name}")
                folder_item.setText(1, "")
                folder_item.setText(2, f"{len(sessions)} sessions")
                self._preview_tree.addTopLevelItem(folder_item)

                # Add sessions
                for sess in sessions:
                    item = QTreeWidgetItem()
                    item.setText(0, sess.get("display_name", ""))
                    item.setText(1, sess.get("host", ""))

                    # Build description preview
                    device_type = sess.get("DeviceType", "")
                    model = sess.get("Model", "")
                    desc = f"{device_type} - {model}" if model else device_type
                    item.setText(2, desc)

                    folder_item.addChild(item)

            self._preview_tree.expandAll()
            for i in range(3):
                self._preview_tree.resizeColumnToContents(i)

            # Enable import button
            self._button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)

        except Exception as e:
            QMessageBox.critical(
                self,
                "Load Failed",
                f"Failed to load file:\n{e}"
            )

    def _on_import(self) -> None:
        """Perform import."""
        if not self._import_path:
            return

        try:
            folders, imported, skipped = import_terminal_telemetry(
                self.store,
                self._import_path,
                merge=self._merge_check.isChecked()
            )

            msg = f"Created {folders} folders.\nImported {imported} sessions."
            if skipped:
                msg += f"\nSkipped {skipped} duplicates."

            QMessageBox.information(self, "Import Complete", msg)
            self.accept()

        except Exception as e:
            QMessageBox.critical(
                self,
                "Import Failed",
                f"Failed to import sessions:\n{e}"
            )