"""Chat-pipe activity ring buffer + journal-hint builder — v2.5.1.

Mirror of ``src/td_mcp/observability/activity_log.py`` (the MCP-server side)
adapted for the chat-pipe agent that runs inside ``tdpilot_API.tox``.

Why two copies?

The ``.tox`` file is baked from ``td_component/`` files only — code under
``src/td_mcp/`` cannot be imported from inside TouchDesigner. We keep the
two implementations identical in spirit and pin both with separate test
files (``tests/test_v25_activity_log.py`` for the MCP side,
``tests/test_v25_activity_log_chat_pipe.py`` for this side).

Args hashing reuses the existing B-010 deep-canonical implementation from
``tdpilot_api_cycle_detector`` (lives in the same ``td_component/`` package)
so we DON'T duplicate it here.
"""

from __future__ import annotations

import time
from collections import deque

# B-010 deep-canonical args_hash already lives in cycle_detector. Reuse.
# Late import via ``from ... import`` so this module is safe to import early
# (cycle_detector imports AgentError from agent; agent late-imports both).
try:
    from tdpilot_api_cycle_detector import args_hash  # type: ignore[import-not-found]
except Exception:  # pragma: no cover — TD-environment-only import path
    args_hash = None  # type: ignore[assignment]


# Tools that B-007 protocol point 6 calls out as loop-prone read-only
# probes. The journal-hint message singles them out so the hint lines up
# with the static prompt's strategy-switch guidance.
LOOP_PRONE_PROBES = frozenset(
    {
        "td_get_errors",
        "td_analyze_frame",
        "td_get_node_detail",
        "td_cooking_info",
        "td_get_connections",
    }
)


class ActivityRecord:
    """Single tool-call observation. Plain class (not dataclass) because the
    .tox-baked Python avoids ``slots=True``/``frozen=True`` dataclass quirks
    in older TD Python builds."""

    __slots__ = (
        "ts",
        "tool_name",
        "args_hash",
        "duration_ms",
        "result_kind",
        "error_msg",
    )

    def __init__(
        self,
        ts,
        tool_name,
        args_hash,
        duration_ms,
        result_kind,
        error_msg=None,
    ):
        self.ts = ts
        self.tool_name = tool_name
        self.args_hash = args_hash
        self.duration_ms = duration_ms
        self.result_kind = result_kind
        self.error_msg = error_msg

    def to_dict(self):
        d = {
            "ts": self.ts,
            "tool_name": self.tool_name,
            "args_hash": self.args_hash,
            "duration_ms": self.duration_ms,
            "result_kind": self.result_kind,
        }
        if self.error_msg is not None:
            d["error_msg"] = self.error_msg
        return d


class ActivityRing:
    """Bounded FIFO ring of ``ActivityRecord`` entries.

    Default cap of 200 entries (mirrors upstream + the MCP-server-side
    ring). The chat-pipe Agent calls ``start_turn()`` from ``_loop`` so
    each turn sees its own per-turn slice via ``records_this_turn()``;
    journal-hint counting uses that slice.
    """

    DEFAULT_MAXLEN = 200

    def __init__(self, maxlen=None):
        cap = self.DEFAULT_MAXLEN if maxlen is None else int(maxlen)
        self._records = deque(maxlen=cap)
        self.maxlen = cap
        self._turn_start_idx = 0
        self._total_appended = 0

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append(self, record):
        self._records.append(record)
        self._total_appended += 1

    def record(self, tool_name, args, duration_ms, result_kind, error_msg=None):
        """One-shot helper. Builds the record + appends + returns it."""
        if args_hash is None:
            # Defensive: cycle_detector not loadable (test envs); use
            # repr() as a low-quality fallback. Should never hit in
            # production .tox.
            h = repr(args) if args else "{}"
        else:
            h = args_hash(args)
        rec = ActivityRecord(
            ts=time.monotonic(),
            tool_name=tool_name,
            args_hash=h,
            duration_ms=duration_ms,
            result_kind=result_kind,
            error_msg=error_msg,
        )
        self.append(rec)
        return rec

    def start_turn(self):
        self._turn_start_idx = self._total_appended

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def records(self, limit=None, tool_filter=None, since_ts=None):
        out = list(self._records)
        if tool_filter is not None:
            out = [r for r in out if r.tool_name == tool_filter]
        if since_ts is not None:
            out = [r for r in out if r.ts >= since_ts]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def records_this_turn(self):
        n_this_turn = self._total_appended - self._turn_start_idx
        if n_this_turn <= 0:
            return []
        n = min(n_this_turn, len(self._records))
        return list(self._records)[-n:]

    def count_for(self, tool_name, args_hash_value):
        records = self.records_this_turn()
        return sum(1 for r in records if r.tool_name == tool_name and r.args_hash == args_hash_value)

    def __len__(self):
        return len(self._records)


# ---------------------------------------------------------------------------
# Journal-hint builder
# ---------------------------------------------------------------------------


def build_journal_hint(tool_name, args_hash_value, activity_ring, cycle_threshold=3):
    """Return a ``_read_journal`` hint dict or None.

    Hint fires at count == 2 (one call short of cycle-detect at
    threshold=3). At count >= threshold, returns None because
    cycle-detect should already have raised in the caller (chat-pipe
    raises CycleDetected at the top of the dispatch loop, BEFORE the
    dispatcher and BEFORE this builder runs).
    """
    count = activity_ring.count_for(tool_name, args_hash_value)
    if count < 2:
        return None
    remaining = cycle_threshold - count
    if remaining <= 0:
        return None
    is_loop_prone = tool_name in LOOP_PRONE_PROBES
    suffix = (
        " You're calling a loop-prone read-only probe (per protocol point 6) — "
        "switch strategy NOW: probe a different node, take ONE screenshot of "
        "the suspect TOP, inspect upstream connections, or report the stuckness."
        if is_loop_prone
        else " Consider switching strategy before the third call ends your turn."
    )
    return {
        "call_count": count,
        "calls_until_cycle_detect": remaining,
        "hint": (
            f"You've called '{tool_name}' with these exact args {count} times "
            f"this turn. One more identical call will trip cycle-detect and "
            f"end your turn." + suffix
        ),
    }


def build_activity_ring_factory(maxlen=None):
    """Return a zero-arg factory returning a fresh ``ActivityRing``.

    Used by ``tdpilot_api_runtime.Runtime`` to mirror the pattern of
    ``build_cycle_ledger_factory``. The Agent stores the factory and
    calls it once per turn so per-turn slicing semantics work correctly.
    """

    def _factory():
        return ActivityRing(maxlen=maxlen)

    return _factory


__all__ = [
    "ActivityRecord",
    "ActivityRing",
    "LOOP_PRONE_PROBES",
    "build_activity_ring_factory",
    "build_journal_hint",
]
