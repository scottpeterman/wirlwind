# Wirlwind Telemetry — Architecture & Roadmap

## Design Philosophy

This is an operations console, not a monitoring dashboard. The difference matters.

A monitoring dashboard shows you aggregate health across a fleet. This tool shows you what *one device* is doing right now — the same information you'd get from an SSH session, but structured, trended, and continuously updated. It's the view a network engineer needs when they're troubleshooting a device, validating a change, or watching a migration in progress.

The design language is deliberate: dark background, monospace type, scanline overlay, ECharts gauges. It's meant to feel like sitting at a console, not reading a Grafana board. The mockup in `demo.html` is the north star.

## Current State (February 2026)

### Working

- **7 collections** polling live over SSH: CPU, memory, interfaces, interface_detail, log, neighbors, BGP summary
- **Complete Cisco IOS/IOS-XE widget set** — every dashboard panel has live data from a validated collection
- **Parser chain** with ordered fallback: TextFSM → TTP → regex, with custom template override support
- **Vendor driver system** abstracting all vendor-specific normalization out of the engine
- **Structured parse tracing** — every parse attempt logged with full provenance
- **ECharts dashboard** with CPU/memory gauges, throughput chart (per-interface selector, auto-scaling), CDP neighbor force-directed graph, process table, interface status table, trend chart, log viewer
- **Embeddable widget** — runs standalone or as an nterm tab
- **Custom template override** — local TextFSM templates in `templates/textfsm/` shadow NTC templates when they break

### Architecture Wins

1. **Poll engine is vendor-agnostic (410 lines).** No vendor field names, no platform-specific math. Adding a vendor means adding a driver file and collection YAML — zero engine changes.

2. **Parser chain preserves the template list contract.** NTC templates break. OS versions present one-offs. The collection YAML's ordered template list handles this cleanly: custom overrides first, NTC second, regex fallback third. The parse trace tells you exactly which parser won and why others didn't.

3. **Driver auto-discovery.** Drop a `.py` file in `drivers/` with a `@register_driver("vendor_id")` decorator. It registers automatically via `pkgutil` on import. No manual wiring.

4. **Dashboard is decoupled.** The HTML/JS knows nothing about vendors, parsers, or SSH. It receives normalized JSON over QWebChannel and renders it. The data contracts are defined by collection schemas and the normalize maps in YAML.

5. **Collection system scales cleanly.** Adding `interface_detail` and `neighbors` required zero engine changes and zero driver framework changes — just YAML configs, a driver post-process method, and a JS handler function.

## Target State (demo.html Vision)

The mockup in `demo.html` represents the full design target. Here's the gap analysis:

### Dashboard Panels

| Panel | Demo | Current | What's Needed |
|-------|------|---------|---------------|
| CPU gauge | ✓ | ✓ | — |
| Memory gauge | ✓ | ✓ | — |
| Top processes | PID, name, CPU%, MEM, runtime | ✓ (PID, name, CPU%, MEM) | Add runtime column (data already in TextFSM output) |
| CPU/Memory trend | 24hr line chart | ✓ (6hr) | Extend history depth |
| Interface throughput | Aggregate + per-interface area chart | ✓ | — |
| CDP/LLDP neighbors | Force-directed graph with port labels | ✓ | — |
| Interface status | Name, status, protocol, IP | ✓ | — |
| Device log | Newest-first syslog viewer | ✓ | — |
| **Environment sensors** | PSU temps, fan RPM, inlet/outlet/CPU die | — | New collection: `environment`. Vendor configs for `show environment all`. Horizontal bar chart panel in dashboard. |
| **Interface table (full)** | Speed, MTU, utilization bars, errors, description | Basic (name, status, protocol, IP) | Dashboard table enhancement using `interface_detail` data (already collected) |
| **Interface errors** | Stacked bar chart (CRC, Input, Drops per interface) | — | Dashboard panel using `interface_detail` data (already collected) |
| **BGP/Routing** | Peer table, state, prefixes | Data collected, no panel | Planned as separate routing module |

### Info Strip Enrichment

The demo's info strip shows: mgmt IP, loopback0, AS number, BGP peer count summary, SNMP config, location, chassis temp. This requires a `device_info` collection that runs once at connect (interval: 0) and pulls from `show version`, `show inventory`, `show running-config | include snmp|location`.

### Remaining Data Pipeline Gaps

1. **Counter-based rate calculation (deferred).** The current throughput implementation uses IOS 5-minute exponentially weighted average rates from `show interfaces`. This is accurate with `load-interval 30` and avoids counter-wrap complexity. True delta computation is a future enhancement — the `state_store` parameter is already wired to drivers but unused for `interface_detail`.

