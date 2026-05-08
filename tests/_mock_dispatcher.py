"""Stub TD dispatcher used by both the capture script and the
mock-driven eval tests.

Real TouchDesigner is not in the loop for fixture replay — the
captured DeepSeek responses are deterministic, so as long as the
dispatcher returns shape-realistic results in deterministic order
the agent's tool-use loop unfolds identically each run.

The shapes mirror what the real TD MCP handlers return so the model
sees consistent schemas; field values are project-agnostic enough
to match any user's setup at capture time.
"""

from __future__ import annotations

from typing import Any

STUB_TD_PROJECT_INFO = {
    "name": "/project1",
    "fps": 60.0,
    "build": "2025.32820",
    "platform": "darwin",
    "absolute_time": 12345.6,
}

STUB_TD_NODE_LIST = {
    "path": "/project1",
    "children": [
        {"name": "moviefilein1", "type": "moviefilein", "family": "TOP"},
        {"name": "constant1", "type": "constant", "family": "CHOP"},
        {"name": "noise1", "type": "noise", "family": "TOP"},
    ],
}

STUB_TD_INFO_CAPABILITIES = {
    "build": "2025.32820",
    "features": [
        "trace_pop",
        "triangulate_pop",
        "dmx_pop",
        "layer_mix_top",
        "rtx_video_top",
        "st2110",
    ],
    "version_tier": "current",
}


def stub_dispatcher(name: str, args: dict) -> Any:
    """Realistic-shaped TD tool results for capture + replay sessions.

    Add a case here when a new eval scenario invokes a tool that
    isn't yet covered. The default branch returns a structured
    "no canned response" warning so the model can keep moving and
    the test surfaces the gap clearly.
    """
    if name == "td_get_info":
        return STUB_TD_PROJECT_INFO
    if name == "td_get_nodes":
        path = args.get("path") or "/project1"
        return {**STUB_TD_NODE_LIST, "path": path}
    if name == "td_get_capabilities":
        return STUB_TD_INFO_CAPABILITIES
    if name == "td_get_errors":
        return {"errors": [], "path": args.get("path") or "/project1"}
    if name == "td_create_node":
        op_type = args.get("op_type") or args.get("type") or "noise"
        op_name = args.get("name") or "node1"
        parent = args.get("parent_path") or "/project1"
        if "fakeNonexistent" in op_type:
            # v1.10.0+: stamp `_tool_error: True` to flag failures
            # (the legacy `{"error": ...}` heuristic is deprecated and
            # removed in v2.0). External user-provided dispatchers
            # should follow the same convention.
            return {
                "_tool_error": True,
                "error": f"Unknown operator type: {op_type}",
                "recovery_hint": (
                    "td_list_families to enumerate available operator types, "
                    "or td_search_official_docs for fuzzy matches."
                ),
            }
        return {
            "path": f"{parent}/{op_name}",
            "type": op_type,
            "created": True,
        }
    if name == "td_delete_node":
        return {"deleted": True, "path": args.get("path", "")}
    if name == "td_search_official_docs":
        return {
            "results": [
                {
                    "name": "Noise TOP",
                    "url": "https://docs.derivative.ca/Noise_TOP",
                    "snippet": "Noise TOP generates 2D noise...",
                    "trust_tier": "official",
                    "corpus": "derivative",
                }
            ]
        }
    if name == "knowledge_search":
        query = args.get("query", "")
        return {
            "results": [
                {
                    "name": "noise_top_basics",
                    "snippet": f"Knowledge entry matching '{query}'",
                    "trust_tier": "bundled",
                    "corpus": "derivative",
                }
            ]
        }
    if name == "memory_save":
        return {
            "saved": True,
            "name": args.get("name", "untitled"),
            "type": args.get("type", "feedback"),
        }
    if name in ("memory_get", "memory_recall"):
        return {
            "name": args.get("name", "eval_phase42_marker"),
            "content": "hello phase 4.2",
            "type": "feedback",
        }
    if name == "recipe_save":
        return {
            "saved": True,
            "name": args.get("name", "untitled_recipe"),
        }
    if name == "td_validate_recipe":
        replay = args.get("replay") or []
        invalid_tools = []
        for step in replay:
            tool_name = step.get("tool", "") if isinstance(step, dict) else ""
            if "fake" in tool_name or "does_not_exist" in tool_name:
                invalid_tools.append(tool_name)
        if invalid_tools:
            return {
                "valid": False,
                "errors": [f"Unknown tool: {t}" for t in invalid_tools],
            }
        return {"valid": True, "errors": []}
    if name == "tool_batch":
        sub = args.get("calls", [])
        results = []
        for call in sub:
            sub_name = call.get("tool") if isinstance(call, dict) else None
            sub_args = call.get("args") if isinstance(call, dict) else None
            if not sub_name:
                results.append({"error": "missing tool name"})
                continue
            results.append(stub_dispatcher(sub_name, sub_args or {}))
        return {"results": results}
    return {
        "warning": f"Stub dispatcher has no canned response for {name}",
        "args": args,
    }


def default_tools_for_capture() -> list[dict]:
    """Tool schemas the capture/replay agent needs visibility into.

    Real production loads ~103 tools; this list only covers the ones
    the eval scenarios touch. Grow it when a new scenario joins the
    suite. Keep schemas minimal — the agent doesn't validate against
    them, the schemas just shape DeepSeek's tool selection.
    """
    return [
        {
            "name": "td_get_info",
            "description": "Get TouchDesigner project info (FPS, build, platform).",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "td_get_nodes",
            "description": "List operators at the given path.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "td_get_capabilities",
            "description": "Report TD build features.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "td_get_errors",
            "description": "Validate a node — return any errors at the path.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "td_create_node",
            "description": "Create an operator at a parent path.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "parent_path": {"type": "string"},
                    "op_type": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["parent_path", "op_type", "name"],
            },
        },
        {
            "name": "td_delete_node",
            "description": "Delete an operator at the given path.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "td_search_official_docs",
            "description": "Search official Derivative docs.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "knowledge_search",
            "description": "BM25 search over the knowledge corpus.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_save",
            "description": "Persist a named memory entry.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string"},
                    "description": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["name", "type", "content"],
            },
        },
        {
            "name": "memory_get",
            "description": "Retrieve a memory entry by name.",
            "input_schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "recipe_save",
            "description": "Save a multi-step build sequence as a replayable recipe.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "replay": {"type": "array"},
                },
                "required": ["name", "replay"],
            },
        },
        {
            "name": "td_validate_recipe",
            "description": "Validate a recipe's replay sequence.",
            "input_schema": {
                "type": "object",
                "properties": {"replay": {"type": "array"}},
                "required": ["replay"],
            },
        },
        {
            "name": "tool_batch",
            "description": "Call multiple tools in parallel.",
            "input_schema": {
                "type": "object",
                "properties": {"calls": {"type": "array"}},
                "required": ["calls"],
            },
        },
    ]
