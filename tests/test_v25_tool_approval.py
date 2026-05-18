"""Tests for v2.5.3 — tool approval gates.

Pure-Python coverage of the approval module + registry + the
threading.Event-based wait/signal protocol. Integration with the
agent dispatch loop is exercised via a synthetic approval_provider
callback that simulates the runtime.
"""

from __future__ import annotations

import threading
import time

import pytest
import tdpilot_api_approval as ap  # noqa: E402  — chat-pipe module via conftest sys.path

# ---------------------------------------------------------------------------
# is_approval_required — gate decision matrix
# ---------------------------------------------------------------------------


class TestIsApprovalRequired:
    def test_off_mode_never_requires(self):
        for tool in ["td_exec_python", "td_delete_node", "td_get_info"]:
            assert ap.is_approval_required(tool_name=tool, args={}, mode=ap.MODE_OFF) is False

    def test_all_mode_always_requires(self):
        for tool in ["td_exec_python", "td_get_info", "td_screenshot"]:
            assert ap.is_approval_required(tool_name=tool, args={}, mode=ap.MODE_ALL) is True

    def test_destructive_only_always_set(self):
        for tool in ap.DESTRUCTIVE_TOOLS_ALWAYS:
            assert ap.is_approval_required(tool_name=tool, args={}, mode=ap.MODE_DESTRUCTIVE_ONLY) is True

    def test_destructive_only_non_destructive_safe(self):
        for tool in ["td_get_info", "td_get_nodes", "td_screenshot"]:
            assert ap.is_approval_required(tool_name=tool, args={}, mode=ap.MODE_DESTRUCTIVE_ONLY) is False

    def test_path_aware_inside_agent_comp_does_not_gate(self):
        # td_rename_node targeting a child of the agent's COMP is internal
        # bookkeeping — not destructive.
        assert (
            ap.is_approval_required(
                tool_name="td_rename_node",
                args={"path": "/project1/tdpilot_API/cache_dat"},
                mode=ap.MODE_DESTRUCTIVE_ONLY,
                agent_comp_path="/project1/tdpilot_API",
            )
            is False
        )

    def test_path_aware_outside_agent_comp_gates(self):
        assert (
            ap.is_approval_required(
                tool_name="td_rename_node",
                args={"path": "/project1/render1"},
                mode=ap.MODE_DESTRUCTIVE_ONLY,
                agent_comp_path="/project1/tdpilot_API",
            )
            is True
        )

    def test_path_aware_missing_path_gates_conservatively(self):
        # No path → can't tell → gate by default (fail safe).
        assert (
            ap.is_approval_required(
                tool_name="td_set_content",
                args={},
                mode=ap.MODE_DESTRUCTIVE_ONLY,
                agent_comp_path="/project1/tdpilot_API",
            )
            is True
        )

    def test_unknown_mode_falls_back_to_destructive_only(self):
        assert ap.is_approval_required(tool_name="td_exec_python", args={}, mode="nonsense_mode") is True


# ---------------------------------------------------------------------------
# build_denied_result — shape contract
# ---------------------------------------------------------------------------


class TestBuildDeniedResult:
    def test_deny_decision_carries_tool_error_flag(self):
        result = ap.build_denied_result("td_exec_python", ap.DECISION_DENY)
        assert result["_tool_error"] is True
        assert result["_tool_denied"] is True
        assert result["decision"] == ap.DECISION_DENY
        assert "denied by user" in result["error"]

    def test_timeout_decision_carries_distinct_message(self):
        result = ap.build_denied_result("td_delete_node", ap.DECISION_TIMEOUT)
        assert result["decision"] == ap.DECISION_TIMEOUT
        assert "did not approve" in result["error"]

    def test_optional_reason_appended_for_deny(self):
        result = ap.build_denied_result("td_exec_python", ap.DECISION_DENY, reason="too risky")
        assert "too risky" in result["error"]


# ---------------------------------------------------------------------------
# ApprovalRegistry — register / record_response / pop
# ---------------------------------------------------------------------------


