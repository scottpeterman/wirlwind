"""
Parser Chain — Ordered fallback parser for CLI output.

TextFSM → TTP → Regex fallback.
First parser that returns valid structured data wins.

Each result carries metadata:
    _parsed_by:  "textfsm" | "ttp" | "regex" | "none"
    _template:   template filename or "inline"
    _error:      error message (only on failure)

The chain now accepts an optional ParseTrace object for structured
audit logging. Every template tried, every resolution attempt, every
failure reason is recorded.

Collection config (YAML):
    command: "show ip interface brief"
    interval: 60
    parsers:
      - type: textfsm
        templates:
          # Tried in order — first match wins.
          # Local overrides resolve before ntc-templates.
          - my_custom_show_ip_interface_brief.textfsm    # local override
          - cisco_ios_show_ip_interface_brief.textfsm    # ntc-templates
      - type: ttp
        templates:
          - cisco_ios_show_ip_interface_brief.ttp
      - type: regex
        pattern: '^(\\S+)\\s+...'
        flags: MULTILINE
        groups:
          intf: 1
          ipaddr: 2
    normalize:
      name: intf
      ip_address: ipaddr
"""

from __future__ import annotations
import re
import logging
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .parse_trace import ParseTrace

logger = logging.getLogger(__name__)

# ── Optional imports — degrade gracefully ──────────────────────────

_HAS_TEXTFSM = False
_HAS_TTP = False

try:
    import textfsm
    _HAS_TEXTFSM = True
except ImportError:
    logger.info("textfsm not installed — TextFSM parser unavailable")

try:
    from ttp import ttp
    _HAS_TTP = True
except ImportError:
    logger.info("ttp not installed — TTP parser unavailable")


# ── Metadata helper ────────────────────────────────────────────────

def _meta(parsed_by: str, template: str = "inline", error: str = None) -> dict:
    """Build parser metadata dict."""
    m = {"_parsed_by": parsed_by, "_template": template}
    if error:
        m["_error"] = error
    return m


# ── CLI output sanitizer ──────────────────────────────────────────

def _sanitize_cli_output(
    raw: str,
    command: str | None = None,
    trace: "ParseTrace" = None,
) -> str:
    """
    Strip command echo and trailing prompt from raw CLI output.

    Network device SSH sessions (invoke_shell) include:
    1. Command echo on the first line(s)
    2. Actual command output
    3. Device prompt on the last line

    NTC TextFSM templates have strict '^. -> Error' rules that reject
    unrecognized lines like the command echo, causing silent parse failures.
    """
    if not raw:
        return raw

    original_lines = len(raw.splitlines())
    lines = raw.splitlines()
    stripped_top = 0
    stripped_bottom = 0

    # ── Strip command echo ──────────────────────────────────────
    if command and lines:
        cmd_stripped = command.strip()
        for i in range(min(3, len(lines))):
            line = lines[i].strip()
            if not line:
                continue
            if (line == cmd_stripped
                    or line.endswith(cmd_stripped)
                    or cmd_stripped in line):
                stripped_top = i + 1
                lines = lines[i + 1:]
                break

    # ── Strip trailing prompt ───────────────────────────────────
    while lines and not lines[-1].strip():
        lines.pop()
        stripped_bottom += 1

    if lines:
        last = lines[-1].strip()
        if (len(last) < 60
                and last
                and last[-1] in ('#', '>', '$', '%', ')')
                and not last[0].isdigit()):
            lines.pop()
            stripped_bottom += 1

    result = '\n'.join(lines)
    total_stripped = stripped_top + stripped_bottom

    if trace and total_stripped > 0:
        trace.sanitized(result, lines_stripped=total_stripped)

    return result


# ── Individual parsers ─────────────────────────────────────────────

