"""v2.4 / Phase C.9 — configurable thinking.budget_tokens.

When ``thinking_budget > 0``, the Agent includes
``"thinking": {"type": "enabled", "budget_tokens": N}`` in every
/v1/messages request body. 0 = disabled (legacy pre-v2.4 behaviour,
byte-stable cache prefix).

Tests pin:
  * Default constructor → thinking_budget = 0 → no thinking block.
  * thinking_budget=N > 0 → thinking block present with correct shape.
  * Negative value clamped to 0.
  * thinking block matches the byte-stable contract across turns
    (same value → same JSON serialisation).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

from tdpilot_api_agent import Agent  # noqa: E402, I001


class _CtxMgr:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


def _mk_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    fake = SimpleNamespace(read=lambda: body)
    return _CtxMgr(fake)


def _capture_request_body(captured: list[dict]):
    def _urlopen(req, *_a, **_kw):
        captured.append(json.loads(req.data.decode()))
        return _mk_response({"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})

    return _urlopen


def test_c9_default_no_thinking_block():
    """Default Agent (thinking_budget=0) MUST NOT include a thinking
    block in the request body — preserves byte-stable cache prefix
    for users who don't opt in."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None)
    agent.add_user_message("hi")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert "thinking" not in captured[0], (
        f"default agent must not send a thinking block, got body keys: {list(captured[0].keys())}"
    )


def test_c9_explicit_zero_no_thinking_block():
    """Explicit thinking_budget=0 same as default — no thinking block."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None, thinking_budget=0)
    agent.add_user_message("hi")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert "thinking" not in captured[0]


def test_c9_positive_budget_adds_thinking_block():
    """thinking_budget > 0 MUST add the correct thinking block."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None, thinking_budget=8000)
    agent.add_user_message("hi")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert "thinking" in captured[0]
    assert captured[0]["thinking"] == {"type": "enabled", "budget_tokens": 8000}


def test_c9_negative_budget_clamped_to_zero():
    """Negative budget clamped → no thinking block (safe default)."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None, thinking_budget=-100)
    assert agent.thinking_budget == 0
    agent.add_user_message("hi")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert "thinking" not in captured[0]


def test_c9_thinking_block_byte_stable_across_turns():
    """Same thinking_budget across turns → identical serialisation
    of the thinking block (cache-prefix stability)."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None, thinking_budget=12000)
    captured: list[dict] = []
    agent.add_user_message("first")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    agent.add_user_message("second")
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert captured[0]["thinking"] == captured[1]["thinking"]
    assert captured[0]["thinking"]["budget_tokens"] == 12000


def test_c9_large_budget_accepted():
    """High budget (e.g., 32K) is accepted — clamping is only on lower bound."""
    agent = Agent(api_key="sk-fake", dispatcher=lambda *a: None, thinking_budget=32000)
    assert agent.thinking_budget == 32000
    agent.add_user_message("hi")
    captured: list[dict] = []
    with patch("urllib.request.urlopen", side_effect=_capture_request_body(captured)):
        agent.run_turn()
    assert captured[0]["thinking"]["budget_tokens"] == 32000