class TestApprovalRegistry:
    def test_register_creates_pending_with_event(self):
        reg = ap.ApprovalRegistry()
        pending = reg.register("td_exec_python", {"code": "print(1)"})
        assert pending.approval_id
        assert pending.event.is_set() is False
        assert len(reg) == 1
        assert pending.approval_id in reg.pending_ids()

    def test_record_response_signals_event(self):
        reg = ap.ApprovalRegistry()
        pending = reg.register("td_exec_python", {})
        ok = reg.record_response(pending.approval_id, ap.DECISION_APPROVE)
        assert ok is True
        assert pending.event.is_set() is True
        assert pending.decision == ap.DECISION_APPROVE

    def test_record_response_unknown_id_returns_false(self):
        reg = ap.ApprovalRegistry()
        assert reg.record_response("nonexistent", ap.DECISION_APPROVE) is False

    def test_record_response_invalid_decision_returns_false(self):
        reg = ap.ApprovalRegistry()
        pending = reg.register("td_x", {})
        assert reg.record_response(pending.approval_id, "garbage") is False

    def test_pop_removes_entry(self):
        reg = ap.ApprovalRegistry()
        pending = reg.register("td_x", {})
        record = reg.pop(pending.approval_id)
        assert record is pending
        assert len(reg) == 0
        assert reg.pop(pending.approval_id) is None


# ---------------------------------------------------------------------------
# request_approval_or_skip — end-to-end via threading
# ---------------------------------------------------------------------------


class TestRequestApprovalOrSkip:
    def test_not_required_returns_immediately(self):
        reg = ap.ApprovalRegistry()
        called = []
        decision, _ = ap.request_approval_or_skip(
            tool_name="td_get_info",
            args={},
            mode=ap.MODE_DESTRUCTIVE_ONLY,
            agent_comp_path="/project1/tdpilot_API",
            registry=reg,
            on_request=lambda *a, **kw: called.append(a),
        )
        assert decision == ap.DECISION_NOT_REQUIRED
        assert called == []

    def test_approve_from_other_thread(self):
        reg = ap.ApprovalRegistry()
        captured_id: list[str] = []

        def _on_request(approval_id, tool_name, args, timeout_ms):
            captured_id.append(approval_id)

            # Simulate the user clicking Approve from the cook thread.
            def _approve():
                time.sleep(0.05)
                reg.record_response(approval_id, ap.DECISION_APPROVE)

            threading.Thread(target=_approve, daemon=True).start()

        decision, reason = ap.request_approval_or_skip(
            tool_name="td_exec_python",
            args={"code": "print(1)"},
            mode=ap.MODE_DESTRUCTIVE_ONLY,
            agent_comp_path="",
            registry=reg,
            on_request=_on_request,
            timeout_s=2.0,
        )
        assert decision == ap.DECISION_APPROVE
        assert reason == ""
        assert captured_id  # banner was notified
        assert len(reg) == 0  # registry cleaned up

    def test_deny_from_other_thread(self):
        reg = ap.ApprovalRegistry()

        def _on_request(approval_id, *_):
            def _deny():
                time.sleep(0.05)
                reg.record_response(approval_id, ap.DECISION_DENY, reason="risky")

            threading.Thread(target=_deny, daemon=True).start()

        decision, reason = ap.request_approval_or_skip(
            tool_name="td_delete_node",
            args={"path": "/project1/important"},
            mode=ap.MODE_DESTRUCTIVE_ONLY,
            agent_comp_path="/project1/tdpilot_API",
            registry=reg,
            on_request=_on_request,
            timeout_s=2.0,
        )
        assert decision == ap.DECISION_DENY
        assert reason == "risky"

    def test_timeout_when_no_response(self):
        reg = ap.ApprovalRegistry()
        decision, _ = ap.request_approval_or_skip(
            tool_name="td_exec_python",
            args={},
            mode=ap.MODE_DESTRUCTIVE_ONLY,
            agent_comp_path="",
            registry=reg,
            on_request=lambda *a, **kw: None,
            timeout_s=0.2,  # tight to keep test fast
        )
        assert decision == ap.DECISION_TIMEOUT
        # Registry cleans up the abandoned entry.
        assert len(reg) == 0

    def test_on_request_exception_denies_safely(self):
        reg = ap.ApprovalRegistry()

        def _bad_on_request(*a, **kw):
            raise RuntimeError("WS broadcast failed")

        decision, reason = ap.request_approval_or_skip(
            tool_name="td_exec_python",
            args={},
            mode=ap.MODE_DESTRUCTIVE_ONLY,
            agent_comp_path="",
            registry=reg,
            on_request=_bad_on_request,
            timeout_s=1.0,
        )
        assert decision == ap.DECISION_DENY
        assert "WS broadcast failed" in reason
        assert len(reg) == 0
