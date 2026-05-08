"""PR-12 (Phase 2 / 1.8.0) — node-path chips + screenshot thumbnails.

Two enrichments to the tool result rendering layer:

* Tool result text scanned for TD node paths (regex
  ``/[A-Za-z_][A-Za-z0-9_]*(?:/...)+``); each match becomes a
  clickable chip that pre-fills the input with an inspect prompt.
  The chip stays inline-flow so the surrounding monospace JSON keeps
  its layout; it's never rendered via innerHTML.

* When a tool result has a ``data_base64`` field with an allowed
  image format (jpeg/jpg/png/webp), the chat renders an inline
  thumbnail bounded to 320×240. Click opens the lightbox overlay
  (full-size view, click or Escape to close). The data: URL is
  permitted ONLY for the <img src> here — isSafeUrl still rejects
  data: in <a href> contexts.

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
# Node path chips
# ---------------------------------------------------------------------------


def test_node_path_regex_present(iife: str):
    """The detection regex sits at module scope so all callers
    share one source of truth."""
    assert "const NODE_PATH_RE" in iife
    # Lookbehind defends against URL false-positives.
    assert "(?<![A-Za-z0-9_:/])" in iife
    # Requires at least two segments — single "/foo" stays as text.
    assert "(?:\\/[A-Za-z_][A-Za-z0-9_]*)+" in iife


def test_append_with_node_path_chips_function(iife: str):
    """The text-walker creates a chip per match and text nodes for
    everything else. Never builds an HTML string — chips and
    surrounding text are appended individually."""
    m = re.search(r"function appendWithNodePathChips\(parent, text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "appendWithNodePathChips not found"
    body = m.group(1)
    assert "createTextNode" in body
    assert "createElement('span')" in body
    assert "node-path-chip" in body
    # Click handler populates the input rather than auto-sending —
    # the user must explicitly press send. Auto-sending mid-thought
    # would surprise the user.
    assert "$input.value" in body
    assert "$input.focus()" in body
    # Reset lastIndex so consecutive calls don't skip matches.
    assert "NODE_PATH_RE.lastIndex" in body


def test_node_path_regex_python_parallel():
    """Python parallel that exercises the same intent. If the JS
    regex drifts, this gives reviewers a quick reference for what
    paths should and shouldn't match."""
    # Same pattern as the JS, minus the lookbehind (Python supports it
    # but the test only needs the matching portion).
    pat = re.compile(r"(?<![A-Za-z0-9_:/])(\/[A-Za-z_][A-Za-z0-9_]*(?:\/[A-Za-z_][A-Za-z0-9_]*)+)")
    must_match = [
        "/project1/noise1",
        "/project1/perform/render",
        "/panel1/ui_root/section",
        "/local/foo_bar/baz",
    ]
    must_not_match = [
        # Single-segment isn't a chip-worthy path.
        "/project1",
        "/foo",
        # Inside a URL — lookbehind blocks both.
        "https://example.com/foo/bar/baz",
        "ftp://x.test/a/b/c",
        # Windows path — lookbehind sees `:` and rejects.
        "C:/users/me/Desktop",
        # Numbers-leading aren't valid TD identifiers.
        "/1foo/bar",
    ]
    for s in must_match:
        assert pat.search(s), f"expected to find a path in {s!r}"
    for s in must_not_match:
        assert not pat.search(s), f"unexpectedly matched a path in {s!r}"


def test_chip_css_present(html: str):
    assert ".node-path-chip" in html
    assert ".node-path-chip:hover" in html


