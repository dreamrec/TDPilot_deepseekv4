"""v2.1.1 chat-HTML regression — user-message red-mark styling.

The user wanted their own messages to be visually unmistakable in the
scrollback (vs. v2.1.0's quieter purple treatment). v2.1.1 changes
the user-message rendering to a red/white "LCD stamp" idiom while
keeping the terminal/CRT vibe:

  - 4px solid red left rule (other roles still use 2px).
  - Subtle red background gradient fading rightward.
  - Pure white body text for max contrast.
  - White-on-red ``[ USER ]`` role stamp, brackets included.

These tests pin each piece so a future edit can't drop the design
silently — if a contributor tightens the palette and the red goes
away, CI fails here pointing at this file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = REPO_ROOT / "td_component" / "tdpilot_api_chat.html"


@pytest.fixture(scope="module")
def html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def test_user_red_palette_variables_are_declared(html: str):
    """Three CSS custom properties drive the red mark — solid red,
    a brighter hover red (reserved for future hover state), and a
    translucent bg tint. Locking in the names so the rest of the CSS
    can reference them."""
    assert "--user-red:" in html
    assert "--user-red-2:" in html
    assert "--user-red-bg:" in html


def test_user_message_has_thick_red_left_rule(html: str):
    """4px (vs. the default 2px on other roles) plus the red token —
    the rule alone should be enough to spot a user message at a glance.
    """
    pattern = re.compile(
        r"\.msg\.user\s*\{[^}]*?border-left-width:\s*4px[^}]*?border-left-color:\s*var\(--user-red\)",
        re.DOTALL,
    )
    assert pattern.search(html), "user message must declare 4px solid red left rule"


def test_user_message_has_red_gradient_background(html: str):
    """Gradient that fades right so long messages don't read as a
    solid red block — just the leading edge is highlighted."""
    pattern = re.compile(
        r"\.msg\.user\s*\{[^}]*?background:\s*linear-gradient\([^)]*?var\(--user-red-bg\)",
        re.DOTALL,
    )
    assert pattern.search(html), "user message must use the red-bg linear-gradient"


def test_user_message_body_text_is_white(html: str):
    """White body text — the user explicitly asked for max-contrast
    white over the red elements."""
    pattern = re.compile(r"\.msg\.user\s*\{[^}]*?color:\s*#ffffff", re.DOTALL)
    assert pattern.search(html), "user message body must be #ffffff white"


def test_user_role_tag_is_white_on_red_stamp(html: str):
    """The ``[ USER ]`` tag flips to a solid red block with white
    text — the "stamp" the user asked for. Brackets must inherit
    via the .role span (display: inline-block, background = red)."""
    pattern = re.compile(
        r"\.msg\.user\s+\.role\s*\{[^}]*?"
        r"display:\s*inline-block[^}]*?"
        r"background:\s*var\(--user-red\)[^}]*?"
        r"color:\s*#ffffff",
        re.DOTALL,
    )
    assert pattern.search(html), "user role tag must be white-on-red inline-block stamp"


def test_user_role_brackets_are_white(html: str):
    """The ``[`` and ``]`` are rendered via ``::before`` / ``::after``;
    the global rule colors them ``var(--rule)`` (dim purple), which
    would clash with the red bg. v2.1.1 overrides them to white so
    the whole stamp reads as one monospace block."""
    assert ".msg.user .role::before" in html
    assert ".msg.user .role::after" in html
    # Both override colours must be #ffffff.
    pattern = re.compile(
        r"\.msg\.user\s+\.role::before\s*\{[^}]*?color:\s*#ffffff[^}]*?\}\s*"
        r"\.msg\.user\s+\.role::after\s*\{[^}]*?color:\s*#ffffff",
        re.DOTALL,
    )
    assert pattern.search(html), "user role brackets must override to white"


def test_user_message_styling_does_not_leak_to_other_roles(html: str):
    """Red treatment is user-only — assistant / tool / error / hint
    must keep their existing palette. Sanity check: no other role
    selector references --user-red.
    """
    for role in ("assistant", "tool_call", "tool_result", "error", "hint"):
        # A simple structural check — the role's selectors should not
        # reference any --user-red* token. A future contributor copy-
        # pasting the user block and forgetting to retune the colors
        # would trip this.
        block_pattern = re.compile(rf"\.msg\.{role}[^{{]*\{{[^}}]*--user-red", re.DOTALL)
        assert not block_pattern.search(html), f"role '{role}' should not reference --user-red tokens"
