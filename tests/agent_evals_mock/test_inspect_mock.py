"""Mock-driven mirror of ``tests/agent_evals/test_inspect.py`` (PR-20).

Runs in regular CI (no ``agent_eval`` marker) by replaying captured
DeepSeek responses from ``tests/fixtures/deepseek/`` against the
real ``Agent`` class via a localhost mock server.

Capture command if a fixture goes stale::

    uv run python scripts/capture_deepseek_fixtures.py \\
        --scenario inspect_basic_fps \\
        --prompt "What's the current FPS of the project? Use td_get_info..."
"""

from __future__ import annotations

from agent_evals_mock._eval_harness import run_mock_eval


def test_inspect_basic_fps_mock(mock_deepseek):
    """The agent must call td_get_info and surface the FPS in plain text."""
    server = mock_deepseek("inspect_basic_fps")
    result = run_mock_eval(
        server,
        prompt=("What's the current FPS of the project? Use td_get_info and report the FPS in plain text."),
    )
    result.assert_no_error()
    result.assert_tool_called("td_get_info")
    result.assert_text_contains("60")
    assert server.thinking_violations() == []


def test_inspect_nodes_list_mock(mock_deepseek):
    """List the children at /project1 — expect td_get_nodes + at least
    one operator-shaped reference in the reply."""
    server = mock_deepseek("inspect_nodes_list")
    result = run_mock_eval(
        server,
        prompt="List the operators that live at /project1. Use td_get_nodes.",
    )
    result.assert_no_error()
    result.assert_tool_called("td_get_nodes")
    result.assert_text_contains("/project1")
    assert server.thinking_violations() == []
