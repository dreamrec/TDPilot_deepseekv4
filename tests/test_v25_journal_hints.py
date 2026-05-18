"""Tests for v2.5.1 _read_journal hint builder — both sides.

Pins the thresholds, message shape, loop-prone detection, and the
parity between ``src/td_mcp/observability/activity_log.py`` and
``td_component/tdpilot_api_activity_log.py``.

No TouchDesigner required.
"""

from __future__ import annotations

import pytest

# Chat-pipe side
import tdpilot_api_activity_log as cpal  # noqa: E402

# MCP-server-side
from td_mcp.observability import (
    ActivityRecord,
    ActivityRing,
    args_hash,
    build_journal_hint,
)
from td_mcp.observability.activity_log import LOOP_PRONE_PROBES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_mcp_ring(tool_name: str, args: dict, count: int) -> tuple[ActivityRing, str]:
    """Build an MCP ring with ``count`` records for ``(tool_name, args)``."""
    ring = ActivityRing()
    ring.start_turn()
    h = args_hash(args)
    for _ in range(count):
        ring.append(ActivityRecord(ts=0.0, tool_name=tool_name, args_hash=h, duration_ms=1, result_kind="ok"))
    return ring, h


def _seed_cp_ring(tool_name: str, args: dict, count: int) -> tuple[cpal.ActivityRing, str]:
    ring = cpal.ActivityRing()
    ring.start_turn()
    h = None
    for _ in range(count):
        rec = ring.record(tool_name, args, 1, "ok")
        h = rec.args_hash
    return ring, h


# ---------------------------------------------------------------------------
# MCP-server-side journal-hint logic
# ---------------------------------------------------------------------------


class TestMcpJournalHints:
    def test_no_hint_at_count_one(self):
        ring, h = _seed_mcp_ring("td_get_errors", {"path": "/p"}, 1)
        hint = build_journal_hint(tool_name="td_get_errors", args_hash=h, activity_ring=ring)
        assert hint is None

    def test_hint_fires_at_count_two(self):
        ring, h = _seed_mcp_ring("td_get_errors", {"path": "/p"}, 2)
        hint = build_journal_hint(tool_name="td_get_errors", args_hash=h, activity_ring=ring)
        assert hint is not None
        assert hint["call_count"] == 2
        assert hint["calls_until_cycle_detect"] == 1
        # Loop-prone probe → suffix mentions protocol point 6.
        assert "protocol point 6" in hint["hint"]

    def test_no_hint_at_count_three_or_higher(self):
        # Defensive branch: at count >= threshold, cycle-detect should
        # already have raised; we return None.
        ring, h = _seed_mcp_ring("td_get_errors", {"path": "/p"}, 3)
        hint = build_journal_hint(tool_name="td_get_errors", args_hash=h, activity_ring=ring)
        assert hint is None

    def test_general_tool_suffix_omits_protocol_point_6(self):
        ring, h = _seed_mcp_ring("td_create_node", {"type": "noiseTOP"}, 2)
        hint = build_journal_hint(tool_name="td_create_node", args_hash=h, activity_ring=ring)
        assert hint is not None
        assert "protocol point 6" not in hint["hint"]
        assert "switching strategy" in hint["hint"]

    def test_args_hash_b010_regression_list_order_invariant(self):
        """Pin Bug 10 — list-permuted args must hash identically so
        cycle counting can't be evaded by swapping list element order."""
        ring = ActivityRing()
        ring.start_turn()
        h1 = args_hash({"modes": ["a", "b"], "path": "X"})
        h2 = args_hash({"modes": ["b", "a"], "path": "X"})
        assert h1 == h2
        ring.append(ActivityRecord(0.0, "td_analyze_frame", h1, 1, "ok"))
        ring.append(ActivityRecord(0.0, "td_analyze_frame", h2, 1, "ok"))
        # Hint should fire because count_for(...) sees 2 entries with same hash.
        hint = build_journal_hint(tool_name="td_analyze_frame", args_hash=h1, activity_ring=ring)
        assert hint is not None
        assert hint["call_count"] == 2

    def test_per_turn_reset_clears_count(self):
        ring, h = _seed_mcp_ring("td_get_errors", {"path": "/p"}, 2)
        hint = build_journal_hint(tool_name="td_get_errors", args_hash=h, activity_ring=ring)
        assert hint is not None  # fires this turn
        ring.start_turn()  # new turn — count_for sees only this turn's records
        hint2 = build_journal_hint(tool_name="td_get_errors", args_hash=h, activity_ring=ring)
        assert hint2 is None

    def test_loop_prone_probes_set_matches_b007(self):
        # The hint suffix differs for loop-prone tools — pin the set
        # against the B-007 list so docs and behavior stay in sync.
        assert (
            frozenset(
                {
                    "td_get_errors",
                    "td_analyze_frame",
                    "td_get_node_detail",
                    "td_cooking_info",
                    "td_get_connections",
                }
            )
            == LOOP_PRONE_PROBES
        )


