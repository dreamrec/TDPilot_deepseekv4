"""Cross-runtime schema parity — chat-pipe `TOOL_SCHEMAS` vs MCP `@mcp.tool` surface.

PR-#53 added bidirectional parity *within* ``td_component/`` (closes the
v2.5.1 alias-gap class on the chat-pipe side). This test adds the
**cross-runtime** direction so a new MCP tool added to
``src/td_mcp/registry/tools_*.py`` that should also be exposed to the
chat-pipe LLM can't silently ship as MCP-only — and so a new chat-pipe
schema entry can't silently ship without a matching MCP implementation.

The two surfaces have **different naming conventions** by design:

- Chat-pipe uses short names for internal-state tools: ``memory_save``,
  ``knowledge_get``, ``patch_begin``, ``recipe_save``, ``skill_load``,
  ``tool_batch``, ``snapshot_save``, ``spawn_subagent``, etc.
- MCP uses ``td_``-prefixed names for everything: ``td_memory_save``,
  ``td_knowledge_get``, etc.

So a pure set-equality parity test would have 41+ false positives. Instead
this is a **snapshot test** that freezes the current legitimate-asymmetry
baseline. Any drift from the snapshot fails CI and forces a code review
decision: either the new tool should bridge the gap, or its omission is
intentional and the snapshot moves with the change.

Why snapshot-style instead of forbidden/allowed lists: the asymmetry is
intentional product design (chat-pipe is a curated subset), so the test
should detect *change*, not *presence*. A frozen list makes intent
auditable in git history — every diff requires a reviewer to think.
"""

from __future__ import annotations

import asyncio

import pytest

# Chat-pipe surface (lives in td_component/).
from tdpilot_api_schema_defs import TOOL_SCHEMAS  # type: ignore[import-not-found]

# MCP server surface.
from td_mcp import server as _td_mcp_server

# ---------------------------------------------------------------------------
# Snapshot — the as-of-v2.5.4 state of the two surfaces' asymmetry.
#
# To update: run pytest, copy the actual diff into the constants below,
# and put a one-line code-review comment explaining the new
# inclusion/exclusion.
# ---------------------------------------------------------------------------

# Tools in chat-pipe TOOL_SCHEMAS but NOT in MCP's @mcp.tool registry.
# Most are chat-pipe-internal (operate on ~/.tdpilot-api/ stores) and
# legitimately have no MCP counterpart. ``td_get_recent_traces`` is the
# v2.5.1 alias artifact (the long name kept for backward compat with the
# pre-fix chat history; ``td_get_traces`` is the short canonical name).
CHAT_PIPE_ONLY_BASELINE: frozenset[str] = frozenset(
    {
        # chat-pipe-internal: knowledge corpus operations (~/.tdpilot-api/knowledge/)
        "knowledge_add",
        "knowledge_get",
        "knowledge_list",
        "knowledge_search",
        # chat-pipe-internal: macro definitions
        "macro_get",
        "macro_list",
        "macro_run",
        # chat-pipe-internal: memory store (~/.tdpilot-api/memory/)
        "memory_delete",
        "memory_export",
        "memory_favorite",
        "memory_get",
        "memory_import",
        "memory_list",
        "memory_recall",
        "memory_save",
        # chat-pipe-internal: patch sessions
        "patch_begin",
        "patch_commit",
        "patch_rollback",
        "patch_validate",
        # chat-pipe-internal: recipe store
        "recipe_get",
        "recipe_list",
        "recipe_recall",
        "recipe_replay",
        "recipe_save",
        # chat-pipe-internal: skill packs
        "skill_get",
        "skill_list",
        "skill_load",
        "skill_validate",
        # chat-pipe-internal: snapshot operations
        "snapshot_list",
        "snapshot_restore_scoped",
        "snapshot_save",
        "snapshot_save_scoped",
        # chat-pipe-internal: subagent orchestration
        "spawn_subagent",
        "subagent_cancel",
        "subagent_list",
        "subagent_status",
        "subagent_wait",
        # chat-pipe-internal: batch + tool registry
        "tool_batch",
        "tool_list_user",
        "tool_validate",
        # v2.5.1 alias artifact: chat-pipe keeps the long name for
        # back-compat (the LLM may see it in old chat history). MCP only
        # exposes the short name ``td_get_traces``.
        "td_get_recent_traces",
    }
)

