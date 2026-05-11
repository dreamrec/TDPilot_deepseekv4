"""Cycle detection in tool chains — Phase 1.2 of the v2.2.0→v3.0 roadmap.

Pairs with auto-rollback (Phase 1.1):

  * **1.1 auto-rollback** catches "agent broke things" — undoes a batch
    when new critical errors appear.
  * **1.2 cycle detection** catches "agent is stuck" — breaks the turn
    when the model issues the same tool call with identical args 3
    times in a row.

The two failure modes are complementary. Without cycle detection,
an agent that gets confused by an error response can loop forever
(or until the turn budget exhausts) re-attempting the same call,
burning DeepSeek tokens with no progress.

How it works
============

A ``CycleLedger`` is constructed once per turn (inside
``Agent._loop``) and tracks ``(tool_name, args_hash) -> count``.
Each call to ``ledger.record(name, args)`` increments and returns
the new count. The agent loop checks the count against the
configured threshold (default 3) BEFORE dispatching:

  * count == 1: first time, dispatch.
  * count == 2: second time, dispatch.
  * count >= 3: ``CycleDetected`` raised; ``run_turn`` catches it
    and fires ``on_error`` → ``EV_ERROR`` in the chat event stream
    → red banner in the chat UI.

Args identity is JSON-based with sorted keys — so
``{"path": "/a", "recurse": True}`` and
``{"recurse": True, "path": "/a"}`` hash to the same key, but
``{"path": "/a"}`` and ``{"path": "/b"}`` don't.

Disable via env var ``TDPILOT_DISABLE_CYCLE_DETECTION=1``. The
AgentRuntime honours this when building the factory; setting the
var = "1" makes the factory return ``None``, which makes the
agent loop a literal no-op around the would-be ledger.

This module is **pure-Python** — no TouchDesigner imports. Lives
in the chat-pipe alongside ``tdpilot_api_rollback`` and has the
same testing posture: every code path runnable from pytest
without a live TD session.
"""

from __future__ import annotations

import json
import os
from typing import Any

from tdpilot_api_agent import AgentError  # noqa: E402 — for typed subclass

# ---------------------------------------------------------------------------
# Exception — propagates through run_turn's BaseException catch-all into
# on_error → EV_ERROR. Subclassing AgentError keeps it consistent with
# TurnBudgetExceeded (the existing budget-exhausted exit).
# ---------------------------------------------------------------------------


class CycleDetected(AgentError):
    """Raised when a tool is requested with identical args more times
    than the configured threshold within a single turn.

    Carries ``tool_name`` / ``count`` / ``args_summary`` attributes so
    the on_error handler can build a richer event payload than just the
    formatted message string (used by AgentRuntime's EV_ERROR push).
    """

    def __init__(self, tool_name: str, count: int, args_summary: str = "") -> None:
        self.tool_name = str(tool_name)
        self.count = int(count)
        self.args_summary = str(args_summary)
        msg = f"Cycle detected: {self.tool_name} ×{self.count} with identical args"
        if self.args_summary:
            msg += f" ({self.args_summary})"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Argument hashing
# ---------------------------------------------------------------------------


def args_hash(args: dict | None) -> str:
    """Stable, order-independent hash for a tool-args dict.

    Used as the second half of the ``(tool_name, args_hash)`` ledger
    key. Implementation notes:

      * ``None`` and ``{}`` collapse to the same string ``"{}"`` so a
        no-args call is the same identity regardless of how the agent
        framed it.
      * ``sort_keys=True`` makes the order in which the LLM emitted
        the keys irrelevant.
      * ``separators=(",", ":")`` is the most compact form — fewer
        bytes through the dict-keying path.
      * ``default=str`` is defensive: tool args come from the LLM as
        JSON, so non-serializable values shouldn't reach us. But if
        an internal call somehow injects one (e.g. a Path object),
        we fall through to ``str()`` rather than raising.
      * On a flat-out TypeError/ValueError (cycle in input, etc.) we
        fall back to ``repr(args)`` — the agent never crashes on
        unhashable args; the worst case is the same args produce
        slightly different reprs and don't trigger detection.
    """
    if args is None or args == {}:
        return "{}"
    try:
        return json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(args)


