"""Mock-driven mirror of ``tests/agent_evals/test_build.py`` (PR-20).

Note: ``test_build_no_validation_emits_hint`` from the live suite
exercises the AgentRuntime's ``_maybe_emit_validation_hint`` event,
not the Agent class proper. That hint emission lives at the runtime
layer (see runtime.py:658 — fired in on_turn_done) and isn't
reachable from the Agent-class harness used here. Coverage for it
stays in the live suite; this mock port covers the create+validate+
delete happy path only.
"""

from __future__ import annotations

from agent_evals_mock._eval_harness import run_mock_eval


def test_build_create_node_mock(mock_deepseek):
    """Create a noiseTOP, validate with td_get_errors, then delete it."""
    server = mock_deepseek("build_create_node")
    result = run_mock_eval(
        server,
        prompt=(
            "Create a noiseTOP at /project1 named eval_test_noise. "
            "Validate after with td_get_errors. "
            "Then delete it again so the project state is clean."
        ),
    )
    result.assert_no_error()
    names = result.tool_call_names()
    for expected in ("td_create_node", "td_get_errors", "td_delete_node"):
        assert expected in names, f"expected {expected} in tool calls; saw: {names}"
    result.assert_text_contains("eval_test_noise")
    assert server.thinking_violations() == []