def _parse_textfsm(raw: str, template_path: Path) -> tuple[list[dict] | None, str]:
    """
    Parse raw CLI output using a TextFSM template file.

    Returns (rows, error_reason):
        rows:   list of dicts (one per parsed row) or None on failure
        error:  human-readable reason for failure (empty on success)
    """
    if not _HAS_TEXTFSM:
        return None, "textfsm not installed"
    if not template_path.exists():
        return None, f"template file not found: {template_path}"

    try:
        with open(template_path, "r") as f:
            fsm = textfsm.TextFSM(f)

        rows = fsm.ParseTextToDicts(raw)
        if rows:
            # TextFSM returns uppercase keys — normalize to lowercase
            normalized = [{k.lower(): v for k, v in row.items()} for row in rows]
            return normalized, ""
        return None, "0 rows returned (pattern matched but no data extracted)"
    except textfsm.TextFSMTemplateError as e:
        return None, f"template syntax error: {e}"
    except Exception as e:
        return None, f"parse exception: {e}"


def _parse_ttp(raw: str, template_path: Path) -> tuple[list[dict] | None, str]:
    """
    Parse raw CLI output using a TTP template file.

    Returns (rows, error_reason).
    """
    if not _HAS_TTP:
        return None, "ttp not installed"
    if not template_path.exists():
        return None, f"template file not found: {template_path}"

    try:
        template_text = template_path.read_text()
        parser = ttp(data=raw, template=template_text)
        parser.parse()
        results = parser.result()

        if results and results[0]:
            flat = results[0]
            if isinstance(flat, list) and len(flat) > 0:
                if isinstance(flat[0], dict):
                    return flat, ""
                elif isinstance(flat[0], list):
                    return (flat[0] if flat[0] else None), "nested list was empty"
            elif isinstance(flat, dict):
                return [flat], ""
        return None, "0 rows returned"
    except Exception as e:
        return None, f"parse exception: {e}"


def _parse_regex(raw: str, parser_config: dict) -> tuple[list[dict] | None, str]:
    """
    Parse raw CLI output using inline regex from collection config.

    Returns (rows, error_reason).
    """
    pattern = parser_config.get("pattern")
    if not pattern:
        return None, "no pattern defined"

    # Build regex flags
    flags_str = parser_config.get("flags", "")
    flags = 0
    for flag_name in flags_str.replace("|", ",").replace(" ", ",").split(","):
        flag_name = flag_name.strip().upper()
        if flag_name == "MULTILINE":
            flags |= re.MULTILINE
        elif flag_name == "DOTALL":
            flags |= re.DOTALL
        elif flag_name == "IGNORECASE":
            flags |= re.IGNORECASE

    group_map = parser_config.get("groups", {})

    try:
        matches = list(re.finditer(pattern, raw, flags))
        if not matches:
            return None, f"0 matches for pattern"

        results = []
        for m in matches:
            row = {}
            if group_map:
                for field_name, group_idx in group_map.items():
                    try:
                        row[field_name] = m.group(int(group_idx))
                    except (IndexError, ValueError):
                        row[field_name] = None
            else:
                row = m.groupdict()
                if not row:
                    for i, g in enumerate(m.groups(), 1):
                        row[f"field_{i}"] = g
            results.append(row)

        return results if results else None, "" if results else "no groups captured"

    except re.error as e:
        return None, f"regex compile error: {e}"


# ── Normalizer ─────────────────────────────────────────────────────

def _normalize(
    rows: list[dict],
    normalize_map: dict | None,
    trace: "ParseTrace" = None,
) -> list[dict]:
    """
    Remap field names from parser output to canonical schema names.

    normalize_map format (config says canonical: parser_field):
        five_sec: five_sec_total
        one_min: one_min
    """
    if not normalize_map:
        return rows

    # Invert: parser_field → canonical
    remap = {v: k for k, v in normalize_map.items()}

    if trace and rows:
        before_fields = list(rows[0].keys()) if rows else []

    normalized = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            canonical = remap.get(key, key)
            new_row[canonical] = value
        normalized.append(new_row)

    if trace and normalized:
        after_fields = list(normalized[0].keys()) if normalized else []
        trace.normalized(
            before_fields=before_fields,
            after_fields=after_fields,
            remap=remap,
        )

    return normalized


