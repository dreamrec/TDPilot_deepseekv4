"""TDPilot API — ``tool_batch`` handler (Phase 2.1).

Run multiple TDPilot tool calls in one round trip. The win is
LLM-round-trip latency: instead of issuing N tool_use blocks and
paying N model→server→model cycles, the agent issues one
``tool_batch`` and gets all N results back together.

Sub-calls execute SEQUENTIALLY on the cook thread because TD's
Python API isn't thread-safe — ``ThreadPoolExecutor.submit``
doesn't help here, the per-tool latency is unchanged. What we save
is the chain of ``tool_use → tool_result → next-think → next
tool_use`` that the model would otherwise traverse.

Failure handling: a sub-call that returns ``{"error": ...}`` does
NOT abort the batch. Each result is reported in
``results[i].error`` and the rest still run. This matches the way
``recipe_replay`` was always *supposed* to behave when the agent
runs heterogeneous read-only lookups.

Hard caps:
    max 8 sub-calls (matches the schema's maxItems).
    nested ``tool_batch`` is rejected per sub-call (cheap recursion
    guard so a confused agent can't fork-bomb the dispatcher).
"""

from __future__ import annotations

import time
from typing import Any

# Phase 3 (F-12) — soft-import the tool-error sentinel helper. Same
# story as agent.py: the dispatcher module owns the canonical
# predicate.
try:
    from tdpilot_api_dispatcher import is_tool_error_result  # type: ignore[import-not-found]
except ImportError:

    def is_tool_error_result(result):  # type: ignore[misc]
        if not isinstance(result, dict):
            return False
        if "_tool_error" in result:
            return bool(result["_tool_error"])
        return "error" in result


MAX_BATCH_SIZE = 8


def _resolve_raw_dispatcher() -> Any:
    """Resolve the cook-thread-bypass dispatcher.

    PR-19 (F-18): now a one-line delegation to the shared
    ``tdpilot_api_lookup.get_raw_dispatcher`` helper. The previous
    bespoke walk through COMP → extension → runtime is gone — the
    helper handles every soft-failure mode and any future shape
    changes propagate through one place. Soft-import so a stripped
    test embed without the helper still has a working resolver.
    """
    try:
        from tdpilot_api_lookup import get_raw_dispatcher  # type: ignore[import-not-found]
    except ImportError:
        return None
    return get_raw_dispatcher()


def handle_tool_batch(body: dict) -> dict:
    """Dispatch a list of tool calls and return all results.

    Body schema:
        {"calls": [{"tool": str, "args": dict}, ...]}

    Returns:
        {"ok": True,
         "count": int,
         "results": [
             {"tool": str,
              "ok": bool,
              "result": dict | None,
              "error": str | None,
              "elapsed_ms": int},
             ...
         ]}

        On invalid body shape returns ``{"error": str}`` instead.
    """
    if not isinstance(body, dict):
        return {"error": "tool_batch body must be a dict"}

    calls = body.get("calls")
    if not isinstance(calls, list) or not calls:
        return {"error": "tool_batch requires non-empty 'calls' list"}

    if len(calls) > MAX_BATCH_SIZE:
        return {"error": (f"tool_batch capped at {MAX_BATCH_SIZE} sub-calls (received {len(calls)})")}

    raw_dispatcher = _resolve_raw_dispatcher()
    if raw_dispatcher is None:
        return {"error": "tool_batch could not access the runtime dispatcher"}

    results: list[dict] = []
    for i, call in enumerate(calls):
        if not isinstance(call, dict):
            results.append(
                {
                    "tool": None,
                    "ok": False,
                    "result": None,
                    "error": f"call[{i}] is not an object",
                    "elapsed_ms": 0,
                }
            )
            continue
        tool_name = call.get("tool")
        if not isinstance(tool_name, str) or not tool_name.strip():
            results.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "result": None,
                    "error": f"call[{i}].tool is missing or not a string",
                    "elapsed_ms": 0,
                }
            )
            continue
        if tool_name == "tool_batch":
            results.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "result": None,
                    "error": "Nested tool_batch is not allowed",
                    "elapsed_ms": 0,
                }
            )
            continue

        tool_args = call.get("args") or {}
        if not isinstance(tool_args, dict):
            results.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "result": None,
                    "error": f"call[{i}].args must be an object",
                    "elapsed_ms": 0,
                }
            )
            continue

        t_start = time.monotonic()
        try:
            result = raw_dispatcher(tool_name, tool_args)
        except Exception as exc:  # noqa: BLE001 — surface as per-call error
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            results.append(
                {
                    "tool": tool_name,
                    "ok": False,
                    "result": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": elapsed_ms,
                }
            )
            continue
        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        # F-12: prefer the explicit `_tool_error` sentinel, fall back
        # to the legacy `error`-key heuristic for one release.
        is_error = is_tool_error_result(result)
        results.append(
            {
                "tool": tool_name,
                "ok": not is_error,
                "result": result if not is_error else None,
                "error": (result.get("error") if (is_error and isinstance(result, dict)) else None),
                "elapsed_ms": elapsed_ms,
            }
        )

    return {
        "ok": True,
        "count": len(results),
        "results": results,
    }
