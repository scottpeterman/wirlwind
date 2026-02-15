"""
Template Loader - Loads vendor-specific command/parse templates.

Templates define:
  - What CLI command to run
  - How to parse the output (regex patterns + group mappings)
  - What normalized keys to produce

Template folder structure:
  templates/
    cpu/
      cisco_ios_xe.yaml
      arista_eos.yaml
      juniper_junos.yaml
    memory/
      cisco_ios_xe.yaml
      ...
"""

from __future__ import annotations
import re
import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """Template parsing failed."""
    pass


class Template:
    """A loaded, ready-to-use command/parse template."""

    def __init__(self, collection: str, vendor: str, data: dict):
        self.collection = collection
        self.vendor = vendor
        self.command: str = data["command"]
        self.interval: int = data.get("interval", 60)
        self.patterns: dict = data.get("patterns", {})
        self.post_process: Optional[str] = data.get("post_process")

    def parse(self, raw_output: str) -> dict:
        """
        Parse raw CLI output into normalized data dict.

        Supports two pattern types:
          - 'single': Extract one set of values (e.g., CPU headline)
          - 'table': Extract multiple records (e.g., process list, interface list)
        """
        result = {}

        for key, pattern_def in self.patterns.items():
            try:
                if pattern_def.get("type") == "table":
                    result[key] = self._parse_table(raw_output, pattern_def)
                elif pattern_def.get("type") == "block":
                    result[key] = self._parse_blocks(raw_output, pattern_def)
                else:
                    # Single match
                    match_data = self._parse_single(raw_output, pattern_def)
                    if match_data:
                        result.update(match_data)
            except Exception as e:
                logger.warning(f"Pattern '{key}' failed for {self.vendor}/{self.collection}: {e}")

        return result

    def _parse_single(self, text: str, pdef: dict) -> Optional[dict]:
        """Extract a single match with named groups."""
        pattern = pdef["pattern"]
        flags = self._compile_flags(pdef)
        match = re.search(pattern, text, flags)
        if not match:
            return None

        groups = pdef.get("groups", {})
        result = {}
        for norm_key, group_ref in groups.items():
            raw = match.group(group_ref)
            result[norm_key] = self._cast(raw, pdef.get("types", {}).get(norm_key, "auto"))
        return result

    def _parse_table(self, text: str, pdef: dict) -> list[dict]:
        """Extract multiple rows matching a pattern."""
        pattern = pdef["pattern"]
        flags = self._compile_flags(pdef)
        groups = pdef.get("groups", {})
        rows = []

        for match in re.finditer(pattern, text, flags):
            row = {}
            for norm_key, group_ref in groups.items():
                raw = match.group(group_ref)
                row[norm_key] = self._cast(raw, pdef.get("types", {}).get(norm_key, "auto"))
            rows.append(row)

        # Optional: sort and limit
        sort_by = pdef.get("sort_by")
        if sort_by and rows:
            reverse = pdef.get("sort_desc", True)
            rows.sort(key=lambda r: r.get(sort_by, 0), reverse=reverse)

        limit = pdef.get("limit")
        if limit:
            rows = rows[:limit]

        return rows

    def _parse_blocks(self, text: str, pdef: dict) -> list[dict]:
        """
        Parse block-structured output (e.g., 'show lldp neighbors detail').

        Splits text on a delimiter, then applies field patterns to each block.
        """
        delimiter = pdef.get("delimiter", r"\n-{3,}")
        blocks = re.split(delimiter, text)
        fields = pdef.get("fields", {})
        results = []

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            record = {}
            matched_any = False
            for field_name, field_pattern in fields.items():
                match = re.search(field_pattern, block)
                if match:
                    record[field_name] = match.group(1).strip()
                    matched_any = True

            if matched_any:
                results.append(record)

        return results

    @staticmethod
    def _compile_flags(pdef: dict) -> int:
        flags = 0
        flag_str = pdef.get("flags", "")
        if "m" in flag_str or "MULTILINE" in flag_str:
            flags |= re.MULTILINE
        if "s" in flag_str or "DOTALL" in flag_str:
            flags |= re.DOTALL
        if "i" in flag_str or "IGNORECASE" in flag_str:
            flags |= re.IGNORECASE
        return flags

    @staticmethod
    def _cast(value: str, type_hint: str) -> any:
        """Cast a string value to the appropriate type."""
        if value is None:
            return None
        value = value.strip()
        if type_hint == "int":
            return int(value.replace(",", ""))
        elif type_hint == "float":
            return float(value.replace(",", ""))
        elif type_hint == "str":
            return value
        elif type_hint == "auto":
            # Try int, then float, then string
            clean = value.replace(",", "")
            try:
                return int(clean)
            except ValueError:
                try:
                    return float(clean)
                except ValueError:
                    return value
        return value

    def __repr__(self):
        return f"Template({self.vendor}/{self.collection}: '{self.command}')"


class TemplateLoader:
    """
    Loads and manages vendor parse templates.

    Discovers templates from a folder structure organized by collection type.
    """

    def __init__(self, template_dir: str | Path = None):
        if template_dir is None:
            template_dir = Path(__file__).parent / "templates"
        self.template_dir = Path(template_dir)
        self._cache: dict[str, Template] = {}  # "collection/vendor" -> Template
        self._scan()

    def _scan(self) -> None:
        """Scan template directory and cache available templates."""
        if not self.template_dir.exists():
            logger.warning(f"Template directory not found: {self.template_dir}")
            return

        count = 0
        for collection_dir in self.template_dir.iterdir():
            if not collection_dir.is_dir() or collection_dir.name.startswith("_"):
                continue

            collection = collection_dir.name
            for tmpl_file in collection_dir.glob("*.yaml"):
                vendor = tmpl_file.stem
                try:
                    with open(tmpl_file) as f:
                        data = yaml.safe_load(f)
                    if data and "command" in data:
                        key = f"{collection}/{vendor}"
                        self._cache[key] = Template(collection, vendor, data)
                        count += 1
                        logger.debug(f"Loaded template: {key}")
                except Exception as e:
                    logger.error(f"Failed to load template {tmpl_file}: {e}")

        logger.info(f"Loaded {count} templates from {self.template_dir}")

    def get_template(self, collection: str, vendor: str) -> Optional[Template]:
        """Get a template for a specific collection and vendor."""
        key = f"{collection}/{vendor}"
        return self._cache.get(key)

    def get_collections_for_vendor(self, vendor: str) -> list[Template]:
        """Get all available templates for a vendor."""
        templates = []
        for key, tmpl in self._cache.items():
            if tmpl.vendor == vendor:
                templates.append(tmpl)
        return templates

    def get_supported_vendors(self) -> list[str]:
        """List all vendors with at least one template."""
        vendors = set()
        for tmpl in self._cache.values():
            vendors.add(tmpl.vendor)
        return sorted(vendors)

    def get_supported_collections(self) -> list[str]:
        """List all collection types with at least one template."""
        collections = set()
        for tmpl in self._cache.values():
            collections.add(tmpl.collection)
        return sorted(collections)

    def reload(self) -> None:
        """Rescan and reload all templates."""
        self._cache.clear()
        self._scan()
