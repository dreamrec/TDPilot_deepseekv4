"""PR-27 (v2.0) — chat font-size toggle structural regressions.

The toggle is pure frontend (HTML + CSS + JS in
``td_component/tdpilot_api_chat.html``). There is no backend, no WS
protocol change, no Python integration point. These tests parse the
served HTML body as a string and pin:

  * The `<button id="font-toggle">` exists with two glyph children
    (`.fs-a`, `.fs-A`).
  * The toggle sits OUTSIDE the `.rhs` span — so the bracket
    pseudo-elements (`[ ... ]`) do NOT wrap it.
  * The CSS scales `#history` and `#input` to 17px in `:root.fs-large`
    and leaves chrome (status bar, buttons, modal headers) at its
    designed scale.
  * The active glyph is highlighted via `:root.fs-small #font-toggle
    .fs-a { color: var(--accent); }` (and the symmetric `fs-large`
    rule).
  * The JS persists via `localStorage` under the `tdpilot.fontMode`
    key, restores before any history rendering happens, and wires a
    click handler.
  * Cmd/Ctrl + `+` / `-` shortcuts call `applyFontMode` and
    `preventDefault()` so they never bleed into browser-native zoom.
  * `.rhs` has `margin-left: auto` so the toggle can sit as a third
    flex child without pushing `.rhs` to the middle of the bar.

Deliberately structural: matches the existing pattern in
``test_chat_html_state.py`` so we catch regressions without spinning
up a JS runtime in CI.
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
# DOM structure
# ---------------------------------------------------------------------------


def test_font_toggle_button_exists(html: str):
    """The toggle is a single button with the canonical id, type, and
    aria-label. Title text doubles as a hover hint. aria-pressed
    starts at "false" (small mode = default); applyFontMode keeps
    it in sync with the active mode."""
    assert 'id="font-toggle"' in html
    assert 'type="button"' in html
    # The button has aria-label (a11y), title (hover tip), and
    # aria-pressed (toggle state for screen readers).
    pattern = re.compile(
        r'<button\s+id="font-toggle"[\s\S]*?'
        r'aria-label="Toggle chat font size"[\s\S]*?'
        r'aria-pressed="false"',
        re.MULTILINE,
    )
    assert pattern.search(html), "font-toggle button missing aria-label or aria-pressed='false'"


def test_apply_font_mode_updates_aria_pressed(html: str):
    """`applyFontMode` keeps aria-pressed in sync with the active
    mode — large = pressed, small = not pressed. Lets screen readers
    announce the toggle state on focus."""
    pattern = re.compile(
        r"\$fontToggle\.setAttribute\('aria-pressed',\s*"
        r"mode === 'large' \? 'true' : 'false'\)",
    )
    assert pattern.search(html), "applyFontMode is not updating aria-pressed"


def test_font_toggle_has_focus_visible_styling(html: str):
    """Keyboard tab navigation needs a clear focus indicator that
    matches the project's existing pattern (see #input:focus). The
    browser default focus ring would clash with the chat aesthetic."""
    # Either :focus or :focus-visible should set the accent ring.
    assert "#font-toggle:focus-visible" in html
    pattern = re.compile(
        r"#font-toggle:focus-visible \{[^}]*"
        r"box-shadow:[^}]*var\(--accent\)[^}]*\}",
        re.MULTILINE,
    )
    assert pattern.search(html), "font-toggle is missing accent-colored :focus-visible ring"


def test_font_toggle_has_two_glyph_children(html: str):
    """Two spans — `.fs-a` (lowercase) and `.fs-A` (uppercase) — make
    the toggle's legend self-evident."""
    pattern = re.compile(
        r'<button\s+id="font-toggle"[\s\S]*?'
        r'<span class="fs-a">a</span>\s*'
        r'<span class="fs-A">A</span>'
        r"[\s\S]*?</button>",
        re.MULTILINE,
    )
    assert pattern.search(html), "font-toggle button missing fs-a/fs-A child spans"


