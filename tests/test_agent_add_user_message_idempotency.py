"""PR-18 (F-13) — ``Agent.add_user_message`` idempotency.

Pre-1.8.1, a UI double-click or transient retry could append the
same user text twice in a row. Two consecutive ``user`` blocks make
DeepSeek's compat layer 400 with ``messages: roles must alternate``.

The guard:
  * Inspects the most recent message.
  * If it's a ``user`` block with the same text as the new send,
    no-op.
  * Otherwise append normally.

Crucially the guard ONLY blocks a duplicate of the immediately
previous user message. Same text after an assistant turn (a
legitimate "ask the same thing again") goes through fine.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


@pytest.fixture
def agent():
    """Build an Agent with the cheapest possible config — no key
    needed because we never invoke ``_call_api``, only ``add_user_message``."""
    import tdpilot_api_agent as agent_mod  # noqa: WPS433 — late import after sys.path tweak

    a = agent_mod.Agent(
        api_key="dummy",
        dispatcher=lambda name, args: {},
        tools=[],
        system_prompt="",
        model="deepseek-v4-pro",
        base_url="http://localhost",
    )
    a.reset()
    return a


def _user_blocks(messages):
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "user"]


def test_first_user_message_appends(agent):
    agent.add_user_message("hello")
    assert len(agent.messages) == 1
    assert agent.messages[0]["role"] == "user"


def test_duplicate_consecutive_user_message_is_dropped(agent):
    """The killer invariant — back-to-back same-text user messages
    collapse to one. Pre-fix this generated a 400 on the next API call."""
    agent.add_user_message("hello")
    agent.add_user_message("hello")
    assert len(agent.messages) == 1
    assert _user_blocks(agent.messages) == agent.messages


def test_different_text_appends_second_message(agent):
    """Same role but different text — both messages survive. The
    guard's purpose is to block accidental duplicates, not to dedupe
    distinct sends."""
    agent.add_user_message("hello")
    agent.add_user_message("hello again")
    assert len(agent.messages) == 2


def test_same_text_after_assistant_turn_appends_normally(agent):
    """If an assistant message intervenes, the same user text is no
    longer a duplicate — re-asking after a reply is a legitimate flow.
    The guard must NOT block this."""
    agent.add_user_message("hello")
    # Simulate an assistant turn by appending directly to messages.
    agent.messages.append({"role": "assistant", "content": [{"type": "text", "text": "hi"}]})
    agent.add_user_message("hello")
    assert len(agent.messages) == 3
    assert agent.messages[-1]["role"] == "user"


def test_idempotency_handles_empty_messages_list(agent):
    """First call against an empty list still works — the guard
    must short-circuit on no-prior-messages."""
    assert agent.messages == []
    agent.add_user_message("first")
    assert len(agent.messages) == 1


def test_idempotency_handles_malformed_prior_message(agent):
    """Defensive: a previous message with an unexpected shape must
    not crash the guard."""
    agent.messages.append({"role": "user", "content": "not a list"})
    agent.add_user_message("hi")
    # Append happened — guard didn't crash on the malformed prior.
    assert len(agent.messages) == 2
    assert agent.messages[-1]["content"] == [{"type": "text", "text": "hi"}]


def test_idempotency_handles_prior_message_with_no_content(agent):
    agent.messages.append({"role": "user"})
    agent.add_user_message("hi")
    assert len(agent.messages) == 2


def test_idempotency_handles_prior_user_with_non_text_first_block(agent):
    """E.g. a tool_result block as the first content item — guard
    must NOT misclassify and drop a legitimate text send."""
    agent.messages.append(
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_x", "content": "ok"},
            ],
        }
    )
    agent.add_user_message("free-form follow-up")
    assert len(agent.messages) == 2


def test_three_in_a_row_collapses_to_one(agent):
    agent.add_user_message("ping")
    agent.add_user_message("ping")
    agent.add_user_message("ping")
    assert len(agent.messages) == 1


def test_empty_text_is_still_idempotent(agent):
    """Empty string is a valid (if useless) message; the guard's
    equality check should treat ``""`` == ``""`` as a duplicate."""
    agent.add_user_message("")
    agent.add_user_message("")
    assert len(agent.messages) == 1
