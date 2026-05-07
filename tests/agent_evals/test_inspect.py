"""Phase 4.2 — agent eval: inspection flows.

These exercise the bread-and-butter "what's in my project" path:
  - inspect_basic — "What's the FPS?" → td_get_info, "60" in reply.
  - inspect_nodes — "List operators in /project1" → td_get_nodes,
    operator names mentioned.

Each eval resets the session, sends one prompt, waits for the turn
to complete, and asserts the tool-call sequence + reply markers.
"""

from __future__ import annotations

import pytest

from .conftest import (
    assert_no_error_event,
    assert_reply_contains,
    assert_tool_in_sequence,
    run_eval_turn,
)

pytestmark = pytest.mark.agent_eval


def test_inspect_basic_fps(base_url):
    """The agent must call td_get_info and surface the FPS in plain text."""
    rows = run_eval_turn(base_url, "What's the current FPS of the project?")
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_get_info"])
    assert_reply_contains(rows, "60")


def test_inspect_nodes_list(base_url):
    """List the children at /project1 — expect td_get_nodes + at least
    one operator-shaped reference in the reply.
    """
    rows = run_eval_turn(base_url, "List the operators that live at /project1.")
    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_get_nodes"])
    # The reply should at least mention "/project1" or an op name.
    # We don't pin a specific op name because user projects vary.
    assert_reply_contains(rows, "/project1")
