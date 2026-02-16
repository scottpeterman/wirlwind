"""
Arista EOS Vendor Driver.

Handles raw TextFSM field names directly (global_cpu_percent_idle, etc.)
rather than depending on the normalize map for remapping.
"""

from __future__ import annotations
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

        return data

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

    @staticmethod
    def _build_process_list(data: dict) -> dict:
        """
        Alias Arista process fields to dashboard-expected names.

        TextFSM per-process rows have:
          pid, command, percent_cpu, percent_memory, resident_memory_size
        Dashboard expects:
          pid, name, cpu_pct, five_sec, holding
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

            if cpu_pct is not None and cpu_pct > 0.0:
                proc["pid"] = proc.get("pid", "")
                proc["name"] = (
                    proc.get("command")
                    or proc.get("name")
                    or ""
                )
                proc["cpu_pct"] = cpu_pct
                proc["five_sec"] = cpu_pct

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

        active.sort(key=lambda p: p.get("cpu_pct", 0), reverse=True)
        data["processes"] = active
        return data

    def __repr__(self):
        return f"AristaEOSDriver({self.vendor})"