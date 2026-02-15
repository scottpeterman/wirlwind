"""
Poll Engine - Synchronous SSH command loop with template-driven parsing.

Uses the SCNG SSHClient for robust device interaction:
- Legacy cipher/KEX support for old devices
- ANSI sequence filtering
- Sophisticated prompt detection
- Shotgun pagination disabling

Runs in a QThread to keep the UI responsive.
"""

from __future__ import annotations
import time
import logging
import traceback
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from .auth_interface import SSHCredentials
from .state_store import DeviceStateStore
from .template_loader import TemplateLoader, Template
from .ssh_client import SSHClient, SSHClientConfig

logger = logging.getLogger(__name__)

# Default polling interval per collection type (seconds)
DEFAULT_INTERVALS = {
    "cpu": 30,
    "memory": 30,
    "interfaces": 60,
    "interface_detail": 60,
    "bgp_summary": 60,
    "neighbors": 300,
    "environment": 120,
    "processes": 30,
    "log": 30,
}


class PollEngine(QThread):
    """
    SSH polling thread.

    Connects to a device using SCNG SSHClient, runs commands on schedule,
    parses output via vendor templates, and writes normalized data to
    the state store.

    Signals:
        connected: SSH session established
        disconnected: SSH session lost
        error: Fatal error (connection failure, auth failure)
        poll_tick: Emitted each poll cycle with cycle number
    """

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    poll_tick = pyqtSignal(int)

    def __init__(
        self,
        credentials: SSHCredentials,
        state_store: DeviceStateStore,
        template_loader: TemplateLoader,
        vendor: str,
        collections: list[str] = None,
        legacy_mode: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self.credentials = credentials
        self.state_store = state_store
        self.template_loader = template_loader
        self.vendor = vendor
        self.legacy_mode = legacy_mode

        # Determine which collections to poll
        available = template_loader.get_collections_for_vendor(vendor)
        available_names = {t.collection for t in available}

        if collections:
            self.collections = [c for c in collections if c in available_names]
        else:
            self.collections = sorted(available_names)

        # Build template map
        self._templates: dict[str, Template] = {}
        for coll in self.collections:
            tmpl = template_loader.get_template(coll, vendor)
            if tmpl:
                self._templates[coll] = tmpl

        # Timing: track last poll time per collection
        self._last_poll: dict[str, float] = {}

        # Control
        self._running = False
        self._mutex = QMutex()
        self._client: Optional[SSHClient] = None

        # Connection settings
        self._base_interval = 5  # Main loop sleep interval (seconds)

        logger.info(
            f"PollEngine configured: vendor={vendor}, "
            f"collections={self.collections}, legacy={legacy_mode}"
        )

    # ── Thread lifecycle ─────────────────────────────────────────────

    def run(self):
        """Main thread loop."""
        self._running = True
        cycle = 0

        try:
            self._connect()
            self.connected.emit()

            while self._running:
                cycle += 1
                self._poll_cycle(cycle)
                self.poll_tick.emit(cycle)

                # Sleep in small increments so we can stop promptly
                for _ in range(self._base_interval * 2):
                    if not self._running:
                        break
                    time.sleep(0.5)

        except Exception as e:
            msg = f"Poll engine fatal error: {e}"
            logger.error(msg)
            logger.debug(traceback.format_exc())
            self.error.emit(msg)
        finally:
            self._disconnect()
            self.disconnected.emit()

    def stop(self):
        """Signal the poll loop to stop."""
        self._running = False

    # ── SSH Connection (via SCNG client) ─────────────────────────────

    def _connect(self):
        """Establish SSH connection using SCNG SSHClient."""
        creds = self.credentials
        logger.info(f"Connecting to {creds.display} (legacy={self.legacy_mode})...")

        # Build SSHClientConfig from our credentials
        config = SSHClientConfig(
            host=creds.hostname,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            key_content=creds.key_data,
            key_file=creds.key_path,
            key_passphrase=creds.key_passphrase,
            timeout=30,
            shell_timeout=5.0,
            inter_command_time=1.0,
            expect_prompt_timeout=5000,
            legacy_mode=self.legacy_mode,
        )

        self._client = SSHClient(config)
        self._client.connect()

        # Auto-detect prompt
        prompt = self._client.find_prompt()
        self._client.set_expect_prompt(prompt)
        logger.info(f"Prompt detected: {prompt!r}")

        # Extract hostname from prompt for device info
        hostname = self._client.extract_hostname_from_prompt(prompt)
        if hostname:
            self.state_store.set_device_info({
                **self.state_store.device_info,
                "detected_hostname": hostname,
                "prompt": prompt,
            })

        # Disable pagination (shotgun approach — all vendors)
        self._client.disable_pagination()

        logger.info(f"Connected to {creds.display}, prompt={prompt!r}")

    def _disconnect(self):
        """Close SSH connection."""
        if self._client:
            try:
                self._client.disconnect()
            except Exception as e:
                logger.debug(f"Disconnect cleanup: {e}")
            finally:
                self._client = None

    # ── Poll cycle ───────────────────────────────────────────────────

    def _poll_cycle(self, cycle: int):
        """
        Run one poll cycle.

        Checks each collection's interval and runs commands that are due.
        Parsing is synchronous — all commands run, all results parsed,
        then state store is updated atomically per collection.
        """
        now = time.time()

        for collection, template in self._templates.items():
            interval = template.interval or DEFAULT_INTERVALS.get(collection, 60)
            last = self._last_poll.get(collection, 0)

            if now - last < interval and cycle > 1:
                continue  # Not due yet

            try:
                logger.debug(f"Polling [{collection}]: {template.command}")
                raw_output = self._client.execute_command(template.command)

                if not raw_output or not raw_output.strip():
                    logger.warning(f"Empty output for [{collection}]")
                    self.state_store.record_error(collection, "Empty command output")
                    continue

                # Parse through template
                parsed = template.parse(raw_output)

                # Post-processing
                parsed = self._post_process(collection, parsed, template)

                # Write to state store
                self.state_store.update(collection, parsed)
                self._last_poll[collection] = now

                logger.debug(f"Parsed [{collection}]: {list(parsed.keys())}")

            except Exception as e:
                msg = f"Poll [{collection}] failed: {e}"
                logger.warning(msg)
                logger.debug(traceback.format_exc())
                self.state_store.record_error(collection, str(e))

        self.state_store.poll_cycle_complete.emit()

    # ── Post-processing ──────────────────────────────────────────────

    def _post_process(self, collection: str, data: dict, template: Template) -> dict:
        """Apply post-processing transforms to parsed data."""

        # Compute memory percentage
        if template.post_process == "compute_memory_pct":
            data = self._compute_memory_pct(data)

        # Normalize CPU across vendors
        if collection == "cpu":
            data = self._normalize_cpu(data)

        # Normalize BGP state
        if collection == "bgp_summary" and "peers" in data:
            data["peers"] = self._normalize_bgp_peers(data.get("peers", []))

        return data

    @staticmethod
    def _compute_memory_pct(data: dict) -> dict:
        """Compute used_pct from whatever memory fields are available."""
        total = data.get("total_bytes") or data.get("total_kb") or data.get("total_mb")
        used = data.get("used_bytes") or data.get("used_kb") or data.get("used_mb")

        if total and used and total > 0:
            data["used_pct"] = round(used / total * 100, 1)

            # Normalize to bytes for display
            if "total_bytes" in data:
                data["total_display"] = f"{total / (1024**3):.1f} GB"
                data["used_display"] = f"{used / (1024**3):.1f} GB"
            elif "total_kb" in data:
                data["total_display"] = f"{total / (1024**2):.1f} GB"
                data["used_display"] = f"{used / (1024**2):.1f} GB"
            elif "total_mb" in data:
                data["total_display"] = f"{total / 1024:.1f} GB"
                data["used_display"] = f"{used / 1024:.1f} GB"

        return data

    @staticmethod
    def _normalize_cpu(data: dict) -> dict:
        """
        Normalize CPU data across vendors to common keys.

        Target keys: five_sec_total, one_min, five_min
        """
        # Arista/Juniper: compute total from user + system + interrupt
        if "idle_pct" in data:
            total = round(100 - data["idle_pct"], 1)
            data.setdefault("five_sec_total", total)
            data.setdefault("one_min", total)
            data.setdefault("five_min", total)
        elif "user_pct" in data:
            total = data.get("user_pct", 0) + data.get("system_pct", 0)
            data.setdefault("five_sec_total", round(total, 1))
            data.setdefault("one_min", round(total, 1))
            data.setdefault("five_min", round(total, 1))

        return data

    @staticmethod
    def _normalize_bgp_peers(peers: list[dict]) -> list[dict]:
        """Normalize BGP peer state across vendors."""
        for peer in peers:
            state_pfx = str(peer.get("state_pfx", ""))
            # Determine if established (has a number = prefix count)
            try:
                pfx_count = int(state_pfx)
                peer["state"] = "Established"
                peer["prefixes_rcvd"] = pfx_count
            except (ValueError, TypeError):
                peer["state"] = state_pfx if state_pfx else "Unknown"
                peer["prefixes_rcvd"] = 0
        return peers

    # ── Status ───────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Check if SSH session is active."""
        try:
            return (
                self._client is not None
                and self._client._client is not None
                and self._client._client.get_transport() is not None
                and self._client._client.get_transport().is_active()
            )
        except Exception:
            return False
