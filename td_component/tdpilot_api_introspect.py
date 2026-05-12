"""TDPilot API — server-side introspection (Tier 2 port from CLI).

Three read-only tools that report on the agent runtime itself rather
than TouchDesigner. Useful when the agent needs to answer "what can I
do?" or when debugging "why is this slow?".

Tools:
  td_get_server_metrics    process metrics — uptime, memory, message
                           count, tool dispatch count
  td_describe_surface      compact list of all available tools (built-in
                           + user-pluggable) with their categories
  td_get_capabilities      feature flags — which Sprint 4 features are
                           wired, which model tier is active, exec mode

All execute in microseconds — pure dict construction.
"""

from __future__ import annotations

import os
import platform
import time
from typing import Any

# Module-load timestamp — close enough to "TD-load timestamp" for the
# uptime calculation. Resets when the module reloads (which happens on
# Reload Config).
_LOAD_TIME = time.time()


def _runtime_handle() -> Any | None:
    """Reach the AgentRuntime via the TD COMP, or None outside TD."""
    try:
        comp = parent()  # type: ignore[name-defined]
    except NameError:
        return None
    if comp is None:
        return None
    ext_dat = comp.op("tdpilot_api_extension")
    if ext_dat is None:
        return None
    try:
        ext = ext_dat.module.get_extension(comp)
    except Exception:
        return None
    return getattr(ext, "_runtime", None) if ext is not None else None


def handle_get_server_metrics(body: dict) -> dict:
    """Return process-level metrics for the standalone agent.

    All fields are best-effort; missing ones are omitted (not errored)
    so the agent still gets a usable snapshot when running outside TD.
    """
    out: dict[str, Any] = {
        "ok": True,
        "uptime_seconds": round(time.time() - _LOAD_TIME, 2),
        "platform": platform.platform(),
        "pid": os.getpid(),
    }

    # Optional: memory + cpu via psutil if installed in TD's bundled
    # Python (most TD installs have it).
    try:
        import psutil  # type: ignore[import-not-found]

        proc = psutil.Process(os.getpid())
        with proc.oneshot():
            mem = proc.memory_info()
            out["rss_mb"] = round(mem.rss / 1024 / 1024, 1)
            out["cpu_percent"] = round(proc.cpu_percent(interval=None), 1)
            out["thread_count"] = proc.num_threads()
    except Exception:
        # psutil missing or restricted exec mode — skip silently.
        pass

    # Runtime-level counters when available.
    rt = _runtime_handle()
    if rt is not None:
        for attr in ("turn_count", "tool_call_count", "active_subagents", "queue_depth"):
            val = getattr(rt, attr, None)
            if val is not None:
                out[attr] = val

    return out


def handle_describe_surface(body: dict) -> dict:
    """Return a compact view of every tool the agent currently has access
    to — built-in + user-pluggable. Categorises by name prefix so the
    response stays organised.
    """
    from tdpilot_api_schema_defs import TOOL_SCHEMAS  # type: ignore[import-not-found]
    from tdpilot_api_schema_map import TOOL_TO_HANDLER  # type: ignore[import-not-found]

    # User-pluggable tools register via extra_mappings on the dispatcher,
    # NOT in TOOL_TO_HANDLER. Fish them out of the user_tools registry.
    user_tools: list[dict] = []
    try:
        from tdpilot_api_user_tools import _LOADED  # type: ignore[import-not-found]

        user_tools = [
            {"name": e.get("name"), "description": e.get("description", "")} for e in _LOADED if e.get("ok")
        ]
    except Exception:
        pass

    # Categorise built-ins by name prefix.
    categories: dict[str, list[str]] = {}
    for schema in TOOL_SCHEMAS:
        name = schema.get("name", "")
        if name.startswith("td_"):
            # Sub-categorise by second token: td_search_official_docs ->
            # search_official, td_get_nodes -> get, etc. Cheap heuristic.
            parts = name.split("_", 2)
            cat = "td_" + (parts[1] if len(parts) > 1 else "misc")
        else:
            cat = name.split("_", 1)[0]
        categories.setdefault(cat, []).append(name)

    return {
        "ok": True,
        "builtin_count": len(TOOL_SCHEMAS),
        "user_tool_count": len(user_tools),
        "total_count": len(TOOL_SCHEMAS) + len(user_tools),
        "categories": {cat: sorted(names) for cat, names in sorted(categories.items())},
        "user_tools": user_tools,
        "handler_table_consistent": len(TOOL_SCHEMAS)
        == len({s.get("name") for s in TOOL_SCHEMAS} & set(TOOL_TO_HANDLER.keys())),
    }


