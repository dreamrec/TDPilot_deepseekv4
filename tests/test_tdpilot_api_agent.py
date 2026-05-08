"""End-to-end tests for the standalone (in-TD) agent loop.

Mocks the DeepSeek /v1/messages endpoint via monkey-patched urlopen so
the loop runs against canned responses. Validates:
  - text-only response → on_text + on_turn_done fired, history correct
  - single tool_use → dispatcher invoked → tool_result appended →
    second call returns text → loop terminates
  - dispatcher returning {"error": ...} surfaces is_error=True
  - exhausted turn budget raises TurnBudgetExceeded
  - HTTP 4xx/5xx surfaces as AgentError via on_error
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

# Make the td_component package importable in the test process.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

from tdpilot_api_agent import (  # noqa: E402
    Agent,
    AgentError,
    TurnBudgetExceeded,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk_response(payload: dict) -> Any:
    """Build a fake urlopen() context-manager return value."""
    body = json.dumps(payload).encode("utf-8")
    fake = SimpleNamespace(read=lambda: body)
    return _CtxMgr(fake)


class _CtxMgr:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


def _text_only(text: str = "Hello, network is healthy.") -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


def _tool_use_then_text(tool_name: str, args: dict) -> list[dict]:
    return [
        {
            "content": [
                {"type": "text", "text": "Looking up state."},
                {"type": "tool_use", "id": "tu_1", "name": tool_name, "input": args},
            ],
            "stop_reason": "tool_use",
        },
        {
            "content": [{"type": "text", "text": "Done — created the noise TOP."}],
            "stop_reason": "end_turn",
        },
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_text_only_response_completes_in_one_call():
    fake = _text_only("FPS is 60.")
    calls = []

    text_seen: list[str] = []
    done_seen: list[str] = []

    def dispatcher(name, args):
        raise AssertionError("dispatcher should not be called for text-only")

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[],
        on_text=text_seen.append,
        on_turn_done=done_seen.append,
    )
    agent.add_user_message("What's the FPS?")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: (calls.append(1), _mk_response(fake))[1]
        out = agent.run_turn()

    assert out == "FPS is 60."
    assert text_seen == ["FPS is 60."]
    assert done_seen == ["FPS is 60."]
    assert len(calls) == 1
    # User + assistant turns recorded. Assert structurally rather than
    # exact-equality so an implementation change that prepends a
    # synthetic system message (or similar housekeeping) doesn't break
    # the test even though the public behaviour is unchanged.
    roles = [m["role"] for m in agent.messages]
    assert roles[0] == "user"
    assert roles[-1] == "assistant"
    assert {"user", "assistant"}.issubset(set(roles))
    # No tool messages on a text-only turn.
    assert "tool" not in roles
    assert "tool_result" not in roles


def test_tool_use_dispatched_and_result_fed_back():
    responses = _tool_use_then_text("td_create_node", {"parent_path": "/project1", "op_type": "noiseTOP"})
    calls = iter(responses)
    dispatch_args: list[tuple[str, dict]] = []

    def dispatcher(name, args):
        dispatch_args.append((name, args))
        return {"path": "/project1/noise1", "type": "noiseTOP"}

    tool_calls_seen: list[tuple[str, dict]] = []
    tool_results_seen: list[tuple[str, Any, bool]] = []

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        tools=[{"name": "td_create_node", "description": "x", "input_schema": {"type": "object"}}],
        on_tool_call=lambda n, a: tool_calls_seen.append((n, a)),
        on_tool_result=lambda n, r, e: tool_results_seen.append((n, r, e)),
    )
    agent.add_user_message("Create a noise TOP in /project1.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(calls))
        out = agent.run_turn()

    assert out == "Done — created the noise TOP."
    assert dispatch_args == [("td_create_node", {"parent_path": "/project1", "op_type": "noiseTOP"})]
    assert tool_calls_seen == [("td_create_node", {"parent_path": "/project1", "op_type": "noiseTOP"})]
    assert len(tool_results_seen) == 1
    name, result, is_error = tool_results_seen[0]
    assert name == "td_create_node"
    assert result == {"path": "/project1/noise1", "type": "noiseTOP"}
    assert is_error is False

    # Conversation: user → assistant (tool_use) → user (tool_result) → assistant (text)
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant", "user", "assistant"]
    # The tool_result block must reference the tool_use_id from the model's call.
    tool_result_block = agent.messages[2]["content"][0]
    assert tool_result_block["type"] == "tool_result"
    assert tool_result_block["tool_use_id"] == "tu_1"
    assert tool_result_block["is_error"] is False
    # Result was JSON-serialized for transport.
    assert json.loads(tool_result_block["content"]) == {
        "path": "/project1/noise1",
        "type": "noiseTOP",
    }


def test_dispatcher_error_surfaces_as_is_error():
    responses = _tool_use_then_text("td_get_errors", {"path": "/project1"})
    calls = iter(responses)

    def dispatcher(name, args):
        # v1.10.0+: emit `_tool_error: True` to flag failures (sentinel
        # is authoritative). The legacy `{"error": ...}` fallback is
        # deprecated in v1.10.0 and removed in v2.0.
        return {"_tool_error": True, "error": "node not found", "path": args["path"]}

    seen: list[tuple[str, Any, bool]] = []
    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        on_tool_result=lambda n, r, e: seen.append((n, r, e)),
    )
    agent.add_user_message("Check errors.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(calls))
        agent.run_turn()

    assert seen[0][2] is True  # is_error
    tool_result_block = agent.messages[2]["content"][0]
    assert tool_result_block["is_error"] is True


def test_dispatcher_raising_exception_is_caught():
    responses = _tool_use_then_text("td_get_info", {})
    calls = iter(responses)

    def dispatcher(name, args):
        raise RuntimeError("simulated TD failure")

    seen: list[tuple[str, Any, bool]] = []
    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        on_tool_result=lambda n, r, e: seen.append((n, r, e)),
    )
    agent.add_user_message("Status?")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(calls))
        agent.run_turn()

    assert seen[0][2] is True
    assert "simulated TD failure" in seen[0][1]["error"]


def test_turn_budget_exceeded_raises():
    """Model keeps requesting tools forever — loop must abort cleanly."""

    def make_loop_response():
        return {
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_x",
                    "name": "td_get_info",
                    "input": {},
                }
            ],
            "stop_reason": "tool_use",
        }

    def dispatcher(name, args):
        return {"ok": True}

    errors_seen: list[BaseException] = []
    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        turn_budget=2,
        on_error=errors_seen.append,
    )
    agent.add_user_message("loop please")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(make_loop_response())
        with pytest.raises(TurnBudgetExceeded):
            agent.run_turn()

    assert errors_seen and isinstance(errors_seen[0], TurnBudgetExceeded)


def test_http_error_surfaces_as_agent_error():
    import urllib.error

    def fail(*a, **k):
        raise urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized", hdrs=None, fp=io.BytesIO(b'{"error":"no key"}')
        )

    errors_seen: list[BaseException] = []
    agent = Agent(
        api_key="sk-bad",
        dispatcher=lambda *a: None,
        on_error=errors_seen.append,
    )
    agent.add_user_message("anything")

    with patch("urllib.request.urlopen", side_effect=fail):
        with pytest.raises(AgentError) as exc_info:
            agent.run_turn()

    assert "401" in str(exc_info.value)
    assert errors_seen and isinstance(errors_seen[0], AgentError)


def test_stop_flag_aborts_before_next_call():
    """Cooperative cancellation: stop() before run_turn returns None."""

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
    )
    agent.add_user_message("anything")
    agent.stop()  # set the flag before we even start

    with patch("urllib.request.urlopen") as urlopen:
        # Should never be called.
        result = agent.run_turn()

    assert result is None
    urlopen.assert_not_called()


# ---------------------------------------------------------------------------
# _strip_reasoning behaviour — locks in the correct field handling for
# DeepSeek's Anthropic-compat layer.
#
# The error this guards against:
#   HTTP 400: ``The content[].thinking in the thinking mode must be
#   passed back to the API.``
#
# Two reasoning-related artefacts have OPPOSITE handling:
#   * thinking / redacted_thinking content blocks → KEEP (echo back)
#   * reasoning_content sub-keys                  → STRIP
# ---------------------------------------------------------------------------


def test_strip_reasoning_keeps_thinking_blocks():
    """Regression: dropping thinking blocks caused 400 on every tool turn."""
    from tdpilot_api_agent import _strip_reasoning

    blocks = [
        {"type": "thinking", "thinking": "let me think...", "signature": "abc123"},
        {"type": "text", "text": "Hello"},
    ]
    out = _strip_reasoning(blocks)
    assert len(out) == 2
    assert out[0]["type"] == "thinking"
    assert out[0]["thinking"] == "let me think..."
    assert out[0]["signature"] == "abc123"  # all sub-fields preserved
    assert out[1]["type"] == "text"


def test_strip_reasoning_keeps_redacted_thinking():
    from tdpilot_api_agent import _strip_reasoning

    blocks = [{"type": "redacted_thinking", "data": "encrypted-blob"}]
    out = _strip_reasoning(blocks)
    assert len(out) == 1
    assert out[0]["type"] == "redacted_thinking"
    assert out[0]["data"] == "encrypted-blob"


def test_strip_reasoning_removes_reasoning_content_subkey():
    """The OpenAI-format reasoning_content sub-key on a block IS stripped."""
    from tdpilot_api_agent import _strip_reasoning

    blocks = [
        {"type": "text", "text": "Hi", "reasoning_content": "internal"},
        {"type": "thinking", "thinking": "ok", "reasoning_content": "leaked"},
    ]
    out = _strip_reasoning(blocks)
    assert "reasoning_content" not in out[0]
    assert out[0]["text"] == "Hi"
    # Even on a thinking block, the reasoning_content sub-key gets stripped
    # but the block ITSELF is preserved with its other fields.
    assert "reasoning_content" not in out[1]
    assert out[1]["type"] == "thinking"
    assert out[1]["thinking"] == "ok"


def test_strip_reasoning_passes_through_text_blocks_unchanged():
    from tdpilot_api_agent import _strip_reasoning

    blocks = [{"type": "text", "text": "plain"}]
    out = _strip_reasoning(blocks)
    assert out == blocks


def test_strip_reasoning_handles_tool_use_blocks():
    """Tool_use blocks are passed through untouched — they're neither
    reasoning artefacts nor strippable in any way."""
    from tdpilot_api_agent import _strip_reasoning

    blocks = [
        {"type": "tool_use", "id": "abc", "name": "td_get_info", "input": {}},
    ]
    out = _strip_reasoning(blocks)
    assert out == blocks


def test_strip_reasoning_handles_non_dict_entries():
    """Defensive: stray non-dict entries don't blow up the loop."""
    from tdpilot_api_agent import _strip_reasoning

    blocks = ["unexpected-string", {"type": "text", "text": "ok"}, None]
    out = _strip_reasoning(blocks)
    assert len(out) == 3
    assert out[0] == "unexpected-string"
    assert out[1]["type"] == "text"
    assert out[2] is None


