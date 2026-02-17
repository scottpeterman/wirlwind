# Wirlwind Telemetry — Next Steps

Action items to close the remaining gaps between current state and the full design target.

## Recently Completed

### Arista EOS — Full 7/7 Collection Parity

Arista EOS now matches Cisco IOS/IOS-XE with all 7 collections polling and all dashboard panels rendering live data. This is the second vendor at full coverage.

- **CPU collection** — `show processes top once` with custom TextFSM template (fixes NTC's PRIORITY regex for negative values). Driver computes `five_sec_total` from `100 - idle`. Process table shows top 20 by CPU then memory — no >0% filter, because Arista's single-shot `top` snapshot reports instantaneous CPU (usually 0% for most processes unlike Cisco's averaged values)
- **Memory collection** — Same `show processes top once` command, driver extracts KiB memory fields and computes `used_pct`
- **Interfaces collection** — `show interfaces description` (see dashboard change below). NTC template returns `port`, `status`, `protocol`, `description`
- **Interface detail collection** — `show interfaces` with custom TextFSM template that extends NTC to capture rate and error fields. NTC's template matched rate lines but didn't capture them. Custom template captures `input_rate`/`output_rate` as strings with units ("1.23 Mbps"). Driver converts rate strings to integer bps via `_parse_rate_to_bps()`
- **Neighbors collection** — `show lldp neighbors detail` (Arista uses LLDP, not CDP). NTC template returns `neighbor_name`, `mgmt_address`, `local_interface`, `neighbor_interface`, `neighbor_description`. Driver strips FQDN from device IDs, extracts short platform from verbose LLDP system descriptions, shortens Arista interface names for graph labels
- **Log collection** — Uses base driver's `_post_process_log()` as-is
- **BGP summary** — Uses base driver's `_normalize_bgp_peers()` as-is

### Custom TextFSM Templates for Arista

Two custom templates in `templates/textfsm/` that shadow NTC versions:

- `arista_eos_show_processes_top_once.textfsm` — Fixes NTC's PRIORITY regex to allow negative values (`-51` for real-time kernel threads). Removes `^. -> Error` line that choked on unexpected output
- `arista_eos_show_interfaces.textfsm` — Adds 11 fields NTC ignores: `input_rate`, `output_rate` (with unit strings), `input_rate_pps`, `output_rate_pps`, `input_errors`, `output_errors`, `crc`, `runts`, `giants`, `duplex`, `speed`. Uses `^.` instead of `^. -> Error` for unmatched lines

### Interface Description Panel

Replaced the "Interface Status" panel with "Interface Description" across both vendors:

- Command changed from `show ip interface brief` to `show interfaces description`
- Table columns: Interface, Description, Status (was: Interface, Status, Protocol, IP Address)
- Row-level color highlighting: green left border + interface name for up/up, red for down, dim for admin-down
- Glowing status dots (green/red) next to combined status text ("up/up", "down/down", "admin down")
- Missing descriptions render as italic dim "—"
- Badge unchanged: "N UP / N DOWN / N ADMIN-DOWN"

Both `collections/interfaces/cisco_ios.yaml` and `collections/interfaces/arista_eos.yaml` updated.

### Driver Development Guide

Created `README_Driver_Development.md` — comprehensive guide for adding new vendors, distilled from the Cisco and Arista build-out. Covers data contracts for all 7 collections, the normalize map inversion, TextFSM gotchas, output shaping, validation workflow, vendor-specific notes for JunOS and NX-OS, and a full completion checklist.

### Previous Completions (unchanged from last cycle)

- Interface names — Fixed normalize map (`name: interface` not `name: intf`)
- Log schema — Created `collections/log/_schema.yaml`
- `interface_detail` collection — Cisco IOS/IOS-XE with full 43-field parse
- Throughput chart — Auto-scaling, per-interface history, dropdown selector
- `neighbors` collection — Cisco CDP with force-directed graph
- Log viewer ordering — Fixed double-reverse bug
- BGP panel removed — Collection still polls, routing module planned separately

## Quick Fixes (< 1 hour each)

### 1. Process table runtime column

The TextFSM output already includes `process_runtime`. Add aliasing in `drivers/cisco_ios.py` inside `_filter_cpu_processes()`, and add the column to the dashboard's process table header and row template.

### 2. Screenshot update

Replace the screenshot in the repo root with the current dashboard. Should show both Cisco and Arista at full 7-collection coverage with the new Interface Description panel.

## Next Collection: `environment`

This is the last major collection gap for the base Cisco IOS/IOS-XE widget set. Arista will also need an environment config — likely `show environment all` or `show environment temperature`.

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

## Next Vendor: Juniper JunOS

JunOS is the next multi-vendor target. See `README_Driver_Development.md` for the full guide, including JunOS-specific notes. Key differences from Cisco/Arista:

- **Pagination:** `set cli screen-length 0`
- **CPU/Memory:** `show chassis routing-engine` returns both in one command
- **Process list:** `show system processes extensive` for Linux-style process output
- **Interfaces:** `show interfaces` text output differs significantly; NTC template exists but needs field validation
- **Neighbors:** `show lldp neighbors` / `show lldp neighbors detail`
- **Log:** `show log messages` or `show system syslog`
- **Prompt:** Ends with `>` (operational) or `#` (config)

The driver framework, dashboard, and engine require no changes — only `drivers/juniper_junos.py`, collection YAML configs, and custom TextFSM templates where NTC falls short.

## Multi-Vendor Status

| Vendor | Driver | Collections | Status |
|--------|--------|-------------|--------|
| Cisco IOS/IOS-XE | `CiscoIOSDriver` | 7/7 | ✓ Full coverage |
| Arista EOS | `AristaEOSDriver` | 7/7 | ✓ Full coverage |
| Juniper JunOS | `JuniperJunOSDriver` | 0/7 | Next target — driver exists but no collection configs |
| Cisco NX-OS | `CiscoNXOSDriver` | 0/7 | Driver exists but no validated collection configs |

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

The current throughput implementation uses vendor-reported average rates (`input_rate` and `output_rate` from `show interfaces`). This works and is responsive with appropriate load-interval settings. A future enhancement could compute true delta rates from interface counters:

```
rate = (current_octets - previous_octets) / interval_seconds * 8
```

This would require the driver to access previous state via `state_store` (the parameter is already wired through but unused for `interface_detail`). Deferred because the vendor average rates are accurate enough for console use and avoid the complexity of counter wrap handling.

Note: Arista's rate fields arrive as strings with units ("1.23 Mbps") and are converted to integer bps by the driver's `_parse_rate_to_bps()`. Cisco's arrive as bare integers. The dashboard receives integer bps regardless of vendor.

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

**Lessons from Arista build-out:**
- NTC templates can match lines without capturing them (Arista `show interfaces` matched rate lines but had no Value for them)
- `^. -> Error` in NTC templates kills resilience — custom templates should use `^.` (match and discard)
- No blank lines between comments and Value declarations in TextFSM (causes parse error)
- TextFSM `Value Required` controls which rows get emitted — understand it before debugging empty output
- Rate fields with unit suffixes need driver conversion to integer bps
- Single-shot `top` output has different CPU semantics than Cisco's averaged `show processes cpu` — don't filter processes on >0% CPU

## Files to Delete

- `template_loader.py` — Dead code. The collection system + parser chain replaced it entirely. `poll_engine.py` no longer references it. Widget no longer imports it. Remove it and its tests.

## Testing Priority

1. **Parse trace validation.** Run with `--debug` against a live device and verify all 7 collections produce a TRACE line. Any collection showing `parsed_by=none` needs investigation.

2. **Custom template override.** Place a dummy TextFSM file in `templates/textfsm/` with the same name as an NTC template. Verify it resolves first in the preflight output and actually gets used during parsing.

3. **Driver post-processing.** Verify CPU gauge shows non-zero values (confirms `_normalize_cpu()` is running). Verify throughput chart shows rate values on active interfaces (confirms rate parsing and conversion). Verify neighbor graph renders with correct node shapes (confirms capabilities/description parsing).

4. **Cross-vendor.** Connect to both Cisco and Arista devices and confirm all 7 collections parse successfully on each. Interface Description panel should show descriptions from `show interfaces description` on both vendors.

5. **JunOS bring-up.** Connect to a Juniper device with `--preflight-only --debug` to see which NTC templates resolve. Then connect live and check which collections parse vs fail. This identifies the starting point for JunOS collection configs.