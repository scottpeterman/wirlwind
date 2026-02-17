# Wirlwind Telemetry — Driver & Collection Development Guide

How to add a new vendor to Wirlwind Telemetry. This guide was written after
bringing Cisco IOS/IOS-XE to full coverage (7 collections) and then
replicating that coverage on Arista EOS. Every gotcha documented here
was hit in production.


## Data Flow

Understanding the pipeline end-to-end is essential before writing anything.
Every poll cycle, for every collection, the engine runs this sequence:

```
SSH Command → Raw Output → Sanitizer → Parser Chain → Normalize → Shape → Post-Process → State Store → Dashboard
```

Concretely:

1. **SSH Client** sends the CLI command from the collection YAML
2. **Sanitizer** strips command echo and trailing prompt (parser_chain.py `_sanitize_cli_output`)
3. **Parser Chain** tries parsers in order from the YAML config (TextFSM → TTP → regex). First to return rows wins.
4. **Normalize** remaps field names using the YAML `normalize:` map
5. **Shape** (`_default_shape_output`) converts rows into the dict structure the state store expects
6. **Post-Process** (vendor driver) applies vendor-specific transforms
7. **State Store** receives the dict, emits Qt signals
8. **Bridge** (QWebChannel) pushes JSON to the dashboard
9. **Dashboard** (ECharts) renders the panel

You only touch steps 1, 3, 4, and 6. Everything else is vendor-agnostic.


## Data Contracts

The dashboard expects specific field names and structures. Your driver and
collection configs must produce data matching these contracts. If a field
is missing, the panel renders empty or shows "No data" — it won't crash.

### CPU Collection

State store receives a **flat dict** (single-row collection):

```json
{
  "five_sec_total": 13.2,
  "one_min": 11.0,
  "five_min": 10.5,
  "processes": [
    {
      "pid": "2882",
      "name": "ConfigAgent",
      "cpu_pct": 0.3,
      "five_sec": 0.3,
      "holding": 48128,
      "holding_display": "47K"
    }
  ]
}
```

**Required fields:**
- `five_sec_total` — drives the CPU gauge (0–100)
- `one_min`, `five_min` — displayed in gauge subtitle and trend
- `processes` — list of dicts for the Top Processes table

**Process fields:** `pid`, `name`, `cpu_pct` (or `five_sec`), optionally `holding` (bytes) and `holding_display` (human string)

### Memory Collection

State store receives a **flat dict** (single-row collection):

```json
{
  "used_pct": 60.3,
  "total_display": "3.8 GB",
  "used_display": "2.3 GB"
}
```

**Required fields:**
- `used_pct` — drives the memory gauge (0–100)
- `total_display`, `used_display` — shown in gauge subtitle