def test_font_toggle_lives_outside_rhs(html: str):
    """The bracket pseudo-elements `.rhs::before` (`[`) and
    `.rhs::after` (`]`) wrap whatever sits inside `.rhs`. The toggle
    must NOT be inside that span — it sits as a sibling of `.rhs` so
    the `]` renders BEFORE the toggle, putting it visually outside
    the bracket group at the far right of the status bar."""
    # The toggle's <button> must appear AFTER the closing </span> of
    # the .rhs span and BEFORE the closing </div> of #status-bar.
    rhs_close = html.find("</span>\n  </div>\n  <!--")  # this fragment is unique to pre-PR-27
    if rhs_close == -1:
        # Either layout has changed entirely or the toggle landed
        # somewhere unexpected. Fall back to ordering assertion.
        rhs_close = html.find('</span>\n    <button id="font-toggle"')
    button_pos = html.find('<button id="font-toggle"')
    status_bar_close = html.find("</div>", button_pos)
    assert button_pos != -1, "font-toggle button not found"
    assert status_bar_close != -1, "status-bar closing </div> not found after toggle"
    # The toggle must appear after the closing </span> of the .rhs
    # span and before the closing </div> of #status-bar.
    rhs_span_close_idx = html.rfind("</span>", 0, button_pos)
    assert rhs_span_close_idx != -1, "no </span> precedes the font-toggle"


def test_font_toggle_is_status_bar_third_child(html: str):
    """`#status-bar` sequence must be `.lhs`, `.rhs`, `#font-toggle`
    in that order. Anything else means the layout will reflow."""
    sb_open = html.find('<div id="status-bar">')
    sb_close = html.find("</div>", sb_open)
    assert sb_open != -1 and sb_close != -1
    block = html[sb_open:sb_close]
    lhs_idx = block.find('class="lhs"')
    rhs_idx = block.find('class="rhs"')
    btn_idx = block.find('id="font-toggle"')
    assert 0 < lhs_idx < rhs_idx < btn_idx, (
        f"status-bar children out of order: lhs={lhs_idx} rhs={rhs_idx} font-toggle={btn_idx}"
    )


# ---------------------------------------------------------------------------
# CSS — scaling rules + active-state colors
# ---------------------------------------------------------------------------


def test_large_mode_scales_history_and_input(html: str):
    """`:root.fs-large` raises `#history` + `#input` from 13px to 17px.
    Chrome (status bar, buttons) is intentionally NOT scaled."""
    assert ":root.fs-large #history { font-size: 17px; }" in html
    assert ":root.fs-large #input   { font-size: 17px; }" in html


def test_chrome_does_not_scale(html: str):
    """`:root.fs-large` must NOT touch the status bar, buttons, or
    other chrome elements — those keep their designed scale so the
    layout doesn't reflow."""
    # Status bar sits at 11px regardless.
    assert ":root.fs-large #status-bar" not in html
    # No global font-size override on body/html either.
    assert ":root.fs-large body" not in html
    assert ":root.fs-large html" not in html


def test_glyph_sizes_match_design(html: str):
    """`a` is rendered at 9px (small-mode preview) and `A` at 13px
    (large-mode preview). The size pair is the legend itself — no
    "small" / "large" labels are needed."""
    assert "#font-toggle .fs-a { font-size: 9px;" in html
    assert "#font-toggle .fs-A { font-size: 13px;" in html


def test_active_glyph_uses_accent_color(html: str):
    """The active glyph lights up in `--accent`; the inactive stays
    `--muted`. Symmetric rules for small / large modes."""
    assert ":root.fs-small #font-toggle .fs-a { color: var(--accent); }" in html
    assert ":root.fs-large #font-toggle .fs-A { color: var(--accent); }" in html


def test_rhs_has_margin_left_auto(html: str):
    """In the 3-child status bar, `.rhs` needs `margin-left: auto` so
    it (and the toggle that follows) cluster at the right edge of
    the bar instead of getting space-between'd into the middle."""
    rhs_block_pattern = re.compile(
        r"#status-bar \.rhs \{[^}]*margin-left:\s*auto[^}]*\}",
        re.MULTILINE,
    )
    assert rhs_block_pattern.search(html), "#status-bar .rhs is missing `margin-left: auto`"


def test_toggle_overrides_status_bar_text_transform(html: str):
    """`#status-bar` is `text-transform: uppercase`. The toggle's
    lowercase `a` would render as `A` if we didn't override. Same
    for `letter-spacing`, which would space the two glyphs apart."""
    toggle_block_match = re.search(
        r"#font-toggle \{[\s\S]*?\}",
        html,
    )
    assert toggle_block_match
    block = toggle_block_match.group(0)
    assert "text-transform: none" in block
    assert "letter-spacing: 0" in block


# ---------------------------------------------------------------------------
# JS — persistence, restoration, click + keyboard handlers
# ---------------------------------------------------------------------------


def test_localstorage_key_is_namespaced(html: str):
    """The key uses the `tdpilot.` prefix to avoid collisions with
    other apps or browser extensions."""
    assert "const FONT_KEY = 'tdpilot.fontMode';" in html


