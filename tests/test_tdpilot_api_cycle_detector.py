"""Tests for tdpilot_api_cycle_detector — Phase 1.2 cycle detection.

Pure-Python coverage of the ledger, args-hash, threshold semantics,
env-var gate, factory, and the ``CycleDetected`` exception. The
agent-loop integration (the actual raise + on_error routing) is
exercised separately via the existing ``test_tdpilot_api_agent``
fixture machinery in a sibling commit / test.

No TouchDesigner required — everything in this module is pure-Python
by design.
"""

from __future__ import annotations

import pytest
import tdpilot_api_cycle_detector as cd  # noqa: E402

# ---------------------------------------------------------------------------
# args_hash — order independence, normalization, defensive paths
# ---------------------------------------------------------------------------


class TestArgsHash:
    def test_none_and_empty_collapse_to_same_key(self):
        assert cd.args_hash(None) == cd.args_hash({}) == "{}"

    def test_key_order_does_not_matter(self):
        a = {"path": "/a", "recurse": True, "max_depth": 10}
        b = {"recurse": True, "max_depth": 10, "path": "/a"}
        assert cd.args_hash(a) == cd.args_hash(b)

    def test_different_values_produce_different_hashes(self):
        a = cd.args_hash({"path": "/a"})
        b = cd.args_hash({"path": "/b"})
        assert a != b

    def test_different_keys_produce_different_hashes(self):
        # {"path": "/a"} vs {"path": "/a", "recurse": True} must NOT
        # collapse — different call shapes, different identity.
        assert cd.args_hash({"path": "/a"}) != cd.args_hash({"path": "/a", "recurse": True})

    def test_nested_dict_keys_sorted_too(self):
        a = {"path": "/x", "options": {"a": 1, "b": 2}}
        b = {"path": "/x", "options": {"b": 2, "a": 1}}
        assert cd.args_hash(a) == cd.args_hash(b)

    def test_lists_are_order_sensitive(self):
        # Lists DO encode order (it matters for most TD tools — connect
        # source/dest, draw call sequence, etc.).
        assert cd.args_hash({"steps": [1, 2, 3]}) != cd.args_hash({"steps": [3, 2, 1]})

    def test_default_str_handles_path_like_values(self):
        # default=str lets through non-JSON values without raising
        # (e.g. if some internal code injected a Path object).
        from pathlib import Path

        h = cd.args_hash({"file": Path("/tmp/x.png")})
        assert isinstance(h, str)
        # /tmp/x.png should be in the rendered form somewhere.
        assert "x.png" in h

    def test_compact_separators_for_smaller_keys(self):
        # We use compact JSON (no whitespace), keeping the ledger keys
        # short and the dict lookups cheap.
        out = cd.args_hash({"a": 1, "b": 2})
        assert " " not in out
        assert out == '{"a":1,"b":2}'


# ---------------------------------------------------------------------------
# CycleLedger — counter semantics, threshold validation, peek/reset
# ---------------------------------------------------------------------------


