"""
Parse Trace — Structured audit log for the parser chain.

Every parse attempt records a trace with full provenance:
  - What command was sent
  - What came back (preview + length)
  - What sanitization removed
  - Which templates were tried, in order, and why each failed or succeeded
  - What normalize/coerce/post-process produced
  - Final field inventory delivered to the state store

Traces are emitted as structured JSON log lines at INFO level,
making them grep-friendly and machine-parseable.

Usage:
    trace = ParseTrace("cpu", "cisco_ios_xe")
    trace.raw_received(raw_output, command="show processes cpu sorted")
    trace.sanitized(cleaned_output, lines_stripped=3)
    trace.parser_tried("textfsm", "cisco_ios_show_processes_cpu.textfsm",
                       resolved_path="/path/to/template",
                       success=False, reason="0 rows returned",
                       rows=0)
    trace.parser_tried("regex", "inline", success=True, rows=1,
                       fields=["five_sec_total", "one_min", "five_min"])
    trace.normalized(before_fields=["five_sec_total"], after_fields=["five_sec"])
    trace.post_processed(added_fields=["used_pct"], removed_fields=[])
    trace.delivered(final_fields=["five_sec", "one_min", "five_min", "used_pct"],
                    row_count=1)

    # Emit the full trace as a single structured log line
    trace.emit()

    # Or get the trace dict for programmatic use
    record = trace.as_dict()
"""

from __future__ import annotations
import time
import json
import logging
from typing import Any, Optional

logger = logging.getLogger("wirlwind_telemetry.parse_trace")


class ParseTrace:
    """
    Accumulates parse provenance for one collection poll cycle.

    Build incrementally as data flows through the chain, then emit()
    to write a single structured log record.
    """

    def __init__(self, collection: str, vendor: str):
        self._collection = collection
        self._vendor = vendor
        self._ts = time.time()
        self._steps: list[dict] = []
        self._result: Optional[dict] = None
        self._raw_len: int = 0
        self._raw_preview: str = ""
        self._command: str = ""
        self._duration_ms: float = 0

    # ── Recording steps ──────────────────────────────────────────

    def raw_received(self, raw: str, command: str = ""):
        """Record raw CLI output receipt."""
        self._command = command
        self._raw_len = len(raw) if raw else 0
        # First 200 chars, single-line for log readability
        self._raw_preview = (raw or "")[:200].replace("\n", "\\n")
        self._steps.append({
            "step": "raw",
            "length": self._raw_len,
            "command": command,
        })

    def sanitized(self, cleaned: str, lines_stripped: int = 0):
        """Record sanitization results."""
        cleaned_len = len(cleaned) if cleaned else 0
        self._steps.append({
            "step": "sanitize",
            "original_len": self._raw_len,
            "cleaned_len": cleaned_len,
            "lines_stripped": lines_stripped,
            "delta": self._raw_len - cleaned_len,
        })

    def template_resolved(
        self,
        template_name: str,
        resolved_path: str | None,
        search_paths: list[str] = None,
    ):
        """Record template resolution attempt."""
        self._steps.append({
            "step": "resolve",
            "template": template_name,
            "resolved": str(resolved_path) if resolved_path else None,
            "found": resolved_path is not None,
            "search_paths": search_paths or [],
        })

    def parser_tried(
        self,
        parser_type: str,
        template: str = "inline",
        resolved_path: str = None,
        success: bool = False,
        reason: str = "",
        rows: int = 0,
        fields: list[str] = None,
        error: str = None,
    ):
        """Record one parser attempt in the chain."""
        step = {
            "step": "parse",
            "parser": parser_type,
            "template": template,
            "success": success,
            "rows": rows,
        }
        if resolved_path:
            step["resolved_path"] = resolved_path
        if reason:
            step["reason"] = reason
        if fields:
            step["fields"] = fields
        if error:
            step["error"] = error
        self._steps.append(step)

    def normalized(
        self,
        before_fields: list[str] = None,
        after_fields: list[str] = None,
        remap: dict = None,
    ):
        """Record field normalization."""
        self._steps.append({
            "step": "normalize",
            "before": before_fields or [],
            "after": after_fields or [],
            "remap": remap or {},
        })

    def coerced(self, type_changes: dict = None):
        """Record type coercion. type_changes: {field: "str→int", ...}"""
        self._steps.append({
            "step": "coerce",
            "changes": type_changes or {},
        })

    def post_processed(
        self,
        transform: str = "",
        added_fields: list[str] = None,
        removed_fields: list[str] = None,
        notes: str = "",
    ):
        """Record a post-processing transform."""
        step = {
            "step": "post_process",
            "transform": transform,
        }
        if added_fields:
            step["added"] = added_fields
        if removed_fields:
            step["removed"] = removed_fields
        if notes:
            step["notes"] = notes
        self._steps.append(step)

    def delivered(
        self,
        final_fields: list[str] = None,
        row_count: int = 0,
        parsed_by: str = "none",
        template: str = "",
        error: str = None,
    ):
        """Record final delivery to state store."""
        elapsed = (time.time() - self._ts) * 1000
        self._duration_ms = round(elapsed, 1)

        self._result = {
            "parsed_by": parsed_by,
            "template": template,
            "fields": final_fields or [],
            "rows": row_count,
            "duration_ms": self._duration_ms,
        }
        if error:
            self._result["error"] = error

    # ── Output ───────────────────────────────────────────────────

    def as_dict(self) -> dict:
        """Return the complete trace as a dict."""
        return {
            "collection": self._collection,
            "vendor": self._vendor,
            "command": self._command,
            "raw_len": self._raw_len,
            "raw_preview": self._raw_preview,
            "steps": self._steps,
            "result": self._result or {"parsed_by": "none", "error": "trace incomplete"},
            "duration_ms": self._duration_ms,
        }

    def emit(self):
        """Write the trace as a single structured JSON log line."""
        record = self.as_dict()
        result = record.get("result", {})
        parsed_by = result.get("parsed_by", "none")
        error = result.get("error", "")

        # Summary line for human scanning
        summary = (
            f"[{self._collection}] "
            f"parsed_by={parsed_by} "
            f"rows={result.get('rows', 0)} "
            f"fields={len(result.get('fields', []))} "
            f"duration={self._duration_ms}ms"
        )

        if error:
            summary += f" ERROR={error}"

        # Log summary at appropriate level
        if parsed_by == "none" or error:
            logger.warning(f"TRACE {summary}")
        else:
            logger.info(f"TRACE {summary}")

        # Full structured trace at debug level
        logger.debug(f"TRACE_DETAIL {json.dumps(record, default=str)}")

    def emit_step(self, step_name: str):
        """Emit just the most recent step (for real-time debugging)."""
        if self._steps:
            last = self._steps[-1]
            if last.get("step") == step_name:
                logger.debug(
                    f"[{self._collection}] {step_name}: "
                    f"{json.dumps(last, default=str)}"
                )

    # ── Convenience: count failures ──────────────────────────────

    @property
    def parsers_tried(self) -> int:
        """How many parser attempts were recorded."""
        return sum(1 for s in self._steps if s.get("step") == "parse")

    @property
    def parsers_failed(self) -> int:
        """How many parser attempts failed."""
        return sum(
            1 for s in self._steps
            if s.get("step") == "parse" and not s.get("success")
        )

    @property
    def success(self) -> bool:
        """Did any parser succeed?"""
        return any(
            s.get("step") == "parse" and s.get("success")
            for s in self._steps
        )


