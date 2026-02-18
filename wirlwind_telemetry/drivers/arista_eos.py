"""
Arista EOS Vendor Driver.

Handles EOS-specific field normalization:
  - CPU: Linux 'top' output → five_sec_total from idle percentage
  - Memory: KiB values from 'top' output → used_pct
  - Processes: Linux 'top' per-process rows → dashboard format
  - Neighbors: LLDP fields → dashboard neighbor graph format
  - Interface Detail: bandwidth/rate/error normalization

Handles raw TextFSM field names directly (global_cpu_percent_idle, etc.)
rather than depending on the normalize map for remapping.
"""

from __future__ import annotations
import re
import logging
from typing import TYPE_CHECKING

from . import (
    VendorDriver,
    register_driver,
    _post_process_log,
    _normalize_bgp_peers,
)

if TYPE_CHECKING:
    from ..state_store import DeviceStateStore

logger = logging.getLogger(__name__)

# Regex to extract numeric Kbps from bandwidth field
_BW_PATTERN = re.compile(r'(\d+)\s*[Kk]')

# Rate string patterns: "1234 bps", "1.23 Kbps", "5.67 Mbps", "1.2 Gbps"
_RATE_PATTERN = re.compile(
    r'([\d.]+)\s*(bps|[Kk]bps|[Mm]bps|[Gg]bps)',
    re.IGNORECASE,
)

_RATE_MULTIPLIERS = {
    'bps': 1,
    'kbps': 1_000,
    'mbps': 1_000_000,
    'gbps': 1_000_000_000,
}


def _to_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _first(*vals):
    """Return first non-None _to_float result."""
    for v in vals:
        r = _to_float(v)
        if r is not None:
            return r
    return None


def _parse_rate_to_bps(rate_str) -> int:
    """
    Convert a rate string with units to integer bps.

    Arista 'show interfaces' reports rates with unit suffixes:
        "0 bps"       → 0
        "1234 bps"    → 1234
        "1.23 Kbps"   → 1230
        "5.67 Mbps"   → 5670000
        "1.2 Gbps"    → 1200000000

    Also handles bare integers (already in bps).
    """
    if rate_str is None or rate_str == "":
        return 0
    s = str(rate_str).strip()

    # Try bare integer first
    try:
        return int(s)
    except ValueError:
        pass

    # Try float (already in bps)
    try:
        return int(float(s))
    except ValueError:
        pass

    # Try rate with units
    m = _RATE_PATTERN.search(s)
    if m:
        value = float(m.group(1))
        unit = m.group(2).lower()
        multiplier = _RATE_MULTIPLIERS.get(unit, 1)
        return int(value * multiplier)

    return 0


