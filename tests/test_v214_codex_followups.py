"""v2.1.4 — Codex review follow-ups on PR #28.

Two structural fixes for v2.1.3's chat-pipe queue + send-button gate.

Both bugs were caught by the Codex bot's automated review on PR #28
(commit eaf90cf5f7); both are real reliability holes in the v2.1.3
fixes. These tests assert the source-level wiring stays correct so
future refactors don't regress.

P1 — drain inbox queue on EV_ERROR (not just EV_DONE)
-----------------------------------------------------
v2.1.3 introduced a FIFO inbox queue on ``comp.storage`` that drained
one message per ``EV_DONE``. Codex pointed out: failed turns emit
``EV_ERROR`` (and ``EV_STATE: idle``) without calling
``_drain_inbox_one()``. So a queued message after an errored turn sits
in storage indefinitely until a later successful turn happens to
emit ``EV_DONE``. Fix: also drain on ``EV_ERROR``.

P2 — safety net for the send-button gate when WS drops
------------------------------------------------------
v2.1.3 moved the send-button re-enable from the /send fetch
resolution to the WebSocket-driven ``setAgentStatus()`` call. Codex
pointed out: if the WS drops between /send and the terminal status
event, ``awaitingTurnEnd`` stays ``true`` and the user is locked
out of the chat permanently (until they reload the page). Fix: a
90s safety timer + a reset on every ``ws.onopen`` — if the runtime
is genuinely still busy, the next status event re-disables; if the
user fires a message while busy, the v2.1.3 FIFO inbox catches it
(no message is lost).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TD_COMP = ROOT / "td_component"


@pytest.fixture(scope="module")
def extension_src() -> str:
    return (TD_COMP / "tdpilot_api_extension.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_html_src() -> str:
    return (TD_COMP / "tdpilot_api_chat.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P1 — EV_ERROR drains inbox queue
# ---------------------------------------------------------------------------


def _ev_error_block(extension_src: str) -> str:
    """Extract the source of the ``elif kind == EV_ERROR:`` branch in
    ``_handle_event``. Used by the P1 tests below."""
    m = re.search(
        r"elif kind == EV_ERROR:\s*\n((?:[ \t]{12}.*\n)+)",
        extension_src,
    )
    assert m, "EV_ERROR branch not found — _handle_event refactor?"
    return m.group(1)


def test_p1_ev_error_branch_drains_inbox(extension_src: str):
    """The ``elif kind == EV_ERROR:`` branch must call
    ``_drain_inbox_one`` so a queued message after an errored turn
    doesn't sit in storage forever (Codex P1)."""
    block = _ev_error_block(extension_src)
    assert "_drain_inbox_one" in block, (
        "EV_ERROR branch must call self._drain_inbox_one() — Codex P1 "
        "fix on PR #28. Pre-2.1.4 only EV_DONE drained the inbox; a "
        "failed turn left queued messages stranded forever."
    )


def test_p1_ev_done_branch_still_drains_inbox(extension_src: str):
    """Sanity — make sure the original EV_DONE drain wasn't dropped
    when adding the EV_ERROR drain."""
    m = re.search(
        r"elif kind == EV_DONE:\s*\n((?:[ \t]{12}.*\n)+)",
        extension_src,
    )
    assert m, "EV_DONE branch not found"
    assert "_drain_inbox_one" in m.group(1), (
        "EV_DONE branch must still call self._drain_inbox_one() — that "
        "was the original v2.1.3 fix and must not regress."
    )


# ---------------------------------------------------------------------------
# P2 — safety net for the send-button gate when WS drops
# ---------------------------------------------------------------------------


def test_p2_safety_timer_constant_present(chat_html_src: str):
    """A safety-cap constant must exist so ``awaitingTurnEnd`` can't
    pin the send button forever if the WS drops mid-turn (Codex P2)."""
    assert "TURN_END_SAFETY_MS" in chat_html_src, (
        "Missing TURN_END_SAFETY_MS — Codex P2 fix on PR #28. The "
        "send button needs a hard cap so a dropped WS connection "
        "doesn't lock the user out of the chat."
    )


def test_p2_clear_helper_resets_flag_button_and_timer(chat_html_src: str):
    """``clearAwaitingTurnEnd()`` must reset ALL three pieces of
    state: the timer handle, the awaitingTurnEnd flag, and the
    button's disabled attribute. Anything less leaks state.

    Captures up to the closing brace at column 2 (the indent level of
    the function declaration) so the inner ``if (...) {...}`` block's
    closing brace doesn't terminate the capture early.
    """
    m = re.search(
        r"function clearAwaitingTurnEnd\s*\([^)]*\)\s*\{(.+?)\n  \}\s*\n",
        chat_html_src,
        re.DOTALL,
    )
    assert m, "clearAwaitingTurnEnd helper missing"
    body = m.group(1)
    assert "clearTimeout" in body, "must clear the safety timer"
    assert "awaitingTurnEnd = false" in body, "must reset the flag"
    assert "$send.disabled = false" in body, "must re-enable the button"


def test_p2_send_arms_the_safety_timer(chat_html_src: str):
    """``send()`` must arm the safety timer alongside setting
    ``awaitingTurnEnd = true``. Without this the turn-end gate
    has no ceiling."""
    # Find the send() function body and assert it sets the timer.
    m = re.search(
        r"function send\s*\([^)]*\)\s*\{(.+?)\n\s{2}\}\s*\n",
        chat_html_src,
        re.DOTALL,
    )
    assert m, "send() function not found"
    body = m.group(1)
    assert "awaitingTurnEnd = true" in body, "send() must set the flag"
    assert "setTimeout" in body and "TURN_END_SAFETY_MS" in body, (
        "send() must arm the TURN_END_SAFETY_MS timer."
    )


def test_p2_ws_onopen_resets_awaiting_flag(chat_html_src: str):
    """``ws.onopen`` must reset ``awaitingTurnEnd`` on every reconnect
    so a turn that started before a WS drop doesn't permanently
    disable the send button."""
    m = re.search(
        r"ws\.onopen\s*=\s*\(\s*\)\s*=>\s*\{(.+?)\};",
        chat_html_src,
        re.DOTALL,
    )
    assert m, "ws.onopen handler not found"
    body = m.group(1)
    assert "awaitingTurnEnd" in body, "ws.onopen must touch awaitingTurnEnd — Codex P2 fix on PR #28."
    assert "clearAwaitingTurnEnd" in body, (
        "ws.onopen must call clearAwaitingTurnEnd to reset the gate on reconnect."
    )


def test_p2_set_agent_status_uses_clear_helper(chat_html_src: str):
    """When ``setAgentStatus`` sees a non-working state, it must call
    ``clearAwaitingTurnEnd()`` (the centralised helper) — NOT the
    pre-2.1.4 inline pair of ``awaitingTurnEnd = false; $send.disabled
    = false`` which forgot to clear the safety timer."""
    m = re.search(
        r"function setAgentStatus\s*\([^)]*\)\s*\{(.+?)\n\s{2}\}\s*\n",
        chat_html_src,
        re.DOTALL,
    )
    assert m, "setAgentStatus function not found"
    body = m.group(1)
    assert "clearAwaitingTurnEnd" in body, (
        "setAgentStatus must call clearAwaitingTurnEnd() so the safety "
        "timer is cleared on the happy path too. Otherwise a dangling "
        "timer fires after a normal turn end and re-clears state mid-"
        "next-turn."
    )
