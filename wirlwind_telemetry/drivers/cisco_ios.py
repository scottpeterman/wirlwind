"""
Cisco IOS / IOS-XE Vendor Driver.

Handles the field normalization quirks specific to IOS and IOS-XE:
  - CPU: TextFSM returns cpu_usage_5_sec / cpu_usage_1_min / cpu_usage_5_min
    which need mapping to the dashboard's five_sec_total / one_min / five_min
  - Memory: NTC template returns parallel lists (process_id, process_holding)
  - Processes: Filter idle, add dashboard aliases, merge memory holdings
  - Interface Detail: Parse bandwidth string, ensure rate fields are numeric,
    compute utilization percentage
"""

from __future__ import annotations
import re
import logging
from typing import TYPE_CHECKING

from . import (
    VendorDriver,
    register_driver,
    _compute_memory_pct,
    _filter_cpu_processes,
    _merge_memory_into_processes,
    _post_process_log,
    _normalize_bgp_peers,
    _to_float,
)

if TYPE_CHECKING:
    from ..state_store import DeviceStateStore

logger = logging.getLogger(__name__)

# Regex to extract numeric Kbps from NTC bandwidth field
# Matches: "1000000 Kbit", "100000 Kbit/sec", "1000000Kbit"
_BW_PATTERN = re.compile(r'(\d+)\s*[Kk]')


@register_driver("cisco_ios", "cisco_ios_xe")
class CiscoIOSDriver(VendorDriver):
    """
    Driver for Cisco IOS and IOS-XE platforms.

    Tested against: IOS 15.x, IOS-XE 16.x/17.x (CSR1000v, ISR, ASR)
    """

    @property
    def pagination_command(self) -> str:
        return "terminal length 0"

    def post_process(
        self,
        collection: str,
        data: dict,
        state_store: "DeviceStateStore" = None,
    ) -> dict:
        """
        Cisco-specific post-processing.

        CPU: normalize field names from TextFSM, filter idle processes,
             merge memory holdings from the memory collection.
        Memory: compute used_pct.
        Log: assemble timestamps, trim.
        BGP: normalize state/prefix fields.
        Interface Detail: parse bandwidth, ensure numeric rates, compute utilization.
        """
        if collection == "cpu":
            data = self._normalize_cpu(data)
            data = _filter_cpu_processes(data)
            if state_store:
                data = _merge_memory_into_processes(data, state_store)

        elif collection == "memory":
            data = _compute_memory_pct(data)

        elif collection == "log":
            data = _post_process_log(data)

        elif collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        elif collection == "interface_detail" and "interfaces" in data:
            data["interfaces"] = self._post_process_interfaces(
                data["interfaces"]
            )

        return data

    # ── Interface Detail ─────────────────────────────────────────

    @staticmethod
    def _post_process_interfaces(interfaces: list[dict]) -> list[dict]:
        """
        Post-process interface detail rows.

        1. Parse bandwidth string ("1000000 Kbit") → numeric bandwidth_kbps
        2. Ensure input_rate_bps / output_rate_bps are int
        3. Ensure error counts are int
        4. Compute utilization_pct if bandwidth is known
        """
        for intf in interfaces:
            # ── Parse bandwidth ──────────────────────────────────
            bw_raw = intf.get("bandwidth_raw") or intf.get("bandwidth") or ""
            bw_kbps = 0
            if bw_raw:
                m = _BW_PATTERN.search(str(bw_raw))
                if m:
                    bw_kbps = int(m.group(1))
            intf["bandwidth_kbps"] = bw_kbps

            # ── Ensure rate fields are int ───────────────────────
            for field in ("input_rate_bps", "output_rate_bps"):
                val = intf.get(field)
                if val is not None:
                    try:
                        intf[field] = int(val)
                    except (ValueError, TypeError):
                        intf[field] = 0
                else:
                    intf[field] = 0

            # ── Ensure error counts are int ──────────────────────
            for field in ("in_errors", "out_errors", "crc_errors"):
                val = intf.get(field)
                if val is not None:
                    try:
                        intf[field] = int(val)
                    except (ValueError, TypeError):
                        intf[field] = 0
                else:
                    intf[field] = 0

            # ── Ensure MTU is int ────────────────────────────────
            mtu = intf.get("mtu")
            if mtu is not None:
                try:
                    intf["mtu"] = int(mtu)
                except (ValueError, TypeError):
                    intf["mtu"] = 0

            # ── Compute utilization percentage ───────────────────
            # Utilization = max(in, out) / bandwidth * 100
            if bw_kbps > 0:
                bw_bps = bw_kbps * 1000
                peak_bps = max(intf["input_rate_bps"], intf["output_rate_bps"])
                intf["utilization_pct"] = round(
                    (peak_bps / bw_bps) * 100, 1
                )
            else:
                intf["utilization_pct"] = 0.0

            # ── Clean up intermediate fields ─────────────────────
            # Remove the raw bandwidth string — dashboard uses bandwidth_kbps
            intf.pop("bandwidth_raw", None)

        return interfaces

    # ── CPU ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_cpu(data: dict) -> dict:
        """
        Map Cisco IOS CPU fields to canonical dashboard keys.

        TextFSM (NTC) returns:
            cpu_usage_5_sec, cpu_usage_1_min, cpu_usage_5_min

        Normalize map (if applied) produces:
            five_sec, one_min, five_min

        Dashboard expects:
            five_sec_total, one_min, five_min

        This method handles both mapped and unmapped fields.
        """
        if "five_sec_total" not in data:
            # Try normalize-mapped names first, then raw TextFSM names
            raw_5s = (
                data.get("five_sec")
                or data.get("five_sec_total")
                or data.get("cpu_usage_5_sec")
            )
            raw_1m = (
                data.get("one_min")
                or data.get("cpu_usage_1_min")
            )
            raw_5m = (
                data.get("five_min")
                or data.get("cpu_usage_5_min")
            )

            val = _to_float(raw_5s)
            if val is not None:
                data["five_sec_total"] = val
            val = _to_float(raw_1m)
            if val is not None:
                data["one_min"] = val
            val = _to_float(raw_5m)
            if val is not None:
                data["five_min"] = val

        return data

    def __repr__(self):
        return f"CiscoIOSDriver({self.vendor})"