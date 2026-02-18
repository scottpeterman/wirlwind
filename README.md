# Wirlwind Telemetry

Real-time network device telemetry dashboard. SSH into a device, poll CLI commands on schedule, parse output through a fallback chain (TextFSM → TTP → regex), and render live throughput charts, interface tables, neighbor graphs, and device logs in a PyQt6 widget.

Built for network engineers who need to see what a device is doing *right now* — not what a monitoring system polled 5 minutes ago. Operational focus: traffic, topology, interface state, and log events. The panels you actually look at during an outage.


## What It Does

Wirlwind connects to a network device over SSH, runs vendor-specific CLI commands (`show interfaces`, `show lldp neighbors detail`, `show logging`, etc.), parses the output into structured data, and drives a live HTML dashboard rendered in a QWebEngine panel. Everything updates on a configurable poll schedule — interfaces and throughput every 60 seconds, neighbors every 5 minutes, logs every 30 seconds.

The dashboard runs standalone (own window) or embedded as a tab in [nterm](https://github.com/scottpeterman/nterm), a PyQt6 SSH terminal with network tooling integration.


![wirlwind screenshot](https://raw.githubusercontent.com/scottpeterman/wirlwind/refs/heads/main/screenshots/sample2.png)
![wirlwind screenshot](https://raw.githubusercontent.com/scottpeterman/wirlwind/refs/heads/main/screenshots/sample_light.png)


## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌───────────────┐     ┌────────────┐
│  SSH Client  │────▶│ Poll Engine  │────▶│ Parser Chain  │────▶│ State Store│
│  (paramiko)  │     │  (QThread)   │     │ FSM→TTP→regex │     │            │
└──────────────┘     └──────┬───────┘     └───────────────┘     └─────┬──────┘
                            │                                         │
                     ┌──────▼───────┐                          ┌──────▼──────┐
                     │Vendor Driver │                          │   Bridge    │
                     │ (normalize)  │                          │(QWebChannel)│
                     └──────────────┘                          └──────┬──────┘
                                                                      │
                                                               ┌──────▼──────┐
                                                               │  Dashboard  │
                                                               │  (ECharts)  │
                                                               └─────────────┘
```

**Key design principle:** The poll engine and dashboard are vendor-agnostic. All vendor-specific behavior — pagination commands, field name normalization, rate parsing — lives in vendor drivers (`drivers/`). All command definitions and parse instructions live in collection configs (`collections/`). Adding a new vendor or a new collection never requires touching the engine or the frontend.

### Components

| Component | File | Role |
|-----------|------|------|
| **Poll Engine** | `poll_engine.py` | QThread that runs the SSH → parse → store loop on schedule |
| **Parser Chain** | `parser_chain.py` | Ordered fallback: TextFSM → TTP → regex. First parser that returns structured data wins |
| **Collection Configs** | `collections/*/` | YAML files defining commands, parser templates, normalize maps, and schemas per vendor |
| **Vendor Drivers** | `drivers/` | Vendor-specific post-processing (field normalization, computed fields, rate unit conversion) |
| **Parse Trace** | `parse_trace.py` | Structured audit log — every parse attempt records what was tried and why it succeeded or failed |
| **State Store** | `state_store.py` | In-memory state with history ring buffers, emits Qt signals on update |
| **Bridge** | `bridge.py` | QWebChannel bridge between Python state store and JavaScript dashboard |
| **Dashboard** | `dashboard/index.html` | Single-file ECharts dashboard, receives JSON updates via QWebChannel |
| **Widget** | `widget.py` | Top-level PyQt6 widget that wires everything together |
| **SSH Client** | `ssh_client.py` | Paramiko wrapper with legacy cipher support, ANSI filtering, and prompt detection |
| **Client** | `client.py` | High-level client API |

## Quickstart

### Prerequisites

```bash
pip install PyQt6 PyQt6-WebEngine paramiko pyyaml textfsm ntc-templates
```

Optional: `pip install ttp` for TTP template support.

### Run Standalone

```bash
python -m wirlwind_telemetry --host 10.0.0.1 --vendor cisco_ios_xe --user admin
```

### Preflight Check

Validate templates resolve before connecting:

```bash
python -m wirlwind_telemetry --host 10.0.0.1 --vendor cisco_ios_xe --user admin --preflight-only --debug
```

### Embed in nterm

```python
from wirlwind_telemetry.widget import TelemetryWidget
from wirlwind_telemetry.auth_interface import SimpleAuthProvider, DeviceTarget

auth = SimpleAuthProvider("admin", password="cisco")
target = DeviceTarget("10.0.0.1", vendor="cisco_ios_xe")

widget = TelemetryWidget(auth_provider=auth, parent=tab_widget)
widget.start(target)
```

## Supported Vendors

| Vendor ID | Platform | Driver | Status |
|-----------|----------|--------|--------|
| `cisco_ios` | Cisco IOS 15.x | `CiscoIOSDriver` | Full |
| `cisco_ios_xe` | Cisco IOS-XE 16.x/17.x | `CiscoIOSDriver` | Full |
| `cisco_nxos` | Cisco NX-OS | `CiscoNXOSDriver` | Partial |
| `arista_eos` | Arista EOS | `AristaEOSDriver` | Production-tested |
| `juniper_junos` | Juniper JunOS | `JuniperJunOSDriver` | Production-tested |

Adding a vendor requires only a new driver file in `drivers/` and collection YAML configs — no engine changes.

## Collections

Each collection is a directory under `collections/` containing per-vendor YAML configs (command, parsers, normalize map) and a `_schema.yaml` defining canonical fields and types.

| Collection | Command (IOS/IOS-XE) | Interval | Dashboard Panel |
|------------|----------------------|----------|-----------------|
| `interfaces` | `show ip interface brief` | 60s | Interface status table |
| `interface_detail` | `show interfaces` | 60s | Throughput chart (per-interface selector, auto-scaling bps→Kbps→Mbps→Gbps) |
| `neighbors` | `show cdp neighbors detail` | 300s | CDP/LLDP force-directed topology graph |
| `log` | `show logging` | 30s | Device log viewer with severity coloring |

## Dashboard Panels

The dashboard is built around four operational panels — the things you actually look at during an incident:

| Panel | Data Source | Features |
|-------|-----------|----------|
| **Interface Throughput** | `interface_detail` | Area chart with auto-scaling (bps/Kbps/Mbps/Gbps), per-interface dropdown or aggregate all, live rate badge |
| **LLDP/CDP Neighbors** | `neighbors` | Force-directed graph: routers (cyan roundRect), switches (green rect), unknown (amber circle). Edge labels show local ↔ remote interfaces. Management IP overlay |
| **Interface Status** | `interfaces` | Full interface table: name, description, status. Color-coded up/down/admin-down with count badge |
| **Device Log** | `log` | Newest-first syslog entries, severity-colored facility/mnemonic tags, warning count badge. Raw text fallback if structured parsing fails — the panel always shows something |

Four collections, four panels — each one earning its screen space during an incident.

## Log Resilience

Log parsing across vendors is inherently fragile — different IOS versions, JunOS BSD syslog format, Arista's structured logging all produce different output. The log pipeline is designed to never fail silently:

- Structured parsing processes entries individually (one bad row can't kill the panel)
- If all structured entries fail, falls back to raw CLI output split one-line-per-entry
- JunOS gets its own log processor with keyword-based severity inference
- The `_log_fallback` flag in debug output shows when raw mode activated

## Custom Templates

When an NTC TextFSM template breaks on a specific IOS version (and they do), drop a fixed copy into `templates/textfsm/`. The resolver searches local templates first, NTC second. Reference multiple templates in collection YAML and they're tried in order:

```yaml
parsers:
  - type: textfsm
    templates:
      - my_fixed_show_interfaces.textfsm          # tried first (local)
      - cisco_ios_show_interfaces.textfsm          # tried second (ntc-templates)
  - type: regex                                     # tried third
    pattern: 'Interface\s+(\S+)\s+is\s+(up|down)...'
```

## Debug & Troubleshooting

Run with `--debug` for full parse trace output:

```
TRACE [interfaces] parsed_by=textfsm rows=15 fields=6 duration=5.2ms
TRACE [interface_detail] parsed_by=textfsm rows=15 fields=25 duration=18.7ms
TRACE [neighbors] parsed_by=textfsm rows=3 fields=7 duration=4.1ms
TRACE [log] parsed_by=regex rows=28 fields=5 duration=2.1ms
```

When a parser fails, the trace shows exactly why:

```
TRACE [log] parsed_by=none rows=0 fields=0 ERROR=all parsers failed (regex: 0 matches for pattern)
```

At DEBUG level, full structured JSON traces show every step: command sent, raw output preview, sanitization, each template tried and its resolution path, normalization, type coercion, and final delivery to the state store.

The dashboard's `{ }` debug buttons (on each panel header) dump the current state store JSON for any collection, showing exactly what data reached the frontend — including parser metadata (`_parsed_by`, `_template`) and any error state.

## Project Structure

```
wirlwind_telemetry/
├── __init__.py
├── __main__.py              # CLI launcher + preflight checks
├── poll_engine.py           # SSH poll loop (vendor-agnostic)
├── parser_chain.py          # TextFSM → TTP → regex fallback chain
├── parse_trace.py           # Structured parse audit logging
├── widget.py                # PyQt6 top-level widget
├── bridge.py                # QWebChannel Python↔JS bridge
├── state_store.py           # In-memory state + history
├── ssh_client.py            # Paramiko wrapper (legacy ciphers, ANSI filter)
├── client.py                # High-level client API
├── auth_interface.py        # Auth provider abstraction
├── drivers/                 # Vendor-specific behavior
│   ├── __init__.py          # Base driver + registry + shared transforms
│   ├── cisco_ios.py         # Cisco IOS/IOS-XE
│   ├── cisco_nxos.py        # Cisco NX-OS
│   ├── arista_eos.py        # Arista EOS
│   └── juniper_junos.py     # Juniper JunOS
├── collections/             # Collection configs (YAML per vendor + schema)
│   ├── interface_detail/
│   │   ├── arista_eos.yaml
│   │   ├── cisco_ios.yaml
│   │   ├── cisco_ios_xe.yaml
│   │   ├── juniper_junos.yaml
│   │   └── _schema.yaml
│   ├── interfaces/
│   │   ├── arista_eos.yaml
│   │   ├── cisco_ios.yaml
│   │   ├── cisco_ios_xe.yaml
│   │   ├── juniper_junos.yaml
│   │   └── _schema.yaml
│   ├── log/
│   │   ├── arista_eos.yaml
│   │   ├── cisco_ios.yaml
│   │   ├── cisco_ios_xe.yaml
│   │   ├── juniper_junos.yaml
│   │   └── _schema.yaml
│   └── neighbors/
│       ├── arista_eos.yaml
│       ├── cisco_ios.yaml
│       ├── cisco_ios_xe.yaml
│       ├── juniper_junos.yaml
│       └── _schema.yaml
├── templates/
│   └── textfsm/             # Custom TextFSM overrides (searched before ntc-templates)
│       ├── arista_eos_show_interfaces.textfsm
│       ├── arista_eos_show_processes_top_once.textfsm
│       ├── cisco_ios_show_processes_memory_sorted.textfsm
│       ├── juniper_junos_show_chassis_routing-engine.textfsm
│       ├── juniper_junos_show_interfaces_descriptions.textfsm
│       ├── juniper_junos_show_interfaces_detail.textfsm
│       ├── juniper_junos_show_interfaces_terse.textfsm
│       ├── juniper_junos_show_lldp_neighbors.textfsm
│       ├── juniper_junos_show_lldp_neighbors_detail.textfsm
│       ├── juniper_junos_show_log_messages.textfsm
│       └── juniper_junos_show_system_processes_extensive.textfsm
├── dashboard/
│   └── index.html           # ECharts dashboard (single file)
```

## License

GPLv3 — required by PyQt6 dependency.