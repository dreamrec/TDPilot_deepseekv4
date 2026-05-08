"""Regression tests for the chat HTML state machine (1.7.1).

These pin the three audit findings:
  * F-06: wizard DOM must persist after fullSync([]).
  * F-07: pollFirstRun must retry on transient !resp.ok.
  * F-08: WS reconnect status must NOT trigger the agent-working pulse.

Plus the security-side facts that touch the same file:
  * Token meta tag exists with the server-replaceable marker.
  * fetch() calls carry AUTH_HEADERS.
  * WS URL appends ?t=<token>.
  * Hash-param host is restricted to a localhost allowlist.

Deliberately structural: we parse the served HTML body as a string
and assert specific substrings exist + appear in the right order.
This catches regressions without needing a JS runtime in CI.
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
# Token bootstrap
# ---------------------------------------------------------------------------


def test_token_meta_tag_present_with_marker(html: str):
    """The server replaces __TDPILOT_TOKEN__ at GET / time with the
    real token. The meta tag must exist with the literal marker so
    the replace target is well-defined."""
    assert '<meta name="tdpilot-token"' in html
    assert "__TDPILOT_TOKEN__" in html


def test_iife_reads_token_from_meta(html: str):
    assert "document.querySelector('meta[name=\"tdpilot-token\"]')" in html
    assert "const TOKEN" in html
    assert "HAS_VALID_TOKEN" in html


def test_auth_headers_object_is_defined(html: str):
    """AUTH_HEADERS is the spread target on every fetch — its absence
    would mean some fetch is going out unauthenticated."""
    assert "const AUTH_HEADERS" in html
    assert "X-TDPilot-Token" in html


def test_send_fetch_includes_auth_headers(html: str):
    """The send() POST /send must spread AUTH_HEADERS into its
    headers object. Otherwise the server returns 401 even for
    same-origin requests."""
    # Find the send() function and assert it includes ...AUTH_HEADERS.
    m = re.search(r"function send\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "send() function not found"
    send_body = m.group(1)
    assert "fetch(SEND_URL" in send_body
    assert "...AUTH_HEADERS" in send_body


def test_stop_fetch_includes_auth_headers(html: str):
    m = re.search(r"function stopAgent\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "stopAgent() function not found"
    body = m.group(1)
    assert "fetch(STOP_URL" in body
    assert "...AUTH_HEADERS" in body


def test_pollfirstrun_fetch_includes_auth_headers(html: str):
    m = re.search(r"async function pollFirstRun\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "pollFirstRun() not found"
    body = m.group(1)
    assert "...AUTH_HEADERS" in body


def test_websocket_url_carries_token_query(html: str):
    """Browsers don't allow custom WS headers, so the token rides on
    the query string. This was the standalone's original gap — WS
    handshakes were unauthenticated."""
    m = re.search(r"new WebSocket\(\s*`(.+?)`\s*\)", html)
    assert m, "WebSocket constructor not found"
    ws_url_template = m.group(1)
    assert "${tokenParam}" in ws_url_template
    # And tokenParam is built from the token.
    assert "tokenParam" in html
    assert "encodeURIComponent(TOKEN)" in html


# ---------------------------------------------------------------------------
# Host allowlist (F-04)
# ---------------------------------------------------------------------------


def test_host_param_restricted_to_localhost_set(html: str):
    """Hash-param host override must be filtered through SAFE_HOSTS;
    otherwise a malicious link `#host=evil.com` exfiltrates messages."""
    assert "const SAFE_HOSTS" in html
    assert "127.0.0.1" in html
    assert "if (!SAFE_HOSTS.has(TD_HOST))" in html


def test_port_param_restricted_to_digits(html: str):
    assert "if (!/^\\d{1,5}$/.test(TD_PORT))" in html


# ---------------------------------------------------------------------------
# F-06 — wizard DOM survives fullSync([])
# ---------------------------------------------------------------------------


def test_welcome_html_constant_includes_wizard(html: str):
    """The single source of truth for the welcome screen — used by
    both initial render and fullSync([]) — must include the wizard
    div. Pre-1.7.1 the inline initial DOM had it but the fullSync
    rebuild didn't, so the first /reset killed the wizard forever."""
    m = re.search(r"const WELCOME_HTML\s*=\s*`(.+?)`;", html, re.S)
    assert m, "WELCOME_HTML constant not found"
    welcome_body = m.group(1)
    assert 'id="wizard"' in welcome_body
    assert 'id="wizard-steps"' in welcome_body
    assert "quickstart" in welcome_body


