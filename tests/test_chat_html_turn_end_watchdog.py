"""Regression tests for the chat-HTML turn-end watchdog (v2.3.1+).

Pre-fix the chat panel used a one-shot 90s wall-clock timer set inside
``send()`` to flip status to ``idle (timeout)``. The runtime correctly kept
emitting EV_TEXT / EV_TOOL_CALL / EV_TOOL_RESULT events past 90s during
long tool chains, but the browser had already lied: ``idle (timeout)``
showed even though the agent was still working.

Fix converts the wall-clock timer to an activity watchdog — every
runtime event arriving over the WS reschedules the timer fresh, so it
only fires after true silence from the runtime (genuine WS drop / worker
hang). Tests assert the structural invariant: the ``setTimeout(... 'idle
(timeout)' ...)`` call lives in a single function reachable from BOTH
``send()`` AND ``applyMessage()``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CHAT_HTML_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_chat.html"


@pytest.fixture(scope="module")
def html() -> str:
    return CHAT_HTML_PATH.read_text(encoding="utf-8")


def test_watchdog_function_defined(html: str):
    assert "function armTurnEndWatchdog" in html


def test_send_uses_watchdog_function(html: str):
    """``send()`` must arm the watchdog via the helper, not inline a setTimeout."""
    send_start = html.index("function send()")
    send_end = html.index("function ", send_start + 1)
    send_body = html[send_start:send_end]
    assert "armTurnEndWatchdog()" in send_body
    assert "'idle (timeout)'" not in send_body, (
        "setTimeout literal still inlined in send(); should live only inside "
        "armTurnEndWatchdog so the timer can be rescheduled on activity."
    )


def test_apply_message_rearms_watchdog(html: str):
    """Every runtime event (append/tool_call/tool_result/model/usage/status)
    must keep the watchdog alive — otherwise long tool chains false-trip
    while the runtime is healthy."""
    apply_start = html.index("function applyMessage(msg)")
    apply_end = html.index("function ", apply_start + 1)
    apply_body = html[apply_start:apply_end]
    assert "armTurnEndWatchdog()" in apply_body


def test_idle_timeout_only_inside_watchdog_function(html: str):
    """Only one site in the file should ever emit the literal `idle
    (timeout)`. Two sites means the watchdog was duplicated."""
    assert html.count("'idle (timeout)'") == 1


def test_turn_end_safety_ms_constant_still_exists(html: str):
    """We're not redefining the budget — just gating it on activity."""
    assert "TURN_END_SAFETY_MS" in html
