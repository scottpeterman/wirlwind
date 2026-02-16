"""
Vendor Drivers — Vendor-specific behavior abstracted from the poll engine.

The poll engine delegates all vendor-specific logic to a driver:
  - Pagination commands
  - Post-processing transforms (normalize CPU fields, compute memory %, etc.)
  - Output shaping (which collections are single-row vs list, wrapper keys)
  - Cross-collection joins (merge memory holdings into CPU processes)
  - Field aliasing for dashboard consumption

Each vendor subclass overrides only what differs from the base.
The base driver handles common transforms that work across vendors.

Usage:
    driver = get_driver("cisco_ios_xe")
    driver.disable_pagination(ssh_client)
    data = driver.shape_output("cpu", rows, meta)
    data = driver.post_process("cpu", data, state_store)

Adding a new vendor:
    1. Create drivers/my_vendor.py
    2. Subclass VendorDriver
    3. Register with @register_driver("my_vendor")
    4. Override only what differs
"""

from __future__ import annotations
import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .state_store import DeviceStateStore

logger = logging.getLogger(__name__)


# ── Driver registry ──────────────────────────────────────────────────

_DRIVER_REGISTRY: dict[str, type["VendorDriver"]] = {}


def register_driver(*vendor_ids: str):
    """
    Class decorator to register a driver for one or more vendor IDs.

    Usage:
        @register_driver("cisco_ios", "cisco_ios_xe")
        class CiscoIOSDriver(VendorDriver):
            ...
    """
    def decorator(cls):
        for vid in vendor_ids:
            _DRIVER_REGISTRY[vid] = cls
            logger.debug(f"Registered driver: {vid} → {cls.__name__}")
        return cls
    return decorator


def get_driver(vendor: str) -> "VendorDriver":
    """
    Get a driver instance for a vendor.

    Falls back to BaseDriver for unknown vendors — never fails,
    always returns something usable.
    """
    driver_cls = _DRIVER_REGISTRY.get(vendor)

    if driver_cls is None:
        # Try without trailing platform suffix: cisco_ios_xe → cisco_ios
        if "_" in vendor:
            base = vendor.rsplit("_", 1)[0]
            driver_cls = _DRIVER_REGISTRY.get(base)

    if driver_cls is None:
        logger.info(f"No driver registered for '{vendor}', using BaseDriver")
        return BaseDriver(vendor)

    return driver_cls(vendor)


def list_drivers() -> dict[str, str]:
    """List registered vendor IDs and their driver class names."""
    return {vid: cls.__name__ for vid, cls in _DRIVER_REGISTRY.items()}


# ── Base driver ──────────────────────────────────────────────────────

# Default wrapper keys for multi-row collections
# Maps collection name → the key used to wrap the list in the state dict
COLLECTION_LIST_KEYS = {
    "interfaces": "interfaces",
    "interface_detail": "interfaces",
    "bgp_summary": "peers",
    "neighbors": "neighbors",
    "log": "entries",
    "environment": "sensors",
}

# Collections where rows collapse to a flat dict (first row = summary)
SINGLE_ROW_COLLECTIONS = {"cpu", "memory", "device_info"}


class VendorDriver(ABC):
    """
    Abstract base for vendor-specific behavior.

    Subclasses override methods where their vendor diverges from
    the common base implementation.
    """

    def __init__(self, vendor: str):
        self.vendor = vendor

    @property
    @abstractmethod
    def pagination_command(self) -> str:
        """Command to disable CLI pagination on this vendor."""
        ...

    def shape_output(
        self, collection: str, rows: list[dict], meta: dict
    ) -> dict:
        """
        Convert parsed rows into the dict structure the state store expects.

        Override in subclasses only if a vendor has truly different output
        shapes. The base implementation handles the common patterns.
        """
        return _default_shape_output(collection, rows)

    def post_process(
        self,
        collection: str,
        data: dict,
        state_store: "DeviceStateStore" = None,
    ) -> dict:
        """
        Apply vendor-specific transforms after parsing and shaping.

        The base implementation runs common transforms (memory %,
        log assembly). Vendor subclasses call super() then add
        their own transforms.
        """
        if collection == "memory":
            data = _compute_memory_pct(data)

        if collection == "log":
            data = _post_process_log(data)

        if collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        return data