class ParseTraceStore:
    """
    Ring buffer of recent parse traces for diagnostic access.

    Keeps the last N traces per collection, queryable from
    the debug overlay or a diagnostic CLI command.
    """

    def __init__(self, max_per_collection: int = 10):
        self._max = max_per_collection
        self._traces: dict[str, list[dict]] = {}

    def store(self, trace: ParseTrace):
        """Store a completed trace."""
        record = trace.as_dict()
        collection = record["collection"]

        if collection not in self._traces:
            self._traces[collection] = []

        self._traces[collection].append(record)

        # Ring buffer: trim to max
        if len(self._traces[collection]) > self._max:
            self._traces[collection] = self._traces[collection][-self._max:]

    def get_recent(self, collection: str, n: int = 5) -> list[dict]:
        """Get the N most recent traces for a collection."""
        traces = self._traces.get(collection, [])
        return traces[-n:]

    def get_failures(self, collection: str = None) -> list[dict]:
        """Get all recent failures, optionally filtered by collection."""
        failures = []
        collections = [collection] if collection else self._traces.keys()

        for coll in collections:
            for trace in self._traces.get(coll, []):
                result = trace.get("result", {})
                if result.get("parsed_by") == "none" or result.get("error"):
                    failures.append(trace)

        return failures

    def get_all_latest(self) -> dict[str, dict]:
        """Get the most recent trace for every collection."""
        return {
            coll: traces[-1]
            for coll, traces in self._traces.items()
            if traces
        }

    def summary(self) -> dict:
        """Get a summary of parse health across all collections."""
        summary = {}
        for coll, traces in self._traces.items():
            if not traces:
                continue
            latest = traces[-1]
            result = latest.get("result", {})
            recent_failures = sum(
                1 for t in traces
                if t.get("result", {}).get("parsed_by") == "none"
            )
            summary[coll] = {
                "last_parsed_by": result.get("parsed_by", "none"),
                "last_template": result.get("template", ""),
                "last_error": result.get("error"),
                "last_duration_ms": result.get("duration_ms", 0),
                "recent_failures": recent_failures,
                "total_traces": len(traces),
            }
        return summary
