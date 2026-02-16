"""
Poll Engine — SSH command loop with collection-driven parsing.

Connects to a device, runs commands on schedule, parses output via the
parser chain (TextFSM → TTP → regex), and writes normalized data to
the state store.

Vendor-specific behavior (pagination, field normalization, output shaping)
is delegated to vendor drivers — the engine itself is vendor-agnostic.

Runs in a QThread to keep the UI responsive.

Key changes from v1:
  - template_loader.py removed — collections/ + parser_chain is the sole
    parsing system. Custom TextFSM templates go in templates/textfsm/ and
    are referenced by name in collection YAML configs.
  - Vendor normalization moved to drivers/ — engine doesn't know about
    CPU field names, memory percentage math, or BGP state parsing.
  - ParseTrace integrated — every poll cycle produces a structured audit
    record showing exactly what happened at each step.
"""

from __future__ import annotations
import time
import logging
import traceback
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal, QMutex

from .auth_interface import SSHCredentials
from .state_store import DeviceStateStore
from .parser_chain import ParserChain, CollectionLoader
from .ssh_client import SSHClient, SSHClientConfig
from .parse_trace import ParseTrace, ParseTraceStore
from .drivers import get_driver, VendorDriver

logger = logging.getLogger(__name__)

# Default polling intervals (seconds) — overridden by collection YAML
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

    Connects via SSHClient, runs commands per collection schedule,
    parses via parser chain, delegates post-processing to the vendor
    driver, and writes to the state store.

    Signals:
        connected:     SSH session established
        disconnected:  SSH session lost
        error:         Fatal error (connection failure, auth failure)
        poll_tick:     Emitted each poll cycle with cycle number
    """

    connected = pyqtSignal()
    disconnected = pyqtSignal()
    error = pyqtSignal(str)
    poll_tick = pyqtSignal(int)

    def __init__(
        self,
        credentials: SSHCredentials,
        state_store: DeviceStateStore,
        vendor: str,
        collections: list[str] = None,
        legacy_mode: bool = True,
        collections_dir: str = None,
        template_search_paths: list[str] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.credentials = credentials
        self.state_store = state_store
        self.vendor = vendor
        self.legacy_mode = legacy_mode

        # ── Vendor driver ────────────────────────────────────────
        self._driver: VendorDriver = get_driver(vendor)
        logger.info(f"Vendor driver: {self._driver}")

        # ── Parser chain ─────────────────────────────────────────
        search_paths = list(template_search_paths or [])

        # Always include local textfsm overrides (custom templates)
        local_fsm = Path(__file__).parent / "templates" / "textfsm"
        if local_fsm.exists():
            search_paths.insert(0, str(local_fsm))

        self._parser_chain = ParserChain(template_search_paths=search_paths)
        self._collection_loader = CollectionLoader(collections_dir)

        caps = self._parser_chain.capabilities
        logger.info(
            f"Parser chain: textfsm={caps['textfsm']}, "
            f"ttp={caps['ttp']}, "
            f"ntc_templates={caps['ntc_templates']}, "
            f"search_paths={caps['search_paths']}"
        )

        # ── Collections to poll ──────────────────────────────────
        available = set(self._collection_loader.list_collections(vendor))

        if collections:
            self.collections = [c for c in collections if c in available]
            missing = set(collections) - available
            if missing:
                logger.warning(
                    f"Requested collections not available for {vendor}: {missing}"
                )
        else:
            self.collections = sorted(available)

        if not self.collections:
            logger.warning(
                f"No collections found for vendor '{vendor}'. "
                f"Check collections/ directory has {vendor}.yaml configs."
            )

        # ── Parse trace store ────────────────────────────────────
        self._trace_store = ParseTraceStore(max_per_collection=20)

        # ── Timing ───────────────────────────────────────────────
        self._last_poll: dict[str, float] = {}

        # ── Control ──────────────────────────────────────────────
        self._running = False
        self._mutex = QMutex()
        self._client: Optional[SSHClient] = None
        self._base_interval = 5  # Main loop sleep (seconds)

        logger.info(
            f"PollEngine: vendor={vendor}, "
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

                # Sleep in small increments for responsive stop
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

    # ── SSH Connection ───────────────────────────────────────────────

    def _connect(self):
        """Establish SSH connection."""
        creds = self.credentials
        logger.info(f"Connecting to {creds.display} (legacy={self.legacy_mode})...")

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

        # Detect prompt
        prompt = self._client.find_prompt()
        self._client.set_expect_prompt(prompt)
        logger.info(f"Prompt detected: {prompt!r}")

        # Extract hostname
        hostname = self._client.extract_hostname_from_prompt(prompt)
        if hostname:
            self.state_store.set_device_info({
                **self.state_store.device_info,
                "detected_hostname": hostname,
                "prompt": prompt,
            })

        # Disable pagination via driver
        pager_cmd = self._driver.pagination_command
        if pager_cmd:
            logger.debug(f"Disabling pagination: {pager_cmd}")
            self._client.execute_command(pager_cmd)
            # Re-validate prompt after pagination change
            prompt = self._client.find_prompt(attempt_count=2, timeout=3.0)
            self._client.set_expect_prompt(prompt)
            logger.debug(f"Post-pagination prompt: {prompt!r}")
        else:
            # Unknown vendor — shotgun approach
            logger.debug(f"No pagination command for '{self.vendor}', using shotgun")
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

        For each collection due:
          1. Execute CLI command
          2. Parse via chain (TextFSM → TTP → regex)
          3. Shape output for state store
          4. Post-process via vendor driver
          5. Record parse trace
          6. Write to state store
        """
        now = time.time()

        for collection in self.collections:
            config = self._collection_loader.get_config(collection, self.vendor)
            if not config:
                continue

            # ── Check interval ───────────────────────────────────
            interval = config.get(
                "interval",
                DEFAULT_INTERVALS.get(collection, 60),
            )
            last = self._last_poll.get(collection, 0)
            if now - last < interval and cycle > 1:
                continue

            command = config.get("command")
            if not command:
                logger.warning(f"[{collection}] no command in config")
                continue

            # ── Start trace ──────────────────────────────────────
            trace = ParseTrace(collection, self.vendor)

            try:
                # ── Execute command ──────────────────────────────
                logger.debug(f"Polling [{collection}]: {command}")
                raw_output = self._client.execute_command(command)
                trace.raw_received(raw_output, command=command)

                if not raw_output or not raw_output.strip():
                    logger.warning(f"Empty output for [{collection}]")
                    trace.delivered(
                        parsed_by="none",
                        error="empty command output",
                    )
                    trace.emit()
                    self._trace_store.store(trace)
                    self.state_store.record_error(collection, "Empty command output")
                    continue

                # ── Parse via chain ──────────────────────────────
                schema = self._collection_loader.get_schema(collection)
                rows, meta = self._parser_chain.parse(
                    raw_output, config, schema, trace=trace,
                )

                parsed_by = meta.get("_parsed_by", "none")
                template = meta.get("_template", "")
                parse_error = meta.get("_error")

                if not rows or parsed_by == "none":
                    logger.warning(
                        f"[{collection}] parse failed: {parse_error or 'no rows'}"
                    )
                    trace.delivered(
                        parsed_by=parsed_by,
                        template=template,
                        error=parse_error or "no rows",
                    )
                    trace.emit()
                    self._trace_store.store(trace)
                    self.state_store.record_error(
                        collection, parse_error or "no rows"
                    )
                    continue

                # ── Shape output ─────────────────────────────────
                data = self._driver.shape_output(collection, rows, meta)

                # Attach parse metadata
                data["_parsed_by"] = parsed_by
                data["_template"] = template
                if parse_error:
                    data["_error"] = parse_error

                # ── Post-process via driver ──────────────────────
                data = self._driver.post_process(
                    collection, data, state_store=self.state_store
                )

                trace.post_processed(
                    transform=f"{self._driver.__class__.__name__}.post_process",
                    added_fields=[
                        k for k in data.keys()
                        if k not in ("_parsed_by", "_template", "_error")
                    ],
                )

                # ── Deliver to state store ───────────────────────
                final_fields = [
                    k for k in data.keys()
                    if not k.startswith("_")
                ]
                trace.delivered(
                    final_fields=final_fields,
                    row_count=len(rows),
                    parsed_by=parsed_by,
                    template=template,
                )
                trace.emit()
                self._trace_store.store(trace)

                self.state_store.update(collection, data)
                self._last_poll[collection] = now

                logger.debug(
                    f"[{collection}] → {parsed_by}/{template}: "
                    f"{len(rows)} rows, fields={final_fields}"
                )

            except Exception as e:
                msg = f"Poll [{collection}] failed: {e}"
                logger.warning(msg)
                logger.debug(traceback.format_exc())

                trace.delivered(
                    parsed_by="none",
                    error=str(e),
                )
                trace.emit()
                self._trace_store.store(trace)

                self.state_store.record_error(collection, str(e))

        self.state_store.poll_cycle_complete.emit()

    # ── Diagnostics ──────────────────────────────────────────────────

    @property
    def trace_store(self) -> ParseTraceStore:
        """Access the parse trace store for diagnostics."""
        return self._trace_store

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
