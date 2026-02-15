"""
Telemetry Widget - Embeddable PyQt6 widget for device telemetry.

This is the top-level component. Embed it in nterm as a tab/window,
or run it standalone. It manages the poll engine, state store, and
dashboard display.
"""

from __future__ import annotations
import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStatusBar, QFrame
)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import QUrl, Qt

from .auth_interface import AuthProvider, DeviceTarget, SSHCredentials
from .state_store import DeviceStateStore
from .template_loader import TemplateLoader
from .poll_engine import PollEngine
from .bridge import TelemetryBridge

logger = logging.getLogger(__name__)


class TelemetryWidget(QWidget):
    """
    Self-contained telemetry dashboard widget.

    Usage (standalone):
        auth = SimpleAuthProvider("admin", password="cisco")
        target = DeviceTarget("10.0.0.1", vendor="cisco_ios_xe")
        widget = TelemetryWidget(auth_provider=auth)
        widget.start(target)
        widget.show()

    Usage (nterm integration):
        auth = NtermAuthProvider(credential_resolver)
        widget = TelemetryWidget(auth_provider=auth, parent=tab_widget)
        widget.start(target)
    """

    def __init__(
        self,
        auth_provider: AuthProvider,
        template_dir: str | Path = None,
        legacy_mode: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._auth = auth_provider
        self._template_loader = TemplateLoader(template_dir)
        self._legacy_mode = legacy_mode

        self._poll_engine: Optional[PollEngine] = None
        self._credentials: Optional[SSHCredentials] = None
        self._target: Optional[DeviceTarget] = None

        self._setup_ui()

    def _setup_ui(self):
        """Build the widget UI."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Control bar (minimal — most chrome is in the HTML dashboard)
        self._control_bar = QFrame()
        self._control_bar.setFixedHeight(32)
        self._control_bar.setStyleSheet("""
            QFrame {
                background: #0a0e14;
                border-bottom: 1px solid #1e2a38;
            }
            QLabel {
                color: #6b7d93;
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
            QPushButton {
                background: #151d27;
                border: 1px solid #2a3a4e;
                color: #c8d6e5;
                font-family: 'Consolas', monospace;
                font-size: 11px;
                padding: 2px 12px;
                border-radius: 2px;
            }
            QPushButton:hover {
                background: #1e2a38;
                border-color: #00e5ff;
            }
            QPushButton:pressed {
                background: #0a0e14;
            }
        """)

        bar_layout = QHBoxLayout(self._control_bar)
        bar_layout.setContentsMargins(8, 0, 8, 0)

        self._status_label = QLabel("Disconnected")
        bar_layout.addWidget(self._status_label)

        bar_layout.addStretch()

        self._reconnect_btn = QPushButton("Reconnect")
        self._reconnect_btn.clicked.connect(self._on_reconnect)
        self._reconnect_btn.setVisible(False)
        bar_layout.addWidget(self._reconnect_btn)

        self._stop_btn = QPushButton("Stop")
        self._stop_btn.clicked.connect(self.stop)
        self._stop_btn.setVisible(False)
        bar_layout.addWidget(self._stop_btn)

        layout.addWidget(self._control_bar)

        # Web view for ECharts dashboard
        self._web_view = QWebEngineView()
        self._web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Allow local HTML to load CDN resources (ECharts, fonts)
        settings = self._web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)

        # Set up web channel — register early with a placeholder bridge
        self._channel = QWebChannel()
        self._state_store = DeviceStateStore(self)
        self._bridge = TelemetryBridge(self._state_store, self)
        self._channel.registerObject("telemetry", self._bridge)
        self._web_view.page().setWebChannel(self._channel)

        layout.addWidget(self._web_view)

        # Load the dashboard HTML
        self._load_dashboard()

    def _load_dashboard(self):
        """Load the ECharts dashboard HTML into the web view."""
        dashboard_path = Path(__file__).parent / "dashboard" / "index.html"
        if dashboard_path.exists():
            self._web_view.setUrl(QUrl.fromLocalFile(str(dashboard_path.resolve())))
        else:
            logger.error(f"Dashboard not found: {dashboard_path}")
            self._web_view.setHtml(
                "<html><body style='background:#0a0e14;color:#ff1744;padding:20px;'>"
                "<h2>Dashboard Not Found</h2>"
                f"<p>Expected at: {dashboard_path}</p>"
                "</body></html>"
            )

    # ── Public API ───────────────────────────────────────────────────

    def start(self, target: DeviceTarget, credentials: SSHCredentials = None):
        """
        Start telemetry for a device.

        Args:
            target: Device to monitor
            credentials: Pre-resolved credentials (optional).
                         If not provided, uses the auth provider.
        """
        self._target = target

        # Resolve credentials
        if credentials:
            self._credentials = credentials
        else:
            self._credentials = self._auth.get_credentials(target)

        if not self._credentials:
            self._set_status("Auth failed — no credentials", error=True)
            return

        # Set device info in state store
        self._state_store.set_device_info({
            "hostname": target.display_name or target.hostname,
            "ip": target.hostname,
            "port": target.port,
            "vendor": target.vendor,
            "tags": target.tags,
            "username": self._credentials.username,
        })

        # Notify dashboard of device info
        import json
        self._bridge.deviceInfoChanged.emit(json.dumps(self._state_store.device_info, default=str))

        # Start poll engine
        vendor = target.vendor
        if not vendor:
            self._set_status("No vendor specified — cannot load templates", error=True)
            return

        self._poll_engine = PollEngine(
            credentials=self._credentials,
            state_store=self._state_store,
            template_loader=self._template_loader,
            vendor=vendor,
            legacy_mode=self._legacy_mode,
            parent=self,
        )

        # Connect signals
        self._poll_engine.connected.connect(self._on_connected)
        self._poll_engine.disconnected.connect(self._on_disconnected)
        self._poll_engine.error.connect(self._on_error)
        self._poll_engine.finished.connect(self._on_engine_finished)

        self._set_status(f"Connecting to {self._credentials.display}...")
        self._stop_btn.setVisible(True)
        self._reconnect_btn.setVisible(False)

        self._poll_engine.start()

    def stop(self):
        """Stop telemetry polling."""
        if self._poll_engine and self._poll_engine.isRunning():
            self._set_status("Stopping...")
            self._poll_engine.stop()
            self._poll_engine.wait(5000)  # Wait up to 5 seconds

    def restart(self):
        """Stop and restart telemetry."""
        self.stop()
        if self._target:
            self.start(self._target, self._credentials)

    # ── Signal handlers ──────────────────────────────────────────────

    def _on_connected(self):
        self._set_status(
            f"Connected: {self._credentials.display} · "
            f"Vendor: {self._target.vendor} · "
            f"Polling {len(self._poll_engine.collections)} collections"
        )
        if self._bridge:
            self._bridge.connectionStatus.emit("connected")

    def _on_disconnected(self):
        self._set_status("Disconnected")
        self._stop_btn.setVisible(False)
        self._reconnect_btn.setVisible(True)
        if self._bridge:
            self._bridge.connectionStatus.emit("disconnected")

    def _on_error(self, msg: str):
        self._set_status(msg, error=True)
        self._stop_btn.setVisible(False)
        self._reconnect_btn.setVisible(True)
        if self._bridge:
            self._bridge.connectionStatus.emit(f"error:{msg}")

    def _on_engine_finished(self):
        self._stop_btn.setVisible(False)

    def _on_reconnect(self):
        if self._target:
            self.start(self._target, self._credentials)

    def _set_status(self, text: str, error: bool = False):
        color = "#ff1744" if error else "#6b7d93"
        self._status_label.setStyleSheet(f"color: {color};")
        self._status_label.setText(text)

    # ── Cleanup ──────────────────────────────────────────────────────

    def closeEvent(self, event):
        """Clean up on widget close."""
        self.stop()
        super().closeEvent(event)