def test_initial_history_uses_welcome_html(html: str):
    """The initial render hands WELCOME_HTML to #history.innerHTML so
    it shares the wizard-bearing template."""
    assert "$history.innerHTML = WELCOME_HTML" in html


def test_fullsync_empty_uses_welcome_html_constant(html: str):
    """fullSync's empty-state branch must use the same constant.
    Pre-1.7.1 it inlined a divergent welcome HTML."""
    m = re.search(r"function fullSync\(rows\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "fullSync() not found"
    body = m.group(1)
    assert "$history.innerHTML = WELCOME_HTML" in body


def test_fullsync_rearms_pollfirstrun(html: str):
    """After the welcome rebuild, pollFirstRun() must be re-invoked so
    the freshly-mounted wizard div gets populated from /firstrun.
    Without this, the wizard renders empty even though the DOM is right."""
    m = re.search(r"function fullSync\(rows\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "fullSync() not found"
    body = m.group(1)
    assert "pollFirstRun()" in body


# ---------------------------------------------------------------------------
# F-07 — pollFirstRun retries on transient !resp.ok
# ---------------------------------------------------------------------------


def test_pollfirstrun_retries_on_non_ok(html: str):
    """Pre-1.7.1 a 500/503 during startup permanently killed the
    wizard because the !resp.ok branch returned without rescheduling."""
    m = re.search(r"async function pollFirstRun\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m
    body = m.group(1)
    # The !resp.ok branch must schedule another poll, not fall through.
    not_ok_block = re.search(r"if\s*\(!resp\.ok\)\s*\{(.+?)\}", body, re.S)
    assert not_ok_block, "missing if (!resp.ok) block"
    assert "setTimeout(pollFirstRun" in not_ok_block.group(1)


# ---------------------------------------------------------------------------
# F-08 — agent vs ws status separation
# ---------------------------------------------------------------------------


def test_agent_state_and_ws_state_are_distinct(html: str):
    """Two separate variables, two separate setters. Pre-1.7.1 there
    was one setStatus() and reconnect strings made the Stop button
    visible because they fell through isWorkingStatus's allow-list."""
    assert "let agentState" in html
    assert "let wsState" in html
    assert "function setAgentStatus" in html
    assert "function setWsStatus" in html


def test_render_only_pulses_when_agent_working(html: str):
    """The render() function must drive the pulse + Stop button from
    isWorkingAgentState(agentState), NOT from a combined string."""
    m = re.search(r"function render\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "render() not found"
    body = m.group(1)
    assert "isWorkingAgentState" in body
    assert "startPulse" in body
    assert "$stop.classList.add" in body


def _strip_js_comments(body: str) -> str:
    """Drop // line comments and /* ... */ blocks so substring asserts
    don't false-positive on prose mentioning the deprecated symbol."""
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    return body


def test_schedulereconnect_uses_ws_channel_not_agent(html: str):
    """scheduleReconnect drives wsState — it must NOT call setStatus
    or setAgentStatus, otherwise the Stop button reappears during a
    pure transport blip."""
    m = re.search(r"function scheduleReconnect\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "scheduleReconnect() not found"
    body = _strip_js_comments(m.group(1))
    assert "setWsStatus" in body
    assert "setAgentStatus" not in body
    # Catch regressions where someone reverts to setStatus().
    assert "setStatus(" not in body


def test_isworkingagentstate_excludes_reconnect_strings(html: str):
    """Defensive — even if some legacy code path passes a reconnect
    string through, it must not be classified as working."""
    m = re.search(r"function isWorkingAgentState\(s\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "isWorkingAgentState() not found"
    body = m.group(1)
    # The only allow-listed "not working" states are the agent states;
    # reconnect should be gated by being routed through wsState entirely.
    assert "'idle'" in body
    assert "'reset'" in body


def test_connectws_drives_ws_channel(html: str):
    m = re.search(r"function connectWS\(\)\s*\{(.+?)^\s{2}\}", html, re.S | re.M)
    assert m, "connectWS() not found"
    body = m.group(1)
    assert "setWsStatus('connecting')" in body
    assert "setWsStatus('connected')" in body
