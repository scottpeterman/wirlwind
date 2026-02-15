"""
State Store - In-memory normalized device model.

The single source of truth for device state. Poll engine writes here,
the broker/UI reads from here. All vendor-specific data is normalized
into a common schema before storage.

Emits Qt signals on state changes so the UI layer can react.
"""

from __future__ import annotations
import json
import time
import copy
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from PyQt6.QtCore import QObject, pyqtSignal

logger = logging.getLogger(__name__)


class DeviceStateStore(QObject):
    """
    Normalized device state with change notification.

    State is organized by collection key (cpu, memory, interfaces, etc.)
    Each collection stores the latest parsed data plus metadata.
    """

    # Emitted when any collection is updated: (collection_key, data_dict)
    state_updated = pyqtSignal(str, dict)

    # Emitted when a full poll cycle completes (all collections refreshed)
    poll_cycle_complete = pyqtSignal()

    # Emitted on errors: (collection_key, error_message)
    collection_error = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state: dict[str, dict] = {}
        self._metadata: dict[str, dict] = {}
        self._history: dict[str, list] = {}
        self._history_max = 360  # ~6 hours at 60s interval, ~3 hours at 30s
        self._device_info: dict = {}

    # ── Device identity ──────────────────────────────────────────────

    def set_device_info(self, info: dict) -> None:
        """Set static device identity (hostname, model, version, serial, etc.)."""
        self._device_info = info
        logger.info(f"Device info set: {info.get('hostname', 'unknown')}")

    @property
    def device_info(self) -> dict:
        return copy.deepcopy(self._device_info)

    # ── State read/write ─────────────────────────────────────────────

    def update(self, collection: str, data: dict) -> None:
        """
        Write normalized data for a collection.

        Args:
            collection: Key name (cpu, memory, interfaces, bgp_summary, etc.)
            data: Normalized data dict from the parser
        """
        now = datetime.now(timezone.utc)

        self._state[collection] = data
        self._metadata[collection] = {
            "last_updated": now.isoformat(),
            "timestamp": time.time(),
            "success": True,
        }

        # Append to history for trend data
        if collection in ("cpu", "memory"):
            if collection not in self._history:
                self._history[collection] = []
            self._history[collection].append({
                "timestamp": time.time(),
                "data": self._extract_headline(collection, data),
            })
            # Trim history
            if len(self._history[collection]) > self._history_max:
                self._history[collection] = self._history[collection][-self._history_max:]

        self.state_updated.emit(collection, data)
        logger.debug(f"State updated: {collection}")

    def record_error(self, collection: str, error: str) -> None:
        """Record a collection failure without overwriting last good data."""
        self._metadata.setdefault(collection, {})
        self._metadata[collection]["last_error"] = error
        self._metadata[collection]["last_error_time"] = datetime.now(timezone.utc).isoformat()
        self._metadata[collection]["success"] = False
        self.collection_error.emit(collection, error)
        logger.warning(f"Collection error [{collection}]: {error}")

    def get(self, collection: str) -> Optional[dict]:
        """Get current state for a collection."""
        data = self._state.get(collection)
        return copy.deepcopy(data) if data else None

    def get_metadata(self, collection: str) -> Optional[dict]:
        """Get metadata (timestamps, errors) for a collection."""
        meta = self._metadata.get(collection)
        return copy.deepcopy(meta) if meta else None

    def get_history(self, collection: str) -> list:
        """Get historical data points for a collection."""
        return copy.deepcopy(self._history.get(collection, []))

    # ── Full snapshot (for dashboard) ────────────────────────────────

    def snapshot(self) -> dict:
        """
        Return complete state snapshot for the dashboard.

        This is what gets sent over QWebChannel to the JS dashboard.
        """
        return {
            "device": copy.deepcopy(self._device_info),
            "collections": copy.deepcopy(self._state),
            "metadata": copy.deepcopy(self._metadata),
            "history": {
                "cpu": self._history.get("cpu", [])[-self._history_max:],
                "memory": self._history.get("memory", [])[-self._history_max:],
            },
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
        }

    def snapshot_json(self) -> str:
        """Snapshot as JSON string for JS consumption."""
        return json.dumps(self.snapshot(), default=str)

    # ── Helpers ──────────────────────────────────────────────────────

    def _extract_headline(self, collection: str, data: dict) -> dict:
        """Extract headline metrics for history tracking."""
        if collection == "cpu":
            return {
                "five_min": data.get("five_min", 0),
                "one_min": data.get("one_min", 0),
                "five_sec": data.get("five_sec_total", 0),
            }
        elif collection == "memory":
            return {
                "used_pct": data.get("used_pct", 0),
            }
        return {}

    def clear(self) -> None:
        """Reset all state."""
        self._state.clear()
        self._metadata.clear()
        self._history.clear()
        self._device_info.clear()
        logger.info("State store cleared")
