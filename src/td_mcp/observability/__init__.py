"""Observability surface for TDPilot DPSK4 — v2.5.1.

Captures per-turn agent activity (tool dispatches + their results) into a
bounded ring buffer. Exposed via the ``td_get_activity_log`` MCP tool so the
agent can self-inspect ("have I been looping?").

This module is the **MCP-server-side** ring. The chat-pipe agent inside
``tdpilot_API.tox`` has its OWN parallel ring at
``td_component/tdpilot_api_activity_log.py`` — same shape, separate instance,
because the chat-pipe agent dispatches tools without going through the MCP
``_forward`` path (it runs entirely inside TouchDesigner).

See ``docs/plans/v2.5_IMPLEMENTATION_PLAN.md`` §2 for the v2.5.1 design.
"""

from __future__ import annotations

from td_mcp.observability.activity_log import (
    ActivityRecord,
    ActivityRing,
    args_hash,
    build_journal_hint,
    get_global_ring,
    record_activity,
    reset_global_ring,
)

__all__ = [
    "ActivityRecord",
    "ActivityRing",
    "args_hash",
    "build_journal_hint",
    "get_global_ring",
    "record_activity",
    "reset_global_ring",
]
