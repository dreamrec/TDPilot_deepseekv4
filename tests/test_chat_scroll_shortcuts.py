"""PR-11 (Phase 2 / 1.8.0) — scroll-aware autoscroll + shortcuts.

* Autoscroll only sticks to bottom when the user is already at (or
  near) the bottom. The 100px threshold matches the plan doc.
* When new messages arrive while scrolled up, an unread counter
  drives a floating "↓ N new" jump button.
* ↑ recalls the last sent message (terminal convention); ↓ moves
  forward in the recall buffer; ↑ on a non-empty buffer is silent so
  it doesn't clobber in-progress typing.
* Cmd/Ctrl+K wipes the chat back to the welcome screen.
* Cmd/Ctrl+/ focuses the input.
* End jumps to bottom.
* Each assistant message gets a copy-to-clipboard button; the
  button stores the original markdown source on dataset.source so
  copies preserve formatting.

Tests are structural — pinning the IIFE source against regressions.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHAT_HTML_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_chat.html"


@pytest.fixture(scope="module")
def html() -> str:
    return CHAT_HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def iife(html: str) -> str:
    m = re.search(r"<script>\s*\(\(\)\s*=>\s*\{(.+?)\}\)\(\);\s*</script>", html, re.S)
    assert m
    return m.group(1)


# ---------------------------------------------------------------------------
# Scroll-aware autoscroll
# ---------------------------------------------------------------------------


def test_autoscroll_threshold_constant(iife: str):
    """A named threshold is easier to tweak than a hardcoded 100."""
    assert "AUTOSCROLL_THRESHOLD_PX = 100" in iife


def test_autoscroll_state_variables(iife: str):
    assert "let autoStickToBottom = true" in iife
    assert "let unreadCount = 0" in iife


def test_is_near_bottom_uses_threshold(iife: str):
    m = re.search(r"function isNearBottom\(\)\s*\{(.+?)\}", iife, re.S)
    assert m, "isNearBottom not found"
    body = m.group(1)
    assert "AUTOSCROLL_THRESHOLD_PX" in body
    assert "$history.scrollTop" in body
    assert "$history.scrollHeight" in body


def test_auto_scroll_only_pins_when_sticky(iife: str):
    """When the user is reading older content, new arrivals must NOT
    jerk the viewport — we count them instead."""
    m = re.search(r"function autoScroll\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "if (autoStickToBottom)" in body
    assert "unreadCount++" in body
    assert "refreshScrollJump()" in body


def test_scroll_listener_updates_stick_state(iife: str):
    """Manually scrolling re-evaluates autoStickToBottom so the next
    autoScroll() chooses the right behaviour."""
    assert "$history.addEventListener('scroll'," in iife
    assert "isNearBottom()" in iife
    # Scrolling back to bottom clears the unread badge.
    assert "unreadCount = 0" in iife


def test_scroll_jump_button_present(html: str):
    assert 'id="scroll-jump"' in html
    assert "#scroll-jump" in html  # CSS selector


def test_jump_to_bottom_resets_state(iife: str):
    m = re.search(r"function jumpToBottom\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "jumpToBottom not found"
    body = m.group(1)
    assert "autoStickToBottom = true" in body
    assert "unreadCount = 0" in body
    assert "$history.scrollTop = $history.scrollHeight" in body


def test_full_sync_resets_scroll_state(iife: str):
    m = re.search(r"function fullSync\(rows\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "autoStickToBottom = true" in body
    assert "unreadCount = 0" in body
    assert "refreshScrollJump()" in body


def test_refresh_scroll_jump_hides_when_sticky(iife: str):
    m = re.search(r"function refreshScrollJump\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "autoStickToBottom" in body
    assert "unreadCount === 0" in body
    assert "$scrollJump.classList.remove('visible')" in body
    assert "$scrollJump.classList.add('visible')" in body
    # Counter rendered into the button label.
    assert "'↓ ' + unreadCount + ' new'" in body


# ---------------------------------------------------------------------------
# Keyboard shortcuts
# ---------------------------------------------------------------------------


def test_sent_history_buffer_exists(iife: str):
    assert "const sentHistory = []" in iife
    assert "let recallIndex" in iife


def test_record_sent_dedupes_consecutive_duplicates(iife: str):
    """A double-tap of the same message shouldn't litter the recall
    buffer with duplicate adjacent entries."""
    m = re.search(r"function recordSent\(text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "sentHistory[sentHistory.length - 1] === text" in body
    # Cap to keep memory bounded.
    assert "sentHistory.length > 50" in body
    assert "sentHistory.shift()" in body


def test_send_records_into_history(iife: str):
    m = re.search(r"function send\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "recordSent(trimmed)" in body


def test_recall_prev_only_fires_when_safe(iife: str):
    """↑ recall must not clobber in-progress typing. Either the input
    is empty OR we're already in recall mode."""
    m = re.search(r"\$input\.addEventListener\('keydown'.+?\}\);", iife, re.S)
    assert m, "input keydown listener not found"
    body = m.group(0)
    assert "e.key === 'ArrowUp'" in body
    assert "!$input.value || recallIndex !== -1" in body
    assert "recallPrev()" in body


