# Wirlwind Telemetry — Next Steps

Development roadmap from working prototype to daily-driver tool.

---

## Phase 1: Make What's There Work Right (v0.1.1)

These are bugs and gaps in the current prototype. No new features — just make the existing widgets reliable.

### 1.1 Header / Device Info Sync
**Problem:** Header shows "Waiting for connection" even after SSH is connected and data is flowing. The `deviceInfoChanged` signal fires before the JS listener is wired up.  
**Fix:** Remove reliance on the signal for initial load. Have JS call `bridge.getDeviceInfo()` on QWebChannel connect and again after first poll cycle. The 5-second snapshot poll partially masks this, but first-render should be immediate.

### 1.2 Memory Gauge Showing 0%
**Problem:** Template parses but post-processing doesn't find expected field names. IOS-XE memory output format varies by platform and version.  
**Fix:** Capture actual `show processes memory sorted` output from lab device. Adjust regex in `templates/memory/cisco_ios_xe.yaml`. Likely needs an alternate pattern or different field group names. Test with template test harness (see 2.1).

### 1.3 Process Table Not Populating
**Problem:** CPU template's process regex doesn't match actual output. The `show proc cpu sorted` table format has spacing/column variations across IOS versions.  
**Fix:** Same approach — capture real output, tune regex. The `| exclude 0.00%` filter in the command may also be stripping lines differently than expected.

### 1.4 Panel Error Visibility
**Problem:** When a template fails to parse, the panel shows "Waiting for data" spinner forever. No indication of what went wrong.  
**Fix:** In the JS `handleUpdate`, check for `error:` prefixed collection keys (already emitted by the bridge). Display a red badge with "PARSE ERROR" and optionally the error message in small text. Spinner should timeout after 2 poll cycles and show "No data" instead of spinning forever.

### 1.5 JSON Debug Button on Every Widget
**Problem:** No way to see what the template actually parsed without adding debug prints and re-running.  
**Fix:** Add a `{ }` button in every panel header. On click, show a modal overlay with prettified JSON from `bridge.getCollection(key)`. Include copy-to-clipboard. This turns every running dashboard into a template debugger and is essential for all subsequent template work.

**Implementation:**
- Small monospace `{ }` icon button in panel header, right side, before the badge
- Modal overlay: dark background, monospace font, syntax-highlighted JSON
- `Ctrl+C` / copy button on the modal
- Close on click-outside or Escape
- The bridge method `getCollection(key)` already exists — this is purely a JS/CSS addition

---

## Phase 2: Template Test Harness (v0.1.2)

This unblocks everything else. Every template fix, every new vendor, every new widget requires iterating on regex patterns. Doing that against a live SSH connection is painful. Doing it against captured text files is fast.

### 2.1 CLI Template Tester
**Build:** `wirlwind_telemetry/tools/template_tester.py`

```bash
# Capture raw output once
ssh router "show processes cpu sorted" > samples/cpu_ios_xe.txt

# Test template against it
python -m wirlwind_telemetry.tools.template_tester \
    --template templates/cpu/cisco_ios_xe.yaml \
    --input samples/cpu_ios_xe.txt \
    --verbose

# Output: parsed JSON + match/miss report per pattern
```

**Features:**
- Load YAML template, run all patterns against input text
- Print parsed result as JSON
- Report which patterns matched and which missed (with line numbers of near-misses)
- `--verbose` shows the regex, the groups, and the raw match for each pattern
- `--post-process` flag to also run normalization (memory pct, CPU normalization, BGP state)
- Exit code 0 if all patterns match, 1 if any miss — enables CI testing of templates

### 2.2 Sample Output Collection
**Build:** `samples/` directory with captured command output from lab devices

```
samples/
├── cisco_ios_xe/
│   ├── show_proc_cpu_sorted.txt
│   ├── show_proc_memory_sorted.txt
│   ├── show_ip_int_brief.txt
│   ├── show_interfaces.txt
│   ├── show_ip_bgp_summary.txt
│   ├── show_lldp_neighbors.txt
│   ├── show_environment_all.txt
│   ├── show_logging.txt
│   ├── show_version.txt
│   └── show_ip_protocols.txt
├── arista_eos/
│   └── ...
└── juniper_junos/
    └── ...
```

