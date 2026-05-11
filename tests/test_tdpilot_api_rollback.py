"""Tests for tdpilot_api_rollback — Phase 1.1 auto-rollback feature.

Covers the pure-Python core (predicate, diff, batch classification,
env-var gate) and the guard's behaviour against a recorded mock
dispatcher. No TouchDesigner is required — the cook-thread handlers
``handle_auto_rollback_*`` are exercised via mocks because their
real bodies depend on ``ui.undo``, which only exists inside TD.
"""

from __future__ import annotations

import pytest

# The chat-pipe module sits under td_component/ and is imported as a
# top-level module (TD textDATs load it without a package prefix). The
# tests/conftest.py prepends td_component/ to sys.path so the
# unqualified ``import tdpilot_api_rollback`` resolves under pytest.
import tdpilot_api_rollback as ar  # noqa: E402

# ---------------------------------------------------------------------------
# Predicate — is_critical_error
# ---------------------------------------------------------------------------


class TestIsCriticalError:
    @pytest.mark.parametrize(
        "msg",
        [
            "SyntaxError: invalid syntax",
            "IndentationError: unexpected indent",
            "Expression Error: invalid token",
            "Invalid expression at column 4",
            "Compile error: undeclared identifier",
            "Failed to compile fragment shader",
            "shader error in main()",
            "Script Error: NameError at module load",
            "parse error near `:`",
            # Mixed case shouldn't matter — predicate lower-cases first.
            "SYNTAXERROR: foo",
            "Compile Error\nLine 12: undeclared identifier",
        ],
    )
    def test_known_critical_patterns_fire(self, msg):
        assert ar.is_critical_error(msg) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "",
            None,
            "Missing file: /tmp/foo.png",
            "Warning: deprecated parameter",
            "TOP cook took 21ms",
            "Connection refused on UDP port 9000",
            "RuntimeError during cook",  # runtime, not load-time — non-critical by design
            "ZeroDivisionError",  # same — runtime, NOT a compile-class signal
            "Could not find operator at path /project1/foo",
        ],
    )
    def test_non_critical_messages_pass(self, msg):
        assert ar.is_critical_error(msg) is False

    def test_falsy_inputs_are_non_critical(self):
        assert ar.is_critical_error("") is False
        assert ar.is_critical_error(None) is False
        assert ar.is_critical_error(0) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# count_critical_in_issues
# ---------------------------------------------------------------------------


class TestCountCriticalInIssues:
    def test_empty_input(self):
        assert ar.count_critical_in_issues([]) == {"count": 0, "by_node": []}
        assert ar.count_critical_in_issues(None) == {"count": 0, "by_node": []}

    def test_mixed_critical_and_non_critical(self):
        issues = [
            {"path": "/a", "name": "a", "errors": "SyntaxError: bad code"},
            {"path": "/b", "name": "b", "errors": "Missing file: x.png"},
            {"path": "/c", "name": "c", "errors": "Compile error: undeclared identifier"},
        ]
        result = ar.count_critical_in_issues(issues)
        assert result["count"] == 2
        paths = [item["path"] for item in result["by_node"]]
        assert paths == ["/a", "/c"]

    def test_truncates_long_error_preview(self):
        long_err = "SyntaxError: " + "x" * 500
        result = ar.count_critical_in_issues([{"path": "/a", "name": "a", "errors": long_err}])
        assert result["count"] == 1
        assert len(result["by_node"][0]["error_preview"]) <= 120

    def test_handles_malformed_issue_entries(self):
        # Non-dict entries shouldn't blow up the counter — defensive.
        issues = [
            "not a dict",
            None,
            {"path": "/a", "name": "a", "errors": "SyntaxError: foo"},
        ]
        result = ar.count_critical_in_issues(issues)  # type: ignore[arg-type]
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# diff_errors
# ---------------------------------------------------------------------------


def _err_doc(issues):
    """Helper — build a td_get_errors-shaped dict from a list of issue dicts."""
    return {"path": "/", "recurse": True, "count": len(issues), "issues": issues}


