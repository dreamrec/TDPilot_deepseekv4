"""Observability tools — v2.5.1.

Exposes the activity ring (``src/td_mcp/observability/activity_log.py``) via
``td_get_activity_log`` so the agent can self-inspect its tool-call history
within the current server session.

Side-effect-imported from ``tool_registry.py`` like every other ``tools_*``
submodule. See ``src/td_mcp/registry/__init__.py`` for the intentional-cycle
pattern.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context  # noqa: F401  — referenced via Annotated

# Intentional cycle — see registry/__init__.py.
from td_mcp import tool_registry as _tr  # noqa: E402
from td_mcp.observability import get_global_ring
from td_mcp.tool_registry import mcp  # noqa: E402


@mcp.tool(name="td_get_activity_log")
async def td_get_activity_log(
    ctx: Context,
    limit: int = 50,
    tool_filter: str | None = None,
    since_ts: float | None = None,
) -> str:
    """Return recent agent tool-call activity from the MCP server's
    200-entry ring buffer.

    Use this when you want to inspect what you've been doing this session.
    Especially useful for breaking out of suspected loops: if the same
    ``(tool_name, args_hash)`` appears many times in a row, you're stuck —
    switch strategy.

    Args:
        limit: most recent N records (default 50, cap 200 by ring size).
        tool_filter: exact-match tool name (e.g., "td_get_errors").
        since_ts: only records with ``ts >= since_ts`` (monotonic seconds).

    Returns: JSON dict with ``records`` list (each: ts, tool_name,
    args_hash, duration_ms, result_kind, optional error_msg).
    """
    finish = _tr._start_tool(ctx, "td_get_activity_log")
    try:
        ring = get_global_ring()
        records = ring.records(limit=limit, tool_filter=tool_filter, since_ts=since_ts)
        payload: dict[str, Any] = {
            "schema_version": 1,
            "ring_size": len(ring),
            "ring_capacity": ring.maxlen,
            "filters": {
                "limit": limit,
                "tool_filter": tool_filter,
                "since_ts": since_ts,
            },
            "records": [r.to_dict() for r in records],
        }
        return _tr._as_json_output(payload)
    except Exception as exc:
        _tr._record_tool_error(ctx, "td_get_activity_log")
        from td_mcp.errors import format_tool_error

        return format_tool_error(exc)
    finally:
        finish()
