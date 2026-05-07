"""Phase 4.2 — agent evals: batch dispatch and failure-recovery hint.

Two evals:
  - test_batch_parallel_calls   — Phase 2.1: agent uses tool_batch for
    three independent reads in a single call rather than three separate
    tool_use blocks.
  - test_failure_recovery_hint_visible — Phase 2.3: when td_create_node
    fails with an unknown operator type, the dispatcher attaches a
    recovery_hint field; the agent must surface the suggested alternative
    tool in its reply.

Harness auto-skips both when the webserver is not reachable (TD not
running or .tox not loaded).  Do not attempt to start TD here.
"""

from __future__ import annotations

import pytest

from .conftest import (
    assert_no_error_event,
    assert_tool_in_sequence,
    run_eval_turn,
)

pytestmark = pytest.mark.agent_eval


def test_batch_parallel_calls(base_url):
    """Agent must use tool_batch (exactly once) for three independent reads.

    Validates Phase 2.1 — parallel dispatch via tool_batch instead of
    three sequential tool_use blocks.  The reply must surface information
    from at least two of the three sub-calls so we know the agent actually
    processed all results.
    """
    prompt = (
        "Use the tool_batch tool to call three things at once: "
        "td_get_info with empty args, "
        "td_get_capabilities with empty args, "
        "and td_get_errors with path=/project1. "
        "Then summarize what each returned."
    )
    rows = run_eval_turn(base_url, prompt, timeout=90.0)

    assert_no_error_event(rows)

    # The agent must reach for tool_batch — not three separate calls.
    assert_tool_in_sequence(rows, ["tool_batch"])

    # The reply must mention content from at least 2 of the 3 sub-calls.
    # td_get_info  → "FPS" or "60"
    # td_get_capabilities → "capabilit" or "feature"
    # td_get_errors → "error"
    from .conftest import assistant_replies

    full_reply = "\n".join(assistant_replies(rows)).lower()

    fps_mentioned = "fps" in full_reply or "60" in full_reply
    cap_mentioned = "capabilit" in full_reply or "feature" in full_reply
    err_mentioned = "error" in full_reply

    sub_call_hits = sum([fps_mentioned, cap_mentioned, err_mentioned])
    assert sub_call_hits >= 2, (
        f"reply must mention content from at least 2 of the 3 tool_batch sub-calls "
        f"(FPS/60, capabilit/feature, error); got {sub_call_hits}/3. "
        f"fps={fps_mentioned} cap={cap_mentioned} err={err_mentioned}. "
        f"Tail of reply:\n{full_reply[-600:]}"
    )


def test_failure_recovery_hint_visible(base_url):
    """Agent must surface the recovery_hint from a failed td_create_node call.

    Validates Phase 2.3 — when td_create_node returns is_error=True for an
    unknown operator type, the dispatcher attaches a recovery_hint pointing
    to td_list_families or td_search_official_docs.  The agent must mention
    at least one of those tools in its reply so the user knows where to look
    next.

    Note: assert_no_error_event checks for role=="error" rows (runtime /
    agent crashes).  A tool result with is_error=True is a *tool* error, not
    an agent error — the harness never crashes; the failure is expected and
    surfaced as a tool_result.
    """
    prompt = (
        "Try to create an operator of type 'fakeNonexistentTOP' at /project1 "
        "named eval_recovery_test. "
        "When it fails, tell me what alternative tool the error message suggests."
    )
    rows = run_eval_turn(base_url, prompt, timeout=90.0)

    assert_no_error_event(rows)

    # The agent must attempt td_create_node (which will fail).
    assert_tool_in_sequence(rows, ["td_create_node"])

    # The recovery_hint should point the user toward one of these two tools.
    full_reply = "\n".join(r.get("message", "") for r in rows if r.get("role") == "assistant").lower()

    mentioned_list_families = "td_list_families" in full_reply
    mentioned_search_docs = "td_search_official_docs" in full_reply

    assert mentioned_list_families or mentioned_search_docs, (
        "reply must mention 'td_list_families' or 'td_search_official_docs' "
        "(the recovery_hint alternatives surfaced when an unknown operator type "
        "is requested). "
        f"Tail of reply:\n{full_reply[-600:]}"
    )
