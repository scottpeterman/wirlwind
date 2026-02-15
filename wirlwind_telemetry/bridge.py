"""
Web Bridge - QWebChannel object exposing state store to JavaScript.

This is the Python ↔ JavaScript bridge. The dashboard's JS calls methods
on this object to get device state, and receives signals when state changes.
"""

from __future__ import annotations
import json
import logging

from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal

from .state_store import DeviceStateStore

logger = logging.getLogger(__name__)


class TelemetryBridge(QObject):
    """
    Bridge between Python state store and JavaScript dashboard.

    Exposed to JS via QWebChannel. The dashboard calls getSnapshot()
    on init and listens to stateChanged for incremental updates.
    """

    # Signal to JS: state has changed (collection_key, json_data)
    stateChanged = pyqtSignal(str, str)

    # Signal to JS: full cycle complete
    cycleComplete = pyqtSignal()

    # Signal to JS: device info updated
    deviceInfoChanged = pyqtSignal(str)

    # Signal to JS: connection status changed
    connectionStatus = pyqtSignal(str)  # "connected", "disconnected", "error:msg"

    def __init__(self, state_store: DeviceStateStore, parent=None):
        super().__init__(parent)
        self._store = state_store

        # Connect state store signals to bridge signals
        self._store.state_updated.connect(self._on_state_updated)
        self._store.poll_cycle_complete.connect(self._on_cycle_complete)
        self._store.collection_error.connect(self._on_collection_error)

    def _on_state_updated(self, collection: str, data: dict):
        """Forward state updates to JS."""
        try:
            json_str = json.dumps(data, default=str)
            self.stateChanged.emit(collection, json_str)
        except Exception as e:
            logger.error(f"Bridge serialization error [{collection}]: {e}")

    def _on_cycle_complete(self):
        """Forward cycle completion to JS."""
        self.cycleComplete.emit()

    def _on_collection_error(self, collection: str, error: str):
        """Forward collection errors to JS."""
        self.stateChanged.emit(f"error:{collection}", json.dumps({"error": error}))

    # ── Methods callable from JavaScript ─────────────────────────────

    @pyqtSlot(result=str)
    def getSnapshot(self) -> str:
        """Get complete state snapshot as JSON. Called by JS on init."""
        return self._store.snapshot_json()

    @pyqtSlot(str, result=str)
    def getCollection(self, collection: str) -> str:
        """Get a specific collection's data as JSON."""
        data = self._store.get(collection)
        return json.dumps(data or {}, default=str)

    @pyqtSlot(str, result=str)
    def getHistory(self, collection: str) -> str:
        """Get historical data for a collection as JSON."""
        history = self._store.get_history(collection)
        return json.dumps(history, default=str)

    @pyqtSlot(result=str)
    def getDeviceInfo(self) -> str:
        """Get device identity info as JSON."""
        return json.dumps(self._store.device_info, default=str)

    @pyqtSlot(str, result=str)
    def getMetadata(self, collection: str) -> str:
        """Get collection metadata (timestamps, errors) as JSON."""
        meta = self._store.get_metadata(collection)
        return json.dumps(meta or {}, default=str)
