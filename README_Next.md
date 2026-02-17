# Wirlwind Telemetry — Next Steps

Action items to close the remaining gaps between current state and the full design target.

## Recently Completed

These items from the original roadmap are done and shipping:

- **Interface names** — Fixed normalize map (`name: interface` not `name: intf`) in `collections/interfaces/` for IOS and IOS-XE
- **Log schema** — Created `collections/log/_schema.yaml`, clears preflight warning
- **`interface_detail` collection** — `show interfaces` with NTC TextFSM, full 43-field parse. Driver post-processes bandwidth string, coerces rates/errors to int, computes `utilization_pct`
- **Throughput chart** — Auto-scaling (bps/Kbps/Mbps), per-interface history stored in ring buffer, dropdown selector defaults to aggregate with per-interface filtering. Badge shows current in/out rates
- **`neighbors` collection** — `show cdp neighbors detail` with NTC TextFSM. Dashboard renders force-directed graph: routers (cyan roundRect) vs switches (green rect) based on CDP capabilities. Edge labels show both local and remote interfaces shortened (GigabitEthernet → Gi, etc.). Hover shows platform, mgmt IP, capabilities
- **Log viewer ordering** — Fixed double-reverse bug. Driver sends newest-first, dashboard takes first 30
- **BGP panel removed** — Collection still polls (useful for debug/validation), but panel removed from dashboard. BGP/routing visualization planned as a separate routing module

## Quick Fixes (< 1 hour each)

### 1. Process table runtime column

The TextFSM output already includes `process_runtime`. Add aliasing in `drivers/cisco_ios.py` inside `_filter_cpu_processes()`, and add the column to the dashboard's process table header and row template.

### 2. Screenshot update

Replace the screenshot in the repo root with the current dashboard showing all 7 collections live.

## Next Collection: `environment`

This is the last major collection gap for the base Cisco IOS/IOS-XE widget set.

### Collection config: `collections/environment/cisco_ios_xe.yaml`

```yaml
command: "show environment all"
interval: 120

parsers:
  - type: textfsm
    templates:
      - cisco_ios_show_environment_all.textfsm
  - type: regex
    pattern: '(?P<sensor>\S+.*?)\s+(?P<state>Normal|Warning|Critical|Not Present)\s+(?P<reading>\d+)\s*(?P<unit>[A-Za-z/]+)'
    flags: MULTILINE
```

NTC has `cisco_ios_show_environment_all.textfsm` but it's spotty across IOS versions. The regex fallback covers the common case.

### Dashboard panel

Horizontal bar chart (ECharts bar type, horizontal orientation) with color coding:
- Green: normal range
- Amber: warning
- Red: critical
- Cyan: fan RPM (different scale)

## Future: Routing Module

BGP data collection is already working (`bgp_summary` collection polls and parses). The routing module will be a separate dashboard view (or nterm tab) with:

- BGP peer table (neighbor, AS, state, up/down, prefixes rcvd/sent, description)
- Route table summary
- Prefix count trends
- Possibly OSPF neighbor/topology

This is intentionally separated from the base telemetry dashboard to keep the console focused on device health and interface performance.

## Future: Device Info Enrichment

The info strip currently shows IP, platform, vendor, user. The demo target includes: loopback0 IP, AS number, SNMP location, chassis temp, serial number. This requires a `device_info` collection that runs once at connect (interval: 0) pulling from `show version`, `show inventory`, and `show running-config | include snmp|location`.

## Future: Interface Errors Panel

Error counters (CRC, input errors, drops) are already collected in `interface_detail` — the TextFSM parse includes `in_errors`, `out_errors`, `crc_errors`. A stacked bar chart panel could visualize these per-interface. The data pipeline is ready; this is purely a dashboard panel addition.

## Future: Counter-Based Rate Calculation

The current throughput implementation uses IOS 5-minute exponentially weighted average rates (`input_rate` and `output_rate` from `show interfaces`). This works and is responsive with `load-interval 30` configured on interfaces. A future enhancement could compute true delta rates from interface counters:

```
rate = (current_octets - previous_octets) / interval_seconds * 8
```

This would require the driver to access previous state via `state_store` (the parameter is already wired through but unused for `interface_detail`). Deferred because the IOS average rates are accurate enough for console use and avoid the complexity of counter wrap handling.

## Multi-Vendor Gaps

Cisco IOS/IOS-XE is the first vendor with a complete widget set (7 collections). Remaining vendors need collection YAML configs validated against live devices:

| Vendor | Driver | Collections Needed |
|--------|--------|-------------------|
| Arista EOS | `AristaEOSDriver` | All 7 collection configs, validate TextFSM field names |
| Juniper JunOS | `JuniperJunOSDriver` | All 7 collection configs (XML output → different parse strategy) |
| Cisco NX-OS | `CiscoNXOSDriver` | All 7 collection configs, validate NTC template differences |

The engine, dashboard, and driver framework require no changes — only YAML configs and driver validation.

## Template Strategy Reminders

When adding collections, follow this pattern for every vendor config:

```yaml
parsers:
  # 1. Custom override (if you've hit an NTC bug)
  - type: textfsm
    templates:
      - my_fixed_template.textfsm         # local override, tried first
      - vendor_show_command.textfsm        # NTC template, tried second

  # 2. Regex fallback (always have one)
  - type: regex
    pattern: '...'
    groups:
      field_name: 1
```

Always include a regex fallback. TextFSM templates are fragile — they fail silently when the output format doesn't match exactly. The regex catches the common case and keeps the widget alive.

## Files to Delete

- `template_loader.py` — Dead code. The collection system + parser chain replaced it entirely. `poll_engine.py` no longer references it. Widget no longer imports it. Remove it and its tests.

## Testing Priority

1. **Parse trace validation.** Run with `--debug` against a live device and verify all 7 collections produce a TRACE line. Any collection showing `parsed_by=none` needs investigation.

2. **Custom template override.** Place a dummy TextFSM file in `templates/textfsm/` with the same name as an NTC template. Verify it resolves first in the preflight output and actually gets used during parsing.

3. **Driver post-processing.** Verify CPU gauge shows non-zero values (confirms `_normalize_cpu()` is running). Verify throughput chart shows Kbps values on lab devices (confirms `_post_process_interfaces()` bandwidth parsing and rate coercion). Verify neighbor graph renders with correct node shapes (confirms CDP capabilities parsing).

4. **Multi-vendor.** Connect to an Arista or NX-OS device and check which collections parse successfully vs fall through to regex vs fail entirely. This identifies which collection configs need to be created or adjusted.