class BaseDriver(VendorDriver):
    """
    Fallback driver for unknown vendors.

    Uses shotgun pagination and only common transforms.
    """

    @property
    def pagination_command(self) -> str:
        return ""  # Empty = use shotgun approach in the engine

    def __repr__(self):
        return f"BaseDriver({self.vendor})"


# ── Shared transforms ───────────────────────────────────────────────
# These are used by all drivers. Vendor drivers call them explicitly
# or via super().post_process(). Not in the ABC so they're reusable.

def _default_shape_output(collection: str, rows: list[dict]) -> dict:
    """
    Convert parser chain rows into state store dict.

    Single-row collections (cpu, memory) → flat dict.
    Multi-row collections (interfaces, bgp) → {list_key: [rows]}.
    CPU special case: first row is summary, rest are processes.
    """
    if not rows:
        return {}

    if collection in SINGLE_ROW_COLLECTIONS:
        result = dict(rows[0])
        # CPU: multiple rows = summary + process list
        if collection == "cpu" and len(rows) > 1:
            result["processes"] = rows[1:]
        return result

    # Multi-row: wrap in expected key
    key = COLLECTION_LIST_KEYS.get(collection)
    if key:
        return {key: rows}

    # Unknown collection: generic wrapper
    return {"data": rows}


def _compute_memory_pct(data: dict) -> dict:
    """
    Compute used_pct from whatever memory fields are available.

    Tries normalized names first, then raw TextFSM names, then legacy.
    This is vendor-agnostic — the normalize map in collection YAML
    should have already mapped vendor fields to canonical names,
    but we handle unmapped fields defensively.
    """
    # Find total — try canonical names first
    total = _first_numeric(data,
        "total_bytes", "total_kb", "total_mb", "total", "memory_total")

    # Find used
    used = _first_numeric(data,
        "used_bytes", "used_kb", "used_mb", "used", "memory_used")

    # Find free
    free = _first_numeric(data,
        "free_bytes", "free", "free_kb", "memory_free")

    # Derive used from total - free
    if total is not None and free is not None and used is None:
        used = total - free

    if total is not None and used is not None and total > 0:
        data["used_pct"] = round(used / total * 100, 1)

        # Human-readable display values
        if total > 1_000_000_000:
            data["total_display"] = f"{total / (1024**3):.1f} GB"
            data["used_display"] = f"{used / (1024**3):.1f} GB"
        elif total > 1_000_000:
            data["total_display"] = f"{total / (1024**2):.1f} MB"
            data["used_display"] = f"{used / (1024**2):.1f} MB"
        elif total > 1_000:
            data["total_display"] = f"{total / 1024:.1f} KB"
            data["used_display"] = f"{used / 1024:.1f} KB"

    return data


def _filter_cpu_processes(data: dict) -> dict:
    """
    Filter idle processes and add dashboard-friendly field aliases.

    Removes processes with 0% 5-sec CPU. Adds short aliases:
        process_pid → pid
        process_name → name
        process_cpu_usage_5_sec → cpu_pct / five_sec
        process_cpu_usage_1_min → cpu_1min
        process_cpu_usage_5_min → cpu_5min

    These aliases match what the dashboard JS expects.
    """
    processes = data.get("processes")
    if not processes:
        return data

    active = []
    for proc in processes:
        cpu_5s = _to_float(proc.get("process_cpu_usage_5_sec",
                           proc.get("cpu_pct",
                           proc.get("five_sec", "0"))))

        if cpu_5s is not None and cpu_5s > 0.0:
            # Add dashboard aliases alongside raw fields
            proc["pid"] = proc.get("pid", proc.get("process_pid", ""))
            proc["name"] = proc.get("name", proc.get("process_name", ""))
            proc["cpu_pct"] = cpu_5s
            proc["five_sec"] = cpu_5s

            proc["cpu_1min"] = _to_float(
                proc.get("cpu_1min", proc.get("process_cpu_usage_1_min", "0"))
            ) or 0.0
            proc["cpu_5min"] = _to_float(
                proc.get("cpu_5min", proc.get("process_cpu_usage_5_min", "0"))
            ) or 0.0

            active.append(proc)
        elif cpu_5s is None:
            # Can't parse — keep it, don't silently discard
            active.append(proc)

    data["processes"] = active
    return data