@register_driver("arista_eos")
class AristaEOSDriver(VendorDriver):
    """Driver for Arista EOS platforms."""

    @property
    def pagination_command(self) -> str:
        return "terminal length 0"

    def post_process(
        self,
        collection: str,
        data: dict,
        state_store: "DeviceStateStore" = None,
    ) -> dict:
        if collection == "cpu":
            data = self._normalize_cpu(data)
            data = self._build_process_list(data)

        elif collection == "memory":
            data = self._normalize_memory(data)

        elif collection == "log":
            data = _post_process_log(data)

        elif collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        elif collection == "neighbors" and "neighbors" in data:
            data["neighbors"] = self._post_process_neighbors(data["neighbors"])

        elif collection == "interface_detail" and "interfaces" in data:
            data["interfaces"] = self._post_process_interfaces(data["interfaces"])

        return data

    # ── CPU ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_cpu(data: dict) -> dict:
        """
        Compute five_sec_total from idle percentage.

        Handles all possible field name paths:
          TextFSM:  global_cpu_percent_idle, global_cpu_percent_user, global_cpu_percent_system
          Regex:    idle_pct, user_pct, system_pct
          Normalized: cpu_idle, cpu_usr, cpu_sys
        """
        idle = _first(
            data.get("global_cpu_percent_idle"),
            data.get("idle_pct"),
            data.get("cpu_idle"),
        )
        user = _first(
            data.get("global_cpu_percent_user"),
            data.get("user_pct"),
            data.get("cpu_usr"),
        )
        system = _first(
            data.get("global_cpu_percent_system"),
            data.get("system_pct"),
            data.get("cpu_sys"),
        )

        total = None
        if idle is not None:
            total = round(100.0 - idle, 1)
        elif user is not None:
            total = round(user + (system or 0), 1)

        if total is not None:
            data["five_sec_total"] = total
            data.setdefault("one_min", total)
            data.setdefault("five_min", total)

        return data

    # ── Memory ────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_memory(data: dict) -> dict:
        """
        Compute used_pct from memory values.

        Handles all possible field name paths:
          TextFSM: global_mem_total, global_mem_free, global_mem_used
          Regex:   total_kb, free_kb, used_kb
          Normalized: mem_total, mem_free, mem_used
        """
        total = _first(
            data.get("global_mem_total"),
            data.get("mem_total"),
            data.get("total_kb"),
        )
        used = _first(
            data.get("global_mem_used"),
            data.get("mem_used"),
            data.get("used_kb"),
        )
        free = _first(
            data.get("global_mem_free"),
            data.get("mem_free"),
            data.get("free_kb"),
        )

        if used is None and total is not None and free is not None:
            used = total - free

        if total and total > 0 and used is not None:
            pct = round((used / total) * 100.0, 1)
            data["used_pct"] = pct
            data["used"] = int(used)
            data["total"] = int(total)
            data["free"] = int(free) if free is not None else int(total - used)

        return data

    # ── Processes ──────────────────────────────────────────────────────

    @staticmethod
    def _build_process_list(data: dict) -> dict:
        """
        Alias Arista process fields to dashboard-expected names.

        TextFSM per-process rows from 'show processes top once' have:
          pid, command, percent_cpu, percent_memory, resident_memory_size

        Dashboard expects:
          pid, name, cpu_pct, five_sec, holding

        NOTE: Unlike Cisco's 'show processes cpu sorted' which reports
        per-process CPU averages, Arista's 'top -n 1' snapshot often
        shows 0.0% for processes that aren't actively running at the
        instant of capture. We keep the top 20 processes regardless
        of CPU%, sorted by CPU descending then by memory descending.
        Filtering only >0% would produce an empty table on most polls.
        """
        processes = data.get("processes")
        if not processes:
            return data

        active = []
        for proc in processes:
            cpu_pct = _first(
                proc.get("percent_cpu"),
                proc.get("cpu_pct"),
                proc.get("cpu"),
            )
            if cpu_pct is None:
                cpu_pct = 0.0

            proc["pid"] = proc.get("pid", "")
            proc["name"] = (
                proc.get("command")
                or proc.get("name")
                or ""
            )
            proc["cpu_pct"] = cpu_pct
            proc["five_sec"] = cpu_pct

            # Memory percent from top output
            mem_pct = _first(
                proc.get("percent_memory"),
                proc.get("mem_pct"),
            )
            if mem_pct is not None:
                proc["mem_pct"] = mem_pct

            # Memory: RES field from top (KB or with g/m suffix)
            res_str = str(
                proc.get("resident_memory_size")
                or proc.get("res")
                or "0"
            )
            res_kb = 0
            if res_str.endswith("g"):
                res_kb = (_to_float(res_str[:-1]) or 0) * 1024 * 1024
            elif res_str.endswith("m"):
                res_kb = (_to_float(res_str[:-1]) or 0) * 1024
            else:
                res_kb = _to_float(res_str) or 0

            if res_kb > 0:
                if res_kb > 1_000_000:
                    proc["holding_display"] = f"{res_kb / 1024:.0f}M"
                elif res_kb > 1000:
                    proc["holding_display"] = f"{res_kb:.0f}K"
                else:
                    proc["holding_display"] = f"{res_kb:.0f}"
                proc["holding"] = int(res_kb * 1024)

            active.append(proc)

        # Sort by CPU descending, then by memory descending as tiebreaker
        active.sort(
            key=lambda p: (p.get("cpu_pct", 0), p.get("mem_pct", 0)),
            reverse=True,
        )

        # Keep top 20 — enough to fill the dashboard table without noise
        data["processes"] = active[:20]
        return data

    # ── Neighbors ─────────────────────────────────────────────────────

    @staticmethod
    def _post_process_neighbors(neighbors: list[dict]) -> list[dict]:
        """
        Normalize LLDP neighbor fields for the dashboard graph.

        LLDP TextFSM (NTC) returns:
          neighbor, local_interface, neighbor_interface,
          neighbor_description, management_ip, capabilities

        After the YAML normalize map runs, we get:
          device_id, local_intf, remote_intf,
          platform, mgmt_ip, capabilities

        This method cleans up LLDP-specific quirks:
        - Strips domain suffixes from device_id (switch1.example.com → switch1)
        - Extracts platform from system description if needed
        - Shortens interface names for graph edge labels
        - Normalizes capabilities string
        """
        # Interface name abbreviations for graph labels
        _INTF_SHORT = {
            "Ethernet": "Et",
            "Management": "Ma",
            "Loopback": "Lo",
            "Port-Channel": "Po",
            "Vlan": "Vl",
            "GigabitEthernet": "Gi",
            "TenGigabitEthernet": "Te",
            "FastEthernet": "Fa",
            "TwentyFiveGigE": "Twe",
            "FortyGigabitEthernet": "Fo",
            "HundredGigE": "Hu",
        }

        for nbr in neighbors:
            # Strip FQDN from device_id
            device_id = nbr.get("device_id", "")
            if "." in device_id and not device_id.replace(".", "").isdigit():
                nbr["device_id"] = device_id.split(".")[0]

            # If platform came from neighbor_description (LLDP system description),
            # try to extract a short platform string
            platform = nbr.get("platform", "")
            if not platform:
                # Fallback: use neighbor_description as platform
                platform = nbr.get("neighbor_description", "")
                nbr["platform"] = platform

            # Extract short platform from verbose system description
            # e.g. "Arista Networks EOS version 4.28.3M ..." → "Arista EOS"
            if platform:
                platform_lower = platform.lower()
                if "arista" in platform_lower:
                    nbr["platform"] = "Arista EOS"
                elif "cisco" in platform_lower and "nx-os" in platform_lower:
                    nbr["platform"] = "Cisco NX-OS"
                elif "cisco" in platform_lower and "ios-xe" in platform_lower:
                    nbr["platform"] = "Cisco IOS-XE"
                elif "cisco" in platform_lower:
                    nbr["platform"] = "Cisco IOS"
                elif "juniper" in platform_lower:
                    nbr["platform"] = "Juniper JunOS"

            # Shorten interface names for graph edge labels
            for field in ("local_intf", "remote_intf"):
                intf = nbr.get(field, "")
                for long, short in _INTF_SHORT.items():
                    if intf.startswith(long):
                        nbr[field] = intf.replace(long, short, 1)
                        break

            # Normalize capabilities to uppercase CSV
            caps = nbr.get("capabilities", "")
            if isinstance(caps, list):
                caps = ", ".join(str(c) for c in caps)
            if caps:
                nbr["capabilities"] = caps.strip()

        return neighbors

    # ── Interface Detail ──────────────────────────────────────────────

    @staticmethod
    def _post_process_interfaces(interfaces: list[dict]) -> list[dict]:
        """
        Post-process interface detail rows for Arista EOS.

        1. Parse bandwidth string → numeric bandwidth_kbps
        2. Convert rate strings (input_rate_raw/output_rate_raw) to int bps
        3. Ensure error counts are int
        4. Compute utilization_pct if bandwidth is known
        """
        for intf in interfaces:
            # ── Parse bandwidth ─────────────────────────────────────
            bw_raw = intf.get("bandwidth_raw") or intf.get("bandwidth") or ""
            bw_kbps = 0
            if bw_raw:
                m = _BW_PATTERN.search(str(bw_raw))
                if m:
                    bw_kbps = int(m.group(1))
            intf["bandwidth_kbps"] = bw_kbps

            # ── Convert rate strings to int bps ─────────────────────
            # Arista rates arrive as strings with units: "1.23 Mbps",
            # "456 Kbps", "0 bps". The normalize map renames
            # input_rate → input_rate_raw, output_rate → output_rate_raw.
            # We parse these and store as input_rate_bps / output_rate_bps.
            for raw_field, bps_field in (
                ("input_rate_raw", "input_rate_bps"),
                ("output_rate_raw", "output_rate_bps"),
            ):
                raw_val = intf.get(raw_field) or intf.get(bps_field)
                intf[bps_field] = _parse_rate_to_bps(raw_val)

            # ── Ensure error counts are int ─────────────────────────
            for field in ("in_errors", "out_errors", "crc_errors"):
                val = intf.get(field)
                if val is not None:
                    try:
                        intf[field] = int(val)
                    except (ValueError, TypeError):
                        intf[field] = 0
                else:
                    intf[field] = 0

            # ── Ensure MTU is int ───────────────────────────────────
            mtu = intf.get("mtu")
            if mtu is not None:
                try:
                    intf["mtu"] = int(mtu)
                except (ValueError, TypeError):
                    intf["mtu"] = 0

            # ── Compute utilization percentage ──────────────────────
            if bw_kbps > 0:
                bw_bps = bw_kbps * 1000
                peak_bps = max(intf["input_rate_bps"], intf["output_rate_bps"])
                intf["utilization_pct"] = round((peak_bps / bw_bps) * 100, 1)
            else:
                intf["utilization_pct"] = 0.0

            # Clean up intermediate fields
            intf.pop("bandwidth_raw", None)

        return interfaces

    def __repr__(self):
        return f"AristaEOSDriver({self.vendor})"