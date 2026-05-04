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

    return caps
