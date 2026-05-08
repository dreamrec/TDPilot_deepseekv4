"""Mock-driven mirror of ``tests/agent_evals/test_recipe.py`` (PR-20)."""

from __future__ import annotations

import re

from agent_evals_mock._eval_harness import run_mock_eval

_SUCCESS_WORDS = re.compile(r"\b(saved|stored|created|success)\b", re.IGNORECASE)


def test_recipe_save_mock(mock_deepseek):
    """The agent must call recipe_save and confirm the recipe name."""
    server = mock_deepseek("recipe_save")
    result = run_mock_eval(
        server,
        prompt=(
            "Save the following sequence as a recipe called eval_test_recipe"
            " with description 'Phase 4.2 eval marker'."
            " Replay should be a single tool call: td_get_info with empty args."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("recipe_save")
    result.assert_text_contains("eval_test_recipe")
    assert _SUCCESS_WORDS.search(result.final_text), (
        f"reply did not signal success; tail:\n{result.final_text[-400:]}"
    )
    assert server.thinking_violations() == []


def test_recipe_validate_passes_mock(mock_deepseek):
    """The agent must call td_validate_recipe and report the recipe is valid."""
    server = mock_deepseek("recipe_validate_passes")
    result = run_mock_eval(
        server,
        prompt=(
            "Validate this recipe and report the result:"
            " replay=[{tool: 'td_get_info', args: {}}]."
            " Just call td_validate_recipe and tell me if it's valid."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("td_validate_recipe")
    reply = result.final_text.lower()
    valid_indicated = (
        "valid: true" in reply or "recipe is valid" in reply or ("valid" in reply and "true" in reply)
    )
    assert valid_indicated, f"reply did not indicate the recipe is valid; tail:\n{reply[-400:]}"
    assert server.thinking_violations() == []


def test_recipe_validate_rejects_bogus_tool_mock(mock_deepseek):
    """The agent must call td_validate_recipe and surface the invalid tool name."""
    server = mock_deepseek("recipe_validate_rejects_bogus_tool")
    result = run_mock_eval(
        server,
        prompt=(
            "Validate this recipe:"
            " replay=[{tool: 'fake_tool_that_does_not_exist', args: {}}]."
            " Use td_validate_recipe and tell me what's wrong."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("td_validate_recipe")
    reply = result.final_text.lower()
    rejection_indicated = "invalid" in reply or "unknown" in reply or "fake_tool_that_does_not_exist" in reply
    assert rejection_indicated, f"reply did not indicate the recipe is invalid;\n{reply[-400:]}"
    assert server.thinking_violations() == []