Capture these from your EVE-NG lab. They become the regression test suite for template changes.

---

## Phase 3: Essential New Collections (v0.2.0)

### 3.1 Show Version (One-Shot)
**Purpose:** Populate header, info strip, and device identity.  
**Design:** New collection type with `interval: 0` meaning "run once on connect, never poll."  
**Template produces:** hostname, model, serial, software_version, uptime, boot_image, total_memory_hw  
**Poll engine change:** Check for `interval: 0` templates, run them during connect phase after prompt detection, before entering the poll loop.  
**Dashboard change:** `show_version` data populates the header (hostname, subtitle), info strip (IP, model, serial, AS, location), and status bar (uptime).

### 3.2 Log Widget
**Purpose:** Close the diagnostic loop. Every other widget shows "what is the state" — logs show "what happened and when."  
**Template:** `show logging | last 50` (already exists, needs regex tuning)  
**Known issues with IOS syslog formats:**
- `*Mar 15 14:22:01.234:` (uptime-based, asterisk prefix)
- `.Mar 15 14:22:01.234:` (NTP-synced, dot prefix)
- `Mar 15 2025 14:22:01.234:` (datetime with year)
- `2025 Mar 15 14:22:01:` (ISO-ish)
- Some platforms include hostname, some don't

**Fix:** Broaden the regex to handle all common formats. Multiple pattern variants in the template, first match wins. OR use a more permissive pattern that captures the whole timestamp string and parses severity/facility with a simpler inner regex.

**Dashboard features:**
- Scrolling list, newest on top
- Color by severity (0-2 red, 3 red, 4 amber, 5 cyan, 6-7 dim)
- Severity filter toggles across the top (show/hide by level)
- On subsequent polls, only show new entries (deduplicate by timestamp+message hash)

### 3.3 Routing Protocol Discovery
**Purpose:** Don't assume BGP. Don't assume OSPF. Probe the device and build the collection list dynamically.

**Phase 0 probe (runs once, after show version, before poll loop):**
```yaml
# templates/capabilities/cisco_ios_xe.yaml
command: "show ip protocols summary"
interval: 0  # one-shot

patterns:
  protocols:
    type: table
    pattern: '^\s+(\w+)\s+(\d+)'
    groups:
      protocol: 1
      process_id: 2
```

**Poll engine changes:**
1. After connect + show version, run capabilities probe
2. Parse which protocols are active
3. For each active protocol, check if a template exists (e.g., `templates/bgp_summary/{vendor}.yaml`)
4. Only add matching collections to the poll list
5. Emit a `widgetConfig` signal to the dashboard with the active collection list

**Dashboard changes:**
- On `widgetConfig`, show/hide routing protocol panels
- If no routing protocols detected, collapse the routing section entirely
- Panel for each active protocol: BGP → peer table, OSPF → neighbor table, etc.

**New templates needed:**
```
templates/
├── capabilities/           # Phase 0 probes
│   ├── cisco_ios_xe.yaml   # show ip protocols summary
│   ├── arista_eos.yaml     # show ip route summary
│   └── juniper_junos.yaml  # show route summary
├── ospf_neighbors/
│   ├── cisco_ios_xe.yaml   # show ip ospf neighbor
│   ├── arista_eos.yaml     # show ip ospf neighbor
│   └── juniper_junos.yaml  # show ospf neighbor
├── isis_adjacency/         # if needed
│   └── ...
```

---

## Phase 4: Dashboard Polish (v0.2.5)

### 4.1 Dynamic Widget Grid
**Problem:** All panels are hardcoded in HTML. Missing data = empty panel with spinner.  
**Fix:** Dashboard receives active collection list from bridge. JS builds the grid dynamically based on what's available. Template:
- Always show: CPU, Memory, Interfaces, Throughput, Trend
- Conditionally show: BGP, OSPF, Neighbors, Environment, Log
- Auto-hide after 2 full poll cycles if still no data

