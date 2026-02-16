"""
Juniper JunOS Vendor Driver.
"""

from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from . import (
    VendorDriver,
    register_driver,
    _compute_memory_pct,
    _post_process_log,
    _normalize_bgp_peers,
)

if TYPE_CHECKING:
    from ..state_store import DeviceStateStore

logger = logging.getLogger(__name__)


@register_driver("juniper_junos")
class JuniperJunOSDriver(VendorDriver):

    @property
    def pagination_command(self) -> str:
        return "set cli screen-length 0"

    def post_process(
        self,
        collection: str,
        data: dict,
        state_store: "DeviceStateStore" = None,
    ) -> dict:
        if collection == "cpu":
            data = self._normalize_cpu(data)

        elif collection == "memory":
            data = _compute_memory_pct(data)

        elif collection == "log":
            data = _post_process_log(data)

        elif collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        return data

    @staticmethod
    def _normalize_cpu(data: dict) -> dict:
        """Juniper: compute total from idle."""
        if "idle_pct" in data:
            total = round(100 - float(data["idle_pct"]), 1)
            data.setdefault("five_sec_total", total)
            data.setdefault("one_min", total)
            data.setdefault("five_min", total)
        return data

    def __repr__(self):
        return f"JuniperJunOSDriver({self.vendor})"
