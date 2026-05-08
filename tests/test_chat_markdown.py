"""PR-8 (Phase 2 / 1.8.0) — markdown rendering + DOM sanitization.

The chat HTML applies inline-and-block markdown to **assistant** role
messages only. Every untrusted text node flows through ``textContent``
or ``createTextNode``; link hrefs are filtered through ``isSafeUrl``
before being assigned. ``innerHTML`` is reserved for the WELCOME_HTML
template constant and empty-string clears — never an arbitrary string.

These tests pin the structural contract of the markdown renderer and
its sanitization rules, plus a static enforcement check that no future
edit slips an ``elem.innerHTML = someVar`` into the IIFE.

Adversarial fixtures (``ADVERSARIAL_INPUTS``) document the payloads we
care about — ``<script>``, ``javascript:`` URIs, ``onerror=`` attrs,
``data:text/html`` data URLs. Behavioural verification of these inside
a JS engine is out of scope for unit tests, but the structural
assertions prove the renderer code paths exist and the URL allowlist
covers every payload listed.
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
    """The IIFE body — `(() => { ... })()` — is the surface that
    decides DOM construction. Carve it out so tests don't false-positive
    on prose / CSS appearances of e.g. ``innerHTML``."""
    m = re.search(r"<script>\s*\(\(\)\s*=>\s*\{(.+?)\}\)\(\);\s*</script>", html, re.S)
    assert m, "could not locate the IIFE in chat HTML"
    return m.group(1)


def _strip_js_comments(src: str) -> str:
    """Remove // line comments and /* ... */ blocks. The static
    enforcement regex must not false-positive on a comment that
    happens to contain the literal text `.innerHTML =`."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _is_safe_url_python(href: str) -> bool:
    """Python parallel of the JS isSafeUrl. Mirrors:
    - reject empty / whitespace-only
    - reject internal whitespace or control chars (browser
      normalises these when assigning .href, allowing scheme-mangled
      attacks like ``Java\\tScript:alert(1)`` to slip through a
      scheme-only check)
    - if a scheme is present, only http/https/mailto pass
    - otherwise accept hash, root-rel, query, ./, ../, bare-name."""
    if not href:
        return False
    s = href.strip()
    if not s:
        return False
    if re.search(r"[\x00-\x1f\s]", s):
        return False
    scheme_match = re.match(r"^([a-z][a-z0-9+.\-]*):", s, re.IGNORECASE)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        return scheme in ("http", "https", "mailto")
    return (
        s.startswith("#")
        or s.startswith("/")
        or s.startswith("?")
        or s.startswith("./")
        or s.startswith("../")
        or bool(re.match(r"^[\w-]", s))
    )


# ---------------------------------------------------------------------------
# Renderer surface
# ---------------------------------------------------------------------------


def test_render_markdown_into_function_exists(iife: str):
    """The block-level renderer is the entry point appendMessage uses
    for assistant role. Its absence means assistant messages would
    fall back to plain textContent — markdown silently disabled."""
    assert "function renderMarkdownInto(parent, text)" in iife


def test_render_inline_function_exists(iife: str):
    assert "function renderInline(parent, text)" in iife


def test_tokenize_inline_function_exists(iife: str):
    assert "function tokenizeInline(text)" in iife


def test_build_inline_node_function_exists(iife: str):
    assert "function buildInlineNode(tok)" in iife


def test_is_safe_url_function_exists(iife: str):
    """The URL allowlist is the single XSS guard for link hrefs."""
    assert "function isSafeUrl(href)" in iife


# ---------------------------------------------------------------------------
# appendMessage routing
# ---------------------------------------------------------------------------


