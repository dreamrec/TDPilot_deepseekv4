"""
TDPilot API — in-process tool dispatcher.

Routes a model-issued tool_use block ({name, input}) to the matching
handle_* function in the baked-in copy of mcp_webserver_callbacks.py
WITHOUT going through HTTP. The agent loop runs inside TD; the handlers
run inside TD; this dispatcher is the wire between them.

Handler resolution:
  - The handlers module is provided at construction time. Inside TD this
    is typically `op('mcp_webserver_callbacks').module` (a textDAT baked
    into the tdpilot_API.tox).
  - Tool name is mapped via TOOL_TO_HANDLER from tdpilot_api_schema; the
    body adapter rewrites field names where the HTTP contract differs
    from the schema we sent the model.

Errors are returned as dicts (not raised) so the agent loop can pass
them back to the model as tool_result with is_error=True. Error
strings + tracebacks are redacted (API key + home/config paths
stripped) before being surfaced — the model doesn't need to know the
user's filesystem layout, and leaking it into the DeepSeek logs is a
soft information leak.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from typing import Any

from tdpilot_api_schema import TOOL_TO_HANDLER  # type: ignore[import-not-found]

# Soft-import the redaction helpers — config module may be missing in
# stripped-down test embeds, in which case we degrade to identity.
try:
    from tdpilot_api_config import redact, redact_paths  # type: ignore[import-not-found]
except ImportError:

    def redact(s: str) -> str:  # noqa: D401
        return s

    def redact_paths(s: str) -> str:  # noqa: D401
        return s


def _scrub(s: str) -> str:
    """Run both API-key redaction and path redaction. Order doesn't
    matter — they're idempotent and operate on different substrings."""
    if not isinstance(s, str):
        return s
    return redact_paths(redact(s))


# Phase 2.3 — failure recovery hints. ``attach_hint`` is best-effort:
# the registry module may be missing in stripped-down test embeds,
# so we soft-import + degrade to a no-op.
try:
    from tdpilot_api_recovery import attach_hint as _attach_hint  # type: ignore[import-not-found]
except ImportError:

    def _attach_hint(result: Any) -> Any:  # noqa: ARG001 — fallback shim
        return result


class DispatchError(Exception):
    pass


def make_dispatcher(handlers_modules: Any, extra_mappings: dict | None = None) -> Callable[[str, dict], Any]:
    """Return a callable(tool_name, args) -> result_dict.

    ``handlers_modules`` accepts EITHER a single module (legacy, the
    original 33 td_* tools that all live in mcp_webserver_callbacks.py)
    OR a tuple/list of modules. With multiple modules the dispatcher
    walks them in order and uses the first one that has the handler
    function. This lets us add memory_*, knowledge_*, recipe_* etc.
    tools whose handlers live in separate textDATs without bloating
    mcp_webserver_callbacks.py.

    Inside TD: pass `(op('mcp_webserver_callbacks').module,
    op('tdpilot_api_memory').module, ...)`. Outside TD (tests): any
    object exposing the handle_* functions works.
    """
    if handlers_modules is None:
        raise DispatchError("handlers_modules is None")
    if not isinstance(handlers_modules, (list, tuple)):
        handlers_modules = (handlers_modules,)
    if not handlers_modules:
        raise DispatchError("handlers_modules is empty")

    def _resolve_handler(fn_name: str):
        for mod in handlers_modules:
            h = getattr(mod, fn_name, None)
            if h is not None:
                return h
        return None

    # Sprint 4.2: ``extra_mappings`` lets user-pluggable tools register
    # without monkey-patching TOOL_TO_HANDLER. Lookup order: extras
    # first (so user tools shadow built-ins by name), then the static
    # baked-in dict.
    extras = dict(extra_mappings or {})

    def dispatch(tool_name: str, args: dict | None) -> Any:
        mapping = extras.get(tool_name) or TOOL_TO_HANDLER.get(tool_name)
        if mapping is None:
            return _attach_hint(
                {
                    "error": f"Unknown tool: {tool_name}",
                    "supported": sorted(set(TOOL_TO_HANDLER.keys()) | set(extras.keys())),
                }
            )
        handler_fn_name, adapter = mapping
        handler = _resolve_handler(handler_fn_name)
        if handler is None:
            return _attach_hint(
                {
                    "error": f"Handler {handler_fn_name} not found on any handlers module",
                }
            )
        try:
            body = adapter(args or {})
            result = handler(body)
        except Exception as exc:  # noqa: BLE001 — return as tool error
            return _attach_hint(
                {
                    "error": _scrub(f"{type(exc).__name__}: {exc}"),
                    "traceback": _scrub(traceback.format_exc(limit=5)),
                }
            )
        # mcp_webserver_callbacks handlers may return a plain dict OR a
        # (status_code, dict) tuple depending on the route. Normalize to
        # a dict so the model gets uniform JSON.
        if isinstance(result, tuple) and len(result) == 2:
            status, payload = result
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.setdefault("_status", status)
                return _attach_hint(payload)
            return _attach_hint({"_status": status, "result": payload})
        # Phase 2.3 — annotate handler-returned error dicts too. The
        # attach_hint helper passes successful results through
        # unchanged, so this is safe for the happy path.
        return _attach_hint(result)

    return dispatch