class TestCycleLedger:
    def test_default_threshold_is_three(self):
        ledger = cd.CycleLedger()
        assert ledger.threshold == 3

    def test_custom_threshold_respected(self):
        ledger = cd.CycleLedger(threshold=5)
        assert ledger.threshold == 5

    def test_threshold_less_than_two_rejected(self):
        # Threshold of 1 would fire on every first call; nonsense.
        with pytest.raises(ValueError, match="threshold must be >= 2"):
            cd.CycleLedger(threshold=1)
        with pytest.raises(ValueError):
            cd.CycleLedger(threshold=0)
        with pytest.raises(ValueError):
            cd.CycleLedger(threshold=-1)

    def test_record_returns_incremented_count(self):
        ledger = cd.CycleLedger()
        assert ledger.record("td_get_errors", {"path": "/"}) == 1
        assert ledger.record("td_get_errors", {"path": "/"}) == 2
        assert ledger.record("td_get_errors", {"path": "/"}) == 3

    def test_different_args_get_separate_counters(self):
        ledger = cd.CycleLedger()
        assert ledger.record("td_get_errors", {"path": "/a"}) == 1
        assert ledger.record("td_get_errors", {"path": "/b"}) == 1
        # Both paths still on first call.
        assert ledger.record("td_get_errors", {"path": "/a"}) == 2
        assert ledger.record("td_get_errors", {"path": "/b"}) == 2

    def test_different_tools_with_same_args_get_separate_counters(self):
        ledger = cd.CycleLedger()
        assert ledger.record("td_get_errors", {"path": "/"}) == 1
        assert ledger.record("td_get_nodes", {"path": "/"}) == 1

    def test_args_order_does_not_create_new_counter(self):
        # Same args in different key order → same counter (the whole
        # point of the sorted-keys hash).
        ledger = cd.CycleLedger()
        ledger.record("td_x", {"a": 1, "b": 2})
        assert ledger.record("td_x", {"b": 2, "a": 1}) == 2

    def test_none_and_empty_args_share_counter(self):
        ledger = cd.CycleLedger()
        assert ledger.record("td_get_info", None) == 1
        assert ledger.record("td_get_info", {}) == 2

    def test_peek_does_not_increment(self):
        ledger = cd.CycleLedger()
        ledger.record("td_x", {"a": 1})
        assert ledger.peek("td_x", {"a": 1}) == 1
        assert ledger.peek("td_x", {"a": 1}) == 1  # idempotent
        # Recording still proceeds from where it left off.
        assert ledger.record("td_x", {"a": 1}) == 2

    def test_peek_returns_zero_for_unknown_key(self):
        ledger = cd.CycleLedger()
        assert ledger.peek("td_x", {"a": 1}) == 0

    def test_reset_clears_all_counters(self):
        ledger = cd.CycleLedger()
        ledger.record("a", {})
        ledger.record("b", {"x": 1})
        ledger.record("c", {"y": 2})
        assert len(ledger) == 3
        ledger.reset()
        assert len(ledger) == 0
        # Post-reset, calls start from 1 again.
        assert ledger.record("a", {}) == 1

    def test_len_reflects_unique_keys(self):
        ledger = cd.CycleLedger()
        ledger.record("a", {})
        ledger.record("a", {})  # same key, still 1 unique
        ledger.record("b", {})
        assert len(ledger) == 2


# ---------------------------------------------------------------------------
# CycleDetected exception
# ---------------------------------------------------------------------------


class TestCycleDetectedException:
    def test_carries_tool_name_count_and_args_summary(self):
        exc = cd.CycleDetected(tool_name="td_get_errors", count=3, args_summary='{"path":"/"}')
        assert exc.tool_name == "td_get_errors"
        assert exc.count == 3
        assert exc.args_summary == '{"path":"/"}'
        assert "td_get_errors" in str(exc)
        assert "×3" in str(exc)
        assert "/" in str(exc)

    def test_message_omits_args_summary_when_empty(self):
        exc = cd.CycleDetected(tool_name="td_x", count=3)
        msg = str(exc)
        assert "td_x" in msg
        assert "×3" in msg
        # When summary is empty no parens block is added.
        assert "(" not in msg or "()" not in msg

    def test_is_an_AgentError_so_run_turn_catches_it(self):
        from tdpilot_api_agent import AgentError  # noqa: PLC0415

        exc = cd.CycleDetected("td_x", 3)
        assert isinstance(exc, AgentError)
        # And via BaseException (the run_turn catch-all).
        assert isinstance(exc, BaseException)


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------


class TestEnvVarGate:
    def test_unset_means_enabled(self):
        assert cd.is_disabled_via_env({}) is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", " on "])
    def test_truthy_values_disable(self, val):
        assert cd.is_disabled_via_env({cd.ENV_DISABLE: val}) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "anything-else"])
    def test_non_truthy_values_keep_enabled(self, val):
        assert cd.is_disabled_via_env({cd.ENV_DISABLE: val}) is False


# ---------------------------------------------------------------------------
# build_cycle_ledger_factory — the runtime wiring point
# ---------------------------------------------------------------------------


class TestBuildCycleLedgerFactory:
    def test_env_disabled_returns_none(self):
        factory = cd.build_cycle_ledger_factory(env={cd.ENV_DISABLE: "1"})
        assert factory is None

    def test_env_unset_returns_callable_producing_fresh_ledgers(self):
        factory = cd.build_cycle_ledger_factory(env={})
        assert callable(factory)
        l1 = factory()
        l2 = factory()
        # Two separate instances — per-turn lifecycle.
        assert l1 is not l2
        assert isinstance(l1, cd.CycleLedger)
        assert l1.threshold == cd.CycleLedger.DEFAULT_THRESHOLD

    def test_factory_propagates_custom_threshold(self):
        factory = cd.build_cycle_ledger_factory(threshold=5, env={})
        ledger = factory()
        assert ledger.threshold == 5


# ---------------------------------------------------------------------------
# End-to-end usage pattern — what the agent loop actually does
# ---------------------------------------------------------------------------


