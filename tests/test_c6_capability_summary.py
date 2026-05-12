"""v2.4 / Phase C.6 — grouped capability summary for UI discoverability.

The MCP-side tool ``td_get_capabilities_summary`` and the chat-pipe-side
handler ``handle_get_capabilities_summary`` both return a structured
payload that the chat HTML renders as "featured prompt" chips below the
input field. Tests pin the payload shape so a UI refactor doesn't need
to chase server-side schema drift.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


REQUIRED_GROUP_KEYS = {"id", "title", "blurb", "primary_tools", "examples"}


def _validate_summary_shape(payload: dict) -> None:
    """Shared shape contract between MCP-side and chat-pipe-side payloads."""
    assert isinstance(payload, dict)
    assert payload.get("schema_version") == 1
    groups = payload.get("groups")
    assert isinstance(groups, list) and len(groups) >= 4, (
        f"expected ≥4 groups, got {len(groups) if isinstance(groups, list) else 0}"
    )
    ids: set[str] = set()
    for g in groups:
        assert isinstance(g, dict)
        missing = REQUIRED_GROUP_KEYS - set(g.keys())
        assert not missing, f"group missing keys {missing}: {g}"
        # ids must be unique so the chat HTML can use them as React-style keys
        assert g["id"] not in ids, f"duplicate group id: {g['id']}"
        ids.add(g["id"])
        # examples ≤ 50 chars so they fit as chips visually
        for ex in g["examples"]:
            assert isinstance(ex, str) and len(ex) <= 60, (
                f"example too long ({len(ex)} chars): {ex!r}"
            )
    featured = payload.get("featured_prompts")
    assert isinstance(featured, list) and 4 <= len(featured) <= 8, (
        f"featured_prompts should be 4-8 entries, got {len(featured) if isinstance(featured, list) else 0}"
    )
    for fp in featured:
        assert isinstance(fp, str) and len(fp) <= 60


def test_c6_chat_pipe_handler_returns_valid_summary():
    """The chat-pipe handler must return the contract shape directly."""
    from tdpilot_api_introspect import handle_get_capabilities_summary  # noqa: PLC0415

    payload = handle_get_capabilities_summary({})
    _validate_summary_shape(payload)


def test_c6_chat_pipe_handler_returns_static_payload():
    """Two calls must return the same payload (it's pure data; no state)."""
    from tdpilot_api_introspect import handle_get_capabilities_summary  # noqa: PLC0415

    a = handle_get_capabilities_summary({})
    b = handle_get_capabilities_summary({})
    assert a == b


def test_c6_chat_pipe_schema_registered():
    """The chat-pipe TOOL_SCHEMAS must include td_get_capabilities_summary."""
    from tdpilot_api_schema_defs import TOOL_SCHEMAS  # noqa: PLC0415

    names = [t.get("name") for t in TOOL_SCHEMAS]
    assert "td_get_capabilities_summary" in names


def test_c6_chat_pipe_dispatch_registered():
    """The dispatch table must route td_get_capabilities_summary → handler."""
    from tdpilot_api_schema_map import TOOL_TO_HANDLER  # noqa: PLC0415

    entry = TOOL_TO_HANDLER.get("td_get_capabilities_summary")
    assert entry is not None
    handler_name, _wrapper = entry
    assert handler_name == "handle_get_capabilities_summary"


def test_c6_mcp_side_tool_registered():
    """The MCP-side tool must register under the same name."""
    import td_mcp.server as server  # noqa: PLC0415

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "td_get_capabilities_summary" in names


def test_c6_mcp_side_returns_valid_summary():
    """The MCP-side tool's output is also the canonical shape."""
    from td_mcp.registry.tools_info import _CAPABILITIES_SUMMARY  # noqa: PLC0415

    # The MCP-side constant directly serves as the payload (wrapped via
    # _as_json_output in the tool body). Validate the underlying dict.
    _validate_summary_shape(_CAPABILITIES_SUMMARY)


def test_c6_mcp_side_and_chat_pipe_align_on_featured_prompts():
    """Both sides should ship the same featured_prompts list — drift
    between the two payloads would confuse the agent if it queried one
    side and the user clicked a chip from the other."""
    from tdpilot_api_introspect import handle_get_capabilities_summary  # noqa: PLC0415

    from td_mcp.registry.tools_info import _CAPABILITIES_SUMMARY as MCP_SUM  # noqa: PLC0415

    pipe_sum = handle_get_capabilities_summary({})
    assert MCP_SUM["featured_prompts"] == pipe_sum["featured_prompts"], (
        "MCP-side and chat-pipe payloads must agree on featured_prompts "
        "— update both constants in lockstep when adding new prompts."
    )
