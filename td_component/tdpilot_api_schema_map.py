"""TDPilot API — name → handler routing table + body adapters.

Split off from tdpilot_api_schema.py during the 2026-05-04 audit. The
original file held 1500+ lines of TOOL_SCHEMAS plus this routing table
and the adapter helpers. Now:

  * tdpilot_api_schema_map.py  (this file): routing dict + adapters
  * tdpilot_api_schema_defs.py: TOOL_SCHEMAS list + supported_tool_names()
  * tdpilot_api_schema.py:     thin re-export shim so existing imports
                                ``from tdpilot_api_schema import TOOL_TO_HANDLER``
                                keep working without touching dispatcher /
                                runtime / extension call-sites.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Tool name → (handler_fn_name, body_adapter)
# ---------------------------------------------------------------------------
# body_adapter receives the tool_use.input dict and returns the dict the
# handler expects. Most handlers accept the input as-is, so the default
# adapter is the identity function.


def _id(d: dict) -> dict:
    """Identity adapter — most tool handlers accept the input dict as-is.
    Type matches ``Callable[[dict], dict]`` for the TOOL_TO_HANDLER mapping."""
    return d or {}


def _adapt_create_node(d: dict) -> dict:
    # The TD-side handler `handle_create_node` in mcp_webserver_callbacks.py
    # reads `body.get('parent_path')` and `body.get('node_type')` — confirmed
    # by reading the handler source. Schema exposes `op_type` for ergonomics
    # + accepts `node_type`/`operator_type`/`type` aliases since DeepSeek v4
    # frequently hallucinates these field names.
    out: dict[str, Any] = {
        "parent_path": d.get("parent_path") or d.get("parent"),
        "node_type": (d.get("op_type") or d.get("node_type") or d.get("type") or d.get("operator_type")),
    }
    # CRITICAL: only include ``name`` if the agent actually provided one.
    # The handler does ``parent.create(node_type, name)`` and if name is
    # Python None, TD literally stringifies it to "None" so the new op is
    # named "None" / "None1" / "None2" instead of being given the type's
    # default name (noise1 / level1 / etc.). Confirmed in the field —
    # the handler-side bug is shared with the dpsk4 variant but Claude
    # Code's stronger TD prompt makes it always provide a name. Our
    # DeepSeek-driven agent doesn't always, so we defend here.
    name = d.get("name")
    if name:
        out["name"] = name
    if "nodeX" in d or "node_x" in d:
        out["nodeX"] = d.get("nodeX", d.get("node_x"))
    if "nodeY" in d or "node_y" in d:
        out["nodeY"] = d.get("nodeY", d.get("node_y"))
    return out


def _adapt_get_content(d: dict) -> dict:
    return {"path": d.get("path")}


def _adapt_python_help(d: dict) -> dict:
    return {"target": d.get("target")}


def _adapt_connect(d: dict) -> dict:
    # Schema exposes from_path/to_path/from_index/to_index for ergonomics;
    # the TD-side handler expects source_*/target_*. Translate, accepting
    # either name on the input side as a courtesy.
    return {
        "source_path": d.get("from_path") or d.get("source_path"),
        "target_path": d.get("to_path") or d.get("target_path"),
        "source_index": d.get("from_index", d.get("source_index", 0)),
        "target_index": d.get("to_index", d.get("target_index", 0)),
    }


def _adapt_get_errors(d: dict) -> dict:
    # Schema's `recursive` -> handler's `recurse`. The handler also accepts
    # max_depth which we pass through if the model sets it.
    out: dict[str, Any] = {"path": d.get("path", "/")}
    if "recursive" in d or "recurse" in d:
        out["recurse"] = bool(d.get("recursive", d.get("recurse", True)))
    if "max_depth" in d:
        out["max_depth"] = d["max_depth"]
    return out


# Curated 66-tool standalone surface (Sprint 0–4). The full 103-tool
# CLI surface is generated separately by scripts/export_tool_schemas.py
# for cross-check; this dict is the source of truth for in-TD dispatch.
TOOL_TO_HANDLER: dict[str, tuple[str, Callable[[dict], dict]]] = {
    "td_get_info": ("handle_info", _id),
    "td_list_families": ("handle_list_families", _id),
    "td_get_nodes": ("handle_get_nodes", _id),
    "td_get_node_detail": ("handle_get_node_detail", _id),
    "td_get_params": ("handle_get_params", _id),
    "td_set_params": ("handle_set_params", _id),
    "td_create_node": ("handle_create_node", _adapt_create_node),
    "td_delete_node": ("handle_delete_node", _id),
    "td_connect_nodes": ("handle_connect_nodes", _adapt_connect),
    "td_disconnect": ("handle_disconnect_nodes", _id),
    "td_get_connections": ("handle_get_connections", _id),
    "td_get_errors": ("handle_get_errors", _adapt_get_errors),
    "td_get_content": ("handle_get_content", _adapt_get_content),
    "td_set_content": ("handle_set_content", _id),
    "td_copy_node": ("handle_copy_node", _id),
    "td_rename_node": ("handle_rename_node", _id),
    "td_exec_python": ("handle_exec_python", _id),
    "td_screenshot": ("handle_screenshot", _id),
    "td_chop_data": ("handle_chop_data", _id),
    "td_search_nodes": ("handle_search_nodes", _id),
    "td_python_help": ("handle_python_help", _adapt_python_help),
    "td_python_classes": ("handle_python_classes", _id),
    "td_timeline": ("handle_timeline", _id),
    "td_timeline_set": ("handle_timeline_set", _id),
    "td_cooking_info": ("handle_cooking_info", _id),
    "td_pulse_param": ("handle_pulse_param", _id),
    # The "easy 7" — handlers already exist in mcp_webserver_callbacks.py;
    # only the schema entries were missing. Audit 2026-05-04. Particularly
    # td_project_lifecycle (save/undo/redo) is critical for safety on
    # multi-step builds. Added to TOOL_TO_HANDLER alphabetically below
    # the curated MVP set so the diff is reviewable.
    "td_analyze_frame": ("handle_analyze_frame", _id),
    "td_custom_parameters": ("handle_custom_parameters", _id),
    "td_geometry_data": ("handle_geometry_data", _id),
    "td_pop_inspect": ("handle_pop_inspect", _id),
    "td_project_lifecycle": ("handle_project_lifecycle", _id),
    "td_subscribe": ("handle_monitor_subscribe", _id),
    "td_unsubscribe": ("handle_monitor_unsubscribe", _id),
    # ---- Memory tools (Sprint 2 — handlers in tdpilot_api_memory.py) ----
    "memory_save": ("handle_memory_save", _id),
    "memory_get": ("handle_memory_get", _id),
    "memory_list": ("handle_memory_list", _id),
    "memory_recall": ("handle_memory_recall", _id),
    "memory_delete": ("handle_memory_delete", _id),
    # ---- Knowledge tools (Sprint 2 — handlers in tdpilot_api_knowledge.py) ----
    "knowledge_search": ("handle_knowledge_search", _id),
    "knowledge_get": ("handle_knowledge_get", _id),
    "knowledge_list": ("handle_knowledge_list", _id),
    "knowledge_add": ("handle_knowledge_add", _id),
    # ---- Recipe tools (Sprint 3 — handlers in tdpilot_api_recipes.py) ----
    "recipe_save": ("handle_recipe_save", _id),
    "recipe_get": ("handle_recipe_get", _id),
    "recipe_list": ("handle_recipe_list", _id),
    "recipe_recall": ("handle_recipe_recall", _id),
    "recipe_replay": ("handle_recipe_replay", _id),
    # ---- Skills tools (Sprint 3.2 — handlers in tdpilot_api_skills.py) ----
    # 1.7.2 added skill_validate for surfacing frontmatter errors that
    # used to be silently swallowed.
    "skill_list": ("handle_skill_list", _id),
    "skill_get": ("handle_skill_get", _id),
    "skill_load": ("handle_skill_load", _id),
    "skill_validate": ("handle_skill_validate", _id),
    # ---- Snapshot + Patch tools (Sprint 3.3 — handlers in tdpilot_api_patches.py) ----
    "snapshot_save": ("handle_snapshot_save", _id),
    "snapshot_list": ("handle_snapshot_list", _id),
    # 2026-05-11 — scoped JSON manifest snapshots (Bug 19 fix). Save +
    # restore at scope-level without touching the agent COMP.
    "snapshot_save_scoped": ("handle_snapshot_save_scoped", _id),
    "snapshot_restore_scoped": ("handle_snapshot_restore_scoped", _id),
    "patch_begin": ("handle_patch_begin", _id),
    "patch_validate": ("handle_patch_validate", _id),
    "patch_commit": ("handle_patch_commit", _id),
    "patch_rollback": ("handle_patch_rollback", _id),
    # ---- User-tool management (Sprint 4.2 — handlers in tdpilot_api_user_tools.py) ----
    "tool_list_user": ("handle_tool_list_user", _id),
    "tool_validate": ("handle_tool_validate", _id),
    # ---- Subagent fan-out (Sprint 4.1 — handlers in tdpilot_api_subagents.py) ----
    "spawn_subagent": ("handle_spawn_subagent", _id),
    "subagent_status": ("handle_subagent_status", _id),
    "subagent_wait": ("handle_subagent_wait", _id),
    "subagent_cancel": ("handle_subagent_cancel", _id),
    "subagent_list": ("handle_subagent_list", _id),
    # ---- Macros (Sprint 4.4 — handlers in tdpilot_api_macros.py) ----
    "macro_list": ("handle_macro_list", _id),
    "macro_get": ("handle_macro_get", _id),
    "macro_run": ("handle_macro_run", _id),
    # ---- Memory advanced (export/import/favorite — handlers in tdpilot_api_memory.py) ----
    "memory_export": ("handle_memory_export", _id),
    "memory_import": ("handle_memory_import", _id),
    "memory_favorite": ("handle_memory_favorite", _id),
    # ---- Recipe validation (handler in tdpilot_api_recipes.py) ----
    "td_validate_recipe": ("handle_validate_recipe", _id),
    # ---- Official-docs lookup (handlers in tdpilot_api_official_docs.py) ----
    "td_search_official_docs": ("handle_search_official_docs", _id),
    "td_get_operator_doc": ("handle_get_operator_doc", _id),
    "td_get_param_help": ("handle_get_param_help", _id),
    "td_lookup_snippets": ("handle_lookup_snippets", _id),
    "td_lookup_palette_component": ("handle_lookup_palette_component", _id),
    "td_recommend_official_component": ("handle_recommend_official_component", _id),
    "td_find_official_example": ("handle_find_official_example", _id),
    "td_explain_better_way": ("handle_explain_better_way", _id),
    # ---- TD 2025 native introspection (handlers in tdpilot_api_td2025.py) ----
    "td_python_env_status": ("handle_python_env_status", _id),
    "td_threading_status": ("handle_threading_status", _id),
    "td_logger_status": ("handle_logger_status", _id),
    "td_tdresources_inspect": ("handle_tdresources_inspect", _id),
    "td_color_pipeline": ("handle_color_pipeline", _id),
    "td_component_standardize": ("handle_component_standardize", _id),
    "td_audit_project": ("handle_audit_project", _id),
    # ---- Server introspection (handlers in tdpilot_api_introspect.py) ----
    "td_get_server_metrics": ("handle_get_server_metrics", _id),
    "td_describe_surface": ("handle_describe_surface", _id),
    "td_get_capabilities": ("handle_get_capabilities", _id),
    # v2.4 / Phase C.6 — capability summary for UI discoverability.
    "td_get_capabilities_summary": ("handle_get_capabilities_summary", _id),
    # ---- Tool batch (Phase 2.1 — handler in tdpilot_api_batch.py) ----
    "tool_batch": ("handle_tool_batch", _id),
    # ---- Observability traces (Phase 4.1 — handler in tdpilot_api_tracing.py) ----
    "td_get_recent_traces": ("handle_get_recent_traces", _id),
    # ---- Auto-rollback internal handlers (v2.2.0 — handlers in
    # tdpilot_api_rollback.py). Registered here so the dispatcher can
    # resolve them, but NOT added to TOOL_SCHEMAS in tdpilot_api_schema_defs
    # — meaning the LLM never sees them as callable tools. Only the
    # AutoRollbackGuard invokes these, around each batch in Agent._loop.
    "auto_rollback_begin": ("handle_auto_rollback_begin", _id),
    "auto_rollback_end": ("handle_auto_rollback_end", _id),
}


# v2.2.0 — internal-only tool names. Registered in TOOL_TO_HANDLER so the
# dispatcher can route them, but DELIBERATELY absent from TOOL_SCHEMAS so
# the LLM never sees them as callable tools. Wrapper logic (e.g.
# AutoRollbackGuard) invokes them via the same dispatcher pipeline.
#
# The schema-vs-handler parity pin tests in test_tdpilot_api_batch.py and
# test_tdpilot_api_tracing.py exclude this set from their equality check
# so the parity invariant stays meaningful for everything else.
INTERNAL_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "auto_rollback_begin",
        "auto_rollback_end",
    }
)