def test_recall_next_advances_to_empty(iife: str):
    """Past the most-recent entry, ↓ leaves the input empty rather
    than wrapping back to the oldest."""
    m = re.search(r"function recallNext\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "recallIndex < sentHistory.length - 1" in body
    assert "$input.value = ''" in body


def test_typing_exits_recall_mode(iife: str):
    """If the user starts editing the recalled text, subsequent ↑
    must not blow away their changes."""
    m = re.search(r"\$input\.addEventListener\('input'.+?\}\);", iife, re.S)
    assert m, "input listener not found"
    body = m.group(0)
    assert "recallIndex = -1" in body


def _global_shortcut_listener(iife: str) -> str:
    """The shortcuts listener is the document-scope keydown handler
    that initialises `cmd = e.metaKey || e.ctrlKey`. PR-12 added a
    second listener for the lightbox close — match by content rather
    than ordering so future additions don't break the test."""
    matches = re.findall(
        r"document\.addEventListener\('keydown',\s*\(e\)\s*=>\s*\{(.+?)\}\s*\)\s*;", iife, re.S
    )
    for body in matches:
        if "metaKey" in body and "ctrlKey" in body:
            return body
    raise AssertionError("global-shortcut keydown listener not found")


def test_cmd_k_clears_chat(iife: str):
    """Cmd/Ctrl+K maps to fullSync([]) — same path /reset uses."""
    body = _global_shortcut_listener(iife)
    assert "(e.key === 'k' || e.key === 'K')" in body
    assert "cmd && (e.key === 'k'" in body
    assert "fullSync([])" in body


def test_cmd_slash_focuses_input(iife: str):
    body = _global_shortcut_listener(iife)
    assert "cmd && e.key === '/'" in body
    assert "$input.focus()" in body


def test_end_key_jumps_to_bottom(iife: str):
    body = _global_shortcut_listener(iife)
    assert "e.key === 'End'" in body
    assert "jumpToBottom()" in body


def test_existing_escape_stop_shortcut_preserved(iife: str):
    """The pre-existing Esc → stopAgent shortcut must still work; PR-11
    augmented the document keydown listener but didn't drop it."""
    body = iife
    assert "e.key === 'Escape'" in body
    # Stop is reachable from both input scope and document scope.
    assert body.count("stopAgent()") >= 3


# ---------------------------------------------------------------------------
# Copy-to-clipboard on assistant messages
# ---------------------------------------------------------------------------


def test_copy_button_added_to_assistant_messages(iife: str):
    m = re.search(r"function appendMessage\(role, message\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "copyBtn.className = 'msg-copy'" in body
    assert "copyBtn.dataset.source = text" in body
    assert "copyToClipboard(text)" in body


def test_copy_to_clipboard_falls_back_when_api_missing(iife: str):
    """In TouchDesigner's bundled Chromium the navigator.clipboard API
    may be permission-blocked; we keep an execCommand('copy') fallback
    so the button still works."""
    m = re.search(r"function copyToClipboard\(text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "copyToClipboard not found"
    body = m.group(1)
    assert "navigator.clipboard" in body
    assert "execCommand('copy')" in body


def test_copy_button_css_present(html: str):
    assert ".msg-copy" in html
    assert ".msg.assistant:hover .msg-copy" in html


# ---------------------------------------------------------------------------
# Cross-PR regression: existing behaviour preserved
# ---------------------------------------------------------------------------


def test_send_on_enter_still_works(iife: str):
    """The vanilla "Enter sends, Shift+Enter newline" binding must
    survive PR-11's keydown additions."""
    m = re.search(r"\$input\.addEventListener\('keydown'.+?\}\);", iife, re.S)
    assert m
    body = m.group(0)
    assert "e.key === 'Enter' && !e.shiftKey" in body
    assert "send()" in body
