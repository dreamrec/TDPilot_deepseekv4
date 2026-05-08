"""Regression-detection guard for the thinking-block echo contract.

The mock-driven eval suite asserts ``server.thinking_violations() == []``
after every run. That assertion is only meaningful if it can actually
fail when the agent regresses. This file proves the detection works:
it monkey-patches ``_strip_reasoning`` to wrongly strip thinking blocks,
runs a multi-turn eval, and asserts the mock returned an HTTP 400 + a
violation entry.

Why this matters: ``feedback_deepseek_thinking_blocks_must_echo`` is
load-bearing memory — without it, future contributors might "clean up"
thinking blocks between turns and break production. This test is the
backstop that fires before that lands in main.
"""

from __future__ import annotations

import pytest
import tdpilot_api_agent as agent_mod

from agent_evals_mock._eval_harness import run_mock_eval


def test_stripping_thinking_blocks_is_caught_by_mock(mock_deepseek, monkeypatch):
    """Sabotage ``_strip_reasoning`` to drop thinking blocks → the mock
    must surface a 400 + a violation entry, proving the regression
    detector works."""

    def _broken_strip(blocks):
        # Aggressive (broken) strip — drops thinking blocks. This is the
        # exact regression we want the eval suite to catch.
        out = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") in (
                "thinking",
                "redacted_thinking",
            ):
                continue
            out.append(block)
        return out

    monkeypatch.setattr(agent_mod, "_strip_reasoning", _broken_strip)

    # build_create_node has 4 exchanges with thinking blocks in turns
    # 2/3/4 — perfect for catching multi-turn echo regressions.
    server = mock_deepseek("build_create_node")
    result = run_mock_eval(
        server,
        prompt=(
            "Create a noiseTOP at /project1 named eval_test_noise. "
            "Validate after with td_get_errors. "
            "Then delete it again so the project state is clean."
        ),
    )

    violations = server.thinking_violations()
    assert violations, (
        "Mock did not flag any thinking-block violations even though "
        "_strip_reasoning was sabotaged to drop them. The regression "
        "detector is broken — fix _verify_thinking_echo in "
        "tests/_mock_deepseek.py."
    )
    # The agent should have surfaced the 400 as an AgentError.
    assert result.error is not None, (
        "Mock returned 400 but the agent didn't fail — the strip-reasoning "
        "contract test is no longer load-bearing."
    )
    assert "400" in str(result.error), f"agent error didn't reference HTTP 400; got: {result.error!r}"
