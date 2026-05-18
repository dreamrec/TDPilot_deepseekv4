"""Trace viewer tool — v2.5.8.

Exposes the chat-pipe's per-turn JSONL traces (written by
``td_component/tdpilot_api_tracing.py``) through a single MCP tool so
external agents (Claude Code, Claude Desktop, etc.) can read recent
turn-level forensics — tool latencies, tier choices, outcome status —
without scraping the JSONL files manually.

This complements ``td_get_activity_log`` (v2.5.1):

* ``td_get_activity_log`` — 200-entry RAM ring buffer for the CURRENT
  MCP-server-side session. Real-time. Goes away on restart.
* ``td_get_traces`` (this) — disk-persisted per-turn records from
  the CHAT-PIPE agent. Multi-session history (30-day retention).

The trace files live at ``~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl``;
one record per completed agent turn.
"""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field

from td_mcp import tool_registry as _tr  # noqa: E402  — intentional cycle
from td_mcp.lifecycle.traces import (
    DEFAULT_TRACES_DIR,
    iter_jsonl_files,
    read_last_n_jsonl,
)
from td_mcp.tool_registry import mcp  # noqa: E402


@mcp.tool(name="td_get_traces")
async def td_get_traces(
    ctx: Context,
    limit: Annotated[
        int,
        Field(description="Max records to return (clamped to [1, 500]). Default 20."),
    ] = 20,
    days_back: Annotated[
        int,
        Field(
            description=(
                "How many days of trace files to scan (clamped to [1, 30] — the "
                "tracer's retention period). Default 7."
            )
        ),
    ] = 7,
) -> str:
    """Return recent chat-pipe agent traces (per-turn forensics).

    Reads ``~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl``, parses the
    most recent N records, returns them newest-first. Each record
    captures one completed agent turn: timing, model tier, tool call
    list with latencies + success/error states, prompt-hash (NOT
    prompt text — hashed for privacy), outcome.

    Use this for cross-session debugging when ``td_get_activity_log``'s
    in-RAM ring isn't enough (server was restarted, want yesterday's
    behavior, etc.).

    Args:
        limit: max records to return (default 20).
        days_back: how many days of trace files to look at (default 7,
            cap 30 — the tracer's retention period).

    Returns: JSON dict with ``records`` (list, newest-first), ``traces_dir``,
    ``files_scanned``, ``limit``, ``days_back``.
    """
    finish = _tr._start_tool(ctx, "td_get_traces")
    try:
        # Clamp inputs to safe ranges.
        safe_limit = max(1, min(int(limit), 500))
        safe_days = max(1, min(int(days_back), 30))

        traces_dir = DEFAULT_TRACES_DIR
        files = iter_jsonl_files(traces_dir, safe_days)
        records = read_last_n_jsonl(files, safe_limit)

        payload: dict[str, Any] = {
            "schema_version": 1,
            "traces_dir": str(traces_dir),
            "files_scanned": [str(p) for p in files],
            "limit": safe_limit,
            "days_back": safe_days,
            "record_count": len(records),
            "records": records,
        }
        return _tr._as_json_output(payload)
    except Exception as exc:
        _tr._record_tool_error(ctx, "td_get_traces")
        from td_mcp.errors import format_tool_error

        return format_tool_error(exc)
    finally:
        finish()