def _format_args_summary(args: dict | None, max_len: int = 80) -> str:
    """Render a short human-readable args summary for the error message.

    Truncated to ``max_len`` chars so the EV_ERROR payload stays
    chat-bubble-sized. Returns ``"(no args)"`` for None/empty so the
    error message reads naturally."""
    if not args:
        return "(no args)"
    try:
        summary = json.dumps(args, sort_keys=True, default=str, separators=(", ", ": "))
    except (TypeError, ValueError):
        summary = repr(args)
    if len(summary) > max_len:
        summary = summary[: max_len - 3] + "..."
    return summary


# ---------------------------------------------------------------------------
# The ledger — one per turn, lives inside Agent._loop
# ---------------------------------------------------------------------------


class CycleLedger:
    """Per-turn counter of ``(tool_name, args_hash) -> call_count``.

    Construct once at the top of ``Agent._loop`` and discard at turn
    end. Single-threaded (the agent loop is single-threaded), so no
    locking.

    The threshold defaults to 3 — meaning 2 dispatches happen, the
    3rd attempt is blocked. Conservative on purpose: 2 dispatches
    gives the agent a chance to recover from a one-off error before
    declaring the turn stuck. Increase via constructor kwarg for
    longer-tail workflows; decrease (minimum 2) for stricter loops.
    """

    DEFAULT_THRESHOLD: int = 3

    def __init__(self, threshold: int | None = None) -> None:
        t = self.DEFAULT_THRESHOLD if threshold is None else int(threshold)
        if t < 2:
            raise ValueError(
                f"CycleLedger threshold must be >= 2 (got {t}); "
                "threshold of 1 would fire on the first call to any tool, "
                "which defeats the point."
            )
        self._threshold = t
        self._counts: dict[tuple[str, str], int] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    def record(self, tool_name: str, args: dict | None) -> int:
        """Increment the counter for ``(tool_name, args)`` and return
        the new count. The caller compares the result against
        ``self.threshold`` and raises ``CycleDetected`` if it has been
        reached.

        Always returns at least 1 (the call you just recorded)."""
        key = (str(tool_name), args_hash(args))
        new_count = self._counts.get(key, 0) + 1
        self._counts[key] = new_count
        return new_count

    def peek(self, tool_name: str, args: dict | None) -> int:
        """Return the current count without incrementing. Useful for
        tests + future introspection (e.g. a future "how stuck is the
        agent right now?" UI affordance). Returns 0 for unknown keys."""
        key = (str(tool_name), args_hash(args))
        return self._counts.get(key, 0)

    def reset(self) -> None:
        """Clear the ledger. Not used by the agent loop today (a new
        instance per turn is the lifecycle), but exposed for tests
        and potential future "soft restart" flows."""
        self._counts.clear()

    def __len__(self) -> int:
        return len(self._counts)


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------

ENV_DISABLE = "TDPILOT_DISABLE_CYCLE_DETECTION"


def is_disabled_via_env(env: dict[str, str] | None = None) -> bool:
    """True if the env var is set to a recognised truthy value.

    Mirrors ``tdpilot_api_rollback.is_disabled_via_env``. Pass
    ``env`` as a dict in tests; ``None`` reads ``os.environ``.
    """
    src = env if env is not None else os.environ
    val = (src.get(ENV_DISABLE) or "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Factory helper — used by Agent._loop to materialize a ledger.
#
# The Agent itself takes a ``cycle_ledger_factory: Callable[[], CycleLedger
# | None] | None`` constructor kwarg. AgentRuntime wires the factory so it
# returns ``None`` when the env var is set; otherwise it returns a fresh
# CycleLedger with the configured threshold. The loop calls
# ``factory()`` once at turn start; ``None`` → no-op (cycle detection
# disabled for this turn); ledger instance → real check.
# ---------------------------------------------------------------------------


def build_cycle_ledger_factory(
    threshold: int | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    """Return a zero-arg callable that produces a fresh
    ``CycleLedger`` per call, or ``None`` if disabled via env var.

    Wired by AgentRuntime; tests can construct directly with
    ``env={ENV_DISABLE: "1"}`` to verify the gate without mutating
    os.environ.
    """
    if is_disabled_via_env(env):
        return None
    return lambda: CycleLedger(threshold=threshold)


# Public re-export surface for the chat-pipe module wiring.
__all__ = [
    "CycleDetected",
    "CycleLedger",
    "ENV_DISABLE",
    "args_hash",
    "build_cycle_ledger_factory",
    "is_disabled_via_env",
]