def _merge_memory_into_processes(
    data: dict, state_store: "DeviceStateStore"
) -> dict:
    """
    Cross-reference per-process memory from the memory collection
    into CPU process dicts.

    The NTC memory template returns parallel lists:
        process_id:      ['209', '66', ...]
        process_holding: ['11200', '18600', ...]

    Adds 'holding' (bytes int) to each CPU process for the dashboard.
    """
    processes = data.get("processes")
    if not processes or state_store is None:
        return data

    try:
        mem_data = state_store.get("memory")
    except Exception:
        return data

    if not mem_data:
        return data

    pids = mem_data.get("process_id", [])
    holdings = mem_data.get("process_holding", [])

    if not pids or not holdings or len(pids) != len(holdings):
        return data

    pid_to_holding = {}
    for pid, holding in zip(pids, holdings):
        try:
            pid_to_holding[str(pid)] = int(holding)
        except (ValueError, TypeError):
            pass

    for proc in processes:
        pid = str(proc.get("pid", proc.get("process_pid", "")))
        if pid in pid_to_holding:
            proc["holding"] = pid_to_holding[pid]

    return data


def _normalize_bgp_peers(peers: list[dict]) -> list[dict]:
    """
    Normalize BGP peer state across vendors.

    The state_pfx field is either a state string ("Idle", "Active")
    or a number (prefix count = established).
    """
    for peer in peers:
        state_pfx = str(peer.get("state_pfx", ""))
        try:
            pfx_count = int(state_pfx)
            peer["state"] = "Established"
            peer["prefixes_rcvd"] = pfx_count
        except (ValueError, TypeError):
            peer["state"] = state_pfx if state_pfx else "Unknown"
            peer["prefixes_rcvd"] = 0
    return peers


def _post_process_log(data: dict, max_entries: int = 50) -> dict:
    """
    Post-process log entries:
    - Assemble timestamp from TextFSM month/day/time fields
    - Join message lists into strings
    - Coerce severity to int
    - Reverse to newest-first
    - Trim to max_entries
    """
    entries = data.get("entries")
    if not entries:
        return data

    for entry in entries:
        # Assemble timestamp from components
        if "timestamp" not in entry and "month" in entry:
            parts = [
                entry.get("month", ""),
                entry.get("day", ""),
                entry.get("time", ""),
            ]
            tz = entry.get("timezone", "")
            ts = " ".join(p for p in parts if p)
            if tz:
                ts += f" {tz}"
            entry["timestamp"] = ts

        # Join message list
        msg = entry.get("message", "")
        if isinstance(msg, list):
            entry["message"] = " ".join(str(m) for m in msg if m)

        # Coerce severity
        sev = entry.get("severity")
        if sev is not None:
            try:
                entry["severity"] = int(sev)
            except (ValueError, TypeError):
                pass

    # Newest first
    entries.reverse()
    data["entries"] = entries[:max_entries]
    return data


# ── Utility helpers ──────────────────────────────────────────────────

def _first_numeric(data: dict, *keys) -> Optional[float]:
    """Return the first non-None numeric value from a sequence of keys."""
    for key in keys:
        val = data.get(key)
        if val is not None:
            try:
                return float(str(val).replace(",", ""))
            except (ValueError, TypeError):
                continue
    return None


def _to_float(val) -> Optional[float]:
    """Safely convert a value to float."""
    if val is None:
        return None
    try:
        return float(str(val).replace("%", "").replace(",", ""))
    except (ValueError, TypeError):
        return None


# ── Auto-import driver submodules ────────────────────────────────────
# The @register_driver decorator only fires when Python imports the
# module. This block discovers and imports all .py files in this
# package so drivers register themselves automatically.
#
# To add a new vendor: just drop a .py file in drivers/ with
# @register_driver("vendor_id") on the class. No other wiring needed.

import importlib
import pkgutil

def _auto_import_drivers():
    """Import all driver modules in this package."""
    package_path = __path__
    package_name = __name__

    for importer, modname, ispkg in pkgutil.iter_modules(package_path):
        if modname.startswith("_"):
            continue
        try:
            importlib.import_module(f"{package_name}.{modname}")
        except Exception as e:
            logger.warning(f"Failed to import driver module '{modname}': {e}")

_auto_import_drivers()