# Tools in MCP's @mcp.tool registry but NOT in chat-pipe TOOL_SCHEMAS.
# Chat-pipe is intentionally a curated subset (95 tools vs MCP's 109+).
# Adding a new MCP tool that should ALSO be in chat-pipe must update both
# surfaces; if it's legitimately MCP-only, add it here with a one-line
# rationale comment to keep intent auditable in git history.
MCP_ONLY_BASELINE: frozenset[str] = frozenset(
    {
        # Visual capture/analysis — MCP-only, the chat-pipe uses
        # td_screenshot / td_analyze_frame instead.
        "td_capture_and_analyze",
        "td_capture_frame",
        # Update / release lifecycle (v2.5.0 + earlier) — MCP-side check
        # is the canonical implementation; chat-pipe v2.5.1.3 follow-up
        # is deferred per docs/plans/README.md.
        "td_check_for_updates",
        "td_get_build_compatibility",
        "td_get_release_delta",
        # Param-bounds + emergency stabilize — destructive ops kept MCP-side
        # only (chat-pipe approval gate handles equivalent flows).
        "td_clear_param_bounds",
        "td_emergency_stabilize",
        # Component-level introspection — MCP-only diagnostic surface.
        "td_component_notes",
        "td_describe_dynamics",
        "td_detect_instability",
        "td_diff_snapshots",
        "td_get_events",
        "td_get_focus",
        "td_get_hints",
        "td_get_state_vector",
        "td_get_timescale_state",
        # Macro authoring — MCP-side power tools.
        "td_create_macro",
        "td_get_macro_params",
        "td_list_macros",
        # POPX + knowledge MCP-side (chat-pipe has its own knowledge_* set).
        "td_get_popx_operator",
        "td_knowledge_get",
        "td_knowledge_list",
        "td_knowledge_recall",
        "td_knowledge_save",
        # Locations / snapshots MCP-side.
        "td_list_snapshots",
        "td_locations",
        # Memory MCP-side (chat-pipe has its own memory_* set).
        "td_memory_export",
        "td_memory_favorite",
        "td_memory_import",
        "td_memory_learn",
        "td_memory_list",
        "td_memory_preferences",
        "td_memory_promote",
        "td_memory_recall",
        "td_memory_replay",
        "td_memory_save",
        # MIDI device discovery — MCP-only (v2.4 addition).
        "td_midi_devices",
        # Observability MCP-side (chat-pipe v2.5.1.2 follow-up deferred).
        "td_get_activity_log",
        # Visual streaming MCP-side.
        "td_monitor_visual",
        "td_optimize_visual",
        # OCR sidecar (v2.5.2) MCP-only by design — too heavy for chat-pipe
        # restricted Python (subprocess + 400 MB paddleocr model).
        "td_ocr_image",
        # Patch session MCP-side (chat-pipe uses patch_* short names).
        "td_patch_apply",
        "td_patch_plan",
        "td_patch_preview",
        "td_patch_validate",
        "td_patch_variations",
        "td_plan_patch",
        "td_preflight_patch",
        # POPX docs search (MCP-side; chat-pipe-internal alternative uses knowledge_search).
        "td_search_popx_docs",
        # Param-bounds + snapshot ops MCP-side (chat-pipe uses snapshot_save_scoped etc).
        "td_set_param_bounds",
        "td_restore_snapshot",
        "td_snapshot_scene",
        # Streaming TOPs MCP-side only.
        "td_stop_monitor_visual",
        "td_stop_stream_top",
        "td_stream_top",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chat_pipe_tool_names() -> set[str]:
    return {s["name"] for s in TOOL_SCHEMAS if s.get("name")}


def _mcp_tool_names() -> set[str]:
    tools = asyncio.run(_td_mcp_server.mcp.list_tools())
    return {t.name for t in tools}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCrossRuntimeSchemaParity:
    """The v2.5.1-class regression catch — but cross-runtime.

    Within ``td_component/``, ``tests/test_chat_pipe_surface_parity.py``
    already pins ``TOOL_SCHEMAS ↔ TOOL_TO_HANDLER``. This file adds the
    direction ``td_component/`` ↔ ``src/td_mcp/`` so a new MCP tool
    can't silently ship without a chat-pipe equivalent, and vice versa.
    """

    def test_chat_pipe_only_set_matches_baseline(self) -> None:
        """Chat-pipe TOOL_SCHEMAS entries with no MCP counterpart must
        match ``CHAT_PIPE_ONLY_BASELINE``. Drift = intent change → review.
        """
        chat = _chat_pipe_tool_names()
        mcp = _mcp_tool_names()
        actual = chat - mcp
        new_in_chat = actual - CHAT_PIPE_ONLY_BASELINE
        removed_from_chat = CHAT_PIPE_ONLY_BASELINE - actual
        msg_parts = []
        if new_in_chat:
            msg_parts.append(
                "New chat-pipe-only tools (no MCP counterpart): "
                f"{sorted(new_in_chat)}.\n"
                "Add a matching @mcp.tool in src/td_mcp/registry/tools_*.py, "
                "OR add to CHAT_PIPE_ONLY_BASELINE with a one-line rationale."
            )
        if removed_from_chat:
            msg_parts.append(
                "Tools removed from chat-pipe (no longer in TOOL_SCHEMAS): "
                f"{sorted(removed_from_chat)}.\n"
                "Remove them from CHAT_PIPE_ONLY_BASELINE to update the snapshot."
            )
        assert not msg_parts, "\n\n".join(msg_parts)

    def test_mcp_only_set_matches_baseline(self) -> None:
        """MCP-side ``@mcp.tool`` entries with no chat-pipe counterpart
        must match ``MCP_ONLY_BASELINE``. Drift = intent change → review.
        Catches the case where a new MCP tool is added without
        considering whether it should also reach the chat-pipe LLM.
        """
        chat = _chat_pipe_tool_names()
        mcp = _mcp_tool_names()
        actual = mcp - chat
        new_in_mcp = actual - MCP_ONLY_BASELINE
        removed_from_mcp = MCP_ONLY_BASELINE - actual
        msg_parts = []
        if new_in_mcp:
            msg_parts.append(
                "New MCP-only tools (not exposed to chat-pipe): "
                f"{sorted(new_in_mcp)}.\n"
                "If the tool should reach the chat-pipe LLM, add a TOOL_SCHEMAS "
                "+ TOOL_TO_HANDLER entry in td_component/. Otherwise add to "
                "MCP_ONLY_BASELINE with a one-line rationale."
            )
        if removed_from_mcp:
            msg_parts.append(
                "MCP-only tools removed from @mcp.tool surface: "
                f"{sorted(removed_from_mcp)}.\n"
                "Remove from MCP_ONLY_BASELINE to update the snapshot."
            )
        assert not msg_parts, "\n\n".join(msg_parts)

    def test_no_silent_name_collisions_with_different_handlers(self) -> None:
        """Belt-and-braces: if a tool name appears on BOTH sides, it
        should describe the same underlying capability (i.e., not be
        a name-clash with divergent semantics). We can't check semantics
        directly, but we CAN assert the intersection set is the
        ``td_``-prefixed tools the user expects — anything else is a
        smell."""
        chat = _chat_pipe_tool_names()
        mcp = _mcp_tool_names()
        shared = chat & mcp
        non_td_shared = {n for n in shared if not n.startswith("td_")}
        assert not non_td_shared, (
            f"Non-td_-prefixed names appearing on BOTH surfaces: {sorted(non_td_shared)}. "
            f"By convention chat-pipe-internal tools use short names (without td_ prefix) "
            f"and MCP uses td_-prefixed names. A shared non-prefixed name suggests a "
            f"naming collision worth reviewing."
        )

    def test_surfaces_match_total_counts(self) -> None:
        """Smoke check: confirm the surface sizes are within ~10% of
        the audit-time baseline (chat-pipe 95, MCP 109+ as of v2.5.4)."""
        chat = _chat_pipe_tool_names()
        mcp = _mcp_tool_names()
        # Loose bounds — the test should fail loudly if the surface
        # accidentally HALVES, not if it grows by one or two.
        assert 85 <= len(chat) <= 120, f"chat-pipe TOOL_SCHEMAS count {len(chat)} outside [85, 120]"
        assert 100 <= len(mcp) <= 140, f"MCP @mcp.tool count {len(mcp)} outside [100, 140]"
