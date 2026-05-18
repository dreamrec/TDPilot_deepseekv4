"""Lifecycle tools — v2.5.7+.

Read-only surface for the v2.5.7 update-check. Full auto-apply
(``td_self_update`` per the v2.7 plan) will land here as a sibling tool
once the snapshot-and-swap machinery is in place.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context

# Intentional cycle — see registry/__init__.py.
from td_mcp import tool_registry as _tr  # noqa: E402
from td_mcp.lifecycle import check_for_updates
from td_mcp.tool_registry import mcp  # noqa: E402


@mcp.tool(name="td_check_for_updates")
async def td_check_for_updates(ctx: Context) -> str:
    """Check GitHub Releases + ``.tox`` source-hash freshness.

    Returns:
        JSON dict with three top-level fields:

        * ``server``: ``{current, latest, has_update, comparison, url}``
          or ``{current, check_failed: True, reason}`` on network error.
        * ``tox``: per-``.tox`` dict with ``hash_matches``,
          ``rebuild_needed``, and ``reason`` (None when matched).
        * ``advice``: human-readable next-step string for the agent to
          surface to the user.

    Cached for 1 hour. Safe to poll cheaply. Auto-apply lives in
    v2.7's ``td_self_update``; this is the read-only sibling.

    Failure mode is graceful: a network error doesn't raise — the
    server field reports ``check_failed: True`` and tox freshness is
    still computed locally.
    """
    finish = _tr._start_tool(ctx, "td_check_for_updates")
    try:
        result = check_for_updates()
        return _tr._as_json_output(result.to_dict())
    except Exception as exc:
        _tr._record_tool_error(ctx, "td_check_for_updates")
        from td_mcp.errors import format_tool_error

        return format_tool_error(exc)
    finally:
        finish()
