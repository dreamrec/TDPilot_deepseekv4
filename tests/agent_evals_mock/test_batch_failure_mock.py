"""Mock-driven mirror of ``tests/agent_evals/test_batch_failure.py``."""

from __future__ import annotations

from agent_evals_mock._eval_harness import run_mock_eval


def test_batch_parallel_calls_mock(mock_deepseek):
    """Agent must use tool_batch (exactly once) for three independent reads."""
    server = mock_deepseek("batch_parallel_calls")
    result = run_mock_eval(
        server,
        prompt=(
            "Use the tool_batch tool to call three things at once: "
            "td_get_info with empty args, "
            "td_get_capabilities with empty args, "
            "and td_get_errors with path=/project1. "
            "Then summarize what each returned."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("tool_batch")
    reply = result.final_text.lower()
    fps_mentioned = "fps" in reply or "60" in reply
    cap_mentioned = "capabilit" in reply or "feature" in reply
    err_mentioned = "error" in reply
    sub_call_hits = sum([fps_mentioned, cap_mentioned, err_mentioned])
    assert sub_call_hits >= 2, (
        f"reply must mention content from ≥ 2 of the 3 tool_batch sub-calls "
        f"(FPS/60, capabilit/feature, error); got {sub_call_hits}/3.\n"
        f"Tail:\n{reply[-600:]}"
    )
    assert server.thinking_violations() == []


def test_failure_recovery_hint_visible_mock(mock_deepseek):
    """Agent must surface the recovery_hint from a failed td_create_node."""
    server = mock_deepseek("failure_recovery_hint_visible")
    result = run_mock_eval(
        server,
        prompt=(
            "Try to create an operator of type 'fakeNonexistentTOP' at /project1 "
            "named eval_recovery_test. "
            "When it fails, tell me what alternative tool the error message suggests."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("td_create_node")
    reply = result.final_text.lower()
    mentioned_list_families = "td_list_families" in reply
    mentioned_search_docs = "td_search_official_docs" in reply
    assert mentioned_list_families or mentioned_search_docs, (
        "reply must mention 'td_list_families' or 'td_search_official_docs' "
        f"(recovery_hint surface); tail:\n{reply[-400:]}"
    )
    assert server.thinking_violations() == []