def handle_get_capabilities(body: dict) -> dict:
    """Report which features are wired in this build of the standalone.

    Useful for:
      - clients deciding whether to expose an "advanced" UI
      - the agent itself sanity-checking that a feature is present before
        attempting it (avoids "tool not found" round trips)
    """
    caps: dict[str, Any] = {
        "ok": True,
        "variant": "standalone",
        "exec_mode": os.environ.get("TD_MCP_EXEC_MODE", "full"),
    }

    # Feature presence — derived from module-importability.
    feature_modules = {
        "memory": "tdpilot_api_memory",
        "knowledge": "tdpilot_api_knowledge",
        "recipes": "tdpilot_api_recipes",
        "skills": "tdpilot_api_skills",
        "patches": "tdpilot_api_patches",
        "user_tools": "tdpilot_api_user_tools",
        "subagents": "tdpilot_api_subagents",
        "macros": "tdpilot_api_macros",
        "official_docs": "tdpilot_api_official_docs",
        "td2025_native": "tdpilot_api_td2025",
        "introspect": "tdpilot_api_introspect",
        "bm25": "tdpilot_api_bm25",
    }
    feature_status: dict[str, bool] = {}
    for name, mod in feature_modules.items():
        try:
            __import__(mod)
            feature_status[name] = True
        except ImportError:
            feature_status[name] = False

    # Phase 1.1 — SQLite/FTS5 corpus support. Reports True when the
    # stdlib's sqlite3 module ships FTS5 (it does on every officially
    # supported Python build, but the runtime checks at import time so
    # a stripped-down embed without FTS5 cleanly degrades).
    try:
        import sqlite3 as _sqlite

        _conn = _sqlite.connect(":memory:")
        try:
            _conn.execute("CREATE VIRTUAL TABLE _fts_probe USING fts5(x)")
            feature_status["sqlite_fts"] = True
        except _sqlite.OperationalError:
            feature_status["sqlite_fts"] = False
        finally:
            _conn.close()
    except Exception:
        feature_status["sqlite_fts"] = False

    caps["features"] = feature_status

    # Runtime-level state.
    rt = _runtime_handle()
    if rt is not None:
        caps["model"] = getattr(rt, "_active_model", None) or getattr(rt, "model", None)
        caps["model_tier"] = getattr(rt, "model_tier", None)
        caps["max_tokens"] = getattr(rt, "max_tokens", None)

    # Bundled corpus + skill counts (cheap reads).
    try:
        from tdpilot_api_knowledge import _all_entries  # type: ignore[import-not-found]

        caps["bundled_knowledge_entries"] = len(_all_entries())
    except Exception:
        pass

    try:
        from tdpilot_api_skills import _bundled_entries as _bundled_skills  # type: ignore[import-not-found]

        caps["bundled_skills"] = len(_bundled_skills())
    except Exception:
        pass

    # Phase 5.2 — first-run state. True when the user hasn't pasted
    # an API key AND has no memories AND no external brains. The
    # chat UI uses this to render the 3-step welcome wizard.
    caps["first_run"] = firstrun_status()

    return caps