def test_apply_font_mode_function_is_defined(html: str):
    """`applyFontMode(mode)` is the single source of truth for
    flipping the class + persisting the choice."""
    assert "function applyFontMode(mode)" in html
    assert "document.documentElement.classList.remove('fs-small', 'fs-large')" in html
    assert "document.documentElement.classList.add('fs-' + mode)" in html
    assert "localStorage.setItem(FONT_KEY, mode)" in html


def test_apply_font_mode_normalises_unknown_modes(html: str):
    """Anything that isn't `'small'` / `'large'` falls back to small
    — defends against a corrupted localStorage entry."""
    assert "if (mode !== 'small' && mode !== 'large') mode = 'small';" in html


def test_initial_restoration_runs_before_history_render(html: str):
    """The toggle's class must be applied to `<html>` BEFORE
    `$history.innerHTML = WELCOME_HTML;` so a user with
    `fontMode='large'` doesn't see a small-text flash on page load."""
    apply_idx = html.find("applyFontMode(_savedFontMode);")
    welcome_idx = html.find("$history.innerHTML = WELCOME_HTML;")
    assert apply_idx != -1, "initial applyFontMode call missing"
    assert welcome_idx != -1, "welcome render missing"
    assert apply_idx < welcome_idx, (
        "applyFontMode must run BEFORE the welcome render to avoid a flash of wrong-sized text on reload"
    )


def test_localstorage_read_is_guarded(html: str):
    """Private-mode browsers throw on `localStorage.getItem`. The
    initial read is wrapped in try/catch so a private window doesn't
    break the whole page."""
    pattern = re.compile(
        r"try\s*\{\s*_savedFontMode\s*=\s*localStorage\.getItem\(FONT_KEY\)",
    )
    assert pattern.search(html), "initial localStorage read is not try/catch-guarded"


def test_localstorage_write_is_guarded(html: str):
    """Private-mode browsers throw on `localStorage.setItem` too."""
    pattern = re.compile(
        r"try\s*\{\s*localStorage\.setItem\(FONT_KEY, mode\)\s*;\s*\}\s*catch",
    )
    assert pattern.search(html), "localStorage.setItem is not try/catch-guarded"


def test_click_handler_toggles_mode(html: str):
    """Click flips between small and large based on the current
    class, NOT on a tracked variable — keeps the toggle and the DOM
    state in sync if a Cmd-shortcut beats the click."""
    pattern = re.compile(
        r"\$fontToggle\.addEventListener\('click', \(\) => \{[\s\S]*?"
        r"document\.documentElement\.classList\.contains\('fs-large'\)[\s\S]*?"
        r"applyFontMode\(cur === 'large' \? 'small' : 'large'\)",
        re.MULTILINE,
    )
    assert pattern.search(html), "click handler missing or doesn't toggle off DOM state"


def test_keyboard_shortcut_for_large_mode(html: str):
    """`Cmd/Ctrl + +` (or `=` for the unshifted keycap) goes large
    AND `preventDefault`s so browser-native zoom isn't invoked."""
    pattern = re.compile(
        r"if \(cmd && \(e\.key === '\+' \|\| e\.key === '='\)\) \{[\s\S]*?"
        r"e\.preventDefault\(\);[\s\S]*?"
        r"applyFontMode\('large'\)",
        re.MULTILINE,
    )
    assert pattern.search(html), "Cmd/Ctrl+= shortcut for large mode missing"


def test_keyboard_shortcut_for_small_mode(html: str):
    """`Cmd/Ctrl + -` goes small."""
    pattern = re.compile(
        r"if \(cmd && e\.key === '-'\) \{[\s\S]*?"
        r"e\.preventDefault\(\);[\s\S]*?"
        r"applyFontMode\('small'\)",
        re.MULTILINE,
    )
    assert pattern.search(html), "Cmd/Ctrl+- shortcut for small mode missing"


def test_default_mode_is_small(html: str):
    """Default = `small` so existing users see no change unless they
    opt in. Both the localStorage fallback AND the function-arg
    normalisation must default to `small`."""
    # Initial read defaults to 'small' if storage is empty.
    assert "localStorage.getItem(FONT_KEY) || 'small'" in html
    # Saved fallback is also 'small'.
    assert "let _savedFontMode = 'small';" in html


def test_no_strict_inline_style_overrides(html: str):
    """The toggle button must NOT carry inline styles — all
    presentation lives in the stylesheet so the design vocabulary
    stays in one place."""
    pattern = re.compile(
        r'<button\s+id="font-toggle"[^>]*\sstyle=',
    )
    assert not pattern.search(html), (
        "font-toggle button has an inline style attribute — move it into the stylesheet"
    )