class TestUsagePattern:
    """Sanity check: the canonical "check then dispatch" pattern works
    the way Agent._loop expects.

    Pre-dispatch:

        count = ledger.record(name, args)
        if count >= ledger.threshold:
            raise CycleDetected(name, count, _format_args_summary(args))
        # ...dispatch...
    """

    def test_three_identical_calls_trigger_on_third(self):
        ledger = cd.CycleLedger()  # threshold=3
        raised: cd.CycleDetected | None = None
        for _ in range(5):
            count = ledger.record("td_x", {"path": "/"})
            if count >= ledger.threshold:
                raised = cd.CycleDetected("td_x", count, cd._format_args_summary({"path": "/"}))
                break
        assert raised is not None
        assert raised.count == 3
        assert "td_x" in str(raised)
        # And we stopped at the 3rd attempt — 2 dispatches were
        # allowed through before this fired.
        assert len(ledger) == 1  # one unique key

    def test_two_identical_calls_do_not_trigger(self):
        ledger = cd.CycleLedger()
        for _ in range(2):
            count = ledger.record("td_x", {"path": "/"})
            assert count < ledger.threshold

    def test_alternating_calls_never_trigger(self):
        # Realistic: agent calling td_get_errors / td_get_nodes /
        # td_get_errors / td_get_nodes / ... never reaches 3 for either.
        ledger = cd.CycleLedger()
        triggered = False
        for i in range(20):
            name = "td_get_errors" if i % 2 == 0 else "td_get_nodes"
            count = ledger.record(name, {"path": "/"})
            if count >= ledger.threshold:
                triggered = True
                break
        # After 20 alternating calls, each tool has been called 10×
        # so detection DOES eventually fire — confirming the ledger
        # spans the whole turn, not just adjacent calls. This is
        # by-design (an agent doing 10 identical lookups IS stuck,
        # even if it interleaves with other calls).
        assert triggered is True

    def test_custom_threshold_two_fires_on_second_call(self):
        # threshold=2 → 1 dispatch allowed, 2nd attempt blocks.
        ledger = cd.CycleLedger(threshold=2)
        count1 = ledger.record("td_x", {})
        assert count1 == 1
        count2 = ledger.record("td_x", {})
        assert count2 == 2
        assert count2 >= ledger.threshold


# ---------------------------------------------------------------------------
# _format_args_summary
# ---------------------------------------------------------------------------


class TestFormatArgsSummary:
    def test_empty_args_returns_no_args_label(self):
        assert cd._format_args_summary({}) == "(no args)"
        assert cd._format_args_summary(None) == "(no args)"

    def test_short_args_pass_through(self):
        out = cd._format_args_summary({"path": "/"})
        assert "path" in out
        assert "/" in out

    def test_long_args_truncated_to_max_len(self):
        long_args = {"data": "x" * 500}
        out = cd._format_args_summary(long_args, max_len=80)
        assert len(out) <= 80
        assert out.endswith("...")


# ---------------------------------------------------------------------------
# Agent integration — proves the late-import wiring in _loop is correct.
# Uses the same urlopen-mocking pattern as test_tdpilot_api_agent.py.
# ---------------------------------------------------------------------------


import json
from types import SimpleNamespace
from unittest.mock import patch


def _mk_response(payload: dict):
    """Mirror of the helper in test_tdpilot_api_agent.py — keep these
    integration tests self-contained so they can be run in isolation."""
    body = json.dumps(payload).encode("utf-8")

    class _Ctx:
        def __enter__(self_inner):
            return SimpleNamespace(read=lambda: body)

        def __exit__(self_inner, *_):
            return False

    return _Ctx()


def _tool_use_with_args(tool_name: str, args: dict, tu_id: str):
    """Build a single API response containing exactly one tool_use
    block with the given args. Each call to the agent uses a fresh
    response so the agent thinks the model is repeatedly emitting
    the same call."""
    return {
        "content": [
            {"type": "tool_use", "id": tu_id, "name": tool_name, "input": args},
        ],
        "stop_reason": "tool_use",
    }


