"""TDPilot API — user-pluggable tools loader (Sprint 4.2).

Drop a Python file in ``~/.tdpilot-api/tools/<name>.py`` exposing:

    SCHEMA = {
        "name": "my_tool",
        "description": "What this tool does.",
        "input_schema": {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    }

    def handle(args: dict) -> dict:
        # Runs on the cook thread. op(), parent(), project available.
        return {"result": ...}

After saving, pulse "Reload Config" on the tdpilot_API COMP. The new
tool appears in the agent's tool list.

Security model: NO sandbox. The trust boundary is "the user's home
directory" — same model as pip, VS Code extensions, .bashrc. On first
load we print a notice to TD's Textport so the action is visible.

Conflict resolution: a user tool with the same `name` as a built-in
WINS (the built-in is suppressed for that runtime). Mirrors the
knowledge + skills precedent (user entries shadow bundled).

Discovery: at runtime build time only. Pulse Reload Config to pick up
edits. Mid-session re-scans were rejected because they invalidate
DeepSeek's auto-cache prefix every turn.

Validation: hand-rolled (jsonschema/pydantic aren't in TD's bundled
Python). Bad schemas log + skip; the runtime keeps working with the
remaining tools.
"""

from __future__ import annotations

import importlib.util
import os
import re
import traceback
from pathlib import Path
from typing import Any

USER_TOOLS_DIR = Path.home() / ".tdpilot-api" / "tools"

# Per-runtime registry — populated by _load_user_tools(); read by the
# tool_list_user handler. Cleared on every runtime rebuild.
_LOADED: list[dict] = []


# ---------------------------------------------------------------------------
# Hand-rolled schema validator (Anthropic tool format)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _validate_schema(s: Any) -> str | None:
    """Returns error message if invalid, None if OK.

    Validates the four fields the Anthropic /v1/messages tool format
    requires: ``name``, ``description``, ``input_schema`` (a JSON
    Schema object with ``type: "object"``).
    """
    if not isinstance(s, dict):
        return f"SCHEMA must be a dict, got {type(s).__name__}"
    name = s.get("name")
    if not isinstance(name, str) or not name:
        return "SCHEMA['name'] must be a non-empty string"
    if not _NAME_RE.match(name):
        return f"SCHEMA['name'] {name!r} must match ^[a-zA-Z0-9_-]{{1,64}}$"
    desc = s.get("description")
    if not isinstance(desc, str):
        return "SCHEMA['description'] must be a string"
    inp = s.get("input_schema")
    if not isinstance(inp, dict):
        return "SCHEMA['input_schema'] must be a dict"
    if inp.get("type") != "object":
        return "SCHEMA['input_schema']['type'] must be 'object'"
    return None


# ---------------------------------------------------------------------------
# Loader — called from _build_runtime in extension
# ---------------------------------------------------------------------------


class _UserHandlerNamespace:
    """Lightweight object the dispatcher's getattr() can resolve. Each
    user tool's handle() function is attached as ``handle_<name>`` so
    it fits the existing ``make_dispatcher`` contract without changes.
    """

    pass


def _print(msg: str) -> None:
    """Single-channel printer so we can route to TD's Textport AND
    keep stdout in sync for tests."""
    try:
        print(msg)
    except Exception:
        pass


_TD_GLOBAL_NAMES = (
    # Module-level TD injections that ought to be available inside a
    # user tool's handle(). Pulled from THIS loader's globals at runtime
    # — when this file is itself loaded as a textDAT inside TD, those
    # names are already present in our scope (TD injects them per-DAT).
    "op",
    "ops",
    "parent",
    "project",
    "absTime",
    "tdu",
    "td",
    "ui",
    "app",
)


def _gather_td_globals() -> dict:
    """Return a dict of TD globals to inject into user-tool modules.

    User-tool .py files loaded via importlib get a clean namespace —
    they CANNOT see TD's `op()`, `parent()`, `project`, etc. unless we
    explicitly seed them. Bug caught in production: a user tool's
    handle() called op('/project1') and got NameError, which appears
    to the agent as 'td_count_operators → ERR' with no actionable
    detail.

    We pull from this file's own globals because this file IS loaded
    as a textDAT in TD and has the standard TD injections in scope.
    Names missing from our globals (e.g. running outside TD for tests)
    are silently skipped.
    """
    out: dict = {}
    g = globals()
    for name in _TD_GLOBAL_NAMES:
        if name in g:
            out[name] = g[name]
    return out


