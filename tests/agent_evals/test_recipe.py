"""Phase 4.2 — agent eval: recipe save and validate flows.

These exercise the recipe lifecycle:
  - recipe_save  — "Save a recipe called eval_test_recipe" → recipe_save,
    name echoed + success word in reply.
  - validate_passes — validate a well-formed recipe → td_validate_recipe,
    reply says valid.
  - validate_rejects_bogus_tool — validate a recipe referencing a
    non-existent tool → td_validate_recipe, reply says invalid / unknown.

Each eval resets the session, sends one prompt, waits for the turn
to complete, and asserts the tool-call sequence + reply markers.
"""

from __future__ import annotations

import pytest

from .conftest import (
    assert_no_error_event,
    assert_reply_contains,
    assert_tool_in_sequence,
    run_eval_turn,
)

pytestmark = pytest.mark.agent_eval


def test_recipe_save(base_url):
    """The agent must call recipe_save and confirm the recipe name."""
    rows = run_eval_turn(
        base_url,
        (
            "Save the following sequence as a recipe called eval_test_recipe"
            " with description 'Phase 4.2 eval marker'."
            " Replay should be a single tool call: td_get_info with empty args."
        ),
    )
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["recipe_save"])
    assert_reply_contains(rows, "eval_test_recipe")
    # At least one of these success words must appear in the reply.
    import re

    full_reply = "\n".join(r.get("message", "") for r in rows if r.get("role") == "assistant")
    success_pattern = re.compile(r"\b(saved|stored|created|success)\b", re.IGNORECASE)
    assert success_pattern.search(full_reply), (
        f"reply did not indicate success (saved/stored/created/success);"
        f" final transcript:\n{full_reply[-600:]}"
    )


def test_recipe_validate_passes(base_url):
    """The agent must call td_validate_recipe and report the recipe is valid."""
    rows = run_eval_turn(
        base_url,
        (
            "Validate this recipe and report the result:"
            " replay=[{tool: 'td_get_info', args: {}}]."
            " Just call td_validate_recipe and tell me if it's valid."
        ),
    )
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_validate_recipe"])
    # Accept "valid: true", "recipe is valid", or "valid" near "true".
    full_reply = "\n".join(r.get("message", "") for r in rows if r.get("role") == "assistant").lower()
    valid_indicated = (
        "valid: true" in full_reply
        or "recipe is valid" in full_reply
        or ("valid" in full_reply and "true" in full_reply)
    )
    assert valid_indicated, (
        f"reply did not indicate the recipe is valid; final transcript:\n{full_reply[-600:]}"
    )


def test_recipe_validate_rejects_bogus_tool(base_url):
    """The agent must call td_validate_recipe and surface the invalid tool name."""
    rows = run_eval_turn(
        base_url,
        (
            "Validate this recipe:"
            " replay=[{tool: 'fake_tool_that_does_not_exist', args: {}}]."
            " Use td_validate_recipe and tell me what's wrong."
        ),
    )
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_validate_recipe"])
    # The reply must say something is wrong — accept "invalid", "unknown",
    # or the literal bogus tool name.
    full_reply = "\n".join(r.get("message", "") for r in rows if r.get("role") == "assistant").lower()
    rejection_indicated = (
        "invalid" in full_reply or "unknown" in full_reply or "fake_tool_that_does_not_exist" in full_reply
    )
    assert rejection_indicated, (
        f"reply did not indicate the recipe is invalid or name the unknown tool;"
        f" final transcript:\n{full_reply[-600:]}"
    )