def test_append_message_routes_assistant_through_markdown(iife: str):
    """Assistant messages must go through renderMarkdownInto. Anything
    else uses textContent. Pre-PR-8 every role used textContent."""
    m = re.search(r"function appendMessage\(role, message\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "appendMessage not found"
    body = m.group(1)
    assert "role === 'assistant'" in body
    assert "renderMarkdownInto(body, text)" in body
    assert "body.textContent = text" in body


def test_append_message_non_assistant_uses_text_content(iife: str):
    """The else branch — for user/tool_call/tool_result/error/hint —
    must use textContent so e.g. tool result JSON can't be reinterpreted
    as markdown by mistake."""
    m = re.search(r"function appendMessage\(role, message\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # Find the else branch.
    else_branch = re.search(r"\}\s*else\s*\{(.+?)\}", body, re.S)
    assert else_branch, "appendMessage missing else branch"
    assert "body.textContent" in else_branch.group(1)


# ---------------------------------------------------------------------------
# isSafeUrl — the XSS guard
# ---------------------------------------------------------------------------


# Each entry: (href, should_be_safe).
URL_FIXTURES = [
    # Allowed
    ("https://example.com", True),
    ("https://example.com/path?q=1", True),
    ("http://127.0.0.1:8080/", True),
    ("mailto:user@example.com", True),
    ("/relative/path", True),
    ("./file.html", True),
    ("../parent", True),
    ("#anchor", True),
    ("?query=1", True),
    ("plain-name", True),
    # Blocked — dangerous schemes
    ("javascript:alert(1)", False),
    ("JAVASCRIPT:alert(1)", False),  # case insensitive
    ("Java\tScript:alert(1)", False),  # whitespace mangling — still not http/https/mailto
    ("data:text/html,<script>alert(1)</script>", False),
    ("data:image/png;base64,AAAA", False),  # data: even for images is blocked in <a>
    ("vbscript:msgbox(1)", False),
    ("file:///etc/passwd", False),
    ("blob:https://evil.com/abc", False),
    ("about:blank", False),
    ("chrome://settings", False),
    # Blocked — empty/null
    ("", False),
    ("   ", False),
]


@pytest.mark.parametrize("href,should_be_safe", URL_FIXTURES)
def test_is_safe_url_logic_documented(href: str, should_be_safe: bool):
    """This Python parallel exercises the same rules the JS isSafeUrl
    enforces. If the JS implementation diverges, the structural test
    `test_is_safe_url_blocks_dangerous_schemes` catches the regex
    drift; this parametrised test documents the intent for review."""
    actual = _is_safe_url_python(href)
    assert actual is should_be_safe, f"URL fixture mismatch: {href!r} expected {should_be_safe}, got {actual}"


def test_is_safe_url_blocks_dangerous_schemes(iife: str):
    """Source-level check: the isSafeUrl body must reject anything
    that matches a scheme other than http/https/mailto."""
    m = re.search(r"function isSafeUrl\(href\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "isSafeUrl body not found"
    body = m.group(1)
    # Whitespace / control-char rejection (defends against
    # `Java\tScript:alert(1)` URL-mangling attacks).
    assert "/[\\x00-\\x1f\\s]/" in body
    # Scheme detection regex must be present and case-insensitive.
    assert "schemeMatch" in body
    assert "/^([a-z][a-z0-9+.-]*):/i" in body
    # The allow list is exactly http/https/mailto.
    assert "'http'" in body
    assert "'https'" in body
    assert "'mailto'" in body
    # Defensive: dangerous schemes must NOT appear as allow-list entries.
    for bad in ("'javascript'", "'data'", "'vbscript'", "'file'", "'blob'"):
        assert bad not in body, f"isSafeUrl whitelist must not include {bad}"


def test_build_inline_node_link_falls_back_to_text_on_unsafe(iife: str):
    """When isSafeUrl returns false, buildInlineNode must produce a
    plain text node — never an <a> with a hostile href."""
    m = re.search(r"function buildInlineNode\(tok\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "buildInlineNode body not found"
    body = m.group(1)
    # Locate the link branch.
    link_branch = re.search(r"if \(tok\.type === 'link'\)\s*\{(.+?)\n\s{4}\}", body, re.S)
    assert link_branch, "link branch not found in buildInlineNode"
    branch = link_branch.group(1)
    # Must check isSafeUrl, and the unsafe path returns a text node.
    assert "isSafeUrl(tok.href)" in branch
    assert "createTextNode(" in branch
    # rel must be set to noopener noreferrer to neutralise
    # window.opener-based pivots from clicked links.
    assert "noopener noreferrer" in branch


# ---------------------------------------------------------------------------
# Adversarial fixtures — declarative documentation of attacker payloads
# ---------------------------------------------------------------------------


# Each fixture: (label, raw_input, expectation_kind, expectation).
# expectation_kind:
#   "url_blocked"  — the input is a URL we want isSafeUrl to reject.
#   "verbatim"     — the renderer must emit the input as plain text
#                     (no parsing into HTML), so the <script> / on*= /
#                     attribute appears nowhere as a real element.
ADVERSARIAL_INPUTS = [
    ("inline_script", "<script>alert(1)</script>", "verbatim", "<script>"),
    ("img_onerror", "<img src=x onerror=alert(1)>", "verbatim", "onerror"),
    ("svg_onload", "<svg onload=alert(1)>", "verbatim", "onload"),
    ("iframe_javascript", '<iframe src="javascript:alert(1)"></iframe>', "verbatim", "<iframe"),
    ("link_javascript", "javascript:alert(1)", "url_blocked", "javascript"),
    ("link_javascript_caps", "JaVaScRiPt:alert(1)", "url_blocked", "JaVaScRiPt"),
    ("link_data_html", "data:text/html,<script>alert(1)</script>", "url_blocked", "data"),
    ("link_data_image", "data:image/png;base64,iVBOR", "url_blocked", "data"),
    ("link_vbscript", "vbscript:msgbox(1)", "url_blocked", "vbscript"),
    ("link_file", "file:///etc/passwd", "url_blocked", "file"),
    ("link_blob", "blob:https://evil/abc", "url_blocked", "blob"),
    ("link_about_blank", "about:blank", "url_blocked", "about"),
    ("link_chrome", "chrome://settings", "url_blocked", "chrome"),
    ("link_javascript_tab", "java\tscript:alert(1)", "url_blocked", "java"),
]


def test_adversarial_fixture_count():
    """Ensure the fixture list isn't accidentally empty after a refactor."""
    assert len(ADVERSARIAL_INPUTS) >= 14
    labels = {row[0] for row in ADVERSARIAL_INPUTS}
    assert len(labels) == len(ADVERSARIAL_INPUTS), "fixture labels must be unique"


@pytest.mark.parametrize("label,raw,kind,_payload", ADVERSARIAL_INPUTS)
def test_adversarial_fixtures_have_supported_kind(label: str, raw: str, kind: str, _payload: str):
    """Each fixture should declare a kind the test scaffolding handles."""
    assert kind in ("url_blocked", "verbatim"), f"{label}: unknown kind {kind!r}"


@pytest.mark.parametrize(
    "raw",
    [row[1] for row in ADVERSARIAL_INPUTS if row[2] == "url_blocked"],
)
def test_adversarial_url_inputs_blocked_by_python_parallel(raw: str):
    """Every URL-flavoured adversarial payload must be classified as
    unsafe by the Python rules that mirror the JS allowlist."""
    assert _is_safe_url_python(raw) is False, (
        f"adversarial URL {raw!r} was classified safe by isSafeUrl parallel — XSS regression"
    )


# ---------------------------------------------------------------------------
# Static enforcement: no .innerHTML = <untrusted-var>
# ---------------------------------------------------------------------------


# These RHS expressions are explicitly safe:
#   ''                — empty literal (clearing).
#   WELCOME_HTML      — module constant, baked into the file.
#   "...string..."    — any double/single-quote string literal.
#   `...template...`  — backtick literal (no interpolation of untrusted data).
INNERHTML_ALLOWED_RHS = (
    "''",
    '""',
    "WELCOME_HTML",
)


def test_innerhtml_assignments_use_safe_rhs(iife: str):
    """Static guard against XSS regression: every `.innerHTML =` in
    the IIFE must have a string-literal or WELCOME_HTML right-hand
    side. Allowed forms: empty string, a quoted literal, or the
    WELCOME_HTML constant. Any other RHS is a potential injection
    vector and fails this test."""
    # Strip JS comments first — a comment that legitimately mentions
    # `.innerHTML =` (e.g. doc-strings explaining this very test) must
    # not trigger the static check on the comment text itself.
    code = _strip_js_comments(iife)
    matches = list(re.finditer(r"\.innerHTML\s*=\s*([^;\n]+)", code))
    assert matches, "no .innerHTML assignments found at all — change probably moved them out of the IIFE"
    for m in matches:
        rhs = m.group(1).strip()
        rhs = rhs.rstrip(";").strip()
        is_quoted_literal = (
            (rhs.startswith("'") and rhs.endswith("'"))
            or (rhs.startswith('"') and rhs.endswith('"'))
            or (rhs.startswith("`") and rhs.endswith("`"))
        )
        is_welcome_constant = rhs == "WELCOME_HTML"
        assert is_quoted_literal or is_welcome_constant, (
            f"unsafe .innerHTML RHS detected: {rhs!r}. Only string literals and WELCOME_HTML are allowed."
        )


# ---------------------------------------------------------------------------
# Markdown features — structural assertions
# ---------------------------------------------------------------------------


def test_renderer_handles_code_fences(iife: str):
    """Triple-backtick blocks must produce <pre><code class="lang-{lang}">."""
    m = re.search(r"function renderMarkdownInto\(parent, text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "renderMarkdownInto body not found"
    body = m.group(1)
    assert "createElement('pre')" in body
    assert "createElement('code')" in body
    assert "lang-" in body  # class prefix


def test_renderer_handles_lists(iife: str):
    m = re.search(r"function renderMarkdownInto\(parent, text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "createElement(ordered ? 'ol' : 'ul')" in body
    assert "createElement('li')" in body


def test_renderer_handles_headings(iife: str):
    m = re.search(r"function renderMarkdownInto\(parent, text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # Heading regex expects 1-6 # followed by a space, then text.
    assert "/^(#{1,6})\\s+(.*)$/" in body


def test_renderer_handles_paragraph_breaks(iife: str):
    m = re.search(r"function renderMarkdownInto\(parent, text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # Empty-line branch flushes the running paragraph.
    assert "if (line === '')" in body
    assert "flushPara()" in body


def test_inline_tokenizer_priority_order(iife: str):
    """Tokenizer alternation order is the priority mechanism: code >
    link > bold > italic. If a future edit reshuffles the regex,
    nesting precedence changes silently."""
    m = re.search(r"function tokenizeInline\(text\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # Locate the master regex.
    re_match = re.search(r"const re = (/.+?/g);", body)
    assert re_match, "tokenizeInline regex not found"
    pattern = re_match.group(1)
    # Code (backtick) appears before link in the alternation.
    code_idx = pattern.find("`")
    link_idx = pattern.find(r"\[")
    bold_idx = pattern.find(r"\*\*")
    italic_idx = pattern.find(r"\*[^*\n]")
    assert 0 <= code_idx < link_idx, "code group must precede link group"
    assert link_idx < bold_idx, "link group must precede bold group"
    assert bold_idx < italic_idx, "bold group must precede single-star italic"


# ---------------------------------------------------------------------------
# Cross-check with chat HTML state tests — assistant body now has
# class="body" so existing markup assertions don't drift.
# ---------------------------------------------------------------------------


def test_message_body_has_body_class(iife: str):
    """appendMessage now creates a `.body` wrapper inside the .msg
    element so CSS can scope white-space rules to assistant content."""
    m = re.search(r"function appendMessage\(role, message\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "body.className = 'body'" in body