# ---------------------------------------------------------------------------
# Sprint 4.3 — multi-model routing.
# ---------------------------------------------------------------------------


def _make_agent_for_routing(tier="auto"):
    """Build a minimal Agent for unit-testing _resolve_model only."""
    from tdpilot_api_agent import Agent

    return Agent(
        api_key="test-key",
        dispatcher=lambda n, a: {},
        tools=[],
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        model_tier=tier,
    )


def test_resolve_model_tier_pro_pins_pro():
    a = _make_agent_for_routing("pro")
    # Doesn't matter what user_text is — pinned tier wins.
    assert a._resolve_model("anything goes here") == "deepseek-v4-pro"
    assert a._resolve_model("") == "deepseek-v4-pro"


def test_resolve_model_tier_flash_pins_flash():
    a = _make_agent_for_routing("flash")
    assert a._resolve_model("build me a complex audio reactive system") == "deepseek-v4-flash"


def test_resolve_model_auto_short_lookup_uses_flash():
    a = _make_agent_for_routing("auto")
    # Short, lookup-style — score 0
    assert a._resolve_model("what is a noiseTOP") == "deepseek-v4-flash"


def test_resolve_model_auto_long_imperative_uses_pro():
    """Clearly Pro-tier text — long, imperative, code-fenced, multi-tool —
    routes to pro under the auto heuristic.

    Robustness: this text triggers ALL four scoring signals (length>300,
    pro keyword, code fence, ≥2 tool keywords). A future tightening of
    the pro threshold from 2 → 3 or even 4 still passes this test
    because the input is unambiguously Pro-tier.
    """
    a = _make_agent_for_routing("auto")
    text = (
        "Build me an audio-reactive particle system with feedback loops. "
        "Create the noiseTOP, set parameters, connect to a level. Inspect "
        "each step and screenshot the final result. Make sure the wires "
        "are clean — also fix the existing patch and refactor the GLSL. "
        "Here is the snippet to integrate:\n"
        "```python\n"
        "op('/project1/noise').par.type = 'sparse'\n"
        "```"
    )
    assert a._resolve_model(text) == "deepseek-v4-pro"


