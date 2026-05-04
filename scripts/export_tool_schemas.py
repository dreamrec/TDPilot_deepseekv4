#!/usr/bin/env python3
"""
Export all FastMCP tool schemas to a JSON file the standalone .tox can bake in.

Runs from the CLI venv (NOT inside TD):
    uv run python scripts/export_tool_schemas.py \
        --out td_component/tdpilot_api_tool_schemas.json

The output JSON shape:
    {
      "version": "1.6.11",
      "exported_at": "2026-05-04T12:34:56Z",
      "tools": [
        {"name": "td_get_info",
         "description": "...",
         "input_schema": {...JSON Schema...},
         "endpoint": "info"},     # if discovered from _forward(...) source
        ...
      ]
    }

The endpoint field is best-effort: parsed from the registry source by
scanning for `_forward(ctx, "<tool_name>", "<endpoint>")`. Tools that
don't call _forward (memory/knowledge/patch — pure CLI-side work) get
endpoint=None and are skipped by the standalone dispatcher until ported
to in-process equivalents.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make src/ importable regardless of where this is run from.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _discover_endpoints() -> dict[str, str]:
    """Scan registry/tools_*.py and resolve {tool_name: endpoint_path}.

    Two forwarding patterns are recognized:

      a) Wrapper helper:
           _forward(ctx, "<tool_name>", "<endpoint>", ...)
         The second positional arg is the tool name and the third is the
         endpoint path. Tool name is taken from the call.

      b) Direct client call inside a @mcp.tool function body:
           _get_client(ctx).request("<endpoint>", body)
           _tr._get_client(ctx).request("<endpoint>", body)
         The endpoint is the first arg of .request(); the tool name is
         taken from the enclosing function's @mcp.tool(name=...) decorator
         (or the function name if no name= kwarg).

    Tools that do neither are pure CLI-side and absent from the result.
    """
    import ast

    out: dict[str, str] = {}
    registry_dir = REPO_ROOT / "src" / "td_mcp" / "registry"
    if not registry_dir.is_dir():
        return out

    def _decorator_tool_name(func: ast.AsyncFunctionDef | ast.FunctionDef) -> str | None:
        for dec in func.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            # Match @mcp.tool(...) or @<anything>.tool(...).
            target = dec.func
            attr = getattr(target, "attr", None)
            if attr != "tool":
                continue
            for kw in dec.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
            return func.name
        return None

    def _scan_tree(tree: ast.AST) -> None:
        # Pattern (a): _forward calls anywhere — the tool name is in the call.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fname = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if fname == "_forward" and len(node.args) >= 3:
                    name_arg, endpoint_arg = node.args[1], node.args[2]
                    if (
                        isinstance(name_arg, ast.Constant)
                        and isinstance(endpoint_arg, ast.Constant)
                        and isinstance(name_arg.value, str)
                        and isinstance(endpoint_arg.value, str)
                    ):
                        out.setdefault(name_arg.value, endpoint_arg.value)

        # Pattern (b): direct .request() calls inside @mcp.tool functions.
        for func in ast.walk(tree):
            if not isinstance(func, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            tool_name = _decorator_tool_name(func)
            if tool_name is None or tool_name in out:
                continue
            for sub in ast.walk(func):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "request"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                ):
                    out.setdefault(tool_name, sub.args[0].value)
                    break

    for path in registry_dir.glob("tools_*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        _scan_tree(tree)

    return out


def _extract_schemas() -> list[dict]:
    """Import the FastMCP app and pull every tool's name/description/input_schema."""
    from td_mcp import __version__ as _v  # noqa: F401 — import sanity check
    from td_mcp.tool_registry import mcp

    tool_mgr = getattr(mcp, "_tool_manager", None)
    if tool_mgr is None:
        raise RuntimeError("FastMCP _tool_manager not present — API changed?")

    out: list[dict] = []
    for tool in tool_mgr.list_tools():
        # FastMCP Tool object exposes .name, .description, .parameters
        # (parameters is the JSON Schema for inputs).
        schema = getattr(tool, "parameters", None) or getattr(tool, "input_schema", None) or {}
        out.append(
            {
                "name": getattr(tool, "name", ""),
                "description": (getattr(tool, "description", "") or "").strip(),
                "input_schema": schema,
            }
        )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out",
        type=Path,
        default=REPO_ROOT / "td_component" / "tdpilot_api_tool_schemas.json",
        help="Output path. Defaults to td_component/tdpilot_api_tool_schemas.json.",
    )
    args = p.parse_args()

    try:
        schemas = _extract_schemas()
    except Exception as exc:  # noqa: BLE001
        print(f"[export_tool_schemas] FAILED to load FastMCP tools: {exc}", file=sys.stderr)
        return 2

    endpoints = _discover_endpoints()
    for s in schemas:
        s["endpoint"] = endpoints.get(s["name"])

    from td_mcp import __version__ as version

    payload = {
        "version": version,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool_count": len(schemas),
        "td_dispatchable_count": sum(1 for s in schemas if s["endpoint"]),
        "tools": schemas,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(
        f"[export_tool_schemas] wrote {len(schemas)} tools "
        f"({payload['td_dispatchable_count']} TD-dispatchable) to {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
