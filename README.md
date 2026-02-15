# Wirlwind Telemetry

Real-time network device telemetry dashboard — SSH-driven, multi-vendor, template-based.

**Right-click a device. See everything.** No more login → run 6 commands → try to correlate → decide what's next. One action gives you CPU, memory, interfaces, routing protocol state, neighbors, and syslog in a single mission-control view. The device tells you its story; you decide what to do about it.

## Status: Working Prototype (v0.1.0)

Proven on live Cisco IOS-XE devices via EVE-NG lab. The core loop works:
SSH connects → commands run → templates parse → state updates → dashboard renders.

### What works today
- CPU and memory gauges (live, updating)
- Interface status table (full population from `show ip interface brief`)
- Interface throughput chart (aggregate in/out over time)
- CPU & memory trend (historical line chart)
- SCNG SSH client with legacy cipher/KEX support
- Prompt auto-detection and shotgun pagination disabling
- ANSI sequence filtering
- Template-driven parsing (YAML regex templates)
- QWebChannel bridge between Python state store and ECharts JS
- Standalone launcher with CLI arguments
- PyQt6 QWebEngineView embedding

### What needs work
- **Process table** — template regex needs tuning against real `show proc cpu sorted` output
- **Memory parsing** — IOS-XE memory output format varies by platform/version; needs template variants
- **Neighbor graph** — LLDP template needs testing; CDP may need a separate template
- **Log view** — syslog timestamp formats vary; needs broader regex coverage
- **BGP/OSPF** — should not be hardcoded; must be dynamic (see Routing Protocol Discovery below)
- **Environment sensors** — `show environment` output is wildly platform-dependent
- **Device info strip** — needs to populate from `show version` parse (model, serial, uptime, IOS version)
- **Header** — still shows "Waiting for connection" after connected; device info signal timing

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    QWebEngineView                        │
│                                                         │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌──────────────┐  │
│  │CPU Gauge│ │Mem Gauge│ │Intf Tbl │ │ Throughput   │  │
│  │         │ │         │ │         │ │ Chart        │  │
│  └────┬────┘ └────┬────┘ └────┬────┘ └──────┬───────┘  │
│       └───────────┴─────┬─────┴──────────────┘          │
│                    QWebChannel                           │
│                  ┌──────▼──────┐                         │
│                  │  Telemetry  │   Python ↔ JS bridge    │
│                  │   Bridge    │   signals + polling     │
│                  └──────┬──────┘                         │
└─────────────────────────┼───────────────────────────────┘
                          │
┌─────────────────────────┼───────────────────────────────┐
│    BACKEND              │                                │
│                  ┌──────▼──────┐                         │
│                  │  State Store│  In-memory normalized   │
│                  │             │  device model           │
│                  │  (dict + Qt │  + history ring buffer  │
│                  │   signals)  │  + metadata/errors      │
│                  └──────┬──────┘                         │
│                         │ write                          │
│                  ┌──────▼──────┐                         │
│                  │ Poll Engine │  QThread                │
│                  │             │  Synchronous loop:      │
│                  │  for each   │  check interval →       │
│                  │  collection:│  send cmd → collect →   │
│                  │  cmd→parse→ │  parse → normalize →    │
│                  │  normalize→ │  store → signal         │
│                  │  store      │                         │
│                  └──────┬──────┘                         │
│                         │                                │
│           ┌─────────────┼─────────────┐                  │
│     ┌─────▼─────┐ ┌─────▼─────┐ ┌────▼─────┐           │
│     │ Template  │ │  Parser   │ │  Post-   │           │
│     │ Loader    │ │  (regex)  │ │ Process  │           │
│     │           │ │           │ │ (norm)   │           │
│     │ YAML per  │ │ single /  │ │ cpu norm │           │
│     │ vendor    │ │ table /   │ │ mem pct  │           │
│     │ per widget│ │ block     │ │ bgp state│           │
│     └───────────┘ └───────────┘ └──────────┘           │
│                         │                                │
│                  ┌──────▼──────┐                         │
│                  │ SCNG SSH    │  Legacy cipher/KEX      │
│                  │ Client      │  ANSI filtering         │
│                  │             │  Prompt detection        │
│                  │             │  Shotgun pagination      │
│                  └──────┬──────┘                         │
│                         │                                │
│                  ┌──────▼──────┐                         │
│                  │    Auth     │  ← THE SEAM             │
│                  │  Provider   │                         │
│                  │  (abstract) │  SimpleAuth (standalone) │
│                  │             │  NtermAuth (vault hook)  │
│                  └──────┬──────┘                         │
└─────────────────────────┼───────────────────────────────┘
                          │ SSH (Paramiko)
                     ┌────▼────┐
                     │ DEVICE  │
                     └─────────┘
