"""v2.4 / B-004 — on_model_change fires on every turn, not only on flip.

Pre-fix the Agent only fired ``on_model_change`` when the picked model
differed from ``self._active_model``. The chat HTML's model-badge
populates only on ``EV_MODEL`` events, so a tab reload followed by
several turns at the SAME tier left the badge empty — the user saw
"thinking" with no model indicator. After this fix the badge sees an
``EV_MODEL`` event after every turn's model resolution.
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
    return _CtxMgr(SimpleNamespace(read=lambda: body))


def _text_only(text="ok"):
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
    }


def test_b004_on_model_change_fires_when_tier_unchanged():
    """Even if the picked model matches self._active_model from a prior
    turn, on_model_change must still fire so the chat UI's badge
    populates after a tab reload."""
    fires: list[tuple[str, str]] = []
    agent = Agent(
        api_key="sk-fake",
        dispatcher=lambda *a: None,
        model="deepseek-v4-pro",
        model_tier="pro",  # sticky pro — every turn picks the same model
        on_model_change=lambda tier, picked: fires.append((tier, picked)),
    )
    agent.add_user_message("hello")
    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _mk_response(_text_only())):
        agent.run_turn()
    assert len(fires) == 1, f"expected one firing on the first turn, got {fires}"

    # Second turn — same tier, same model — pre-fix this would skip
    # on_model_change. Post-fix it must still fire.
    agent.add_user_message("again")
    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _mk_response(_text_only())):
        agent.run_turn()
    assert len(fires) == 2, (
        f"expected on_model_change to fire on EVERY turn (badge stability), got {len(fires)} firings: {fires}"
    )
    # Tier and model strings on both firings should match the configured tier.
    for tier, picked in fires:
        assert tier == "pro"
        assert "pro" in picked.lower()


def test_b004_third_turn_still_fires():
    """Three turns in a row at the same tier → three on_model_change events."""
    fires: list[tuple[str, str]] = []
    agent = Agent(
        api_key="sk-fake", dispatcher=lambda *a: None,
        model_tier="flash",
        on_model_change=lambda tier, picked: fires.append((tier, picked)),
    )
    for i, msg in enumerate(("turn1", "turn2", "turn3")):
        agent.add_user_message(msg)
        reply_text = f"r{i}"
        with patch(
            "urllib.request.urlopen",
            side_effect=lambda *a, _t=reply_text, **kw: _mk_response(_text_only(_t)),
        ):
            agent.run_turn()
    assert len(fires) == 3, f"every turn must fire on_model_change, got {fires}"


def test_b004_callback_failure_doesnt_break_turn():
    """If on_model_change raises, the turn still completes (try/except
    around the firing must catch and continue)."""
    def boom(tier, picked):
        raise RuntimeError("simulated UI failure")
    agent = Agent(
        api_key="sk-fake", dispatcher=lambda *a: None,
        on_model_change=boom,
    )
    agent.add_user_message("hi")
    with patch("urllib.request.urlopen", side_effect=lambda *a, **kw: _mk_response(_text_only("survived"))):
        # Must not raise out of run_turn.
        agent.run_turn()
    # Turn completed normally — assistant text in self.messages.
    assert any(m.get("role") == "assistant" for m in agent.messages)