class TestDiffErrors:
    def test_no_new_criticals_when_current_clean(self):
        baseline = _err_doc([{"path": "/a", "name": "a", "errors": "Compile error: x"}])
        current = _err_doc([])
        out = ar.diff_errors(baseline, current)
        assert out["count"] == 0
        assert out["new_criticals"] == []

    def test_pre_existing_critical_is_not_new(self):
        # Same critical present in both — not a regression.
        baseline = _err_doc([{"path": "/a", "name": "a", "errors": "Compile error: x"}])
        current = _err_doc([{"path": "/a", "name": "a", "errors": "Compile error: x"}])
        out = ar.diff_errors(baseline, current)
        assert out["count"] == 0

    def test_genuinely_new_critical_is_flagged(self):
        baseline = _err_doc([])
        current = _err_doc([{"path": "/b", "name": "b", "errors": "SyntaxError: bad"}])
        out = ar.diff_errors(baseline, current)
        assert out["count"] == 1
        assert out["new_criticals"][0]["path"] == "/b"

    def test_non_critical_in_current_does_not_count(self):
        baseline = _err_doc([])
        current = _err_doc([{"path": "/c", "name": "c", "errors": "Missing file: foo"}])
        out = ar.diff_errors(baseline, current)
        assert out["count"] == 0

    def test_handles_none_baseline(self):
        # If baseline capture failed (None), everything critical in
        # current looks "new" — degraded mode by design.
        current = _err_doc([{"path": "/a", "name": "a", "errors": "Compile error"}])
        out = ar.diff_errors(None, current)
        assert out["count"] == 1


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------


class TestEnvVarGate:
    def test_unset_is_enabled(self):
        assert ar.is_disabled_via_env({}) is False

    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", " on "])
    def test_truthy_values_disable(self, val):
        assert ar.is_disabled_via_env({ar.ENV_DISABLE: val}) is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "anything-else"])
    def test_non_truthy_values_keep_enabled(self, val):
        assert ar.is_disabled_via_env({ar.ENV_DISABLE: val}) is False


# ---------------------------------------------------------------------------
# batch_should_be_guarded
# ---------------------------------------------------------------------------


class TestBatchShouldBeGuarded:
    def test_pure_read_batch_is_skipped(self):
        guarded, reason = ar.batch_should_be_guarded(["td_get_nodes", "td_get_params"])
        assert guarded is False
        assert reason == "pure-read batch"

    def test_mutation_batch_is_guarded(self):
        guarded, reason = ar.batch_should_be_guarded(["td_create_node", "td_set_params"])
        assert guarded is True
        assert reason is None

    def test_exec_python_in_batch_stands_down(self):
        guarded, reason = ar.batch_should_be_guarded(["td_create_node", "td_exec_python"])
        assert guarded is False
        assert reason == "non-undoable tool in batch"

    def test_empty_batch_is_skipped(self):
        guarded, _ = ar.batch_should_be_guarded([])
        assert guarded is False


# ---------------------------------------------------------------------------
# AutoRollbackGuard — end-to-end with a recorded mock dispatcher
# ---------------------------------------------------------------------------


class MockDispatcher:
    """Records every dispatch call; returns canned responses by tool name."""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses: dict[str, list] = dict(responses or {})

    def __call__(self, tool_name, args):
        self.calls.append((tool_name, dict(args or {})))
        if tool_name in self.responses and self.responses[tool_name]:
            return self.responses[tool_name].pop(0)
        return {"ok": True}

    def tools_called(self):
        return [c[0] for c in self.calls]


