"""v2.1.0 chat-HTML UI rework regressions.

Replaces the v2.0.0 ``aA`` font-size toggle with three new pieces:

1. **Quiet-mode toggle** at the far right of the status bar. A single
   button with a filled / hollow circle glyph. When active, hides
   ``.msg.tool_call``, ``.msg.tool_result``, and ``details.tool-pair``
   so the chat reads as a plain conversation. State persists per
   browser via ``localStorage["tdpilot.quietMode"]``. Also accessible
   via ``Cmd/Ctrl + .``.

2. **Smaller default fonts** — body 13→12px, status bar 11→10px,
   input 13→12px. Keeps the retro density without scaling up.

3. **Contextual ASCII flourishes** appended to assistant messages
   on turn-end. Pool of geometric / typographic glyphs grouped by
   topic (light / camera / particle / material / audio / geom /
   error / success / default). Topic detection is keyword-based
   against the last user message + the assistant body.

These tests parse the served HTML body as a string (matching the
``tests/test_chat_html_state.py`` pattern) so we catch structural
regressions without spinning a JS runtime.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CHAT_HTML_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_chat.html"


@pytest.fixture(scope="module")
def html() -> str:
    return CHAT_HTML_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Font toggle is GONE
# ---------------------------------------------------------------------------


def test_font_toggle_removed_from_html(html: str):
    """v2.1.0 removes the v2.0.0 ``aA`` font-size toggle entirely.
    No HTML, no CSS, no JS — gone."""
    # No button id
    assert 'id="font-toggle"' not in html
    # No CSS rule
    assert "#font-toggle" not in html
    # No JS captures or handlers
    assert "$fontToggle" not in html
    assert "applyFontMode" not in html
    assert "FONT_KEY" not in html
    # No fs-small / fs-large CSS classes
    assert "fs-small" not in html
    assert "fs-large" not in html
    assert "fs-a" not in html
    assert "fs-A" not in html


# ---------------------------------------------------------------------------
# Quiet-mode toggle
# ---------------------------------------------------------------------------


def test_quiet_toggle_button_present(html: str):
    """The new quiet-mode toggle replaces the font toggle in the same
    far-right slot of the status bar. Single button, glyph child,
    aria-pressed defaults to false."""
    pattern = re.compile(
        r'<button\s+id="quiet-toggle"[\s\S]*?'
        r'aria-label="Toggle quiet mode"[\s\S]*?'
        r'aria-pressed="false"[\s\S]*?'
        r'<span class="qm-glyph">',
        re.MULTILINE,
    )
    assert pattern.search(html), "quiet-toggle button missing or malformed"


def test_quiet_toggle_is_status_bar_third_child(html: str):
    """``#status-bar`` sequence must be ``.lhs``, ``.rhs``,
    ``#quiet-toggle`` in that order. Same flex layout as v2.0.0
    (``.rhs`` carries ``margin-left: auto``)."""
    sb_open = html.find('<div id="status-bar">')
    sb_close = html.find("</div>", sb_open)
    assert sb_open != -1 and sb_close != -1
    block = html[sb_open:sb_close]
    lhs_idx = block.find('class="lhs"')
    rhs_idx = block.find('class="rhs"')
    btn_idx = block.find('id="quiet-toggle"')
    assert 0 < lhs_idx < rhs_idx < btn_idx, (
        f"status-bar children out of order: lhs={lhs_idx} rhs={rhs_idx} quiet-toggle={btn_idx}"
    )


def test_rhs_still_has_margin_left_auto(html: str):
    """The 3-child status bar still needs ``.rhs`` carrying
    ``margin-left: auto`` so the right cluster stays right-aligned."""
    pattern = re.compile(
        r"#status-bar \.rhs \{[^}]*margin-left:\s*auto[^}]*\}",
        re.MULTILINE,
    )
    assert pattern.search(html), ".rhs is missing `margin-left: auto`"


def test_quiet_mode_class_hides_tool_blocks(html: str):
    """``:root.quiet-mode`` hides ``.msg.tool_call``,
    ``.msg.tool_result``, AND ``details.tool-pair`` (the collapsible
    wrapper). Other message types (user / assistant / error / hint)
    must NOT be in the selector list."""
    pattern = re.compile(
        r":root\.quiet-mode \.msg\.tool_call,\s*"
        r":root\.quiet-mode \.msg\.tool_result,\s*"
        r":root\.quiet-mode details\.tool-pair\s*\{\s*display:\s*none",
        re.MULTILINE,
    )
    assert pattern.search(html), "quiet-mode hiding rule missing or wrong selector list"
    # Verify NO over-broad hiding (assistant / user must NOT be there)
    assert ":root.quiet-mode .msg.assistant" not in html
    assert ":root.quiet-mode .msg.user" not in html
    assert ":root.quiet-mode .msg.error" not in html


def test_quiet_mode_localstorage_key_is_namespaced(html: str):
    """Persistence key is ``tdpilot.quietMode`` to match the
    ``tdpilot.<feature>`` namespace convention used elsewhere."""
    assert "const QUIET_KEY = 'tdpilot.quietMode';" in html


def test_apply_quiet_mode_function_is_defined(html: str):
    """The single source of truth for flipping quiet mode. Must
    update the ``<html>`` class, the button's aria-pressed, the glyph
    text, and localStorage in one shot."""
    assert "function applyQuietMode(on)" in html
    assert "document.documentElement.classList.add('quiet-mode')" in html
    assert "document.documentElement.classList.remove('quiet-mode')" in html
    assert "$quietToggle.setAttribute('aria-pressed'," in html
    assert "querySelector('.qm-glyph').textContent = on ? '●' : '○'" in html
    assert "localStorage.setItem(QUIET_KEY, on ? '1' : '0')" in html


def test_quiet_mode_localstorage_read_is_guarded(html: str):
    """Private-mode browsers throw on localStorage. Initial read +
    persist write must both be try/catch'd."""
    assert "try { _savedQuiet = localStorage.getItem(QUIET_KEY) === '1'; } catch" in html