def test_resolve_model_auto_code_fence_signal_lifts_to_pro():
    """The code-fence signal demonstrably shifts routing toward pro.

    Tested as a DELTA: the same surrounding text without a fence routes
    one way, with a fence routes to pro. This is robust to threshold
    changes — what matters is that the fence is a meaningful signal.
    """
    a = _make_agent_for_routing("auto")
    base = "fix the parameter on this op so it animates correctly"
    fenced = (
        "fix the parameter on this op so it animates correctly\n"
        "```python\nop('/project1/foo').par.x = 1\n```\n"
        "and connect it to the noiseTOP, then inspect the wires"
    )
    # The fenced+richer version must route to pro (it scores high enough
    # under any reasonable threshold). The base, without fence and
    # without tool keywords, scores at most 1 — comfortably under any
    # pro threshold.
    assert a._resolve_model(fenced) == "deepseek-v4-pro"
    assert a._resolve_model(base) == "deepseek-v4-flash"


def test_resolve_model_auto_invalid_tier_falls_back_to_auto():
    """Garbage tier values normalise to 'auto'."""
    a = _make_agent_for_routing("garbage")
    # Now in auto mode, short text → flash
    assert a.model_tier == "auto"
    assert a._resolve_model("hi") == "deepseek-v4-flash"