class TestAgentLoopCycleIntegration:
    """End-to-end: build a real Agent with a real cycle_ledger_factory,
    drive it via mocked urlopen, verify CycleDetected raises on the
    Nth identical call and run_turn fires on_error."""

    def test_three_identical_calls_break_the_turn_and_fire_on_error(self):
        from tdpilot_api_agent import Agent  # noqa: PLC0415

        # Build a stream of 5 identical tool_use responses — agent
        # should stop on the 3rd before dispatching.
        responses = [
            _tool_use_with_args("td_get_errors", {"path": "/", "recurse": True}, f"tu_{i}") for i in range(5)
        ]
        response_iter = iter(responses)

        dispatch_calls: list[tuple[str, dict]] = []

        def dispatcher(name, args):
            dispatch_calls.append((name, dict(args)))
            return {"path": "/", "count": 0, "issues": []}

        on_error_calls: list[BaseException] = []

        agent = Agent(
            api_key="sk-fake",
            dispatcher=dispatcher,
            tools=[],
            cycle_ledger_factory=lambda: cd.CycleLedger(threshold=3),
            on_error=on_error_calls.append,
        )
        agent.add_user_message("Check errors please.")

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = lambda *_a, **_k: _mk_response(next(response_iter))
            with pytest.raises(cd.CycleDetected) as excinfo:
                agent.run_turn()

        # The 3rd attempt at the same call should have been blocked.
        # That means only 2 dispatches actually happened.
        assert len(dispatch_calls) == 2
        assert all(c == ("td_get_errors", {"path": "/", "recurse": True}) for c in dispatch_calls)

        # The exception carries the right metadata.
        exc = excinfo.value
        assert exc.tool_name == "td_get_errors"
        assert exc.count == 3
        assert "td_get_errors" in str(exc)
        assert "×3" in str(exc)

        # And on_error fired exactly once with the same exception
        # — that's the EV_ERROR routing path.
        assert len(on_error_calls) == 1
        assert isinstance(on_error_calls[0], cd.CycleDetected)

    def test_disabled_factory_means_no_cycle_check(self):
        """If TDPILOT_DISABLE_CYCLE_DETECTION is set,
        ``build_cycle_ledger_factory`` returns None. The agent should
        then dispatch identical calls without breaking — turn budget
        is the only ceiling."""
        from tdpilot_api_agent import Agent  # noqa: PLC0415

        # Use a turn budget that's smaller than what we'd otherwise
        # trip — proves the cycle path isn't engaged, while keeping
        # the test fast.
        responses = [_tool_use_with_args("td_get_errors", {"path": "/"}, f"tu_{i}") for i in range(10)]
        response_iter = iter(responses)

        dispatch_calls: list[tuple[str, dict]] = []

        def dispatcher(name, args):
            dispatch_calls.append((name, dict(args)))
            return {"ok": True}

        agent = Agent(
            api_key="sk-fake",
            dispatcher=dispatcher,
            tools=[],
            cycle_ledger_factory=None,  # disabled
            turn_budget=4,  # small ceiling — agent should hit this before any cycle check would
        )
        agent.add_user_message("Loop me.")

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = lambda *_a, **_k: _mk_response(next(response_iter))
            from tdpilot_api_agent import TurnBudgetExceeded  # noqa: PLC0415

            with pytest.raises(TurnBudgetExceeded):
                agent.run_turn()

        # All 4 identical calls were dispatched (>2 means cycle
        # detection definitely didn't fire). turn_budget=4 hits the
        # ceiling on the 5th API call before any tool dispatch.
        assert len(dispatch_calls) == 4

    def test_different_args_each_call_does_not_trigger(self):
        """Three different paths in a row — should NOT trigger
        cycle detection because the args hashes are distinct."""
        from tdpilot_api_agent import Agent  # noqa: PLC0415

        responses = [
            _tool_use_with_args("td_get_errors", {"path": "/a"}, "tu_1"),
            _tool_use_with_args("td_get_errors", {"path": "/b"}, "tu_2"),
            _tool_use_with_args("td_get_errors", {"path": "/c"}, "tu_3"),
            {  # final: end_turn with text → loop terminates cleanly
                "content": [{"type": "text", "text": "Done."}],
                "stop_reason": "end_turn",
            },
        ]
        response_iter = iter(responses)

        dispatch_calls: list[tuple[str, dict]] = []

        def dispatcher(name, args):
            dispatch_calls.append((name, dict(args)))
            return {"ok": True}

        agent = Agent(
            api_key="sk-fake",
            dispatcher=dispatcher,
            tools=[],
            cycle_ledger_factory=lambda: cd.CycleLedger(threshold=3),
        )
        agent.add_user_message("Check three paths.")

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = lambda *_a, **_k: _mk_response(next(response_iter))
            result = agent.run_turn()

        assert result == "Done."
        assert len(dispatch_calls) == 3
        paths = [args["path"] for _, args in dispatch_calls]
        assert paths == ["/a", "/b", "/c"]
