"""Phase 4.2 — agent eval: build flows.

These exercise node creation, validation, and deletion:
  - test_build_create_node — create noiseTOP, validate with td_get_errors,
    delete it cleanly.
  - test_build_no_validation_emits_hint — create noiseTOP without calling
    any validator; the runtime must emit an EV_HINT(kind="missing_validation")
    visible as a hint row in the transcript.
"""

from __future__ import annotations

import pytest

from .conftest import (
    assert_no_error_event,
    assert_reply_contains,
    assert_tool_in_sequence,
    hint_messages,
    run_eval_turn,
    send_prompt,
    wait_for_turn_complete,
)

pytestmark = pytest.mark.agent_eval


def test_build_create_node(base_url):
    """Create a noiseTOP, validate with td_get_errors, then delete it."""
    rows = run_eval_turn(
        base_url,
        "Create a noiseTOP at /project1 named eval_test_noise. "
        "Validate after with td_get_errors. "
        "Then delete it again so the project state is clean.",
    )
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_create_node", "td_get_errors", "td_delete_node"])
    assert_reply_contains(rows, "eval_test_noise")


def test_build_no_validation_emits_hint(base_url):
    """Skipping td_get_errors after a create must trigger a validation hint."""
    rows = run_eval_turn(
        base_url,
        "Create a noiseTOP at /project1 named eval_test_no_val. "
        "Do NOT call td_get_errors or any validator after — just create it and stop. "
        "We're testing the validation-hint feature on purpose.",
    )
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_create_node"])

    hints = hint_messages(rows)
    keywords = ("validating", "td_get_errors", "modified the network")
    assert any(any(kw.lower() in h.lower() for kw in keywords) for h in hints), (
        f"expected a hint mentioning validation; got hints: {hints!r}\n"
        "full transcript roles: " + str([r.get("role") for r in rows])
    )

    # Cleanup — no assertions on this turn, it's housekeeping only.
    send_prompt(base_url, "Delete /project1/eval_test_no_val without validating.")
    wait_for_turn_complete(base_url)