def test_resolve_model_pinned_in_loop():
    """The Agent should pin _active_model at turn start and keep it
    stable across the whole tool-use chain (cache stability)."""
    a = _make_agent_for_routing("flash")
    # Simulate a turn — _resolve_model is called by _loop, but we test
    # the post-resolve state directly.
    a._active_model = a._resolve_model("anything")
    assert a._active_model == "deepseek-v4-flash"
    # Even if user_text would push score >=2, the tier wins.
    assert a._resolve_model("build a complex thing with code ```") == "deepseek-v4-flash"


# ---------------------------------------------------------------------------
# Phase 0.1 — cache-stable dynamic-context slot.
#
# Contract: the system prompt prefix MUST be byte-identical across every
# API call within a session so DeepSeek's auto-cache hits (~50× discount).
# Volatile per-turn state (memory / knowledge / recipes indexes) is
# delivered via dynamic_context_provider, which prepends synthetic
# messages WITHOUT mutating Agent.messages.
# ---------------------------------------------------------------------------


def _capture_request_body(captured: list[dict]):
    """urlopen side-effect that snapshots each Request body before
    returning a canned text-only response.
    """

    counter = {"n": 0}

    def side_effect(req, *a, **k):
        counter["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        captured.append(body)
        return _mk_response(_text_only(f"reply {counter['n']}"))

    return side_effect


def test_system_prompt_byte_stable_across_turns():
    """Phase 0.1 acceptance test.

    Run 5 consecutive turns, each with a dynamic context that produces
    DIFFERENT bytes per turn. SHA-256 of the system prompt sent to the
    API must be identical across all 5.
    """
    import hashlib

    counter = {"n": 0}

    def dyn_provider():
        counter["n"] += 1
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"[[TDPILOT_CONTEXT]] turn={counter['n']} mem=foo{counter['n']}",
                    }
                ],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Acknowledged."}]},
        ]

    captured: list[dict] = []
    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE BASE PROMPT v1\n\nLine two for realism.",
        dynamic_context_provider=dyn_provider,
    )

    for i in range(5):
        agent.add_user_message(f"turn {i} question")
        with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
            agent.run_turn()

    assert len(captured) == 5
    sys_hashes = {hashlib.sha256(b["system"].encode("utf-8")).hexdigest() for b in captured}
    assert len(sys_hashes) == 1, f"system prompt drifted across 5 turns: {sys_hashes}"


