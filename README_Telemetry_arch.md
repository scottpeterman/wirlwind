# Wirlwind Telemetry — Architecture & Roadmap

## Design Philosophy

This is an operations console, not a monitoring dashboard. The difference matters.

A monitoring dashboard shows you aggregate health across a fleet. This tool shows you what *one device* is doing right now — the same information you'd get from an SSH session, but structured, trended, and continuously updated. It's the view a network engineer needs when they're troubleshooting a device, validating a change, or watching a migration in progress.

The design language is deliberate: dark background, monospace type, scanline overlay, ECharts gauges. It's meant to feel like sitting at a console, not reading a Grafana board. The mockup in `demo.html` is the north star.

## Current State (February 2026)

### Working

- **5 collections** polling live over SSH: CPU, memory, interfaces, log, BGP summary
- **Parser chain** with ordered fallback: TextFSM → TTP → regex, with custom template override support
- **Vendor driver system** abstracting all vendor-specific normalization out of the engine
- **Structured parse tracing** — every parse attempt logged with full provenance
- **ECharts dashboard** with CPU/memory gauges, trend chart, process table, interface list, log viewer, BGP peers, LLDP neighbor graph
- **Embeddable widget** — runs standalone or as an nterm tab
- **Custom template override** — local TextFSM templates in `templates/textfsm/` shadow NTC templates when they break

### Architecture Wins

1. **Poll engine is vendor-agnostic (410 lines).** No vendor field names, no platform-specific math. Adding a vendor means adding a driver file and collection YAML — zero engine changes.

2. **Parser chain preserves the template list contract.** NTC templates break. OS versions present one-offs. The collection YAML's ordered template list handles this cleanly: custom overrides first, NTC second, regex fallback third. The parse trace tells you exactly which parser won and why others didn't.

3. **Driver auto-discovery.** Drop a `.py` file in `drivers/` with a `@register_driver("vendor_id")` decorator. It registers automatically via `pkgutil` on import. No manual wiring.

4. **Dashboard is decoupled.** The HTML/JS knows nothing about vendors, parsers, or SSH. It receives normalized JSON over QWebChannel and renders it. The data contracts are implicit today but will be schema-driven.

## Target State (demo.html Vision)

The mockup in `demo.html` represents the full design target. Here's the gap analysis:

### Dashboard Panels

| Panel | Demo | Current | What's Needed |
|-------|------|---------|---------------|
| CPU gauge | ✓ | ✓ | — |
| Memory gauge | ✓ | ✓ | — |
| **Environment sensors** | PSU temps, fan RPM, inlet/outlet/CPU die | — | New collection: `environment`. Vendor configs for `show environment all`. Horizontal bar chart panel in dashboard. |
| **Throughput (6hr)** | Aggregate in/out area chart, per-interface selectable | Basic stub | New collection: `interface_detail` using `show interfaces`. Parse counters, compute delta rates. Dashboard needs time-series storage and area chart with interface selector. |
| Top processes | PID, name, CPU%, MEM, runtime | ✓ (PID, name, CPU%, MEM) | Add runtime column. Already in TextFSM output (`process_runtime`), just needs aliasing in driver and column in dashboard. |
| CPU/Memory trend | 24hr line chart | ✓ | Extend history depth. Currently limited by state store ring buffer. |
| LLDP neighbors | Force-directed graph with port labels | Partial | Collection exists in dashboard. Need `show lldp neighbors detail` + `show cdp neighbors detail` collection configs. |
| **Interface table (full)** | Name, status, protocol, IP, speed, MTU, in/out Mbps, utilization bars, error counts, description | Basic (name, status, protocol, IP) | Needs `interface_detail` collection. Dashboard table needs speed, MTU, throughput, utilization bar, errors, description columns. |
| BGP peers | Neighbor, AS, state, up/down, prefixes rcvd/sent, description | Partial (neighbor, AS, state, prefixes) | Add up/down timer, description columns. May need `show ip bgp neighbors` for description field. |
| **Interface errors** | Stacked bar chart (CRC, Input, Drops per interface) | — | New collection: parse error counters from `show interfaces`. New dashboard panel with horizontal stacked bar chart. |

### Info Strip Enrichment

The demo's info strip shows: mgmt IP, loopback0, AS number, BGP peer count summary, SNMP config, location, chassis temp. This requires a `device_info` collection that runs once at connect (interval: 0) and pulls from `show version`, `show inventory`, `show running-config | include snmp|location`.

### Data Pipeline Gaps

1. **Rate calculation.** Interface throughput requires delta computation — store previous counter value, compute bytes/sec between polls. This is a state store enhancement, not a parser concern.

2. **History depth.** The 24-hour trend chart needs ~2,880 data points at 30-second intervals. Current ring buffer size is configurable but needs validation at that depth.

3. **Interface selector.** The throughput chart in the demo supports per-interface selection. The dashboard needs a dropdown or click-to-filter interaction bound to the interface list.

## Roadmap

### Phase 1: Parity (Current)
- [x] Core 5 collections working with parser chain
- [x] Vendor driver system with auto-discovery
- [x] Parse trace logging
- [x] Custom template override support
- [ ] Fix interface name column (normalize map)
- [ ] Add `_schema.yaml` for log collection
- [ ] Process table: add runtime column

### Phase 2: Interface Detail
- [ ] `interface_detail` collection: `show interfaces` → speed, MTU, counters, description, errors
- [ ] Rate calculation in state store (delta counters between polls)
- [ ] Dashboard: full interface table with utilization bars and error counts
- [ ] Dashboard: throughput area chart with interface selector
- [ ] Dashboard: interface errors stacked bar panel

### Phase 3: Environment & Device Info
- [ ] `environment` collection: `show environment all` → PSU, fans, temps
- [ ] Dashboard: environment sensors horizontal bar chart
- [ ] `device_info` collection (one-shot): uptime, serial, software version, location
- [ ] Info strip enrichment: AS, SNMP, location, chassis temp

### Phase 4: Multi-Vendor
- [ ] Arista EOS collection configs + driver validation
- [ ] Juniper JunOS collection configs + driver validation  
- [ ] NX-OS collection configs + driver validation
- [ ] Vendor-specific template overrides where NTC templates fail

### Phase 5: nterm Integration
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