### 4.2 Per-Interface Click-Through
**Action:** Click an interface row in the table.  
**Result:** Popup or slide-in panel showing that interface's counters, error rates, and throughput over time.  
**Data source:** Already in state store from `show interfaces` detail parse. Just needs per-interface history tracking in the state store (currently only CPU and memory have history).

### 4.3 Reconnect Resilience
**Problem:** If SSH drops mid-poll, the engine thread dies.  
**Fix:** Catch connection errors in the poll loop, attempt reconnect with exponential backoff (3s, 6s, 12s, max 60s). Emit `connectionStatus("reconnecting")` so dashboard shows amber status. After max retries, emit error and stop. The SCNG client's connect logic is already robust — just need the retry wrapper in the poll engine.

### 4.4 Connection Status Bar
**Add to info strip or header:** 
- Poll cycle count
- Last successful poll timestamp per collection
- Error count (collections that failed parse on last cycle)
- Uptime of telemetry session (not device uptime)

---

## Phase 5: Multi-Vendor Validation (v0.3.0)

### 5.1 Arista EOS Lab Testing
- Connect to EOS device in EVE-NG
- Capture sample output for all commands
- Tune all Arista templates against real output
- Validate CPU normalization (Linux-style `top` → five_min equivalent)

### 5.2 Juniper JunOS Lab Testing
- Connect to JunOS device in EVE-NG
- Capture sample output for all commands
- Tune all Juniper templates
- This is the real test of normalization — fundamentally different output format

### 5.3 Template Coverage Matrix
Track and publish what works per vendor:

| Collection | IOS-XE | EOS | JunOS |
|---|---|---|---|
| CPU | ✅ tested | ⚠ untested | ⚠ untested |
| Memory | ⚠ partial | ⚠ untested | ⚠ untested |
| Interfaces | ✅ tested | ⚠ untested | ⚠ untested |
| Interface Detail | ⚠ partial | — | — |
| BGP Summary | ⚠ untested | ⚠ untested | ⚠ untested |
| Neighbors | ⚠ untested | ⚠ untested | ⚠ untested |
| Environment | ⚠ untested | — | — |
| Log | ⚠ untested | ⚠ untested | — |
| Show Version | — | — | — |
| Capabilities | — | — | — |

---

## Phase 6: nterm Integration (v0.4.0)

### 6.1 Context Menu Entry
- Right-click device in nterm's session tree → "Telemetry Dashboard"
- Uses fingerprint data (if available) for vendor auto-detection
- Falls back to asking user to select vendor

### 6.2 NtermAuthProvider Wiring
- Import `CredentialResolver` from `wirlwind.vault.resolver`
- `NtermAuthProvider` wraps it (already written, untested)
- Credentials resolved by pattern matching or explicit selection
- No second auth prompt — vault is already unlocked

### 6.3 Tab Integration
- TelemetryWidget opens as a new tab in nterm's QTabWidget
- Tab title: `📊 device-name`
- Tab close triggers widget cleanup (stop polling, close SSH)
- Multiple telemetry tabs allowed (one per device)

### 6.4 Vendor Auto-Detection
- If Secure Cartography fingerprint data is available in the session metadata, use it
- Map fingerprint vendor strings to template vendor names
- If no fingerprint, offer a dropdown selector before connecting

---

## Build Order Summary

```
v0.1.1  Fix header sync, panel error visibility, JSON debug button
        └── All JS/CSS changes, no backend work

v0.1.2  Template test harness + sample output collection
        └── Capture from lab, tune all existing templates

v0.2.0  Show version, log widget, routing protocol discovery
        └── New templates, poll engine changes, dynamic dashboard

v0.2.5  Dashboard polish — dynamic grid, click-through, reconnect
        └── JS-heavy, some state store additions

v0.3.0  Multi-vendor validation — EOS and JunOS lab testing
        └── Template tuning only, no architecture changes

v0.4.0  nterm integration — context menu, auth hook, tab embed
        └── Wire the seam, test the full workflow
```

Each phase produces a usable increment. After v0.1.2 you have reliable template development. After v0.2.0 you have a tool worth using daily. After v0.4.0 it's integrated into your primary workflow.