def test_build_result_block_uses_chip_renderer(iife: str):
    """The path that builds tool result <pre> blocks must call the
    chip-aware renderer rather than `pre.textContent = ...`. Without
    this wiring, paths in result text would render as plain monospace
    with no click affordance."""
    m = re.search(r"function buildResultBlock\(text, isError, options\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "buildResultBlock not found"
    body = m.group(1)
    assert "appendWithNodePathChips(pre, slice)" in body
    assert "appendWithNodePathChips(pre, text)" in body  # used in expand handler


# ---------------------------------------------------------------------------
# Screenshot thumbnails
# ---------------------------------------------------------------------------


def test_looks_like_screenshot_function(iife: str):
    """Detection is structural (presence of data_base64 + allowed
    format), not name-based — so any tool returning the same shape
    gets the same treatment."""
    m = re.search(r"function looksLikeScreenshotResult\(result\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "looksLikeScreenshotResult not found"
    body = m.group(1)
    assert "data_base64" in body
    assert "ALLOWED_SCREENSHOT_FORMATS" in body
    # Sanity check on the base64 contents — guards against a malicious
    # `data_base64` field smuggling raw HTML/text into the data: URL.
    assert "/^[A-Za-z0-9+/=\\s]+$/" in body


def test_allowed_screenshot_formats(iife: str):
    """Allowlist for the data: URL mime. Anything else falls through
    to the JSON path and the bytes are NOT rendered as an image."""
    m = re.search(r"const ALLOWED_SCREENSHOT_FORMATS = new Set\(\[(.+?)\]\)", iife, re.S)
    assert m, "ALLOWED_SCREENSHOT_FORMATS not found"
    formats = m.group(1)
    assert "'jpeg'" in formats
    assert "'jpg'" in formats
    assert "'png'" in formats
    assert "'webp'" in formats
    # gif animations aren't worth rendering inline; svg can carry
    # script. Both stay out of the allowlist by intent.
    assert "'svg'" not in formats
    assert "'gif'" not in formats


def test_render_screenshot_uses_data_url_with_allowed_mime(iife: str):
    m = re.search(r"function renderScreenshotInto\(parent, result\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "renderScreenshotInto not found"
    body = m.group(1)
    # data: URL constructed from the explicit format → mime mapping;
    # never assigned as a string-templated href without going through
    # this branch.
    assert "'data:' + mime + ';base64,'" in body
    assert "createElement('img')" in body
    # Click opens the lightbox.
    assert "openLightbox(img.src" in body
    # Bounded by CSS (.tool-screenshot has max-width 320 / max-height
    # 240). The class assignment proves the bounding box rule applies.
    assert "img.className = 'tool-screenshot'" in body


def test_screenshot_meta_includes_dimensions_and_size(iife: str):
    """The meta line is the only place users see the full path /
    width × height / format / size. It's the screenshot's
    breadcrumb."""
    m = re.search(r"function renderScreenshotInto\(parent, result\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "result.path" in body
    assert "result.width" in body
    assert "result.height" in body
    assert "result.size_bytes" in body
    assert "tool-screenshot-meta" in body


def test_strip_screenshot_payload_redacts_base64(iife: str):
    """The full base64 string would balloon the JSON metadata block;
    we redact it to a length marker so the user sees the rest."""
    m = re.search(r"function stripScreenshotPayload\(result\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "stripScreenshotPayload not found"
    body = m.group(1)
    assert "data_base64" in body
    assert "base64 chars" in body


def test_lightbox_dom_present(html: str):
    assert 'id="lightbox"' in html
    assert 'id="lightbox-img"' in html
    assert "#lightbox" in html  # CSS selector
    assert ".tool-screenshot" in html


def test_lightbox_open_close_handlers(iife: str):
    m = re.search(r"function openLightbox\(src, alt\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "openLightbox not found"
    open_body = m.group(1)
    assert "$lightboxImg.src = src" in open_body
    assert "$lightbox.classList.add('visible')" in open_body
    m2 = re.search(r"function closeLightbox\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m2, "closeLightbox not found"
    close_body = m2.group(1)
    assert "$lightbox.classList.remove('visible')" in close_body
    # Clear src so the in-memory image isn't held after close.
    assert "$lightboxImg.removeAttribute('src')" in close_body


def test_escape_closes_lightbox(iife: str):
    """The Escape handler is paired with the lightbox state so a
    user opening a thumbnail doesn't get stuck on a fullscreen
    image with no close button."""
    matches = re.findall(
        r"document\.addEventListener\('keydown',\s*\(e\)\s*=>\s*\{(.+?)\}\s*\)\s*;",
        iife,
        re.S,
    )
    found = False
    for body in matches:
        if "$lightbox.classList.contains('visible')" in body and "closeLightbox()" in body:
            found = True
            break
    assert found, "lightbox Escape handler missing"


def test_click_outside_closes_lightbox(iife: str):
    """Clicking the dimmed backdrop closes the lightbox — same
    affordance as ESC, just more discoverable."""
    assert "$lightbox.addEventListener('click', closeLightbox)" in iife


# ---------------------------------------------------------------------------
# appendToolResult wires screenshot detection
# ---------------------------------------------------------------------------


def test_append_tool_result_detects_screenshots(iife: str):
    """The result-rendering path checks for screenshot shape and
    routes through buildResultBlock(opts={screenshot: ...}). Without
    this wiring, the JSON dump would still appear but the inline
    thumbnail wouldn't."""
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "looksLikeScreenshotResult(payload.result)" in body
    assert "{ screenshot: screenshot }" in body
    assert "buildResultBlock(resultText, isError, blockOpts)" in body


def test_screenshot_auto_opens_details(iife: str):
    """When a screenshot is in the result, the <details> auto-opens
    so the thumbnail is immediately visible — same treatment as
    errors."""
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "if (isError || screenshot)" in body
