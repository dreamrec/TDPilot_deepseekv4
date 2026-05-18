"""Activity ring buffer + journal-hint builder — v2.5.1.

Three pieces:

1. ``ActivityRecord``  — single tool-call observation (frozen dataclass).
2. ``ActivityRing``    — bounded FIFO ring (default 200 entries) with
   filter helpers used by ``td_get_activity_log``.
3. ``args_hash``       — duplicate of B-010 deep-canonical hash
   (lives in ``td_component/tdpilot_api_cycle_detector.py`` for the
   chat-pipe side; this is the MCP-server-side copy). Same semantics:
   recursively sorts dict keys AND list elements so list-order
   permutations hash to the same identity.

The chat-pipe agent's tool dispatcher already had ``args_hash`` from
B-010. We duplicate the function here rather than crossing the
src/td_mcp ↔ td_component import boundary — the chat-pipe ``.tox`` is
baked from ``td_component/`` files only; it cannot ``import td_mcp``.

Both copies are pinned by ``tests/test_v25_activity_log.py`` and the
existing ``tests/test_tdpilot_api_cycle_detector.py``. If you change
either, update both and re-run both suites.
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Optional

ResultKind = Literal["ok", "error", "no_change"]


# ---------------------------------------------------------------------------
# Argument canonicalization (mirror of B-010 from cycle_detector)
# ---------------------------------------------------------------------------


def _deep_canonicalize(value: Any) -> Any:
    """Recursively normalize a JSON-shaped value for order-independent
    comparison. Same logic as ``td_component/tdpilot_api_cycle_detector.py``
    ``_deep_canonicalize`` — kept in sync manually because the chat-pipe
    cannot import from ``src/td_mcp``.
    """
    if isinstance(value, dict):
        return {k: _deep_canonicalize(value[k]) for k in sorted(value.keys(), key=str)}
    if isinstance(value, list):
        canonized = [_deep_canonicalize(x) for x in value]
        try:
            return sorted(
                canonized,
                key=lambda x: json.dumps(x, sort_keys=True, default=str, separators=(",", ":")),
            )
        except (TypeError, ValueError):
            return canonized
    return value


def args_hash(args: dict | None) -> str:
    """Stable, deeply order-independent string key for a tool-args dict.

    ``None`` and ``{}`` collapse to the same ``"{}"`` so a no-args call
    has one identity regardless of how the LLM framed it.
    """
    if not args:
        return "{}"
    canonical = _deep_canonicalize(args)
    return json.dumps(canonical, sort_keys=True, default=str, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Record + ring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    """Single tool-call observation."""

    ts: float
    tool_name: str
    args_hash: str
    duration_ms: int
    result_kind: ResultKind
    error_msg: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Drop None error_msg to keep the JSON terse for the LLM.
        if d.get("error_msg") is None:
            d.pop("error_msg", None)
        return d


@dataclass
class ActivityRing:
    """Bounded FIFO ring of ``ActivityRecord`` entries.

    Default cap of 200 entries — matches upstream ``dreamrec/TDPilot``
    v1.6.16's ring. Older entries fall off automatically.

    Per-turn reset is the caller's responsibility: the chat-pipe agent
    calls ``ring.start_turn()`` from ``Agent.run_turn`` so each turn
    sees a clean per-turn view via ``records_this_turn()``. The
    full-session ring (``records()``) is unaffected by per-turn reset.
    """

    maxlen: int = 200
    _records: deque[ActivityRecord] = field(default_factory=lambda: deque(maxlen=200), init=False)
    _turn_start_idx: int = field(default=0, init=False)
    _total_appended: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        # ``deque(maxlen=...)`` ignores the maxlen if we keep the default
        # factory's hardcoded 200. Re-bind with the requested cap.
        if self.maxlen != 200:
            existing = list(self._records)
            self._records = deque(existing, maxlen=self.maxlen)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def append(self, record: ActivityRecord) -> None:
        self._records.append(record)
        self._total_appended += 1

    def start_turn(self) -> None:
        """Mark the start of a new turn for ``records_this_turn`` slicing."""
        self._turn_start_idx = self._total_appended

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def records(
        self,
        *,
        limit: int | None = None,
        tool_filter: str | None = None,
        since_ts: float | None = None,
    ) -> list[ActivityRecord]:
        """Return records matching the filter, oldest-first.

        - ``limit``: most recent N (None = all in ring).
        - ``tool_filter``: exact-match tool name.
        - ``since_ts``: only records with ``ts >= since_ts``.
        """
        out = list(self._records)
        if tool_filter is not None:
            out = [r for r in out if r.tool_name == tool_filter]
        if since_ts is not None:
            out = [r for r in out if r.ts >= since_ts]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def records_this_turn(self) -> list[ActivityRecord]:
        """Return only records appended since the most recent ``start_turn``."""
        # Walk back from the right edge: total_appended - turn_start_idx is
        # the number of records added this turn. They're the LAST N entries
        # in _records (unless the ring overflowed mid-turn, in which case
        # some early-turn records may have been evicted — that's OK; we
        # report what's still observable).
        n_this_turn = self._total_appended - self._turn_start_idx
        if n_this_turn <= 0:
            return []
        n = min(n_this_turn, len(self._records))
        return list(self._records)[-n:]

    def count_for(self, tool_name: str, args_hash: str) -> int:
        """How many times (tool_name, args_hash) appears in THIS turn.

        Used by ``build_journal_hint`` to detect repetition. Per-turn
        scope (not full ring) so the count resets when a new turn starts
        — matches the cycle-detector's per-turn ledger semantics.
        """
        records = self.records_this_turn()
        return sum(1 for r in records if r.tool_name == tool_name and r.args_hash == args_hash)

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# Module-level singleton (MCP server side)
# ---------------------------------------------------------------------------

# The MCP server is single-process; a single ring suffices. Tests reset via
# ``reset_global_ring()`` to get a clean slate. The chat-pipe agent does NOT
# use this singleton — it constructs its own ring per Agent instance because
# multiple chat-pipe COMPs can coexist in the same TD process.

_GLOBAL_RING: ActivityRing | None = None


def get_global_ring() -> ActivityRing:
    global _GLOBAL_RING
    if _GLOBAL_RING is None:
        _GLOBAL_RING = ActivityRing()
    return _GLOBAL_RING


def reset_global_ring(maxlen: int = 200) -> ActivityRing:
    """Test helper — rebuilds the singleton. Returns the fresh ring."""
    global _GLOBAL_RING
    _GLOBAL_RING = ActivityRing(maxlen=maxlen)
    return _GLOBAL_RING


def record_activity(
    *,
    tool_name: str,
    args: dict | None,
    duration_ms: int,
    result_kind: ResultKind,
    error_msg: str | None = None,
    ring: ActivityRing | None = None,
) -> ActivityRecord:
    """Append a single ActivityRecord to ``ring`` (default = global ring).

    Returns the appended record so the caller can chain or test.
    """
    target = ring if ring is not None else get_global_ring()
    rec = ActivityRecord(
        ts=time.monotonic(),
        tool_name=tool_name,
        args_hash=args_hash(args),
        duration_ms=duration_ms,
        result_kind=result_kind,
        error_msg=error_msg,
    )
    target.append(rec)
    return rec


# ---------------------------------------------------------------------------
# Journal-hint builder
# ---------------------------------------------------------------------------

# Tools that B-007 protocol point 6 explicitly calls out as loop-prone
# read-only probes. The journal-hint surface mirrors that set so the
# hint message lines up with what the static prompt already says.
LOOP_PRONE_PROBES: frozenset[str] = frozenset(
    {
        "td_get_errors",
        "td_analyze_frame",
        "td_get_node_detail",
        "td_cooking_info",
        "td_get_connections",
    }
)


def build_journal_hint(
    *,
    tool_name: str,
    args_hash: str,
    activity_ring: ActivityRing,
    cycle_threshold: int = 3,
) -> dict[str, Any] | None:
    """Compose a ``_read_journal`` hint for the LLM if the same
    ``(tool_name, args_hash)`` pair has fired before this turn.

    Hint logic (tuned to the existing cycle-detector at threshold=3):

    - count == 1: no hint.
    - count == 2: WARN. This is the LAST chance — one more identical call
      will trip cycle-detect and end the turn.

    Returns the hint dict (to be merged into the tool result under the
    ``_read_journal`` key) or ``None`` if no hint applies.
    """
    count = activity_ring.count_for(tool_name, args_hash)
    if count < 2:
        return None

    # The current call IS the count-th occurrence (we count records that
    # were just appended). One more identical call hits threshold=3.
    remaining = cycle_threshold - count
    if remaining <= 0:
        # Cycle-detect should already have raised; defensive return None.
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
