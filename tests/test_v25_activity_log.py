"""Tests for v2.5.1 activity log — both MCP-server-side and chat-pipe-side.

Covers:
* ``src/td_mcp/observability/activity_log.py`` — MCP-server module
* ``td_component/tdpilot_api_activity_log.py`` — chat-pipe module

The two implementations are intentional duplicates (chat-pipe can't import
from ``src/td_mcp``); these tests pin both behaviors in lockstep so they
don't drift.

No TouchDesigner required — pure Python.
"""

from __future__ import annotations

import time

import pytest

# Chat-pipe module (conftest adds td_component/ to sys.path)
import tdpilot_api_activity_log as cpal  # noqa: E402

# MCP-server-side module
from td_mcp.observability import (
    ActivityRecord,
    ActivityRing,
    args_hash,
    get_global_ring,
    record_activity,
    reset_global_ring,
)

# ---------------------------------------------------------------------------
# MCP-server-side ActivityRing
# ---------------------------------------------------------------------------


class TestMcpActivityRing:
    def test_append_and_len(self):
        ring = ActivityRing(maxlen=5)
        rec = ActivityRecord(
            ts=1.0,
            tool_name="td_get_info",
            args_hash=args_hash({}),
            duration_ms=10,
            result_kind="ok",
        )
        ring.append(rec)
        assert len(ring) == 1

    def test_fifo_eviction_at_capacity(self):
        ring = ActivityRing(maxlen=3)
        for i in range(5):
            ring.append(
                ActivityRecord(
                    ts=float(i),
                    tool_name=f"td_tool_{i}",
                    args_hash="{}",
                    duration_ms=1,
                    result_kind="ok",
                )
            )
        records = ring.records()
        # Ring capped at 3; oldest 2 (tool_0, tool_1) evicted.
        assert len(records) == 3
        assert [r.tool_name for r in records] == ["td_tool_2", "td_tool_3", "td_tool_4"]

    def test_filter_by_tool_name(self):
        ring = ActivityRing()
        ring.append(ActivityRecord(1.0, "td_get_errors", "{}", 1, "ok"))
        ring.append(ActivityRecord(2.0, "td_get_info", "{}", 1, "ok"))
        ring.append(ActivityRecord(3.0, "td_get_errors", "{}", 1, "ok"))
        filtered = ring.records(tool_filter="td_get_errors")
        assert len(filtered) == 2
        assert all(r.tool_name == "td_get_errors" for r in filtered)

    def test_filter_by_since_ts(self):
        ring = ActivityRing()
        ring.append(ActivityRecord(1.0, "td_tool", "{}", 1, "ok"))
        ring.append(ActivityRecord(5.0, "td_tool", "{}", 1, "ok"))
        ring.append(ActivityRecord(10.0, "td_tool", "{}", 1, "ok"))
        filtered = ring.records(since_ts=5.0)
        assert len(filtered) == 2
        assert filtered[0].ts == 5.0

    def test_record_to_dict_is_json_serializable(self):
        rec = ActivityRecord(
            ts=1.5,
            tool_name="td_get_errors",
            args_hash='{"path":"/project1"}',
            duration_ms=42,
            result_kind="ok",
        )
        d = rec.to_dict()
        # error_msg should be elided when None.
        assert "error_msg" not in d
        assert d["tool_name"] == "td_get_errors"
        assert d["result_kind"] == "ok"
        import json

        json.dumps(d)  # round-trip OK

    def test_global_ring_singleton_and_reset(self):
        reset_global_ring()
        a = get_global_ring()
        b = get_global_ring()
        assert a is b
        record_activity(tool_name="td_x", args=None, duration_ms=1, result_kind="ok")
        assert len(a) == 1
        c = reset_global_ring()
        assert c is not a
        assert len(c) == 0


# ---------------------------------------------------------------------------
# Chat-pipe ActivityRing — parity tests
# ---------------------------------------------------------------------------


class TestChatPipeActivityRing:
    def test_imports_and_basic_shape(self):
        assert hasattr(cpal, "ActivityRecord")
        assert hasattr(cpal, "ActivityRing")
        assert hasattr(cpal, "build_activity_ring_factory")
        assert hasattr(cpal, "build_journal_hint")

    def test_record_helper_appends_and_returns(self):
        ring = cpal.ActivityRing(maxlen=10)
        ring.start_turn()
        rec = ring.record(
            "td_get_errors",
            {"path": "/project1"},
            duration_ms=12,
            result_kind="ok",
        )
        assert rec.tool_name == "td_get_errors"
        assert rec.result_kind == "ok"
        # args_hash comes from cycle_detector — should be a stable non-empty str.
        assert isinstance(rec.args_hash, str)
        assert len(rec.args_hash) > 0
        assert len(ring) == 1

    def test_count_for_uses_per_turn_slice(self):
        ring = cpal.ActivityRing()
        ring.start_turn()
        h1 = ring.record("td_get_errors", {"path": "/p"}, 1, "ok").args_hash
        ring.record("td_get_errors", {"path": "/p"}, 1, "ok")
        assert ring.count_for("td_get_errors", h1) == 2

        # New turn resets the per-turn count.
        ring.start_turn()
        assert ring.count_for("td_get_errors", h1) == 0
        ring.record("td_get_errors", {"path": "/p"}, 1, "ok")
        assert ring.count_for("td_get_errors", h1) == 1

    def test_args_hash_order_independent_via_cycle_detector(self):
        """Chat-pipe variant uses cycle_detector's B-010 deep-canonical
        hash — list-order permutations must collide."""
        ring = cpal.ActivityRing()
        ring.start_turn()
        h1 = ring.record(
            "td_analyze_frame",
            {"modes": ["histogram", "luminance"], "path": "X"},
            1,
            "ok",
        ).args_hash
        h2 = ring.record(
            "td_analyze_frame",
            {"modes": ["luminance", "histogram"], "path": "X"},
            1,
            "ok",
        ).args_hash
        assert h1 == h2, "B-010 deep-canonical hash must be list-order-independent"

    def test_factory_produces_fresh_rings(self):
        f = cpal.build_activity_ring_factory(maxlen=50)
        a = f()
        b = f()
        assert a is not b
        assert a.maxlen == 50
        assert b.maxlen == 50


# ---------------------------------------------------------------------------
# Cross-implementation parity
# ---------------------------------------------------------------------------


class TestCrossImplementationParity:
    """Pin that the two implementations behave the same on key invariants."""

    def test_both_have_same_default_maxlen(self):
        mcp_ring = ActivityRing()
        cp_ring = cpal.ActivityRing()
        assert mcp_ring.maxlen == cp_ring.maxlen == 200

    def test_both_emit_compatible_to_dict_shape(self):
        mcp_rec = ActivityRecord(1.0, "td_x", "{}", 5, "ok")
        cp_rec = cpal.ActivityRecord(1.0, "td_x", "{}", 5, "ok")
        mcp_d = mcp_rec.to_dict()
        cp_d = cp_rec.to_dict()
        # Same key set on success result.
        assert set(mcp_d.keys()) == set(cp_d.keys())
        assert mcp_d == cp_d
