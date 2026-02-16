#!/usr/bin/env python3
"""
Parser Chain Test Harness — validate parsing against sample CLI output.

Run:
    python test_parser_chain.py

This tests the regex fallback path (TextFSM/TTP require pip install).
The same harness works for all parser types once installed.
"""

import sys
import json
from pathlib import Path

# Add parent to path for import
sys.path.insert(0, str(Path(__file__).parent))
from parser_chain import ParserChain, _meta

# ── Sample CLI outputs ─────────────────────────────────────────────

SAMPLE_SHOW_IP_INTF_BRIEF = """
Interface                  IP-Address      OK? Method Status                Protocol
FastEthernet0/0            unassigned      YES NVRAM  administratively down down
Ethernet1/0                172.16.1.2      YES NVRAM  up                    up
Ethernet1/1                172.16.100.1    YES NVRAM  up                    up
Ethernet1/2                172.16.128.1    YES NVRAM  up                    up
Ethernet1/3                unassigned      YES NVRAM  administratively down down
Ethernet2/0                unassigned      YES NVRAM  administratively down down
Ethernet2/1                unassigned      YES NVRAM  administratively down down
Ethernet2/2                unassigned      YES NVRAM  administratively down down
Ethernet2/3                unassigned      YES NVRAM  administratively down down
Ethernet3/0                unassigned      YES NVRAM  administratively down down
Ethernet3/1                unassigned      YES NVRAM  administratively down down
"""

SAMPLE_SHOW_PROC_CPU = """
CPU utilization for five seconds: 1%/0%; one minute: 2%; five minutes: 1%
 PID Runtime(ms)     Invoked      uSecs   5Sec   1Min   5Min TTY Process
   1       23480      272893         86  0.00%  0.00%  0.00%   0 Chunk Manager
   2       38920      154882        251  0.00%  0.00%  0.00%   0 Load Meter
   3           0           3          0  0.00%  0.00%  0.00%   0 SpanTree Helper
   5      105300     4831208         21  0.07%  0.01%  0.00%   0 Check heaps
   6         320       27232         11  0.00%  0.00%  0.00%   0 Pool Manager
"""

SAMPLE_SHOW_BGP_SUMMARY = """
BGP router identifier 172.16.100.1, local AS number 65001
BGP table version is 15, main routing table version 15
10 network entries using 1440 bytes of memory
10 path entries using 800 bytes of memory
4/4 BGP path/bestpath attribute entries using 1120 bytes of memory
3 BGP AS-PATH entries using 72 bytes of memory
0 BGP route-map cache entries using 0 bytes of memory
0 BGP filter-list cache entries using 0 bytes of memory
BGP using 3432 total bytes of memory
BGP activity 20/10 prefixes, 22/12 paths, scan interval 60 secs

Neighbor        V           AS MsgRcvd MsgSent   TblVer  InQ OutQ Up/Down  State/PfxRcd
172.16.1.1      4        65002    4521    4518       15    0    0 3d02h           5
172.16.128.2    4        65003    4519    4516       15    0    0 3d02h           5
10.0.0.1        4        65004       0       0        1    0    0 never    Idle
"""

SAMPLE_SHOW_MEM = """
Processor Pool Total:  409190504 Used:  265844792 Free:  143345712
      lsmi Pool Total:    6295128 Used:    6294296 Free:        832
"""

# ── Collection configs (inline — same format as YAML files) ────────

CONFIG_INTERFACES = {
    "command": "show ip interface brief",
    "interval": 60,
    "parsers": [
        {
            "type": "textfsm",
            "templates": ["cisco_ios_show_ip_interface_brief.textfsm"]
        },
        {
            "type": "regex",
            "pattern": r"^(\S+)\s+([\d.]+|unassigned)\s+\w+\s+\w+\s+((?:administratively )?(?:up|down))\s+(up|down)\s*$",
            "flags": "MULTILINE",
            "groups": {"intf": 1, "ipaddr": 2, "status": 3, "proto": 4}
        }
    ],
    "normalize": {"name": "intf", "ip_address": "ipaddr", "status": "status", "protocol": "proto"}
}

CONFIG_CPU = {
    "command": "show processes cpu sorted",
    "interval": 30,
    "parsers": [
        {
            "type": "textfsm",
            "templates": ["cisco_ios_show_processes_cpu_sorted.textfsm"]
        },
        {
            "type": "regex",
            "pattern": r"CPU utilization for five seconds:\s+(\d+)%/(\d+)%;\s+one minute:\s+(\d+)%;\s+five minutes:\s+(\d+)%",
            "flags": "DOTALL",
            "groups": {"five_sec_total": 1, "five_sec_interrupts": 2, "one_min": 3, "five_min": 4}
        }
    ],
    "normalize": {"five_sec": "five_sec_total", "one_min": "one_min", "five_min": "five_min"}
}

CONFIG_BGP = {
    "command": "show ip bgp summary",
    "interval": 60,
    "parsers": [
        {
            "type": "textfsm",
            "templates": ["cisco_ios_show_ip_bgp_summary.textfsm"]
        },
        {
            "type": "regex",
            "pattern": r"^([\d.]+)\s+4\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+(\S+)\s+(\S+)\s*$",
            "flags": "MULTILINE",
            "groups": {"neighbor": 1, "remote_as": 2, "updown": 3, "state_pfx": 4}
        }
    ],
    "normalize": {"neighbor": "neighbor", "remote_as": "remote_as", "uptime": "updown"}
}

