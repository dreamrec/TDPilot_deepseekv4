"""Shared helpers for mock-driven agent evals (PR-20).

Mirrors ``tests/agent_evals/conftest.py`` (the live integration helpers)
but drives the Agent class directly with a fixture-backed mock instead
of going through the standalone webserver. No live TD required.

Usage in a test::

    from tests.agent_evals_mock._eval_harness import run_mock_eval

    def test_inspect_basic_fps(mock_deepseek):
        result = run_mock_eval(mock_deepseek("inspect_basic_fps"),
                               prompt="What's the FPS?")
        assert "60" in result.final_text
        assert "td_get_info" in result.tool_call_names()
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from _mock_dispatcher import default_tools_for_capture, stub_dispatcher

# tdpilot_api_agent and the mock dispatcher are on sys.path via conftest.
from tdpilot_api_agent import Agent


@dataclass
class MockEvalResult:
    """Captured surface of a mock-driven agent run.

    Mirrors the assertion surface of the live agent_evals conftest's
    ``run_eval_turn`` return value (a list of transcript rows), but
    structured for in-process inspection rather than HTTP polling.
    """

    final_text: str
    text_chunks: list[str]
    tool_calls: list[tuple[str, dict]]
    tool_results: list[tuple[str, Any, bool]]
    error: BaseException | None = None
    turn_done_at: list[str] = field(default_factory=list)

    def tool_call_names(self) -> list[str]:
        return [n for n, _ in self.tool_calls]

    def assert_no_error(self) -> None:
        assert self.error is None, f"agent emitted error: {self.error!r}"

    def assert_tool_called(self, expected: str) -> None:
        assert expected in self.tool_call_names(), (
            f"expected {expected!r} in tool calls; saw: {self.tool_call_names()}"
        )

    def assert_text_contains(self, needle: str, *, case_insensitive: bool = True) -> None:
        haystack = self.final_text
        if case_insensitive:
            ok = needle.lower() in haystack.lower()
        else:
            ok = needle in haystack
        assert ok, f"final text did not contain {needle!r}; got:\n{haystack[-400:]}"


def run_mock_eval(
    server,
    *,
    prompt: str | list[str],
    system_prompt: str = ("You are TDPilot, an assistant inside TouchDesigner. Use tools when needed."),
    tools: list[dict] | None = None,
    model: str = "deepseek-v4-pro",
    model_tier: str = "auto",
) -> MockEvalResult:
    """Run the agent against a started ``MockDeepSeek`` and return the
    observed surface.

    ``prompt`` may be a single string (one user turn) or a list (multi-
    turn). The same agent is reused across all prompts so the mock's
    sequence of exchanges flows naturally.
    """
    prompts = [prompt] if isinstance(prompt, str) else list(prompt)

    text_chunks: list[str] = []
    tool_calls: list[tuple[str, dict]] = []
    tool_results: list[tuple[str, Any, bool]] = []
    turn_done: list[str] = []
    captured_error: list[BaseException] = []

    agent = Agent(
        api_key="sk-mock",
        dispatcher=stub_dispatcher,
        tools=tools or default_tools_for_capture(),
        system_prompt=system_prompt,
        base_url=server.base_url,
        model=model,
        model_tier=model_tier,
        on_text=text_chunks.append,
        on_tool_call=lambda n, a: tool_calls.append((n, a)),
        on_tool_result=lambda n, r, e: tool_results.append((n, r, e)),
        on_turn_done=turn_done.append,
        on_error=captured_error.append,
    )

    for p in prompts:
        agent.add_user_message(p)
        try:
            agent.run_turn()
        except BaseException as exc:  # noqa: BLE001
            captured_error.append(exc)
            break

    return MockEvalResult(
        final_text="\n".join(text_chunks),
        text_chunks=list(text_chunks),
        tool_calls=list(tool_calls),
        tool_results=list(tool_results),
        error=captured_error[0] if captured_error else None,
        turn_done_at=list(turn_done),
    )