```

## Key Design Decisions

### Template-driven, not hardcoded
Every widget's data comes from a YAML template that defines the CLI command and regex patterns. Templates are organized by collection type and vendor. To add a new vendor, you write YAML files — no Python changes.

```
templates/
├── cpu/
│   ├── cisco_ios_xe.yaml     # show processes cpu sorted
│   ├── arista_eos.yaml       # show processes top once
│   └── juniper_junos.yaml    # show chassis routing-engine
├── memory/
├── interfaces/
├── bgp_summary/
├── neighbors/
├── environment/
└── log/
```

### Normalized state model
All vendor-specific output is parsed into a common schema. The CPU widget doesn't know if it's talking to IOS-XE or JunOS. It just reads `five_min` from the state store. Normalization happens in the poll engine's post-processing step.

### Two SSH sessions, not one
The telemetry session is separate from any interactive terminal session. This is intentional — you can't screen-scrape while someone is typing config commands. Two sessions from one engineer is nothing for any device.

### Auth provider abstraction
The `AuthProvider` abstract base class is the integration seam. Standalone mode uses `SimpleAuthProvider` (username/password from CLI). nterm integration uses `NtermAuthProvider` which wraps the existing credential vault and resolver. The telemetry system never imports from `wirlwind.*` directly.

### Synchronous polling, not async
The poll engine runs all commands in a single synchronous loop per cycle. This means all data in a given cycle represents the same point in time. No race conditions between "I just got new BGP data but interface counters are 30 seconds old." The QThread keeps the UI responsive.

## Routing Protocol Discovery (TODO — Critical)

BGP should not be a default collection. Neither should OSPF, IS-IS, EIGRP, or any routing protocol. The system needs a discovery phase that determines what's running on the device before building the collection list.

### Proposed approach

1. **Phase 0: Capabilities probe** — Run on first connect, before the poll loop starts
   - `show ip protocols` (IOS-XE) / `show route summary` (JunOS) / `show ip route summary` (EOS)
   - Parse which routing protocols are active
   - Check for `show ip bgp summary` responsiveness (some devices have BGP configured but no peers)

2. **Dynamic collection builder** — Based on probe results:
   - BGP active → add `bgp_summary` collection, load BGP widget
   - OSPF active → add `ospf_neighbors` collection, load OSPF widget
   - IS-IS active → add `isis_adjacency` collection
   - No routing protocols → skip routing section entirely

3. **Dashboard adapts** — Widget grid should be dynamic, not static HTML. Missing collections = missing panels. A device with no BGP shouldn't show an empty BGP table.

### Template additions needed
```
templates/
├── capabilities/           # NEW — phase 0 probes
│   ├── cisco_ios_xe.yaml   # show ip protocols
│   ├── arista_eos.yaml
│   └── juniper_junos.yaml
├── ospf_neighbors/         # NEW
│   ├── cisco_ios_xe.yaml   # show ip ospf neighbor
│   └── ...
├── isis_adjacency/         # NEW
│   └── ...
├── eigrp_neighbors/        # NEW (if we care)
│   └── ...
```

## Show Version Parse (TODO)

The header and info strip should populate from `show version` (or equivalent). This gives us:
- Hostname, model, serial number
- Software version / train
- Uptime
- Total memory (hardware)
- Boot image

This should be a one-shot collection on connect, not polled. Add a `device_info` template collection with `interval: 0` (run once).

## Widget Roadmap

### Core (v0.1 — current)
- [x] CPU gauge
- [x] Memory gauge
- [x] Interface status table
- [x] Interface throughput chart (aggregate)
- [x] CPU & Memory trend
- [ ] Top processes table (template fix needed)
- [ ] LLDP/CDP neighbor graph (template fix needed)
- [ ] Syslog viewer (template fix needed)

### Near-term (v0.2)
- [ ] Device info from `show version` (one-shot parse)
- [ ] Routing protocol auto-discovery
- [ ] Dynamic widget grid (hide panels with no data)
- [ ] Per-interface throughput (click interface → see its chart)
- [ ] BGP peer detail (click peer → prefix count history)
- [ ] OSPF neighbor table
- [ ] Environment sensors (platform-specific template variants)

### Future (v0.3+)
- [ ] ARP/MAC table viewer
- [ ] Route table summary (prefix counts by protocol)
- [ ] Interface error rate trending (not just current count)
- [ ] Config change detection (diff last config snapshot)
- [ ] Alerting thresholds (CPU > 80% → badge goes red + optional notification)
- [ ] Export snapshot to JSON (for troubleshooting handoff)
- [ ] Multiple device comparison view (side-by-side)
- [ ] Template editor UI (edit YAML, test regex against sample output)

## nterm Integration Path

The telemetry widget is designed to embed in nterm (ntermqt / PyQt6) with minimal wiring:

```python
# In nterm's context menu handler
from wirlwind_telemetry.auth_interface import NtermAuthProvider, DeviceTarget
from wirlwind_telemetry.widget import TelemetryWidget

