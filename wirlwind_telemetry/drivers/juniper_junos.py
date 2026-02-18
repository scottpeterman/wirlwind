"""
Juniper JunOS Vendor Driver.

Handles JunOS-specific field normalization:
  - CPU: "show chassis routing-engine" → five_sec_total from idle percentage
  - Memory: same command → used_pct from memory_utilization field
  - Processes: "show system processes extensive" → top 15 by WCPU%
  - Neighbors: LLDP fields → dashboard neighbor graph format
  - Interface Detail: rate/error/bandwidth normalization

Key differences from Cisco/Arista:
  - CPU and memory come from the same command (show chassis routing-engine)
    rather than separate commands or combined top output
  - Process data requires a separate "show system processes extensive" poll;
    this is the only JunOS command that provides real-time WCPU% per process.
    "show system processes detail" (BSD ps) is a fallback but lacks CPU%.
  - Dual routing engines produce two TextFSM rows; driver picks master RE
  - LLDP neighbor fields differ from both CDP (Cisco) and Arista LLDP
  - Interface output format differs significantly from IOS/EOS
"""

from __future__ import annotations
import re
import logging
from typing import TYPE_CHECKING

from . import (
    VendorDriver,
    register_driver,
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

# ── JunOS syslog helpers ──────────────────────────────────────────

# Structured event name pattern: ALL_CAPS_WITH_UNDERSCORES: ...
_JUNOS_MNEMONIC = re.compile(r'^([A-Z][A-Z0-9_]{2,}):\s*')

# Keywords → BSD severity (lower = more severe)
_SEVERITY_KEYWORDS = [
    # 0-1: emergency/alert
    ('panic', 0), ('kernel panic', 0),
    ('core dumped', 1), ('fatal', 1), ('abort', 1),
    # 2: critical
    ('down', 2),
    # 3: error
    ('failed', 3), ('failure', 3), ('error', 3),
    # 4: warning
    ('warning', 4), ('warn', 4), ('exceeded', 4), ('threshold', 4),
    ('mismatch', 4), ('timeout', 4), ('closed', 4), ('exited', 4),
    # 5: notice (state changes, auth)
    ('accepted', 5), ('established', 5), ('logged in', 5),
]

# ── JunOS process helpers ─────────────────────────────────────────

# Regex for parsing memory sizes with unit suffixes from top(1) output
# Matches: "45M", "12K", "128G", "1.5G" — but NOT bare integers
_RES_PATTERN = re.compile(r'^([\d.]+)\s*([KMGT])(?:B)?$', re.IGNORECASE)

_RES_MULTIPLIERS = {
    'k': 1024,
    'm': 1024 ** 2,
    'g': 1024 ** 3,
    't': 1024 ** 4,
}

# Kernel threads and system idle to filter out of top processes
_KERNEL_FILTER = {
    'idle', 'swapper', 'kernel', 'init',
}
_KERNEL_PREFIX = (
    'swi', 'irq', 'g_', 'em0', 'em1', 'kqueue', 'thread',
    'mastersh', 'yarrow', 'busdma',
)


def _to_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_int(val, default: int = 0) -> int:
    """Convert to int with fallback. Strips trailing whitespace."""
    if val is None or val == "":
        return default
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


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

    JunOS 'show interfaces' may report rates as bare integers (bps)
    or with unit suffixes depending on version and interface type.

    Examples:
        "1234"       → 1234
        "1234 bps"   → 1234
        "1.23 Kbps"  → 1230
        "5.67 Mbps"  → 5670000
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


@register_driver("juniper_junos")
class JuniperJunOSDriver(VendorDriver):
    """Driver for Juniper JunOS platforms (EX, QFX, MX, SRX)."""

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
            data = self._post_process_processes(data)

        elif collection == "memory":
            data = self._pick_master_re(data)
            data = self._normalize_memory(data)

        elif collection == "log":
            data = self._post_process_log_junos(data)

        elif collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        elif collection == "neighbors" and "neighbors" in data:
            data["neighbors"] = self._post_process_neighbors(data["neighbors"])

        elif collection == "interface_detail" and "interfaces" in data:
            data["interfaces"] = self._post_process_interfaces(data["interfaces"])

        return data

    # ── Dual-RE Handling ───────────────────────────────────────────────

    @staticmethod
    def _pick_master_re(data: dict) -> dict:
        """
        Handle dual routing engine output.

        The shaper puts row[0] fields into the flat dict and rows[1:]
        into data["processes"]. For dual-RE JunOS output, "processes"
        contains the backup RE row — not actual process data.

        This method:
        1. Checks if row[0] is the master RE (preferred)
        2. If row[0] is backup and "processes" contains master, swaps
        3. Clears the bogus "processes" key either way

        For single-RE platforms, this is a no-op except clearing
        the empty processes list.
        """
        backup_rows = data.pop("processes", None) or []
        status = str(data.get("status", "")).lower()

        # If row[0] is backup and we have another row, check if it's master
        if status == "backup" and backup_rows:
            for row in backup_rows:
                if str(row.get("status", "")).lower() == "master":
                    # Swap: promote master RE to top level
                    logger.info("Dual-RE: promoting master RE (slot %s) over backup",
                                row.get("slot", "?"))
                    master_data = dict(row)
                    master_data["processes"] = []
                    return master_data

        # No per-process data from show chassis routing-engine
        data["processes"] = []
        return data

    # ── CPU ────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_cpu(data: dict) -> dict:
        """
        Compute five_sec_total from idle percentage.

        Now sourced from "show system processes extensive" header:
          CPU:  1.2% user,  0.0% nice,  1.5% system,  0.3% interrupt, 97.0% idle

        After normalize map: cpu_user, cpu_sys, cpu_idle, cpu_interrupt
        (cpu_system → cpu_sys via normalize map)

        JunOS reports instantaneous CPU at poll time — there is no
        5-second or 1-minute CPU average like Cisco provides. We set
        one_min and five_min to the same value as five_sec_total.

        Note: load_avg_1/5/15 from the top header are Unix load averages
        (process queue depth), NOT CPU percentages — not used for gauges.
        """
        idle = _first(
            data.get("cpu_idle"),
            data.get("cpu_idle"),
        )
        user = _first(
            data.get("cpu_user"),
        )
        kernel = _first(
            data.get("cpu_kernel"),
            data.get("cpu_sys"),
        )
        interrupt = _first(
            data.get("cpu_interrupt"),
        )
        background = _first(
            data.get("cpu_background"),
        )

        total = None
        if idle is not None:
            total = round(100.0 - idle, 1)
        elif user is not None:
            # Fallback: sum known components
            total = round(
                user
                + (kernel or 0)
                + (interrupt or 0)
                + (background or 0),
                1,
            )

        if total is not None:
            data["five_sec_total"] = total
            # JunOS has no separate 1-min/5-min CPU averages
            data.setdefault("one_min", total)
            data.setdefault("five_min", total)

        return data

    # ── Memory ─────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_memory(data: dict) -> dict:
        """
        Compute memory metrics from show chassis routing-engine fields.

        The command provides:
          memory_utilization: percentage (0-100) — direct, no math needed
          dram: total DRAM in MB

        This is much simpler than Cisco/Arista where we compute
        percentage from used/total/free. JunOS gives us the percentage
        directly.
        """
        # memory_utilization is already a percentage from the command
        mem_pct = _first(
            data.get("memory_utilization"),
            data.get("used_pct"),
        )
        dram_mb = _first(
            data.get("dram"),
            data.get("mem_total"),
        )

        if mem_pct is not None:
            data["used_pct"] = round(mem_pct, 1)

            if dram_mb and dram_mb > 0:
                total_mb = dram_mb
                used_mb = round(dram_mb * mem_pct / 100.0, 1)

                # Display strings for gauge subtitle
                if total_mb >= 1024:
                    data["total_display"] = f"{total_mb / 1024:.1f} GB"
                else:
                    data["total_display"] = f"{int(total_mb)} MB"

                if used_mb >= 1024:
                    data["used_display"] = f"{used_mb / 1024:.1f} GB"
                else:
                    data["used_display"] = f"{int(used_mb)} MB"

                data["total"] = int(total_mb * 1024)  # KB for consistency
                data["used"] = int(used_mb * 1024)
                data["free"] = int((total_mb - used_mb) * 1024)

        return data

    # ── Log ────────────────────────────────────────────────────────────

    @staticmethod
    def _post_process_log_junos(data: dict) -> dict:
        """
        Post-process JunOS syslog entries for the dashboard.

        JunOS "show log messages" uses BSD syslog format with no
        structured severity field. This method:

        1. Assembles timestamp from month/day/time components
        2. Extracts mnemonic from JunOS structured event names
           (e.g. UI_CHILD_EXITED:) or falls back to daemon name
        3. Infers severity from keywords and facility
        4. Orders newest-first and trims to 50 entries

        Dashboard contract:
            {"entries": [{"timestamp", "facility", "severity", "mnemonic", "message"}]}
        """
        entries = data.get("entries", [])
        if not entries:
            return data

        processed = []
        for entry in entries:
            # Assemble timestamp
            month = entry.get("month", "")
            day = entry.get("day", "")
            time_str = entry.get("time", "")
            timestamp = f"{month} {day} {time_str}".strip()

            facility = entry.get("facility", "")
            message = entry.get("message", "")

            # Extract mnemonic
            m = _JUNOS_MNEMONIC.match(message)
            if m:
                mnemonic = m.group(1)
            else:
                # Clean daemon name: strip leading / and uppercase
                mnemonic = facility.strip("/").upper() or "SYSTEM"

            # Infer severity from keywords
            text = f"{facility} {message}".lower()
            severity = 6  # default: informational

            # Kernel messages default to warning
            if facility == "/kernel":
                severity = 4

            # Check keywords (most severe match wins)
            for keyword, sev in _SEVERITY_KEYWORDS:
                if keyword in text:
                    if sev < severity:
                        severity = sev

            processed.append({
                "timestamp": timestamp,
                "facility": facility.strip("/") or "system",
                "severity": severity,
                "mnemonic": mnemonic,
                "message": message,
            })

        # Newest first (entries come chronologically from show log messages)
        processed.reverse()

        # Trim to 50
        data["entries"] = processed[:50]
        return data

    # ── Processes ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_res_to_bytes(res_str) -> int:
        """
        Parse a memory size string to bytes.

        Handles two formats:
          - top(1) with units:  "45M" → 47185920,  "12K" → 12288
          - ps(1) bare integer: "95432" → 97722368  (assumed KB per BSD convention)

        Returns 0 for None, empty, or unparseable input.
        """
        if res_str is None or res_str == "":
            return 0
        s = str(res_str).strip()
        if not s or s == "0":
            return 0

        # Has unit suffix (K, M, G, T)? Parse with multiplier.
        m = _RES_PATTERN.match(s)
        if m:
            value = float(m.group(1))
            unit = m.group(2).lower()
            return int(value * _RES_MULTIPLIERS[unit])

        # Bare integer — BSD ps RSS convention: value is in KB
        try:
            return int(s) * 1024
        except ValueError:
            return 0

    @staticmethod
    def _post_process_processes(data: dict) -> dict:
        """
        Post-process JunOS process data for the dashboard.

        The shaper flattens row[0] into the top-level dict and puts
        rows[1:] into data["processes"]. For process collections we
        need ALL rows, so we reassemble row[0] from the top-level
        fields and prepend it to the list before filtering.

        Handles output from both templates:
          - extensive: has WCPU% (real CPU percentage)
          - detail:    has RSS in KB, cumulative TIME (no WCPU)

        Dashboard contract for each process:
            {pid, name, cpu_pct, holding}

        Where:
            cpu_pct  = WCPU from extensive, or 0 from detail
            holding  = RES/RSS in bytes (for formatBytes display)
        """
        # ── Reassemble full process list from shaper output ────
        # The shaper flattens row[0] to top-level and rows[1:] to
        # data["processes"]. Reconstruct row[0] if top-level has
        # process fields (pid is the telltale).
        overflow = data.get("processes", [])
        all_rows = list(overflow)  # copy — don't mutate original

        if data.get("pid"):
            # Row[0] was flattened — rebuild it from top-level fields
            row0 = {}
            for key in ("pid", "username", "pri", "nice", "size",
                        "res", "rss", "state", "time", "wcpu",
                        "name", "command", "uid", "ppid", "cpu_sched",
                        "stat", "started", "tt", "wchan"):
                if key in data:
                    row0[key] = data[key]
            all_rows.insert(0, row0)

        if not all_rows:
            data["processes"] = []
            return data

        normalized = []
        for proc in all_rows:
            name = proc.get("name", proc.get("command", ""))

            # Strip brackets from kernel thread names: [idle] → idle
            clean_name = name.strip("[]").strip()
            name_lower = clean_name.lower()

            # Filter kernel threads and system idle
            if name_lower in _KERNEL_FILTER:
                continue
            if any(name_lower.startswith(pfx) for pfx in _KERNEL_PREFIX):
                continue

            # ── Parse CPU percentage ───────────────────────────
            # extensive template: wcpu is "5.12" (% already stripped)
            # detail template:    no wcpu field → default 0
            wcpu_raw = proc.get("wcpu", proc.get("cpu_pct"))
            cpu_pct = 0.0
            if wcpu_raw is not None and wcpu_raw != "":
                try:
                    cpu_pct = round(float(str(wcpu_raw).rstrip('%')), 2)
                except (ValueError, TypeError):
                    cpu_pct = 0.0

            # ── Parse memory ───────────────────────────────────
            # extensive: RES with units ("45M")
            # detail:    RSS bare integer in KB ("95432")
            res_raw = proc.get("res", proc.get("rss"))
            holding = JuniperJunOSDriver._parse_res_to_bytes(res_raw)

            pid = proc.get("pid", "")
            try:
                pid = int(pid)
            except (ValueError, TypeError):
                pass

            normalized.append({
                "pid": pid,
                "name": clean_name,
                "cpu_pct": cpu_pct,
                "holding": holding,
            })

        # Sort by CPU% descending, memory as tiebreaker
        normalized.sort(
            key=lambda p: (p["cpu_pct"], p["holding"]),
            reverse=True,
        )

        # Top 15 for the dashboard widget
        data["processes"] = normalized[:15]
        return data

    # ── Neighbors ──────────────────────────────────────────────────────

    @staticmethod
    def _post_process_neighbors(neighbors: list[dict]) -> list[dict]:
        """
        Normalize LLDP neighbor fields for the dashboard graph.

        JunOS NTC "show lldp neighbors" template returns:
          local_interface, parent_interface, chassis_id,
          neighbor_interface, neighbor_name

        After the YAML normalize map runs, we expect:
          device_id, local_intf, remote_intf, mgmt_ip, platform, capabilities

        JunOS LLDP quirks vs Arista/Cisco:
        - neighbor_name is the LLDP system name (may be FQDN)
        - chassis_id is often the base MAC address
        - No platform/capabilities in the summary template —
          "show lldp neighbors detail" would be needed for those
        - parent_interface may be an aggregate (ae0) when local_interface
          is a member link
        """
        # Interface name abbreviations for graph labels
        _INTF_SHORT = {
            "ge-": "ge-",       # JunOS already uses short names
            "xe-": "xe-",       # but normalize any long forms
            "et-": "et-",
            "ae": "ae",
            "lo": "lo",
            "irb.": "irb.",
            "Ethernet": "Et",
            "GigabitEthernet": "Gi",
            "TenGigabitEthernet": "Te",
            "FastEthernet": "Fa",
            "Management": "Ma",
        }

        for nbr in neighbors:
            # Strip FQDN from device_id
            device_id = nbr.get("device_id", "")
            if "." in device_id and not device_id.replace(".", "").isdigit():
                nbr["device_id"] = device_id.split(".")[0]

            # Platform inference from device_id or any available description
            # JunOS LLDP summary doesn't include platform — infer what we can
            platform = nbr.get("platform", "")
            if not platform:
                desc = nbr.get("neighbor_description", "")
                if desc:
                    desc_lower = desc.lower()
                    if "juniper" in desc_lower or "junos" in desc_lower:
                        platform = "Juniper JunOS"
                    elif "arista" in desc_lower:
                        platform = "Arista EOS"
                    elif "cisco" in desc_lower and "nx-os" in desc_lower:
                        platform = "Cisco NX-OS"
                    elif "cisco" in desc_lower:
                        platform = "Cisco IOS"
                    else:
                        platform = desc[:40]  # Truncate long descriptions
                nbr["platform"] = platform

            # If still no platform, leave empty — graph renders without it

            # Shorten remote interface names (from non-Juniper neighbors)
            for field in ("local_intf", "remote_intf"):
                intf = nbr.get(field, "")
                for long, short in _INTF_SHORT.items():
                    if intf.startswith(long) and long != short:
                        nbr[field] = intf.replace(long, short, 1)
                        break

            # Capabilities inference for node shape
            # JunOS LLDP summary doesn't include capabilities.
            # Infer from platform or device_id if possible.
            caps = nbr.get("capabilities", "")
            if not caps and platform:
                platform_lower = platform.lower()
                if any(kw in platform_lower for kw in ("router", "mx", "srx", "ptx")):
                    caps = "Router"
                elif any(kw in platform_lower for kw in ("switch", "ex", "qfx")):
                    caps = "Switch"
            if caps:
                if isinstance(caps, list):
                    caps = ", ".join(str(c) for c in caps)
                nbr["capabilities"] = caps.strip()

        return neighbors

    # ── Interface Detail ───────────────────────────────────────────────

    @staticmethod
    def _post_process_interfaces(interfaces: list[dict]) -> list[dict]:
        """
        Post-process interface detail rows for Juniper JunOS.

        JunOS "show interfaces" NTC template returns:
          interface, link_status, admin_state, description, mtu,
          local (IP address), hardware_type

        Rate and error fields may or may not be present depending on
        the TextFSM template used (NTC's template captures limited
        fields — custom template may be needed for full counters).

        This method ensures all dashboard-expected fields exist as
        the correct types, regardless of what the parser captured.
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

            # ── Ensure rate fields are int bps ──────────────────────
            for field in ("input_rate_bps", "output_rate_bps"):
                raw = intf.get(field)
                if raw is not None:
                    intf[field] = _parse_rate_to_bps(raw)
                else:
                    # Try alternate field names from JunOS output
                    alt_field = field.replace("_bps", "")
                    raw_alt = intf.get(alt_field)
                    if raw_alt is not None:
                        intf[field] = _parse_rate_to_bps(raw_alt)
                    else:
                        intf[field] = 0

            # ── Ensure error counts are int ─────────────────────────
            for field in ("in_errors", "out_errors", "crc_errors"):
                intf[field] = _to_int(intf.get(field), 0)

            # ── Ensure MTU is int ───────────────────────────────────
            mtu_raw = intf.get("mtu", "")
            if str(mtu_raw).lower() == "unlimited":
                intf["mtu"] = 65535
            else:
                intf["mtu"] = _to_int(mtu_raw, 0)

            # ── Map link_status for consistency ─────────────────────
            # JunOS uses "up"/"down" for link_status, same as dashboard
            # admin_state might be "Enabled"/"Disabled" — map to status
            if "status" not in intf:
                admin = str(intf.get("admin_state", "")).lower()
                link = str(intf.get("link_status", "")).lower()
                if admin in ("disabled", "down"):
                    intf["status"] = "admin down"
                else:
                    intf["status"] = link

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
        return f"JuniperJunOSDriver({self.vendor})"