class TestAutoRollbackGuard:
    def test_pure_read_batch_is_a_noop(self):
        d = MockDispatcher()
        guard = ar.AutoRollbackGuard(d, ["td_get_nodes"])
        with guard:
            pass
        assert guard.guarded is False
        assert guard.rollback_fired is False
        # No dispatcher calls — pure-read skip.
        assert d.calls == []

    def test_clean_mutation_batch_closes_block_without_rollback(self):
        d = MockDispatcher(
            responses={
                "td_get_errors": [_err_doc([]), _err_doc([])],
                "auto_rollback_begin": [{"ok": True, "name": "tdpilot_auto_rollback"}],
                "auto_rollback_end": [{"ok": True, "rolled_back": False}],
            }
        )
        guard = ar.AutoRollbackGuard(d, ["td_create_node"])
        with guard:
            pass
        assert guard.guarded is True
        assert guard.rollback_fired is False
        assert guard.new_critical_count == 0
        # Order: baseline → begin → (batch) → current → end-without-undo.
        assert d.tools_called() == [
            "td_get_errors",
            "auto_rollback_begin",
            "td_get_errors",
            "auto_rollback_end",
        ]
        # Last call's args MUST request no undo on the clean path.
        assert d.calls[-1][1].get("undo") is False

    def test_regression_triggers_rollback(self):
        baseline = _err_doc([])
        current = _err_doc([{"path": "/p1/x", "name": "x", "errors": "Compile error: foo"}])
        d = MockDispatcher(
            responses={
                "td_get_errors": [baseline, current],
                "auto_rollback_begin": [{"ok": True, "name": "tdpilot_auto_rollback"}],
                "auto_rollback_end": [{"ok": True, "rolled_back": True}],
            }
        )
        guard = ar.AutoRollbackGuard(d, ["td_create_node"])
        with guard:
            pass
        assert guard.rollback_fired is True
        assert guard.new_critical_count == 1
        assert "tdpilot_auto_rollback" in guard.hint_text
        assert "/p1/x" in guard.hint_text
        # auto_rollback_end was called with undo=True.
        assert d.calls[-1][1].get("undo") is True

    def test_baseline_capture_failure_degrades_to_noop(self):
        class FailingDispatcher:
            def __call__(self, name, args):
                if name == "td_get_errors":
                    raise RuntimeError("simulated TD disconnect")
                return {"ok": True}

        guard = ar.AutoRollbackGuard(FailingDispatcher(), ["td_create_node"])
        with guard:
            pass
        # We don't claim to guard if we couldn't take a baseline.
        assert guard.guarded is False
        assert "baseline capture failed" in (guard.skip_reason or "")
        assert guard.rollback_fired is False

    def test_undo_block_failure_still_emits_hint_on_regression(self):
        # If auto_rollback_begin returns an error (e.g. running outside TD)
        # we still capture baseline + diff and surface a hint, just without
        # claiming rollback happened.
        baseline = _err_doc([])
        current = _err_doc([{"path": "/p1/x", "name": "x", "errors": "SyntaxError"}])
        d = MockDispatcher(
            responses={
                "td_get_errors": [baseline, current],
                "auto_rollback_begin": [{"error": "ui not available"}],
            }
        )
        guard = ar.AutoRollbackGuard(d, ["td_create_node"])
        with guard:
            pass
        assert guard.rollback_fired is False  # block wasn't open, didn't roll back
        assert guard.new_critical_count == 1
        assert "could not be applied" in guard.hint_text

    def test_exception_inside_block_still_runs_post_check(self):
        # Even if the wrapped batch raises, __exit__ runs and the
        # undo block gets closed. The exception is re-raised.
        d = MockDispatcher(
            responses={
                "td_get_errors": [_err_doc([]), _err_doc([])],
                "auto_rollback_begin": [{"ok": True}],
                "auto_rollback_end": [{"ok": True, "rolled_back": False}],
            }
        )
        guard = ar.AutoRollbackGuard(d, ["td_create_node"])
        with pytest.raises(ValueError):
            with guard:
                raise ValueError("simulated dispatcher crash")
        # auto_rollback_end was still called — the block got closed.
        assert "auto_rollback_end" in d.tools_called()

    def test_exec_python_batch_skipped_even_with_other_mutations(self):
        d = MockDispatcher()
        guard = ar.AutoRollbackGuard(d, ["td_create_node", "td_exec_python"])
        with guard:
            pass
        assert guard.guarded is False
        assert guard.skip_reason == "non-undoable tool in batch"
        # Should NOT have called the dispatcher at all.
        assert d.calls == []


# ---------------------------------------------------------------------------
# format_hint
# ---------------------------------------------------------------------------


class TestFormatHint:
    def test_hint_with_few_items(self):
        diff = {
            "count": 2,
            "new_criticals": [
                {"path": "/a", "name": "a", "error_preview": "Compile error: x"},
                {"path": "/b", "name": "b", "error_preview": "SyntaxError: y"},
            ],
        }
        out = ar.format_hint(diff, rolled_back=True)
        assert "2 new critical" in out
        assert "/a" in out
        assert "/b" in out
        assert "reverted" in out

    def test_hint_with_many_items_truncates(self):
        diff = {
            "count": 7,
            "new_criticals": [
                {"path": f"/n{i}", "name": f"n{i}", "error_preview": "Compile error"} for i in range(7)
            ],
        }
        out = ar.format_hint(diff)
        assert "+4 more" in out  # only first 3 named, +4 elided

    def test_hint_when_rollback_could_not_apply(self):
        diff = {
            "count": 1,
            "new_criticals": [{"path": "/a", "name": "a", "error_preview": "Compile error"}],
        }
        out = ar.format_hint(diff, rolled_back=False)
        assert "could not be applied" in out
        assert "remain" in out


# ---------------------------------------------------------------------------
# Internal handlers — verify they fail cleanly outside TD
# ---------------------------------------------------------------------------


class TestInternalHandlers:
    def test_begin_handler_reports_missing_ui_outside_td(self):
        # Outside TD, the `ui` global doesn't exist — handler should return
        # an error dict rather than raising NameError.
        out = ar.handle_auto_rollback_begin({})
        assert "error" in out
        assert "ui not available" in out["error"]

    def test_end_handler_reports_missing_ui_outside_td(self):
        out = ar.handle_auto_rollback_end({"undo": True})
        assert "error" in out
        assert "ui not available" in out["error"]

    def test_begin_handler_accepts_custom_block_name(self):
        # The body.name path doesn't reach ui.undo before the NameError
        # bails, but we can still confirm the handler doesn't crash on
        # the body shape.
        out = ar.handle_auto_rollback_begin({"name": "my_custom_block"})
        # Either way we shouldn't raise.
        assert isinstance(out, dict)