# Hook credential vault
auth = NtermAuthProvider(self.credential_resolver)

# Create widget as a new tab or popup
widget = TelemetryWidget(auth_provider=auth, parent=self.tab_widget)

# Launch telemetry on the selected device
target = DeviceTarget(
    hostname=session.hostname,
    port=session.port,
    vendor=detected_vendor,  # from fingerprinting
    display_name=session.name,
    tags=session.tags,
)
widget.start(target)
self.tab_widget.addTab(widget, f"📊 {session.name}")
```

### What nterm provides
- Encrypted credential vault with YubiKey support
- Device inventory with hostname, port, tags
- Fingerprinting (vendor detection from Secure Cartography)
- PyQt6 tab infrastructure (QTabWidget)
- The right-click context menu entry point

### What the telemetry widget provides
- Self-contained polling and visualization
- No imports from `wirlwind.*` at runtime
- Clean lifecycle (start/stop/restart, proper cleanup)
- Standalone testability without nterm running

## Day 2 Ecosystem Position

```
Day 0  ──  Discovery         Secure Cartography
                              └── fingerprint → vendor ID
Day 1  ──  Inventory          VelocityCMDB
                              └── device records, tags, sites
Day 1.5 ── Operational        Wirlwind Telemetry  ← THIS
           Awareness          └── real-time device health
                              └── "do I need to investigate?"
Day 2  ──  Validation         FibTrace, TrafikWatch, Day2 tools
                              └── "is the network doing what
                                   it's supposed to?"
```

Wirlwind Telemetry answers the question: **"What is this device doing right now?"**
That's the question you ask before you decide whether to open a terminal, run a workflow, or close the ticket.

## Running Standalone

```bash
pip install -e .

# Cisco IOS-XE
python -m wirlwind_telemetry \
    --host 172.16.100.1 \
    --vendor cisco_ios_xe \
    --user cisco \
    --debug

# Arista EOS
python -m wirlwind_telemetry \
    --host 10.0.0.2 \
    --vendor arista_eos \
    --user admin \
    --key ~/.ssh/id_rsa

# Juniper JunOS
python -m wirlwind_telemetry \
    --host 10.0.0.3 \
    --vendor juniper_junos \
    --user admin

# Disable legacy cipher support (modern devices only)
python -m wirlwind_telemetry \
    --host 10.0.0.1 \
    --vendor cisco_ios_xe \
    --user admin \
    --no-legacy
```

## Dependencies

- Python 3.10+
- PyQt6 + PyQt6-WebEngine
- Paramiko
- PyYAML
- ECharts 5.5 (loaded from CDN at runtime)

## Project Structure

```
wirlwind_telemetry/
├── __init__.py
├── __main__.py            # Standalone CLI launcher
├── auth_interface.py      # AuthProvider ABC + Simple/Nterm implementations
├── bridge.py              # QWebChannel Python↔JS bridge
├── poll_engine.py         # QThread SSH polling loop
├── ssh_client.py          # SCNG Paramiko wrapper (legacy support)
├── state_store.py         # In-memory normalized device model
├── template_loader.py     # YAML template loading + regex parsing
├── widget.py              # Embeddable PyQt6 TelemetryWidget
├── dashboard/
│   └── index.html         # ECharts mission-control dashboard
└── templates/
    ├── cpu/               # cisco_ios_xe, arista_eos, juniper_junos
    ├── memory/            # cisco_ios_xe, arista_eos, juniper_junos
    ├── interfaces/        # cisco_ios_xe, arista_eos, juniper_junos
    ├── interface_detail/  # cisco_ios_xe
    ├── bgp_summary/       # cisco_ios_xe, arista_eos, juniper_junos
    ├── neighbors/         # cisco_ios_xe, arista_eos, juniper_junos
    ├── environment/       # cisco_ios_xe
    └── log/               # cisco_ios_xe, arista_eos
```

## License


MIT