# v2.4 / Phase C.6 — capability summary for UI discoverability.
# Static data, allocated once on module load. Mirrors the MCP-side
# constant in src/td_mcp/registry/tools_info.py — keep them in sync
# when you add a new tool family. Each group's `examples` are <= 50
# chars so they fit as chips below the chat input.
_CAPABILITIES_SUMMARY: dict[str, Any] = {
    "schema_version": 1,
    "groups": [
        {
            "id": "build",
            "title": "Build",
            "blurb": "Create operators, wire networks, scaffold recipes.",
            "primary_tools": [
                "td_create_node",
                "td_connect_nodes",
                "td_set_params",
                "patch_apply",
            ],
            "examples": [
                "Build a kaleidoscope feedback loop",
                "Add a Constant TOP wired to a Composite TOP",
                "Replay my 'audio-react' recipe",
            ],
        },
        {
            "id": "diagnose",
            "title": "Diagnose",
            "blurb": "Find errors, profile cooks, detect drift.",
            "primary_tools": [
                "td_audit_project",
                "td_get_errors",
                "td_cooking_info",
                "td_detect_instability",
            ],
            "examples": [
                "Audit this project for problems",
                "Why is the framerate dropping?",
                "Show recent errors",
            ],
        },
        {
            "id": "inspect",
            "title": "Inspect",
            "blurb": "Survey nodes, describe surface, screenshot.",
            "primary_tools": [
                "td_get_nodes",
                "td_describe_surface",
                "td_screenshot",
                "td_get_node_detail",
            ],
            "examples": [
                "List the top 20 nodes by cook time",
                "Screenshot the network",
                "Describe this component",
            ],
        },
        {
            "id": "remember",
            "title": "Remember",
            "blurb": "Save techniques and recall them by topic.",
            "primary_tools": [
                "memory_save",
                "memory_recall",
                "memory_list",
                "knowledge_save",
            ],
            "examples": [
                "Remember this as 'soft-glow'",
                "What memories about feedback?",
                "List my memories",
            ],
        },
        {
            "id": "recipes",
            "title": "Recipes",
            "blurb": "Replay saved techniques or save a new one.",
            "primary_tools": [
                "recipe_replay",
                "recipe_save",
                "recipe_recall",
            ],
            "examples": [
                "Replay 'audio-react' here",
                "Save this network as a recipe",
                "Show favorite recipes",
            ],
        },
        {
            "id": "learn",
            "title": "Learn / Lookup",
            "blurb": "Search docs, find examples, get operator help.",
            "primary_tools": [
                "knowledge_search",
                "td_find_official_example",
                "td_get_operator_doc",
            ],
            "examples": [
                "How does Trail CHOP work?",
                "Find an example using Particle GPU",
                "Snippet for vertex shader",
            ],
        },
    ],
    "featured_prompts": [
        "Build a kaleidoscope feedback loop",
        "Audit this project for problems",
        "Replay my 'audio-react' recipe",
        "Screenshot the network",
        "Why is the framerate dropping?",
        "What memories about feedback?",
    ],
}


def handle_get_capabilities_summary(body: dict) -> dict:
    """v2.4 / Phase C.6 — return the grouped capability index.

    Pure-data tool: no live TD calls, no side effects. The chat UI
    fetches it on first load to populate "featured prompt" chips
    below the input field; the agent can also call it directly to
    answer "what can you do?".
    """
    return _CAPABILITIES_SUMMARY


# ---------------------------------------------------------------------------
# Phase 5.2 — first-run detection
# ---------------------------------------------------------------------------


