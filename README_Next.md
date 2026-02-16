# Wirlwind Telemetry — Next Steps

Immediate action items to close the gap between current state and the demo.html target.

## Quick Fixes (< 1 hour each)

### 1. Interface names showing dashes

The interface table shows `-` for the name column. The TextFSM template returns `intf` as the field name, but the dashboard expects `name`. Fix the normalize map in `collections/interfaces/cisco_ios.yaml`:

```yaml
normalize:
  name: intf
  ip_address: ipaddr
  status: status
  protocol: proto
```

### 2. Log collection missing schema

Preflight warns: `[log] missing _schema.yaml — no type coercion`. Create `collections/log/_schema.yaml`:

```yaml
description: "Syslog entries from show logging"
fields:
  severity:
    type: int
    description: "Syslog severity level (0-7)"
  facility:
    type: str
    description: "Syslog facility code"
  mnemonic:
    type: str
    description: "Message mnemonic"
  message:
    type: str
    description: "Log message text"
  timestamp:
    type: str
    description: "Assembled timestamp"
```

### 3. Process table runtime column

The TextFSM output already includes `process_runtime`. Add aliasing in `drivers/cisco_ios.py` inside `_filter_cpu_processes()`, and add the column to the dashboard's process table header and row template.

## Next Collection: `interface_detail`

This unlocks three demo panels at once: the full interface table, throughput chart, and interface errors chart.

### Collection config: `collections/interface_detail/cisco_ios_xe.yaml`

```yaml
command: "show interfaces"
interval: 60

parsers:
  - type: textfsm
    templates:
      - cisco_ios_show_interfaces.textfsm

normalize:
  name: interface
  status: link_status
  protocol: protocol_status
  speed: bandwidth
  mtu: mtu
  in_octets: input_rate
  out_octets: output_rate
  in_errors: input_errors
  out_errors: output_errors
  crc_errors: crc
  description: description
```

NTC has `cisco_ios_show_interfaces.textfsm` and it works for this. The big fields: `bandwidth`, `input_rate`, `output_rate`, `input_errors`, `output_errors`, `crc`, `description`.

### Rate calculation

Interface counters are cumulative. To get Mbps you need:

```
rate = (current_octets - previous_octets) / interval_seconds * 8 / 1_000_000
```

This belongs in the driver's `post_process()` for `interface_detail`, not in the engine. The driver needs access to the previous state via `state_store.get("interface_detail")`.

### Dashboard changes

1. Replace the basic interface table with the demo's full table (add speed, MTU, in/out Mbps, utilization bar, errors, description columns)
2. Add `handleUpdate('interface_detail', data)` to the switch
3. Wire the throughput chart to interface_detail data instead of placeholder

## Next Collection: `environment`

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

1. **Parse trace validation.** Run with `--debug` against a live device and verify every collection produces a TRACE line. Any collection showing `parsed_by=none` needs investigation.

2. **Custom template override.** Place a dummy TextFSM file in `templates/textfsm/` with the same name as an NTC template. Verify it resolves first in the preflight output and actually gets used during parsing.

3. **Driver post-processing.** Verify CPU gauge shows non-zero values (confirms `CiscoIOSDriver._normalize_cpu()` is running). Verify process table shows names and CPU percentages (confirms `_filter_cpu_processes()` aliasing works).