def test_dynamic_context_prepended_to_each_api_call():
    """Each API call's messages array starts with the provider's output."""
    captured: list[dict] = []

    counter = {"n": 0}

    def dyn_provider():
        counter["n"] += 1
        return [
            {
                "role": "user",
                "content": [{"type": "text", "text": f"[[TDPILOT_CONTEXT]] turn {counter['n']}"}],
            },
            {"role": "assistant", "content": [{"type": "text", "text": "Acknowledged."}]},
        ]

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
        dynamic_context_provider=dyn_provider,
    )

    agent.add_user_message("first")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    agent.add_user_message("second")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    # Each request had ctx user + ctx assistant prepended to the real
    # conversation. After turn 1, agent.messages = [user, assistant];
    # request 1 = [ctx_u, ctx_a, user1, ...] = at least 3 entries.
    for body in captured:
        msgs = body["messages"]
        assert msgs[0]["role"] == "user"
        first_text = msgs[0]["content"][0]["text"]
        assert first_text.startswith("[[TDPILOT_CONTEXT]]")
        assert msgs[1]["role"] == "assistant"

    # Per-turn content varies even though system prompt does not.
    first_ctx = captured[0]["messages"][0]["content"][0]["text"]
    second_ctx = captured[1]["messages"][0]["content"][0]["text"]
    assert first_ctx != second_ctx


def test_dynamic_context_not_persisted_in_history():
    """The provider's output must NOT mutate ``self.messages``.

    Otherwise the conversation history grows with stale context every
    turn and we lose the cache benefit of a clean history.
    """

    def dyn_provider():
        return [
            {"role": "user", "content": [{"type": "text", "text": "[[TDPILOT_CONTEXT]] foo"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "Acknowledged."}]},
        ]

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
        dynamic_context_provider=dyn_provider,
    )

    agent.add_user_message("hi")
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(_text_only("ok"))
        agent.run_turn()

    # Expected: just the real user/assistant pair, NO ctx blocks.
    roles = [m["role"] for m in agent.messages]
    assert roles == ["user", "assistant"]
    user_text = agent.messages[0]["content"][0]["text"]
    assert user_text == "hi"
    assistant_text = agent.messages[1]["content"][0]["text"]
    assert "TDPILOT_CONTEXT" not in assistant_text


def test_dynamic_context_provider_none_is_noop():
    """Backwards-compat: agent without a provider sends only its own
    messages — pre-Phase-0.1 behaviour preserved."""
    captured: list[dict] = []

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
    )
    assert agent.dynamic_context_provider is None

    agent.add_user_message("hi")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    msgs = captured[0]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"][0]["text"] == "hi"


def test_dynamic_context_provider_exception_degrades_gracefully():
    """A buggy provider must not crash the agent — it logs and yields []."""
    captured: list[dict] = []

    def bad_provider():
        raise RuntimeError("provider exploded")

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
        dynamic_context_provider=bad_provider,
    )

    agent.add_user_message("hi")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    msgs = captured[0]["messages"]
    assert len(msgs) == 1  # only the user msg, ctx fell through
    assert msgs[0]["role"] == "user"


def test_dynamic_context_provider_returns_junk_filtered():
    """Non-list / malformed provider output is filtered, not propagated."""
    captured: list[dict] = []

    def junk_provider():
        # Returns a list, but with malformed entries mixed in. Only the
        # well-formed user/assistant entries should survive.
        return [
            "not-a-dict",
            {"role": "system", "content": []},  # wrong role
            {"role": "user"},  # missing content
            {"role": "user", "content": [{"type": "text", "text": "valid ctx"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
        ]

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
        dynamic_context_provider=junk_provider,
    )

    agent.add_user_message("hi")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    msgs = captured[0]["messages"]
    # user-ctx, assistant-ack, real-user
    assert len(msgs) == 3
    assert msgs[0]["content"][0]["text"] == "valid ctx"
    assert msgs[1]["content"][0]["text"] == "ack"
    assert msgs[2]["content"][0]["text"] == "hi"


def test_dynamic_context_empty_list_means_no_prefix():
    """Empty provider output skips prepending entirely."""
    captured: list[dict] = []

    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        system_prompt="STABLE",
        dynamic_context_provider=lambda: [],
    )

    agent.add_user_message("hi")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()

    msgs = captured[0]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"][0]["text"] == "hi"