The base driver's `_compute_memory_pct()` can compute these if your data has any of: `total_bytes`/`used_bytes`, `total_kb`/`used_kb`, `total`/`used`/`free`. But if your vendor's memory model is unusual (like Arista's Linux `top` output), override in the driver.

### Interfaces Collection

State store receives: `{"interfaces": [...]}`

```json
{
  "interfaces": [
    {
      "interface": "Ethernet1",
      "status": "up",
      "protocol": "up",
      "ip_address": "172.16.2.2/30"
    }
  ]
}
```

**Required fields per interface:** `interface`, `status`, `protocol`, `ip_address`

### Interface Detail Collection

State store receives: `{"interfaces": [...]}`

```json
{
  "interfaces": [
    {
      "interface": "Ethernet1",
      "link_status": "up",
      "input_rate_bps": 1230000,
      "output_rate_bps": 456000,
      "bandwidth_kbps": 1000000,
      "utilization_pct": 0.1,
      "in_errors": 0,
      "out_errors": 0,
      "crc_errors": 0,
      "mtu": 1500,
      "description": "uplink-to-spine1"
    }
  ]
}
```

**Required fields for throughput chart:** `interface`, `input_rate_bps`, `output_rate_bps` (integer bps values)

**Optional but used:** `bandwidth_kbps`, `utilization_pct`, `in_errors`, `out_errors`, `crc_errors`, `mtu`, `description`

**Critical:** The dashboard expects `input_rate_bps` and `output_rate_bps` as **integers in bits per second**. If your vendor returns rates with unit suffixes (like Arista's "1.23 Mbps"), the driver must convert them.

### Neighbors Collection

State store receives: `{"neighbors": [...]}`

```json
{
  "neighbors": [
    {
      "device_id": "switch1",
      "local_intf": "Et1",
      "remote_intf": "Gi0/1",
      "mgmt_ip": "172.16.1.1",
      "platform": "Cisco IOS",
      "capabilities": "Router, Switch"
    }
  ]
}
```

**Required fields:** `device_id`, `local_intf`

**Used by graph:** `remote_intf` (edge labels), `mgmt_ip` (hover tooltip), `platform` (hover), `capabilities` (determines node shape: router vs switch)

**Note:** The dashboard's neighbor graph renders node shapes based on the `capabilities` string — if it contains "Router", the node is a cyan roundRect; if "Switch", green rect. If your vendor doesn't provide capabilities (LLDP often doesn't), consider inferring from the platform string in your driver.

### Log Collection

State store receives: `{"entries": [...]}`

```json
{
  "entries": [
    {
      "timestamp": "Feb 17 01:22:42",
      "facility": "SYS",
      "severity": 5,
      "mnemonic": "CONFIG_I",
      "message": "Configured from console by cisco on vty1"
    }
  ]
}
```

**Required fields:** `timestamp`, `mnemonic`, `message`

**Used for badge:** `severity` (int, 0-7) — entries with severity ≤ 4 counted as warnings

The base driver's `_post_process_log()` handles timestamp assembly from TextFSM components (`month`, `day`, `time`), message list joining, severity coercion, newest-first ordering, and trimming to 50 entries. Most vendors can use it as-is.

### BGP Summary Collection

State store receives: `{"peers": [...]}`

```json
{
  "peers": [
    {
      "neighbor": "10.0.0.1",
      "as": "65001",
      "state": "Established",
      "prefixes_rcvd": 42
    }
  ]
}
```

Currently collected but no dashboard panel — planned for the routing module.


## Creating a New Vendor Driver

### Step 1: Create the driver file

`drivers/my_vendor.py`:

```python
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

from . import (
    VendorDriver,
    register_driver,
    _post_process_log,
    _normalize_bgp_peers,
    # Import shared transforms you need:
    # _compute_memory_pct,      # generic memory % calculation
    # _filter_cpu_processes,     # filter 0% CPU processes (Cisco-style)
    # _merge_memory_into_processes,  # cross-ref memory into CPU procs
)

if TYPE_CHECKING:
    from ..state_store import DeviceStateStore

logger = logging.getLogger(__name__)


@register_driver("my_vendor")
class MyVendorDriver(VendorDriver):

    @property
    def pagination_command(self) -> str:
        # Vendor-specific command to disable CLI paging
        # Common values:
        #   Cisco IOS/NX-OS:  "terminal length 0"
        #   Arista EOS:       "terminal length 0"
        #   Juniper JunOS:    "set cli screen-length 0"
        #   Palo Alto:        "set cli pager off"
        return "terminal length 0"

    def post_process(self, collection, data, state_store=None):
        if collection == "cpu":
            data = self._normalize_cpu(data)
            # Build process list from vendor-specific fields
            # ...

        elif collection == "memory":
            data = self._normalize_memory(data)

        elif collection == "log":
            data = _post_process_log(data)

        elif collection == "bgp_summary" and "peers" in data:
            data["peers"] = _normalize_bgp_peers(data["peers"])

        elif collection == "neighbors" and "neighbors" in data:
            data["neighbors"] = self._post_process_neighbors(data["neighbors"])

        elif collection == "interface_detail" and "interfaces" in data:
            data["interfaces"] = self._post_process_interfaces(data["interfaces"])

        return data
```

**Auto-discovery:** Drop the file in `drivers/` with the `@register_driver` decorator. The `__init__.py` auto-imports all modules in the package via `pkgutil`. No manual wiring needed.

**Pagination:** Get this right first. If the device paginates output with `--More--` prompts, nothing downstream works. Test by SSH'ing in manually and confirming your command disables paging.

### Step 2: Identify vendor-specific field mapping

This is where the real work is. For every collection, you need to know:

1. What CLI command produces the data
2. Whether an NTC TextFSM template exists for it
3. What field names the template returns
4. How those map to the dashboard's expected field names

**Checking NTC templates:**

```python
import ntc_templates
from pathlib import Path
tdir = Path(ntc_templates.__file__).parent / "templates"

# List all templates for a vendor
for f in sorted(tdir.glob("juniper_junos_show_*")):
    print(f.name)

# Check field names in a specific template
t = tdir / "juniper_junos_show_interfaces.textfsm"
for line in t.read_text().splitlines():
    if line.startswith("Value"):
        print(line)
```

**Critical:** TextFSM returns UPPERCASE field names. The parser chain lowercases them (line 164 of `parser_chain.py`). Your driver code should always use lowercase field names.

### Step 3: Write post-processing methods

Each collection needs a method that transforms parser output into the dashboard contract. Common patterns:

**CPU normalization:** Every vendor reports CPU differently. Map whatever your vendor provides to `five_sec_total` (0–100 float).

- Cisco: `cpu_usage_5_sec` directly
- Arista: `100 - global_cpu_percent_idle` (Linux top)
- Juniper: varies by command (`show chassis routing-engine` vs `show system processes`)

**Memory normalization:** Get to `used_pct` plus `total_display`/`used_display`. The base driver's `_compute_memory_pct()` handles most cases if you can get `total` and `used` (or `free`) as numeric values. Override only if the vendor's memory model is non-standard.

**Process list building:** The critical lesson from Arista — **know your vendor's CPU reporting model before writing the filter.** Cisco `show processes cpu sorted` reports per-process 5-second CPU averages that are meaningful and non-zero. Arista `show processes top once` runs a single Linux `top` snapshot where instantaneous CPU is 0% for most processes. If you filter `> 0%`, you get an empty table. Options:

- Filter > 0% (Cisco-style, where averages are meaningful)
- Keep top N sorted by CPU then memory (Arista-style, for instantaneous snapshots)
- No filter, just sort (safe default)

**Rate conversion:** If your vendor returns rates as strings with units ("1.23 Mbps", "456 Kbps"), the driver must convert to integer bps. The dashboard only understands `input_rate_bps` / `output_rate_bps` as bare integers.

**Neighbor normalization:** Different vendors use different discovery protocols and field names:

| Vendor | Protocol | Command | Key Template Differences |
|--------|----------|---------|------------------------|
| Cisco IOS | CDP | `show cdp neighbors detail` | Has `capabilities`, `platform` as separate fields |
| Arista EOS | LLDP | `show lldp neighbors detail` | No `capabilities` field; `neighbor_description` contains system description (platform + version mashed together) |
| Juniper | LLDP | `show lldp neighbors` | Different field names entirely |
| Cisco NX-OS | CDP/LLDP | `show cdp neighbors detail` | Similar to IOS but NTC template field names may differ |

Common cleanup needed: strip FQDN from device IDs, extract short platform from verbose descriptions, shorten interface names for graph labels.


## Creating Collection Configs

### File location

`collections/{collection_name}/{vendor_id}.yaml`

Example: `collections/cpu/juniper_junos.yaml`

**Vendor ID fallback:** The collection loader tries `{vendor}.yaml` first, then strips the last `_` segment. So `cisco_ios_xe` falls back to `cisco_ios.yaml`. This means IOS and IOS-XE can share configs. Arista, Juniper, and NX-OS each need their own files.

### YAML structure

```yaml
# collections/{collection}/{vendor}.yaml
# Always add the path as line 1 — files share names across directories

command: "show processes cpu sorted"
interval: 30

parsers:
  # Priority 1: custom TextFSM override (local templates/textfsm/)
  - type: textfsm
    templates:
      - my_custom_template.textfsm          # tried first (local)
      - vendor_show_command.textfsm          # tried second (NTC)

  # Priority 2: TTP (if installed)
  - type: ttp
    templates:
      - vendor_show_command.ttp

  # Priority 3: regex fallback (always have one)
  - type: regex
    pattern: '...'
    flags: MULTILINE
    groups:
      field_name: 1

# Map parser output field names → canonical field names
normalize:
  canonical_name: parser_field_name
```

### The normalize map

**This is inverted from what you'd expect.** The YAML reads `canonical: parser_field` but the code inverts it to `parser_field → canonical`:

```yaml
# YAML says:       "destination: source"
normalize:
  device_id: neighbor_name        # neighbor_name → device_id
  local_intf: local_interface     # local_interface → local_intf
  mgmt_ip: mgmt_address          # mgmt_address → mgmt_ip
```

The parser chain's `_normalize()` function (line 273 of `parser_chain.py`) does:
```python
remap = {v: k for k, v in normalize_map.items()}
```

So `{canonical: parser_field}` becomes `{parser_field: canonical}`.

**When the normalize map and the field name are the same** (e.g., `interface: interface`), it's a no-op but harmless. Include it for documentation.

**Fields not in the normalize map pass through unchanged.** Only fields explicitly listed get renamed.

### Parser priority — always include a regex fallback

TextFSM templates break. They break on OS version differences, on output format changes, on edge cases the template author never tested. The parser chain tries them in order and the first to return rows wins — but if all TextFSM templates fail, you want a regex that catches the minimum viable data.

The regex doesn't need to capture everything. For CPU, catching the 5-second total is enough to keep the gauge alive. For interfaces, catching interface name and status keeps the status table populated. The full TextFSM parse is better, but the regex keeps the widget alive when templates break.

```yaml
parsers:
  - type: textfsm
    templates:
      - juniper_junos_show_chassis_routing_engine.textfsm
  - type: regex
    pattern: 'CPU utilization[:\s]+(\d+) percent'
    flags: DOTALL
    groups:
      cpu_total: 1
```

### Template resolution order

When the parser chain resolves a template filename:

1. **Local overrides first:** `templates/textfsm/` in the project directory
2. **NTC templates second:** the installed `ntc-templates` package

A file in `templates/textfsm/` with the same name as an NTC template **shadows it**. This is how you fix broken NTC templates without modifying the package.

### Listing the same template twice

If your custom template lives in `templates/textfsm/` with the same name as the NTC template, listing it twice in the YAML is redundant — the resolver finds the local one first regardless. But if your custom template has a **different** filename, list it first:

```yaml
templates:
  - my_fixed_juniper_show_interfaces.textfsm   # custom, tried first
  - juniper_junos_show_interfaces.textfsm       # NTC, tried second
```


## Writing Custom TextFSM Templates

### When you need one

1. **NTC template doesn't exist** for your vendor/command combination
2. **NTC template exists but is missing fields** you need (like Arista `show interfaces` — NTC template matches rate lines but doesn't capture them)
3. **NTC template has `^. -> Error`** that chokes on unexpected output lines
4. **NTC template's regex doesn't handle your OS version's format** (negative priority values, different column layouts, etc.)

### Common gotchas

**No blank line between comments and Values.** TextFSM uses the first blank line to separate the Value section from the State section. A blank line between your header comments and the first `Value` line will cause a parse error:

```
# This comment block
                            ← THIS BLANK LINE BREAKS EVERYTHING
Value Required INTERFACE (\S+)
```

Fix: no blank lines between comments and Values, or between Value lines.

**`^. -> Error` kills flexibility.** Many NTC templates end their Start state with `^. -> Error`, which means any unrecognized line causes a template error. Custom templates should use `^.` (match and discard) instead:

```
  # NTC style (fragile):
  ^. -> Error

  # Custom style (resilient):
  ^.
```

**Uppercase Value names, lowercase in code.** TextFSM Values are defined in UPPERCASE (`Value INPUT_RATE (...)`), but the parser chain lowercases all keys. Your driver code and normalize maps should always reference lowercase (`input_rate`).

**Filldown Values for global stats.** When a command returns header stats followed by per-row data (like `top` or `show processes cpu sorted`), use `Value Filldown` for the header fields. They propagate to every subsequent row, so row[0] has both global stats and the first process. The shaping function handles this — for CPU, row[0] becomes the summary dict and rows[1:] become the `processes` list.

**`Value Required` controls Record creation.** Only rows where all `Required` Values have been captured get emitted. If your template has `Value Required INTERFACE (\S+)`, rows without an interface match are silently dropped. Use this deliberately — it's how you skip header and blank lines.

**Rate fields with units need driver conversion.** If a field captures "1.23 Mbps" as a string, it must be converted to integer bps in the driver's post-process step. The dashboard only understands bare numeric bps values for `input_rate_bps` / `output_rate_bps`.

### Template testing

Always validate templates before deploying:

```python
import textfsm

with open("templates/textfsm/my_template.textfsm") as f:
    fsm = textfsm.TextFSM(f)

print(f"Fields: {[h.lower() for h in fsm.header]}")

# Paste actual device output (from SSH debug) as a triple-quoted string
sample = """..."""
rows = fsm.ParseTextToDicts(sample)
for r in rows:
    r = {k.lower(): v for k, v in r.items()}
    print(r)
```

Get sample output from the actual device. Don't guess at the format — vendor output is full of whitespace variations, optional fields, and version differences.


## Output Shaping

The `_default_shape_output()` function in `drivers/__init__.py` handles the conversion from parser rows to state store dicts. You typically don't need to override this, but you need to understand it.

### Single-row collections: `cpu`, `memory`, `device_info`

Row[0] becomes a flat dict. For CPU, if there are multiple rows, rows[1:] are attached as `data["processes"]`.

### Multi-row collections

Rows are wrapped in a keyed list. The key is defined in `COLLECTION_LIST_KEYS`:

```python
COLLECTION_LIST_KEYS = {
    "interfaces": "interfaces",
    "interface_detail": "interfaces",
    "bgp_summary": "peers",
    "neighbors": "neighbors",
    "log": "entries",
    "environment": "sensors",
}
```

If you add a new multi-row collection, add its key here. Unknown collections get wrapped as `{"data": rows}`.


## Validation Workflow

After creating a driver and collection configs, validate in this order:

### 1. Preflight check

```bash
python -m wirlwind_telemetry --host DEVICE_IP --vendor my_vendor --user admin --preflight-only --debug
```

This resolves all templates without connecting. Look for:
- All collections found for your vendor
- Template resolution paths (local vs NTC)
- Schema warnings (missing `_schema.yaml`)

### 2. Parse trace

```bash
python -m wirlwind_telemetry --host DEVICE_IP --vendor my_vendor --user admin --debug
```

Watch the TRACE lines:

```
TRACE [cpu] parsed_by=textfsm rows=47 fields=5 duration=12.3ms       ← working
TRACE [neighbors] parsed_by=none rows=0 ERROR=all parsers failed      ← broken
```

`parsed_by=none` means all parsers failed. Check:
- Is the command correct for this vendor?
- Does the TextFSM template match the actual output format?
- Is the sanitizer stripping too much (command echo removal)?

### 3. Dashboard debug buttons

Each panel header has `{ }` debug buttons that dump the current state store JSON for that collection. Use these to see exactly what data reached the frontend:

- Fields present but wrong names → fix normalize map
- Fields present but wrong types → fix driver post-process or add schema
- No data at all → parser chain failed (check TRACE)
- Data present but panel empty → dashboard JS handler doesn't recognize the field names

### 4. Collection-by-collection

Don't try to bring all 7 collections up at once. Start with CPU and memory (simplest), then interfaces, then interface_detail, then neighbors, then log and BGP. Each one validates a different part of the pipeline.


## Vendor-Specific Notes

### Juniper JunOS

Juniper is the most different from the Cisco/Arista pattern:

- **Pagination:** `set cli screen-length 0` (operational mode), but beware — Juniper has separate operational and configuration modes
- **Output format:** Juniper supports both text and XML output. NTC templates parse the text format. Some commands benefit from `| display xml` but that requires a completely different parsing strategy (lxml, not TextFSM). Stick with text output and TextFSM for consistency
- **CPU:** `show chassis routing-engine` returns CPU as a percentage. Different from Cisco's per-process output. Consider also `show system processes extensive` for process list
- **Memory:** Also in `show chassis routing-engine` — memory total and used as separate fields
- **Interfaces:** `show interfaces` text output differs significantly from Cisco. NTC template exists but verify field names
- **Neighbors:** `show lldp neighbors` for summary, `show lldp neighbors detail` (or `show lldp neighbors interface`) for full info
- **Log:** `show log messages` or `show system syslog`
- **Prompt detection:** Juniper prompts end with `>` (operational) or `#` (config). The SSH client's prompt detection handles this.

### Cisco NX-OS

Closer to IOS but with differences:

- **Pagination:** `terminal length 0` (same as IOS)
- **CPU:** `show processes cpu sort` (note: `sort` not `sorted`)
- **NTC templates:** Many exist but field names may differ from IOS templates. Always verify field names — don't assume IOS field names work
- **VDC context:** NX-OS can have virtual device contexts. Ensure commands run in the right context
- **JSON output:** NX-OS supports `| json` for structured output. Future opportunity, but stick with text + TextFSM for now

### Template availability

Check NTC template coverage before starting a vendor:

```python
import ntc_templates
from pathlib import Path
tdir = Path(ntc_templates.__file__).parent / "templates"
templates = sorted(f.name for f in tdir.glob("juniper_junos_show_*"))
for t in templates:
    print(t)
```

No template? You'll need a custom TextFSM in `templates/textfsm/` or a regex fallback.


## Checklist: New Vendor to Full Coverage

```
Driver:
  [ ] drivers/{vendor}.py with @register_driver
  [ ] Pagination command tested via manual SSH
  [ ] post_process handles: cpu, memory, log, bgp_summary, neighbors, interface_detail
  [ ] Auto-discovery confirmed (driver appears in list_drivers())

Collections (7 for full parity):
  [ ] collections/cpu/{vendor}.yaml
  [ ] collections/memory/{vendor}.yaml
  [ ] collections/interfaces/{vendor}.yaml
  [ ] collections/interface_detail/{vendor}.yaml
  [ ] collections/neighbors/{vendor}.yaml
  [ ] collections/log/{vendor}.yaml
  [ ] collections/bgp_summary/{vendor}.yaml

Each collection config:
  [ ] Correct CLI command for this vendor
  [ ] TextFSM template verified (exists in NTC or custom written)
  [ ] Regex fallback included
  [ ] Normalize map matches parser output → dashboard field names
  [ ] Path comment on line 1 (files share names across directories)

Custom TextFSM (if needed):
  [ ] Template in templates/textfsm/
  [ ] No blank lines between comments and Value declarations
  [ ] Uses ^. (not ^. -> Error) for unmatched lines
  [ ] Tested against actual device output
  [ ] Rate fields captured if NTC template ignores them

Validation:
  [ ] Preflight check passes (--preflight-only --debug)
  [ ] All 7 collections show parsed_by=textfsm (or regex) in TRACE
  [ ] Dashboard panels render with live data
  [ ] Debug JSON dump shows correct field names and types
  [ ] Throughput chart shows non-zero values on active interfaces
  [ ] Neighbor graph renders with correct node shapes
```