def firstrun_status() -> dict:
    """Report whether the standalone is in a first-run state.

    Returns a dict with three booleans matching the canonical
    setup steps the chat UI walks the user through:

      - has_api_key:   ``<CONFIG_DIR>/config.json`` carries a
                       non-empty ``api_key`` field.
      - has_memory:    at least one ``*.md`` lives in the resolved
                       memory directory (i.e. the user has asked the
                       agent to remember anything yet).
      - has_brains:    at least one external corpus is discoverable
                       (jsonl OR brain.db) under any of the
                       documented data roots.

    A turn is "first run" when all three are False — the chat UI
    should render the welcome wizard and a quickstart checklist.
    Any one of them being True downgrades the welcome to the
    minimal logo + "type a message" hint.
    """
    has_api_key = False
    try:
        from tdpilot_api_config import fetch_api_key  # type: ignore[import-not-found]

        key = fetch_api_key()
        has_api_key = bool(key and isinstance(key, str) and key.strip())
    except Exception:
        has_api_key = False

    has_memory = False
    try:
        import os as _os
        from pathlib import Path as _Path

        # 2.1.3 — resolve via tdpilot_api_config so the new
        # ~/.tdpilot-dpsk4/api/memory and legacy ~/.tdpilot-api/memory
        # are both checked. Falls back to the legacy path if the
        # config module isn't importable.
        try:
            from tdpilot_api_config import resolve_user_dir as _resolve  # type: ignore[import-not-found]

            memory_dir = _resolve("memory")
        except ImportError:
            memory_dir = _Path.home() / ".tdpilot-api" / "memory"
        if memory_dir.is_dir():
            for entry in _os.scandir(memory_dir):
                if entry.is_file() and entry.name.endswith(".md"):
                    has_memory = True
                    break
    except Exception:
        has_memory = False

    has_brains = False
    try:
        from pathlib import Path as _Path

        roots = (
            _Path.home() / ".tdpilot" / "data" / "normalized",
            _Path.home() / ".tdpilot-dpsk4" / "data" / "normalized",
            _Path.home() / ".tdpilot-api" / "data" / "normalized",
        )
        for root in roots:
            if not root.is_dir():
                continue
            for entry in root.iterdir():
                if not entry.is_dir():
                    continue
                if any(entry.glob("*brain.db")) or (entry / "pages.jsonl").is_file():
                    has_brains = True
                    break
            if has_brains:
                break
    except Exception:
        has_brains = False

    is_first_run = not (has_api_key or has_memory or has_brains)

    # v2.4 / Phase B.2 — detect Authmode so the chat UI can prompt
    # legacy COMPs (built pre-v2.3.0, when the default was "open") to
    # opt INTO token mode. Reads the COMP param directly so the wizard
    # accurately reflects current state, not the build-script default.
    authmode = ""
    try:
        comp = parent()  # type: ignore[name-defined] # noqa: F821
        if comp is not None and hasattr(comp.par, "Authmode"):
            authmode = str(comp.par.Authmode.val or "").strip().lower()
    except NameError:
        # Not running inside TD (e.g. in tests) — leave empty.
        authmode = ""
    except Exception:
        authmode = ""
    authmode_is_open = authmode == "open"

    # Steps the chat UI should highlight. Order matches the wizard
    # flow: paste key first (everything else is gated on it), then
    # invite the user to save a memory once they've had a real
    # conversation, then optionally install a brain.
    next_steps: list[dict] = []
    # v2.4 / Phase B.2 — surface the Authmode migration BEFORE other
    # steps so legacy-open users see it first. Acts as a soft nudge,
    # not a forced switch (drag-and-go convenience is preserved for
    # users who genuinely want it).
    if authmode_is_open:
        next_steps.append(
            {
                "name": "switch_to_token_auth",
                "label": (
                    "Auth is OPEN — any local browser tab can drive "
                    "TouchDesigner. Recommended: switch to token auth."
                ),
                "done": False,
                "recommended_action": "switch_to_token",
            }
        )
    if not has_api_key:
        next_steps.append(
            {
                "name": "paste_api_key",
                "label": "Paste your DeepSeek API key into the COMP and pulse Save Key.",
                "done": False,
            }
        )
    if not has_brains:
        next_steps.append(
            {
                "name": "install_brain",
                "label": "Install the official-docs brain: npx tdpilot-dpsk4 brains add derivative",
                "done": has_brains,
                "optional": True,
            }
        )
    if not has_memory:
        next_steps.append(
            {
                "name": "first_memory",
                "label": "Ask the agent something — once it learns your preferences, save with memory_save.",
                "done": False,
            }
        )

    return {
        "is_first_run": is_first_run,
        "has_api_key": has_api_key,
        "has_memory": has_memory,
        "has_brains": has_brains,
        # v2.4 / Phase B.2 — Authmode surface for the migration wizard.
        "authmode": authmode,
        "authmode_is_open": authmode_is_open,
        "next_steps": next_steps,
    }