def _coerce_types(
    rows: list[dict],
    schema: dict | None,
    trace: "ParseTrace" = None,
) -> list[dict]:
    """
    Coerce field values to types defined in the schema.

    Schema fields format:
        field_name: { type: int, description: "..." }
    """
    if not schema:
        return rows

    fields = schema.get("fields", {})
    if not fields:
        return rows

    type_changes = {}
    coerced = []
    for row in rows:
        new_row = {}
        for key, value in row.items():
            field_spec = fields.get(key)
            if field_spec and value is not None:
                target_type = field_spec.get("type", "str")
                original_type = type(value).__name__
                try:
                    if target_type == "int":
                        new_row[key] = int(float(str(value).replace(",", "")))
                    elif target_type == "float":
                        new_row[key] = float(str(value).replace(",", ""))
                    elif target_type == "bool":
                        new_row[key] = str(value).lower() in ("true", "1", "yes")
                    else:
                        new_row[key] = str(value) if value else ""

                    if original_type != target_type:
                        type_changes[key] = f"{original_type}→{target_type}"
                except (ValueError, TypeError):
                    new_row[key] = value
            else:
                new_row[key] = value
        coerced.append(new_row)

    if trace and type_changes:
        trace.coerced(type_changes=type_changes)

    return coerced


# ── Template resolver ──────────────────────────────────────────────

class TemplateResolver:
    """
    Resolves template filenames to filesystem paths.

    Search order:
    1. Local project templates (custom/override) — highest priority
    2. ntc-templates package (if installed)

    The search order means a custom template with the same filename
    as an NTC template will shadow it. This is intentional — when an
    NTC template breaks on a specific IOS version, drop a fixed copy
    into templates/textfsm/ and it takes priority automatically.
    """

    def __init__(self, search_paths: list[str | Path] = None):
        self._paths: list[Path] = []

        if search_paths:
            for p in search_paths:
                path = Path(p)
                if path.exists():
                    self._paths.append(path)
                else:
                    logger.debug(f"Template search path not found: {p}")

        self._ntc_path = self._find_ntc_templates()
        if self._ntc_path:
            self._paths.append(self._ntc_path)

        if self._paths:
            logger.info(
                f"Template search paths (priority order): "
                f"{[str(p) for p in self._paths]}"
            )

    @staticmethod
    def _find_ntc_templates() -> Path | None:
        """Locate the ntc-templates package template directory."""
        try:
            import ntc_templates
            pkg_dir = Path(ntc_templates.__file__).parent / "templates"
            if pkg_dir.exists():
                logger.debug(f"Found ntc-templates at: {pkg_dir}")
                return pkg_dir
        except ImportError:
            pass

        import site
        for sp in site.getsitepackages() + [site.getusersitepackages()]:
            candidate = Path(sp) / "ntc_templates" / "templates"
            if candidate.exists():
                return candidate

        return None

    def resolve(
        self,
        filename: str,
        trace: "ParseTrace" = None,
    ) -> Path | None:
        """
        Find a template file by name.
        Returns the first match across search paths, or None.

        Records resolution attempts in the trace for diagnostics.
        """
        for base in self._paths:
            candidate = base / filename
            if candidate.exists():
                if trace:
                    trace.template_resolved(
                        filename,
                        resolved_path=str(candidate),
                        search_paths=[str(p) for p in self._paths],
                    )
                return candidate

            # Search subdirectories (ntc-templates uses flat structure)
            for match in base.rglob(filename):
                if trace:
                    trace.template_resolved(
                        filename,
                        resolved_path=str(match),
                        search_paths=[str(p) for p in self._paths],
                    )
                return match

        # Not found
        if trace:
            trace.template_resolved(
                filename,
                resolved_path=None,
                search_paths=[str(p) for p in self._paths],
            )
        logger.debug(f"Template not resolved: {filename}")
        return None


# ── Parser Chain ───────────────────────────────────────────────────