def load_user_tools(tool_schemas: list, handler_modules: list, extra_mappings: dict) -> list[dict]:
    """Scan USER_TOOLS_DIR, validate, register. MUTATES inputs:
      - tool_schemas: appended with each user tool's SCHEMA dict
      - handler_modules: appended with a namespace exposing handle_<name>
      - extra_mappings: TOOL_TO_HANDLER-style entries name → (fn, adapter)

    Returns the list of LOADED entries (for diagnostics + tool_list_user).
    Failed entries are logged but do not raise — keeps the runtime
    boot resilient to a broken user tool.
    """
    global _LOADED
    _LOADED = []
    if not USER_TOOLS_DIR.is_dir():
        return _LOADED

    existing_names = {t.get("name", "") for t in tool_schemas}
    td_globals = _gather_td_globals()

    py_files = sorted(USER_TOOLS_DIR.glob("*.py"))
    for py_path in py_files:
        if py_path.name.startswith("_"):
            # Skip files that look like helpers / private (e.g. _helpers.py)
            continue
        info: dict = {
            "filename": py_path.name,
            "path": str(py_path),
            "ok": False,
            "error": None,
            "name": None,
            "description": None,
            "size_bytes": py_path.stat().st_size if py_path.is_file() else 0,
        }
        try:
            # Import the file with a unique module name so multiple
            # user tools can coexist without sys.modules collisions.
            mod_name = f"_tdpilot_api_user_tool_{py_path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py_path)
            if spec is None or spec.loader is None:
                info["error"] = "could not create import spec"
                _LOADED.append(info)
                continue
            mod = importlib.util.module_from_spec(spec)
            # Inject TD globals into the module's namespace BEFORE
            # exec_module — function definitions inside the file bind
            # their __globals__ to mod.__dict__, so `op()` etc. become
            # resolvable at handle()-call time.
            if td_globals:
                mod.__dict__.update(td_globals)
            spec.loader.exec_module(mod)

            schema = getattr(mod, "SCHEMA", None)
            handle_fn = getattr(mod, "handle", None)
            if schema is None:
                info["error"] = "missing module-level SCHEMA dict"
                _LOADED.append(info)
                _print(f"[tdpilot_API] user tool {py_path.name}: missing SCHEMA — skipped")
                continue
            if not callable(handle_fn):
                info["error"] = "missing module-level handle(args) function"
                _LOADED.append(info)
                _print(f"[tdpilot_API] user tool {py_path.name}: no handle() — skipped")
                continue

            err = _validate_schema(schema)
            if err:
                info["error"] = err
                _LOADED.append(info)
                _print(f"[tdpilot_API] user tool {py_path.name}: {err} — skipped")
                continue

            name = schema["name"]
            info["name"] = name
            info["description"] = schema.get("description", "")

            # Conflict resolution: user wins.
            if name in existing_names:
                tool_schemas[:] = [t for t in tool_schemas if t.get("name") != name]
                _print(f"[tdpilot_API] user tool '{name}' overrides built-in")

            # Wrap and register.
            ns = _UserHandlerNamespace()
            setattr(ns, f"handle_{name}", handle_fn)
            handler_modules.append(ns)
            tool_schemas.append(dict(schema))  # defensive copy
            existing_names.add(name)

            # Identity adapter — user tools receive the args dict raw.
            extra_mappings[name] = (f"handle_{name}", lambda d: d or {})

            info["ok"] = True
            _LOADED.append(info)
            _print(f"[tdpilot_API] loaded user tool: {name}  ({py_path.name})")

        except Exception as exc:
            info["error"] = f"{type(exc).__name__}: {exc}"
            info["traceback"] = traceback.format_exc(limit=4)
            _LOADED.append(info)
            _print(f"[tdpilot_API] user tool {py_path.name} load FAILED: {info['error']}")

    if py_files and not _LOADED:
        _print(f"[tdpilot_API] no user tools loaded from {USER_TOOLS_DIR}")
    elif _LOADED:
        ok_count = sum(1 for e in _LOADED if e["ok"])
        _print(
            f"[tdpilot_API] {ok_count}/{len(_LOADED)} user tools active "
            f"({USER_TOOLS_DIR}) — pulse Reload Config after edits"
        )

    return _LOADED


# ---------------------------------------------------------------------------
# Tool handlers (the user-tool MANAGEMENT tools, not the user tools
# themselves)
# ---------------------------------------------------------------------------


def handle_tool_list_user(body: dict) -> dict:
    """Return the registry of user tools — both successfully loaded and
    failed. The failed entries surface the validation error so the user
    can fix the file and pulse Reload Config."""
    return {
        "ok": True,
        "directory": str(USER_TOOLS_DIR),
        "directory_exists": USER_TOOLS_DIR.is_dir(),
        "count": len(_LOADED),
        "active_count": sum(1 for e in _LOADED if e.get("ok")),
        "tools": list(_LOADED),
    }


def handle_tool_validate(body: dict) -> dict:
    """Dry-validate a Python file as a user tool WITHOUT registering
    it. Useful for editing — call this, see if the schema is OK, edit,
    re-validate, then pulse Reload Config to actually register it.

    Accepts ``path`` (absolute or relative to USER_TOOLS_DIR) and
    returns {ok, schema_error, has_handle, schema_summary}.
    """
    path_str = (body.get("path") or "").strip()
    if not path_str:
        return {"error": "Missing required field: path"}
    p = Path(path_str)
    if not p.is_absolute():
        p = USER_TOOLS_DIR / p
    if not p.is_file():
        return {"error": f"File not found: {p}"}

    try:
        mod_name = f"_tdpilot_api_user_tool_validate_{p.stem}"
        spec = importlib.util.spec_from_file_location(mod_name, p)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": "could not create import spec"}
        mod = importlib.util.module_from_spec(spec)
        # Inject TD globals so module-level code that references op() /
        # project / etc. doesn't fail validation. Same fix as the loader.
        td_globals = _gather_td_globals()
        if td_globals:
            mod.__dict__.update(td_globals)
        spec.loader.exec_module(mod)
    except Exception as exc:
        return {
            "ok": False,
            "error": f"import failed: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=4),
        }

    schema = getattr(mod, "SCHEMA", None)
    handle_fn = getattr(mod, "handle", None)
    if schema is None:
        return {"ok": False, "error": "missing module-level SCHEMA dict"}
    if not callable(handle_fn):
        return {"ok": False, "error": "missing module-level handle(args) function"}

    err = _validate_schema(schema)
    if err:
        return {"ok": False, "error": err}

    return {
        "ok": True,
        "path": str(p),
        "schema": {
            "name": schema["name"],
            "description": schema["description"],
            "input_schema_keys": sorted((schema.get("input_schema") or {}).get("properties", {}).keys()),
        },
        "has_handle": True,
        "hint": "Pulse Reload Config on the tdpilot_API COMP to register this tool.",
    }