CONFIG_MEMORY = {
    "command": "show processes memory sorted",
    "interval": 60,
    "parsers": [
        {
            "type": "textfsm",
            "templates": ["cisco_ios_show_processes_memory_sorted.textfsm"]
        },
        {
            "type": "regex",
            "pattern": r"Processor Pool Total:\s+(\d+)\s+Used:\s+(\d+)\s+Free:\s+(\d+)",
            "flags": "",
            "groups": {"total": 1, "used": 2, "free": 3}
        }
    ],
    "normalize": {"total": "total", "used": "used", "free": "free"}
}


# ── Schema stubs for type coercion ─────────────────────────────────

SCHEMA_CPU = {
    "fields": {
        "five_sec": {"type": "float"},
        "one_min": {"type": "float"},
        "five_min": {"type": "float"},
    }
}

SCHEMA_MEMORY = {
    "fields": {
        "total": {"type": "int"},
        "used": {"type": "int"},
        "free": {"type": "int"},
        "used_pct": {"type": "float"},
    }
}

SCHEMA_BGP = {
    "fields": {
        "remote_as": {"type": "int"},
    }
}


# ── Run tests ──────────────────────────────────────────────────────

def run_test(name, raw_output, config, schema=None):
    """Run a single parser chain test."""
    print(f"\n{'='*60}")
    print(f"  {name}")
    print(f"{'='*60}")

    chain = ParserChain()
    rows, meta = chain.parse(raw_output, config, schema)

    print(f"\n  Parser:   {meta['_parsed_by']}")
    print(f"  Template: {meta.get('_template', '—')}")
    if meta.get('_error'):
        print(f"  Error:    {meta['_error']}")
    print(f"  Rows:     {len(rows)}")

    # Pretty-print the combined result (data + metadata)
    output = {"_meta": meta, "data": rows}
    print(f"\n{json.dumps(output, indent=2)}")

    return rows, meta


def main():
    print("Parser Chain Test Harness")
    print("=" * 60)

    chain = ParserChain()
    caps = chain.capabilities
    print(f"\nParser capabilities:")
    print(f"  TextFSM:       {'YES' if caps['textfsm'] else 'NO (pip install textfsm)'}")
    print(f"  TTP:           {'YES' if caps['ttp'] else 'NO (pip install ttp)'}")
    print(f"  Regex:         YES (always)")
    print(f"  ntc-templates: {'YES' if caps['ntc_templates'] else 'NO (pip install ntc-templates)'}")
    if caps['ntc_templates_path']:
        print(f"  ntc path:      {caps['ntc_templates_path']}")

    # Test interfaces
    rows, meta = run_test(
        "INTERFACES — show ip interface brief",
        SAMPLE_SHOW_IP_INTF_BRIEF,
        CONFIG_INTERFACES,
    )
    assert len(rows) == 11, f"Expected 11 interfaces, got {len(rows)}"
    assert rows[0]["name"] == "FastEthernet0/0"
    assert rows[1]["ip_address"] == "172.16.1.2"
    assert rows[1]["status"] == "up"
    assert rows[0]["status"] == "administratively down"
    print("\n  ✓ Interface parsing validated")

    # Test CPU
    rows, meta = run_test(
        "CPU — show processes cpu sorted",
        SAMPLE_SHOW_PROC_CPU,
        CONFIG_CPU,
        SCHEMA_CPU,
    )
    assert len(rows) >= 1
    assert rows[0]["five_sec"] == 1.0
    assert rows[0]["five_min"] == 1.0
    assert rows[0]["one_min"] == 2.0
    print("\n  ✓ CPU parsing validated")

    # Test BGP
    rows, meta = run_test(
        "BGP — show ip bgp summary",
        SAMPLE_SHOW_BGP_SUMMARY,
        CONFIG_BGP,
        SCHEMA_BGP,
    )
    assert len(rows) == 3, f"Expected 3 peers, got {len(rows)}"
    assert rows[0]["neighbor"] == "172.16.1.1"
    assert rows[0]["remote_as"] == 65002
    assert rows[2]["neighbor"] == "10.0.0.1"
    print("\n  ✓ BGP parsing validated")

    # Test Memory
    rows, meta = run_test(
        "MEMORY — show processes memory sorted",
        SAMPLE_SHOW_MEM,
        CONFIG_MEMORY,
        SCHEMA_MEMORY,
    )
    assert len(rows) >= 1
    assert rows[0]["total"] == 409190504
    assert rows[0]["used"] == 265844792
    print("\n  ✓ Memory parsing validated")

    # Test empty input
    rows, meta = run_test(
        "EMPTY INPUT — graceful failure",
        "",
        CONFIG_INTERFACES,
    )
    assert meta["_parsed_by"] == "none"
    assert "_error" in meta
    print("\n  ✓ Empty input handled")

    # Test no matching parsers
    rows, meta = run_test(
        "NO MATCH — garbage input",
        "This is not CLI output at all\nJust random text\n",
        CONFIG_INTERFACES,
    )
    assert meta["_parsed_by"] == "none"
    assert "all parsers failed" in meta["_error"]
    print("\n  ✓ No-match failure handled")

    print(f"\n{'='*60}")
    print("  ALL TESTS PASSED")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