class ParserChain:
    """
    Ordered parser chain: TextFSM → TTP → Regex fallback.

    The chain iterates through parsers defined in a collection config.
    First parser that returns structured data wins. Metadata about
    which parser succeeded rides along with every result.
    """

    def __init__(self, template_search_paths: list[str | Path] = None):
        self._resolver = TemplateResolver(template_search_paths)

    def parse(
        self,
        raw_output: str,
        collection_config: dict,
        schema: dict | None = None,
        trace: "ParseTrace" = None,
    ) -> tuple[list[dict], dict]:
        """
        Parse raw CLI output using the parser chain.

        Args:
            raw_output:        Raw text from the device
            collection_config: Collection YAML config with 'parsers' list
            schema:            Optional canonical schema for type coercion
            trace:             Optional ParseTrace for audit logging

        Returns:
            (parsed_rows, metadata)
        """
        parsers = collection_config.get("parsers", [])
        normalize_map = collection_config.get("normalize")
        command = collection_config.get("command")
        errors = []

        if not raw_output or not raw_output.strip():
            return [], _meta("none", error="empty output")

        # Sanitize: strip command echo and prompt
        cleaned = _sanitize_cli_output(raw_output, command, trace=trace)

        for parser_def in parsers:
            ptype = parser_def.get("type", "").lower()

            if ptype == "textfsm":
                result, template_name = self._try_textfsm(
                    cleaned, parser_def, trace=trace
                )
                if result:
                    result = _normalize(result, normalize_map, trace=trace)
                    result = _coerce_types(result, schema, trace=trace)
                    return result, _meta("textfsm", template_name)
                errors.append(f"textfsm: no match")

            elif ptype == "ttp":
                result, template_name = self._try_ttp(
                    cleaned, parser_def, trace=trace
                )
                if result:
                    result = _normalize(result, normalize_map, trace=trace)
                    result = _coerce_types(result, schema, trace=trace)
                    return result, _meta("ttp", template_name)
                errors.append(f"ttp: no match")

            elif ptype == "regex":
                result, reason = _parse_regex(cleaned, parser_def)
                if trace:
                    trace.parser_tried(
                        "regex", "inline",
                        success=result is not None,
                        reason=reason,
                        rows=len(result) if result else 0,
                        fields=list(result[0].keys()) if result else [],
                    )
                if result:
                    result = _normalize(result, normalize_map, trace=trace)
                    result = _coerce_types(result, schema, trace=trace)
                    return result, _meta("regex")
                errors.append(f"regex: {reason}")

            else:
                errors.append(f"unknown parser type: {ptype}")

        error_detail = "; ".join(errors) if errors else "no parsers defined"
        return [], _meta("none", error=f"all parsers failed ({error_detail})")

    def _try_textfsm(
        self,
        raw: str,
        parser_def: dict,
        trace: "ParseTrace" = None,
    ) -> tuple[list[dict] | None, str]:
        """
        Try TextFSM templates in order. Return (result, template_name).

        Each template in the list is tried sequentially. The first one
        that produces rows wins. This is how custom templates override
        broken NTC templates — put your fixed version first in the list,
        and it resolves from templates/textfsm/ before ntc-templates.
        """
        templates = parser_def.get("templates", [])

        for tname in templates:
            path = self._resolver.resolve(tname, trace=trace)

            if not path:
                if trace:
                    trace.parser_tried(
                        "textfsm", tname,
                        success=False,
                        reason=f"template not found in search paths",
                        rows=0,
                    )
                continue

            result, reason = _parse_textfsm(raw, path)

            if trace:
                trace.parser_tried(
                    "textfsm", tname,
                    resolved_path=str(path),
                    success=result is not None,
                    reason=reason,
                    rows=len(result) if result else 0,
                    fields=list(result[0].keys()) if result else [],
                )

            if result:
                return result, tname

        return None, ""

    def _try_ttp(
        self,
        raw: str,
        parser_def: dict,
        trace: "ParseTrace" = None,
    ) -> tuple[list[dict] | None, str]:
        """Try TTP templates in order. Return (result, template_name)."""
        templates = parser_def.get("templates", [])

        for tname in templates:
            path = self._resolver.resolve(tname, trace=trace)

            if not path:
                if trace:
                    trace.parser_tried(
                        "ttp", tname,
                        success=False,
                        reason="template not found",
                        rows=0,
                    )
                continue

            result, reason = _parse_ttp(raw, path)

            if trace:
                trace.parser_tried(
                    "ttp", tname,
                    resolved_path=str(path),
                    success=result is not None,
                    reason=reason,
                    rows=len(result) if result else 0,
                    fields=list(result[0].keys()) if result else [],
                )

            if result:
                return result, tname

        return None, ""

    @property
    def has_textfsm(self) -> bool:
        return _HAS_TEXTFSM

    @property
    def has_ttp(self) -> bool:
        return _HAS_TTP

    @property
    def capabilities(self) -> dict:
        """Report which parser backends are available."""
        return {
            "textfsm": _HAS_TEXTFSM,
            "ttp": _HAS_TTP,
            "regex": True,
            "ntc_templates": self._resolver._ntc_path is not None,
            "ntc_templates_path": str(self._resolver._ntc_path)
                                  if self._resolver._ntc_path else None,
            "search_paths": [str(p) for p in self._resolver._paths],
        }