# ---------------------------------------------------------------------------
# Chat-pipe-side journal-hint logic — parity tests
# ---------------------------------------------------------------------------


class TestChatPipeJournalHints:
    def test_no_hint_at_count_one(self):
        ring, h = _seed_cp_ring("td_get_errors", {"path": "/p"}, 1)
        hint = cpal.build_journal_hint("td_get_errors", h, ring)
        assert hint is None

    def test_hint_fires_at_count_two(self):
        ring, h = _seed_cp_ring("td_get_errors", {"path": "/p"}, 2)
        hint = cpal.build_journal_hint("td_get_errors", h, ring)
        assert hint is not None
        assert hint["call_count"] == 2
        assert hint["calls_until_cycle_detect"] == 1
        assert "protocol point 6" in hint["hint"]

    def test_general_tool_suffix(self):
        ring, h = _seed_cp_ring("td_create_node", {"type": "noiseTOP"}, 2)
        hint = cpal.build_journal_hint("td_create_node", h, ring)
        assert hint is not None
        assert "protocol point 6" not in hint["hint"]

    def test_loop_prone_set_matches_mcp_side(self):
        # Both implementations must agree on which tools get the
        # stronger protocol-point-6 suffix.
        assert cpal.LOOP_PRONE_PROBES == LOOP_PRONE_PROBES


# ---------------------------------------------------------------------------
# Cross-implementation: same-args produce same-hint
# ---------------------------------------------------------------------------


class TestCrossSideParity:
    def test_same_args_produce_same_hint_payload_keys(self):
        mcp_ring, mcp_h = _seed_mcp_ring("td_get_errors", {"path": "/p"}, 2)
        cp_ring, cp_h = _seed_cp_ring("td_get_errors", {"path": "/p"}, 2)
        mcp_hint = build_journal_hint(tool_name="td_get_errors", args_hash=mcp_h, activity_ring=mcp_ring)
        cp_hint = cpal.build_journal_hint("td_get_errors", cp_h, cp_ring)
        assert mcp_hint is not None
        assert cp_hint is not None
        assert (
            set(mcp_hint.keys())
            == set(cp_hint.keys())
            == {
                "call_count",
                "calls_until_cycle_detect",
                "hint",
            }
        )
        # Same count + same remaining.
        assert mcp_hint["call_count"] == cp_hint["call_count"]
        assert mcp_hint["calls_until_cycle_detect"] == cp_hint["calls_until_cycle_detect"]

    def test_args_hash_collides_across_implementations(self):
        """The MCP-side ``args_hash`` and chat-pipe ``cycle_detector.args_hash``
        must produce identical strings for the same input — otherwise the
        two rings would count differently when wired into the same project."""
        from tdpilot_api_cycle_detector import args_hash as cp_args_hash  # noqa: PLC0415

        for args in (
            None,
            {},
            {"path": "/project1"},
            {"modes": ["a", "b"], "path": "X"},
            {"nested": {"k": [3, 1, 2]}, "z": "end"},
        ):
            assert args_hash(args) == cp_args_hash(args), f"args_hash mismatch for {args!r}"