2. **History depth.** The 24-hour trend chart needs ~2,880 data points at 30-second intervals. Current ring buffer size is configurable but needs validation at that depth.

## Roadmap

### Phase 1: Core Collections ✓ Complete
- [x] Core 5 collections working with parser chain (cpu, memory, interfaces, log, bgp_summary)
- [x] Vendor driver system with auto-discovery
- [x] Parse trace logging
- [x] Custom template override support
- [x] Fix interface name column (normalize map: `name: interface`)
- [x] Add `_schema.yaml` for log collection
- [x] `interface_detail` collection: `show interfaces` → rates, bandwidth, errors, description
- [x] Dashboard: throughput area chart with auto-scaling and per-interface selector
- [x] `neighbors` collection: `show cdp neighbors detail` → force-directed graph
- [x] Dashboard: CDP neighbor graph with router/switch shapes, interface labels, mgmt IP
- [x] Log viewer: newest-first ordering fix
- [x] BGP panel removed (routing module planned separately)
- [ ] Process table: add runtime column

### Phase 2: Environment & Device Info
- [ ] `environment` collection: `show environment all` → PSU, fans, temps
- [ ] Dashboard: environment sensors horizontal bar chart
- [ ] `device_info` collection (one-shot): uptime, serial, software version, location
- [ ] Info strip enrichment: AS, SNMP, location, chassis temp

### Phase 3: Dashboard Enhancements
- [ ] Enhanced interface table using `interface_detail` data (speed, MTU, utilization bars, errors)
- [ ] Interface errors stacked bar panel (data already in `interface_detail`)
- [ ] Counter-based delta rate calculation (optional, enhances throughput accuracy)

### Phase 4: Routing Module
- [ ] BGP peer table as separate dashboard view or nterm tab
- [ ] Route table summary
- [ ] Prefix count trends
- [ ] OSPF neighbor/topology (stretch)

### Phase 5: Multi-Vendor Validation
- [ ] Arista EOS collection configs + driver validation
- [ ] Juniper JunOS collection configs + driver validation
- [ ] NX-OS collection configs + driver validation
- [ ] Vendor-specific template overrides where NTC templates fail

### Phase 6: nterm Integration
- [ ] Embedded telemetry tab in nterm session
- [ ] Auth provider that uses nterm's credential resolver
- [ ] Telemetry auto-start on device connection (optional)
- [ ] Shared SSH session (reuse nterm's transport, avoid double auth)

## Adding a New Vendor

1. **Create driver:** `drivers/my_vendor.py`
   ```python
   @register_driver("my_vendor")
   class MyVendorDriver(VendorDriver):
       @property
       def pagination_command(self) -> str:
           return "terminal length 0"
       
       def post_process(self, collection, data, state_store=None):
           # Vendor-specific transforms
           ...
   ```

2. **Create collection configs:** `collections/cpu/my_vendor.yaml`, etc.
   ```yaml
   command: "show processes cpu"
   interval: 30
   parsers:
     - type: textfsm
       templates:
         - my_vendor_show_processes_cpu.textfsm
     - type: regex
       pattern: '...'
   normalize:
     five_sec: cpu_5sec
   ```

3. **Add schemas** if type coercion is needed: `collections/cpu/_schema.yaml`

4. **Drop custom TextFSM templates** in `templates/textfsm/` if NTC doesn't have them or they're broken.

That's it. No engine changes, no dashboard changes, no wiring.

## Adding a New Collection

1. **Create directory:** `collections/my_collection/`
2. **Add vendor configs:** `collections/my_collection/cisco_ios_xe.yaml`
3. **Add schema:** `collections/my_collection/_schema.yaml`
4. **Add dashboard panel** in `dashboard/index.html` with `data-collection="my_collection"`
5. **Add handler** in the dashboard's `handleUpdate()` switch
6. **If the collection needs special output shaping** (not single-row or standard list), add the list key to `COLLECTION_LIST_KEYS` in `drivers/__init__.py`

## Design Constraints

- **SSH only.** No SNMP, no gNMI, no RESTCONF. This is deliberate — SSH is the universal denominator across network devices, and the tool is built for environments where SNMP isn't configured or gNMI isn't available.
- **Single device.** This is a per-device console, not a fleet view. One widget = one SSH session = one device.
- **PyQt6 + QWebEngine.** The dashboard is HTML/JS rendered in QWebEngine, not a separate web server. No browser required, no port conflicts, embedded in the desktop app.
- **No external dependencies for state.** No database, no time-series store, no message queue. State lives in memory for the duration of the session. This is a live console, not a retention system.