# ── Collection loader ──────────────────────────────────────────────

class CollectionLoader:
    """
    Loads collection configs and schemas from the collections/ directory.

    Directory structure:
        collections/
        ├── interfaces/
        │   ├── _schema.yaml
        │   ├── cisco_ios.yaml
        │   ├── cisco_ios_xe.yaml
        │   └── arista_eos.yaml
        ├── cpu/
        │   ├── _schema.yaml
        │   └── ...
    """

    def __init__(self, collections_dir: str | Path = None):
        if collections_dir:
            self._dir = Path(collections_dir)
        else:
            self._dir = Path(__file__).parent / "collections"

        self._cache: dict[str, dict] = {}
        self._schemas: dict[str, dict] = {}

    def get_config(self, collection: str, vendor: str) -> dict | None:
        """Load a collection config for a specific vendor."""
        cache_key = f"{collection}/{vendor}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        config_path = self._dir / collection / f"{vendor}.yaml"
        if not config_path.exists():
            # Fallback: cisco_ios_xe → cisco_ios
            if "_" in vendor:
                base_vendor = vendor.rsplit("_", 1)[0]
                config_path = self._dir / collection / f"{base_vendor}.yaml"

        if not config_path.exists():
            logger.debug(f"No collection config: {collection}/{vendor}")
            return None

        try:
            import yaml
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            self._cache[cache_key] = config
            return config
        except Exception as e:
            logger.error(f"Failed to load {config_path}: {e}")
            return None

    def get_schema(self, collection: str) -> dict | None:
        """Load the canonical schema for a collection."""
        if collection in self._schemas:
            return self._schemas[collection]

        schema_path = self._dir / collection / "_schema.yaml"
        if not schema_path.exists():
            return None

        try:
            import yaml
            with open(schema_path, "r") as f:
                schema = yaml.safe_load(f)
            self._schemas[collection] = schema
            return schema
        except Exception as e:
            logger.error(f"Failed to load schema {schema_path}: {e}")
            return None

    def list_collections(self, vendor: str) -> list[str]:
        """List available collections for a vendor."""
        collections = []
        if not self._dir.exists():
            return collections

        for subdir in sorted(self._dir.iterdir()):
            if subdir.is_dir() and not subdir.name.startswith("_"):
                config = self.get_config(subdir.name, vendor)
                if config:
                    collections.append(subdir.name)

        return collections

    def get_collection_interval(self, collection: str, vendor: str) -> int:
        """Get polling interval for a collection."""
        config = self.get_config(collection, vendor)
        if config:
            return config.get("interval", 60)
        return 60


# ── Convenience ────────────────────────────────────────────────────

def parse_collection(
    raw_output: str,
    collection: str,
    vendor: str,
    parser_chain: ParserChain,
    collection_loader: CollectionLoader,
    trace: "ParseTrace" = None,
) -> tuple[list[dict], dict]:
    """
    High-level: parse raw output for a specific collection and vendor.
    """
    config = collection_loader.get_config(collection, vendor)
    if not config:
        return [], _meta("none", error=f"no config for {collection}/{vendor}")

    schema = collection_loader.get_schema(collection)
    return parser_chain.parse(raw_output, config, schema, trace=trace)
