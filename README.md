# Wirlwind Telemetry

Real-time network device telemetry dashboard. SSH into a device, poll CLI commands on schedule, parse output through a fallback chain (TextFSM → TTP → regex), and render live ECharts gauges, tables, and trend charts in a PyQt6 widget.

Built for network engineers who need to see what a device is doing *right now* — not what a monitoring system polled 5 minutes ago.

![Wirlwind Telemetry Dashboard](screenshots/Screenshot%20from%202026-02-15%2005-35-50.png)

## What It Does

Wirlwind connects to a network device over SSH, runs vendor-specific CLI commands (`show processes cpu sorted`, `show ip interface brief`, etc.), parses the output into structured data, and drives a live HTML dashboard rendered in a QWebEngine panel. Everything updates on a configurable poll schedule — CPU and memory every 30 seconds, interfaces every 60, neighbors every 5 minutes.

The dashboard runs either standalone (own window) or embedded as a tab in [nterm](https://github.com/scottpeterman/nterm), a PyQt6 SSH terminal with network tooling integration.

## Architecture

```
┌──────────────┐     ┌─────────────┐     ┌──────────────┐     ┌────────────┐
│  SSH Client  │────▶│ Poll Engine  │────▶│ Parser Chain  │────▶│ State Store│
│  (paramiko)  │     │  (QThread)   │     │ FSM→TTP→regex │     │            │
└──────────────┘     └──────┬───────┘     └──────────────┘     └─────┬──────┘
                            │                                        │
                     ┌──────▼───────┐                         ┌──────▼──────┐
                     │Vendor Driver │                         │   Bridge    │
                     │ (normalize)  │                         │(QWebChannel)│
                     └──────────────┘                         └──────┬──────┘
                                                                     │
                                                              ┌──────▼──────┐
                                                              │  Dashboard  │
                                                              │  (ECharts)  │
                                                              └─────────────┘
```

**Key design principle:** The poll engine and dashboard are vendor-agnostic. All vendor-specific behavior — pagination commands, field name normalization, CPU/memory math — lives in vendor drivers (`drivers/`). All command definitions and parse instructions live in collection configs (`collections/`). Adding a new vendor or a new collection never requires touching the engine or the frontend.

### Components

| Component | File | Role |
|-----------|------|------|
| **Poll Engine** | `poll_engine.py` | QThread that runs the SSH → parse → store loop on schedule |
| **Parser Chain** | `parser_chain.py` | Ordered fallback: TextFSM → TTP → regex. First parser that returns structured data wins |
| **Collection Configs** | `collections/*/` | YAML files defining commands, parser templates, normalize maps, and schemas per vendor |
| **Vendor Drivers** | `drivers/` | Vendor-specific post-processing (field normalization, computed fields, cross-collection joins) |
| **Parse Trace** | `parse_trace.py` | Structured audit log — every parse attempt records what was tried and why it succeeded or failed |
| **State Store** | `state_store.py` | In-memory state with history ring buffers, emits Qt signals on update |
| **Bridge** | `bridge.py` | QWebChannel bridge between Python state store and JavaScript dashboard |
| **Dashboard** | `dashboard/index.html` | Single-file ECharts dashboard, receives JSON updates via QWebChannel |
| **Widget** | `widget.py` | Top-level PyQt6 widget that wires everything together |
| **SSH Client** | `ssh_client.py` | Paramiko wrapper with legacy cipher support, ANSI filtering, and prompt detection |

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

| Vendor ID | Platform | Driver |
|-----------|----------|--------|
| `cisco_ios` | Cisco IOS 15.x | `CiscoIOSDriver` |
| `cisco_ios_xe` | Cisco IOS-XE 16.x/17.x | `CiscoIOSDriver` |
| `cisco_nxos` | Cisco NX-OS | `CiscoNXOSDriver` |
| `arista_eos` | Arista EOS | `AristaEOSDriver` |
| `juniper_junos` | Juniper JunOS | `JuniperJunOSDriver` |

Adding a vendor requires only a new driver file in `drivers/` and collection YAML configs — no engine changes.

## Collections

Each collection is a directory under `collections/` containing:
- Per-vendor YAML configs (command, parsers, normalize map)
- A `_schema.yaml` defining canonical fields and types

| Collection | Command (IOS-XE) | Interval | Status |
|------------|-------------------|----------|--------|
| `cpu` | `show processes cpu sorted` | 30s | ✓ Working |
| `memory` | `show processes memory sorted` | 30s | ✓ Working |
| `interfaces` | `show ip interface brief` | 60s | ✓ Working |
| `log` | `show logging` | 30s | ✓ Working |
| `bgp_summary` | `show ip bgp summary` | 60s | ✓ Working |
| `neighbors` | `show lldp neighbors detail` | 300s | Planned |
| `environment` | `show environment all` | 120s | Planned |
| `interface_detail` | `show interfaces` | 60s | Planned |

## Custom Templates

When an NTC TextFSM template breaks on a specific IOS version (and they do), drop a fixed copy into `templates/textfsm/`. The resolver searches local templates first, NTC second. Reference multiple templates in collection YAML and they're tried in order:

```yaml
parsers:
  - type: textfsm
    templates:
      - my_fixed_show_processes_cpu.textfsm      # tried first (local)
      - cisco_ios_show_processes_cpu.textfsm      # tried second (ntc-templates)
  - type: regex                                    # tried third
    pattern: 'CPU utilization for five seconds:\s+(\d+)%...'
```

## Debug & Troubleshooting

Run with `--debug` for full parse trace output:

```
TRACE [cpu] parsed_by=textfsm rows=47 fields=5 duration=12.3ms
TRACE [memory] parsed_by=textfsm rows=1 fields=8 duration=8.1ms
TRACE [interfaces] parsed_by=textfsm rows=15 fields=6 duration=5.2ms
```

When a parser fails, the trace shows exactly why:

```
TRACE [cpu] parsed_by=none rows=0 fields=0 ERROR=all parsers failed (textfsm: 0 rows returned; regex: 0 matches)
```

At DEBUG level, full structured JSON traces show every step: command sent, raw output preview, sanitization, each template tried and its resolution path, normalization, type coercion, and final delivery to the state store.

The dashboard's `{ }` debug buttons (on each panel header) dump the current state store JSON for any collection, showing exactly what data reached the frontend.

## Project Structure

```
wirlwind_telemetry/
├── __main__.py              # CLI launcher + preflight checks
├── poll_engine.py           # SSH poll loop (vendor-agnostic)
├── parser_chain.py          # TextFSM → TTP → regex fallback chain
├── parse_trace.py           # Structured parse audit logging
├── widget.py                # PyQt6 top-level widget
├── bridge.py                # QWebChannel Python↔JS bridge
├── state_store.py           # In-memory state + history
├── ssh_client.py            # Paramiko wrapper (legacy ciphers, ANSI filter)
├── auth_interface.py        # Auth provider abstraction
├── drivers/                 # Vendor-specific behavior
│   ├── __init__.py          # Base driver + registry + shared transforms
│   ├── cisco_ios.py         # Cisco IOS/IOS-XE
│   ├── cisco_nxos.py        # Cisco NX-OS
│   ├── arista_eos.py        # Arista EOS
│   └── juniper_junos.py     # Juniper JunOS
├── collections/             # Collection configs (YAML)
│   ├── cpu/
│   ├── memory/
│   ├── interfaces/
│   ├── log/
│   └── bgp_summary/
├── templates/
│   └── textfsm/             # Custom TextFSM overrides
├── dashboard/
│   └── index.html           # ECharts dashboard (single file)
└── __init__.py
```

## License

[TBD]