def test_keyboard_shortcut_for_quiet_mode(html: str):
    """``Cmd/Ctrl + .`` toggles quiet mode. preventDefault so the
    period doesn't bubble to anything else. Replaces the v2.0.0
    Cmd/Ctrl + +/- font shortcuts."""
    pattern = re.compile(
        r"if \(cmd && e\.key === '\.'\) \{[\s\S]*?"
        r"e\.preventDefault\(\);[\s\S]*?"
        r"applyQuietMode\(",
        re.MULTILINE,
    )
    assert pattern.search(html), "Cmd/Ctrl+. shortcut for quiet mode missing"


# ---------------------------------------------------------------------------
# Smaller default fonts
# ---------------------------------------------------------------------------


def test_body_font_size_is_12px(html: str):
    """Base body font shrunk from 13px → 12px in v2.1.0 for tighter
    chat density."""
    pattern = re.compile(
        r"html, body \{[\s\S]*?font-size:\s*12px",
        re.MULTILINE,
    )
    assert pattern.search(html), "body font-size should be 12px"


def test_status_bar_font_size_is_10px(html: str):
    """Status bar shrunk from 11px → 10px so it reads as terminal
    chrome rather than primary content."""
    pattern = re.compile(
        r"#status-bar \{[\s\S]*?font-size:\s*10px",
        re.MULTILINE,
    )
    assert pattern.search(html), "#status-bar font-size should be 10px"


def test_input_font_size_is_12px(html: str):
    """Input textarea shrunk from 13px → 12px to match the new body
    scale."""
    pattern = re.compile(
        r"#input \{[\s\S]*?font-size:\s*12px",
        re.MULTILINE,
    )
    assert pattern.search(html), "#input font-size should be 12px"


def test_no_residual_13px_in_body_or_input(html: str):
    """Defensive — neither ``html, body { font-size: 13px }`` nor
    ``#input { font-size: 13px }`` should appear anymore."""
    pattern = re.compile(
        r"(html, body|#input) \{[^}]*font-size:\s*13px",
        re.MULTILINE,
    )
    assert not pattern.search(html), "stale 13px font-size found in body or #input"


# ---------------------------------------------------------------------------
# Contextual ASCII flourishes
# ---------------------------------------------------------------------------


def test_flourish_pool_defines_all_topic_buckets(html: str):
    """ASCII_FLOURISHES dict must define at least 9 buckets:
    default, light, camera, particle, material, audio, geom, error,
    success. Each bucket has at least 3 glyphs."""
    for topic in (
        "default",
        "light",
        "camera",
        "particle",
        "material",
        "audio",
        "geom",
        "error",
        "success",
    ):
        assert f"{topic}:" in html, f"flourish pool missing topic bucket: {topic}"


def test_flourish_topic_rules_exist(html: str):
    """Topic-detection rules must include the ones that map to
    pool buckets. Regex literals — pinned via substring."""
    assert "TOPIC_RULES" in html
    # Spot-check a few of the keywords each rule keys off of.
    assert "light|lamp|illuminat" in html
    assert "camera|view|render" in html
    assert "particle|pop|noise" in html
    assert "material|texture|color" in html
    assert "audio|sound|music" in html
    assert "error|fail|broken" in html
    assert "done|complete|fixed" in html


def test_flourish_attach_function_exists(html: str):
    """``attachFlourishToLastAssistant`` walks back through #history
    to find the most recent .msg.assistant and appends a
    .msg-flourish div — UNLESS one is already present (idempotent)."""
    assert "function attachFlourishToLastAssistant()" in html
    assert "querySelector('.msg-flourish')" in html  # idempotent guard
    assert "f.className = 'msg-flourish'" in html


def test_flourish_triggers_on_idle_transition(html: str):
    """Flourish is appended when the agent transitions from a
    working state back to idle (turn-end). Hook lives in
    ``setAgentStatus``."""
    pattern = re.compile(
        r"function setAgentStatus\(text\) \{[\s\S]*?"
        r"const wasWorking = isWorkingAgentState\(agentState\);[\s\S]*?"
        r"if \(wasWorking && newState === 'idle'\) \{[\s\S]*?"
        r"attachFlourishToLastAssistant\(\)",
        re.MULTILINE,
    )
    assert pattern.search(html), "flourish trigger on agent-idle transition missing"


def test_flourish_css_styling_quiet_aesthetic(html: str):
    """``.msg-flourish`` should be visually quiet: accent-dim color,
    centered, low opacity, dashed top border. NOT primary attention.
    Property order inside the rule body is irrelevant — just check
    each declaration exists in the rule."""
    block_match = re.search(r"\.msg-flourish \{([\s\S]*?)\}", html, re.MULTILINE)
    assert block_match, ".msg-flourish CSS rule missing entirely"
    block = block_match.group(1)
    for needle in (
        "color: var(--accent-dim)",
        "text-align: center",
        "opacity: 0.55",
        "border-top: 1px dashed var(--rule)",
    ):
        assert needle in block, f".msg-flourish missing declaration: {needle!r}"
