"""Mock-replay scenarios for the v2.5.2/v2.5.3 cycle-detect bug class.

These are integration-level behavioral tests that run the full Agent loop
against a captured-DeepSeek fixture, complementing the unit-level coverage
in:

  * tests/test_v252_cycle_detect_orphan_tool_use.py (5 tests pinning the
    v2.5.2 orphan-tool_use synthesis invariant + v2.5.3 rollback-hint
    preservation)
  * tests/test_tdpilot_api_cycle_detector.py (ledger / args-hash /
    threshold unit tests)
  * tests/test_tdpilot_api_rollback.py (rollback-guard unit tests)

Status: SCAFFOLDED, fixture capture deferred. The audit-fixes 2026-05-19
v2.5.4 hardening release added these scenarios as the recommended
end-to-end behavioral pin for the v2.5.x patch cascade (per
docs/plans/AUDIT_2026_05_19_FOLLOWUPS.md Section C). They are marked
@pytest.mark.skip pending fixture capture against the live DeepSeek API.

To capture the missing fixtures:
  1. Get a DeepSeek API key (https://platform.deepseek.com/) with
     v4-pro access enabled.
  2. Export DEEPSEEK_API_KEY=sk-... in your shell.
  3. For each scenario below, run:
       uv run python scripts/capture_deepseek_fixtures.py cycle_detect_three_strikes
       uv run python scripts/capture_deepseek_fixtures.py cycle_detect_rollback_hint
       uv run python scripts/capture_deepseek_fixtures.py alias_dispatch_td_get_traces
  4. The recorder writes tests/fixtures/deepseek/<scenario>.json.
  5. Remove the skip decorator from the matching test below.

After capture, these tests run in regular CI without TouchDesigner.
"""

from __future__ import annotations

import pytest

from agent_evals_mock._eval_harness import run_mock_eval

_FIXTURE_PENDING = "fixture capture deferred to a follow-up — see module docstring for how to capture"


@pytest.mark.skip(reason=_FIXTURE_PENDING)
def test_cycle_detect_three_strikes_mock(mock_deepseek):
    """The v2.5.x patch cascade root scenario, behaviorally pinned.

    Agent prompted to call td_get_info 3 times; the mock replays a
    sequence where the LLM repeats td_get_info with identical empty
    args. After turn 3, CycleDetected must raise AND the persisted
    agent.messages must contain a paired synthetic tool_result for the
    offending tool_use_id (v2.5.2 fix). Pre-fix, a follow-up /send
    would HTTP 400 on Anthropic-format /v1/messages.
    """
    server = mock_deepseek("cycle_detect_three_strikes")
    result = run_mock_eval(
        server,
        prompt="What is the project info? Try three times if needed.",
    )
    assert result.cycle_detected, "Expected CycleDetected on the 3rd identical tool call"
    result.assert_no_orphan_tool_use_ids()
    last_result = result.last_tool_result_block()
    assert last_result.get("is_error") is True
    assert "cycle_detected" in str(last_result.get("content", "")).lower()


@pytest.mark.skip(reason=_FIXTURE_PENDING)
def test_cycle_detect_preserves_rollback_hint_mock(mock_deepseek):
    """The v2.5.3 Codex-caught follow-on: when cycle-detect fires
    mid-batch and an earlier mutation in the same batch was rolled
    back, the synthetic tool_result must carry the rollback-hint text
    so the next turn sees BOTH the cycle-detect error AND the
    'mutation was reverted' advisory.
    """
    server = mock_deepseek("cycle_detect_rollback_hint")
    result = run_mock_eval(
        server,
        prompt=(
            "Try to set the seed parameter to 42. If you hit an error, "
            "try a different node and then come back to the first one."
        ),
    )
    assert result.cycle_detected
    last_result = result.last_tool_result_block()
    content = str(last_result.get("content", "")).lower()
    assert "cycle_detected" in content
    assert "rolled back" in content or "rollback" in content


@pytest.mark.skip(reason=_FIXTURE_PENDING)
def test_alias_dispatch_td_get_traces_mock(mock_deepseek):
    """The v2.5.1 chat-pipe alias: td_get_traces (public name) routes
    to handle_get_recent_traces (legacy long name). The static parity
    test (test_chat_pipe_surface_parity.py) pins that the schema +
    handler exist; this test pins that the actual dispatch goes
    through, end-to-end.
    """
    server = mock_deepseek("alias_dispatch_td_get_traces")
    result = run_mock_eval(
        server,
        prompt="Show me the last 5 trace records.",
    )
    result.assert_no_error()
    result.assert_tool_called("td_get_traces")
    assert result.final_text.strip(), "Agent must produce a non-empty reply"
