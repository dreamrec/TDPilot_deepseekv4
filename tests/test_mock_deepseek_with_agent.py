"""End-to-end smoke test: drive the real Agent class against the mock.

Validates that:
  - Agent + MockDeepSeek work together over real HTTP (no monkey-patching).
  - Multi-turn conversations with thinking blocks complete cleanly when
    the agent properly echoes thinking back (no 400s).
  - The mock's request capture surfaces the real outbound payload so
    fixture authoring can be data-driven.

These are the bridge tests between ``test_mock_deepseek_server.py``
(infrastructure-only) and ``test_agent_evals_mock.py`` (real-fixture-
driven user evals). They use synthesized inline fixtures.
"""

from __future__ import annotations

from typing import Any

import pytest
from _mock_deepseek import MockDeepSeek, ResponseBuilder

# tdpilot_api_agent is on sys.path via tests/conftest.py.
from tdpilot_api_agent import Agent

# ---------------------------------------------------------------------------
# Single-turn smoke
# ---------------------------------------------------------------------------


def test_agent_runs_text_only_turn_via_mock():
    exchanges = [
        {
            "request": {},
            "response": ResponseBuilder().with_text("FPS is 60.").build(),
        }
    ]
    text_seen: list[str] = []

    with MockDeepSeek.from_exchanges(exchanges) as server:
        agent = Agent(
            api_key="sk-mock",
            dispatcher=lambda n, a: (_ for _ in ()).throw(AssertionError(f"unexpected dispatch {n}")),
            tools=[],
            base_url=server.base_url,
            on_text=text_seen.append,
        )
        agent.add_user_message("What is the FPS?")
        out = agent.run_turn()

    assert out == "FPS is 60."
    assert text_seen == ["FPS is 60."]


def test_agent_dispatches_tool_use_through_mock():
    """A tool_use stop must drive the dispatcher and feed the result back."""
    exchanges = [
        {
            "request": {},
            "response": (
                ResponseBuilder()
                .with_text("Looking up FPS.")
                .with_tool_use("tu_1", "td_get_info", {})
                .build()
            ),
        },
        {
            "request": {},
            "response": ResponseBuilder().with_text("Done — FPS is 60.").build(),
        },
    ]
    dispatched: list[tuple[str, dict]] = []

    def dispatcher(name: str, args: dict) -> Any:
        dispatched.append((name, args))
        return {"fps": 60, "name": "/project1"}

    with MockDeepSeek.from_exchanges(exchanges) as server:
        agent = Agent(
            api_key="sk-mock",
            dispatcher=dispatcher,
            tools=[
                {
                    "name": "td_get_info",
                    "description": "Get project info",
                    "input_schema": {"type": "object"},
                }
            ],
            base_url=server.base_url,
        )
        agent.add_user_message("What is the FPS?")
        out = agent.run_turn()

    assert dispatched == [("td_get_info", {})]
    assert out == "Done — FPS is 60."


def test_agent_outbound_request_captured_with_load_bearing_fields():
    """The mock should observe the actual model + system + messages
    fields the agent sent. Tests can use this to assert the agent's
    outbound payload matches expectations without strict request
    matching being enabled."""
    exchanges = [
        {
            "request": {},
            "response": ResponseBuilder().with_text("ok").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        agent = Agent(
            api_key="sk-mock",
            dispatcher=lambda *_: None,
            tools=[],
            system_prompt="custom prompt",
            model="deepseek-v4-pro",
            # Force pro tier so _resolve_model doesn't downgrade the short
            # "hello there" prompt to flash via the auto heuristic.
            model_tier="pro",
            base_url=server.base_url,
        )
        agent.add_user_message("hello there")
        agent.run_turn()
        captured = server.captured_requests()

    assert len(captured) == 1
    body = captured[0].body
    assert body["model"] == "deepseek-v4-pro"
    assert body["system"] == "custom prompt"
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"][0]["text"] == "hello there"


# ---------------------------------------------------------------------------
# Multi-turn with thinking-block echo enforcement
# ---------------------------------------------------------------------------


def test_agent_echoes_thinking_blocks_across_tool_use_turn():
    """Multi-turn flow: model emits thinking + tool_use → tool result
    flows back → agent's NEXT request must carry the thinking block in
    its messages history. The mock returns 400 if it doesn't, which
    surfaces as AgentError; if all turns are 200 the agent did its job.

    This is the load-bearing test for the
    feedback_deepseek_thinking_blocks_must_echo memory: stripping
    thinking between turns is a regression and this catches it.
    """
    exchanges = [
        {
            "request": {},
            "response": (
                ResponseBuilder()
                .with_thinking("considering: I need fps + node list")
                .with_text("Looking that up.")
                .with_tool_use("tu_1", "td_get_info", {})
                .build()
            ),
        },
        {
            "request": {},
            "response": (ResponseBuilder().with_text("FPS is 60.").build()),
        },
    ]

    def dispatcher(name, args):
        return {"fps": 60}

    with MockDeepSeek.from_exchanges(exchanges) as server:
        agent = Agent(
            api_key="sk-mock",
            dispatcher=dispatcher,
            tools=[
                {
                    "name": "td_get_info",
                    "description": "x",
                    "input_schema": {"type": "object"},
                }
            ],
            base_url=server.base_url,
        )
        agent.add_user_message("What is the FPS?")
        agent.run_turn()

        assert server.thinking_violations() == [], (
            "Mock saw stripped thinking blocks — the agent regressed on "
            "the thinking-echo contract; check _strip_reasoning in "
            "tdpilot_api_agent.py"
        )

        # Belt-and-braces: confirm BOTH requests reached the mock.
        assert server.remaining() == 0, f"agent didn't drive both turns; remaining: {server.remaining()}"
