"""Regression tests for the standalone .tox HTTP server's auth gate (1.7.1).

Pre-1.7.1 the server shipped with ``Access-Control-Allow-Origin: *`` and
no auth. A live cross-origin probe (Origin: https://attacker.example.com)
got HTTP 200 from POST /send and triggered a real DeepSeek turn. These
tests lock the gate closed.

The web_callbacks module uses TD globals (``parent()``, ``op()``, etc.).
We construct a minimal fake COMP and inject it before loading the module
via importlib so the tests run outside TouchDesigner.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_web_callbacks.py"


class _FakeStorage(dict):
    """Mimics comp.fetch / comp.store backed by a dict."""

    def fetch(self, key, default=None):
        return self.get(key, default)

    def store(self, key, value):
        self[key] = value


class _FakeOp:
    def __init__(self, name: str):
        self.name = name
        # webserver DAT exposes .par.port.eval() — return the documented default.
        self.par = SimpleNamespace(port=SimpleNamespace(eval=lambda: 9987))


class _FakeComp(_FakeStorage):
    """A COMP-shaped object with the bits web_callbacks pokes at."""

    def __init__(self):
        super().__init__()
        self._ops = {"chat_web_server": _FakeOp("chat_web_server")}
        self.par = SimpleNamespace()

    def op(self, name):
        return self._ops.get(name)


@pytest.fixture
def web_module(monkeypatch):
    """Load tdpilot_api_web_callbacks with TD globals mocked.

    Yields (module, comp) so tests can assert on the comp's storage
    state alongside the module's behaviour.
    """
    comp = _FakeComp()

    # Inject TD-runtime globals before module load.
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "debug", lambda *a, **kw: None, raising=False)

    # Force a fresh module load so any previous test's monkeypatching
    # doesn't bleed across cases.
    sys.modules.pop("tdpilot_api_web_callbacks_test", None)
    spec = importlib.util.spec_from_file_location("tdpilot_api_web_callbacks_test", str(MODULE_PATH))
    assert spec and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module, comp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(method: str, path: str, headers: dict[str, str] | None = None, body: str = ""):
    return {
        "method": method,
        "uri": path,
        "headers": dict(headers or {}),
        "data": body.encode("utf-8") if body else b"",
    }


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_session_token_is_persistent_and_random(web_module):
    module, comp = web_module
    t1 = module._session_token()
    t2 = module._session_token()
    assert t1 == t2  # stable across calls
    assert len(t1) >= 24  # token_urlsafe(24) → ~32 chars
    assert comp.fetch("tdpilot_api_session_token") == t1


def test_bootstrap_get_root_does_not_require_token(web_module):
    """GET / must serve the HTML even without a token — the page can't
    fetch its own token until it's loaded."""
    module, _ = web_module
    err = module._check_auth("GET", "/", {})
    assert err is None
    err = module._check_auth("GET", "/index.html", {})
    assert err is None
    err = module._check_auth("GET", "/health", {})
    assert err is None


def test_options_preflight_does_not_require_token(web_module):
    module, _ = web_module
    assert module._check_auth("OPTIONS", "/send", {}) is None


def test_post_send_without_token_is_rejected(web_module):
    module, _ = web_module
    err = module._check_auth("POST", "/send", {})
    assert err is not None
    status, msg = err
    assert status == 401
    assert "token" in msg.lower()


def test_post_send_with_wrong_token_is_rejected(web_module):
    module, _ = web_module
    err = module._check_auth("POST", "/send", {"x-tdpilot-token": "wrong-token-value"})
    assert err is not None
    assert err[0] == 401


def test_post_send_with_cross_origin_is_rejected_even_with_token(web_module):
    """Defense in depth: token AND origin must both be valid."""
    module, _ = web_module
    token = module._session_token()
    err = module._check_auth(
        "POST",
        "/send",
        {"x-tdpilot-token": token, "origin": "https://attacker.example.com"},
    )
    assert err is not None
    assert err[0] == 403
    assert "cross-origin" in err[1].lower()


def test_post_send_with_cross_site_fetch_metadata_is_rejected(web_module):
    """Sec-Fetch-Site: cross-site means the browser sees this as an
    attack-shaped request. Reject regardless of other headers."""
    module, _ = web_module
    token = module._session_token()
    err = module._check_auth(
        "POST",
        "/send",
        {
            "x-tdpilot-token": token,
            "origin": "http://127.0.0.1:9987",
            "sec-fetch-site": "cross-site",
        },
    )
    assert err is not None
    assert err[0] == 403


def test_post_send_with_token_and_same_origin_is_accepted(web_module):
    module, _ = web_module
    token = module._session_token()
    err = module._check_auth(
        "POST",
        "/send",
        {"x-tdpilot-token": token, "origin": "http://127.0.0.1:9987"},
    )
    assert err is None


def test_localhost_variants_in_origin_allowlist(web_module):
    module, _ = web_module
    token = module._session_token()
    for host in ("127.0.0.1", "localhost", "[::1]"):
        err = module._check_auth(
            "POST",
            "/send",
            {"x-tdpilot-token": token, "origin": f"http://{host}:9987"},
        )
        assert err is None, f"expected accept for origin http://{host}:9987, got {err}"


def test_empty_origin_treated_as_same_origin(web_module):
    """file:// loads and direct same-origin requests sometimes omit
    Origin or send 'null'. Both should pass when the token is right."""
    module, _ = web_module
    token = module._session_token()
    for origin in ("", "null"):
        err = module._check_auth("POST", "/send", {"x-tdpilot-token": token, "origin": origin})
        assert err is None, f"expected accept for origin={origin!r}, got {err}"


def test_authorization_bearer_fallback_for_external_tooling(web_module):
    """curl / external panels can use Authorization: Bearer <token>
    instead of the X-TDPilot-Token header — same security level."""
    module, _ = web_module
    token = module._session_token()
    err = module._check_auth(
        "POST",
        "/send",
        {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1:9987"},
    )
    assert err is None


def test_history_endpoint_requires_token(web_module):
    """Pre-1.7.1 GET /history was readable cross-origin — leaked the
    entire chat transcript. Must now be gated."""
    module, _ = web_module
    err = module._check_auth("GET", "/history", {})
    assert err is not None
    assert err[0] == 401


def test_firstrun_endpoint_requires_token(web_module):
    """/firstrun leaks setup state (has_api_key etc.) — also gated."""
    module, _ = web_module
    err = module._check_auth("GET", "/firstrun", {})
    assert err is not None
    assert err[0] == 401


def test_stop_and_reset_require_token(web_module):
    """DoS gate: cross-origin POST /stop or /reset would let an
    attacker continuously interrupt or wipe the user's session."""
    module, _ = web_module
    for path in ("/stop", "/reset"):
        err = module._check_auth("POST", path, {})
        assert err is not None
        assert err[0] == 401, f"{path} should require token"


# ---------------------------------------------------------------------------
# Insecure bypass (TDPILOT_API_INSECURE=1) — escape hatch for external tools.
# 2.1.3 contract: insecure mode bypasses ONLY the token check; origin
# allowlist + Sec-Fetch-Site stay enforced so a malicious browser tab
# can't drive the chat-pipe even when insecure mode is on. See
# CHANGELOG.md's 2.1.3 Security fixes for the audit narrative.
# ---------------------------------------------------------------------------


def test_insecure_mode_bypasses_token_only_not_origin(web_module, monkeypatch):
    module, _ = web_module
    monkeypatch.setenv("TDPILOT_API_INSECURE", "1")

    # Tokenless request with NO Origin header — emulates curl / Python
    # ``requests`` from local tooling. Should pass.
    err = module._check_auth("POST", "/send", {})
    assert err is None

    # Tokenless request with cross-origin Origin header — emulates a
    # malicious browser tab CSRF attempt. Must be rejected.
    err = module._check_auth(
        "POST",
        "/send",
        {"origin": "https://attacker.example.com"},
    )
    assert err is not None
    assert err[0] == 403
    assert "cross-origin" in err[1].lower()

    # Tokenless request with cross-site Sec-Fetch-Site — also blocked.
    err = module._check_auth(
        "POST",
        "/send",
        {"origin": "http://127.0.0.1:9987", "sec-fetch-site": "cross-site"},
    )
    assert err is not None
    assert err[0] == 403


# ---------------------------------------------------------------------------
# WebSocket handshake gate
# ---------------------------------------------------------------------------


def test_ws_token_extraction_from_uri(web_module):
    module, _ = web_module
    # Query-string form (legacy / external-tool friendly).
    assert module._ws_token_from_uri("/?t=abc123") == "abc123"
    assert module._ws_token_from_uri("/path?foo=bar&t=xyz&z=1") == "xyz"
    # Empty / pure-slash URIs return no token.
    assert module._ws_token_from_uri("") == ""
    assert module._ws_token_from_uri("/") == ""
    # 2026-05-11 — path-segment form (what the HTML client now emits as
    # ``ws://host:port/<token>``). Single-segment path = token.
    assert module._ws_token_from_uri("/abc123") == "abc123"
    assert module._ws_token_from_uri("/no-query") == "no-query"
    # Multi-segment paths are obviously not a token.
    assert module._ws_token_from_uri("/a/b/c") == ""


def test_ws_token_extraction_handles_url_encoding(web_module):
    module, _ = web_module
    # token_urlsafe sometimes emits =/+, the chat HTML URL-encodes them.
    assert module._ws_token_from_uri("/?t=abc%3D%3D") == "abc=="
    assert module._ws_token_from_uri("/?t=a%2Fb%2Bc") == "a/b+c"


# ---------------------------------------------------------------------------
# Token does NOT leak through the HTML serve path's marker
# ---------------------------------------------------------------------------


def test_token_marker_is_distinct_from_actual_token(web_module):
    """The HTML uses __TDPILOT_TOKEN__ as a server-replaced marker.
    Actual tokens are url-safe base64 — they never produce that string."""
    module, _ = web_module
    token = module._session_token()
    assert "__TDPILOT_TOKEN__" not in token
    assert module._TOKEN_TEMPLATE_MARKER == "__TDPILOT_TOKEN__"


# ---------------------------------------------------------------------------
# 1.8.2 — token substitution must NOT rewrite the JS sentinel
# ---------------------------------------------------------------------------
#
# Pre-1.8.2 the GET / handler did:
#   body = body.replace(_TOKEN_TEMPLATE_MARKER, _session_token())
# Default replace() rewrites ALL occurrences. The chat HTML has the
# placeholder string TWICE — once in the meta-tag content (the
# substitution target) and once in the JS sentinel:
#   const HAS_VALID_TOKEN = TOKEN && TOKEN !== '__TDPILOT_TOKEN__';
# After global substitution, the JS line read:
#   const HAS_VALID_TOKEN = TOKEN && TOKEN !== '<the-actual-token>';
# i.e. TOKEN !== TOKEN, which is always false. ``send()`` then
# refused to call /send, surfacing a "No session token in this page"
# error to the user.
#
# The fix: ``count=1`` so only the first occurrence (the meta tag,
# which appears at the top of the HTML) gets replaced. These tests
# exercise the actual GET / handler with a stub chat-html DAT that
# embeds the placeholder twice — same shape as the real chat HTML.


class _ChatHtmlDat:
    """Stub for ``op('tdpilot_api_chat_html')``. Holds a fixed HTML
    body so we can verify the token-substitution surgery."""

    def __init__(self, text: str):
        self.text = text


def _install_chat_html_dat(comp, html_text: str) -> None:
    comp._ops["tdpilot_api_chat_html"] = _ChatHtmlDat(html_text)


def _serve_root(module, comp, *, origin: str | None = None) -> str:
    """Invoke the GET / handler and return the served body as a str."""
    request = _make_request(
        "GET",
        "/",
        headers={"Origin": origin} if origin else {},
    )
    response: dict[str, Any] = {}
    module.onHTTPRequest(_FakeOp("chat_web_server"), request, response)
    data = response.get("data", b"")
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return str(data)


# A miniature stand-in for the real chat HTML's substitution targets:
#   * a meta tag whose content is the placeholder
#   * a JS sentinel that compares against the literal placeholder
_TWO_PLACEHOLDER_HTML = """<!doctype html>
<html><head>
<meta name="tdpilot-token" content="__TDPILOT_TOKEN__" />
</head><body>
<script>
  const TOKEN = document.querySelector('meta[name="tdpilot-token"]').content;
  const HAS_VALID_TOKEN = TOKEN && TOKEN !== '__TDPILOT_TOKEN__';
</script>
</body></html>"""


def test_get_root_substitutes_meta_token_only(web_module):
    """The meta-tag substitution must produce a real token in the
    served HTML's content attribute."""
    module, comp = web_module
    _install_chat_html_dat(comp, _TWO_PLACEHOLDER_HTML)
    served = _serve_root(module, comp)
    token = comp.fetch("tdpilot_api_session_token")
    assert token, "session token should have been generated by GET / handler"
    assert f'<meta name="tdpilot-token" content="{token}" />' in served, (
        "meta tag content should be the real token"
    )


def test_get_root_does_not_rewrite_js_sentinel_literal(web_module):
    """The JS sentinel ``TOKEN !== '__TDPILOT_TOKEN__'`` must survive
    substitution. Pre-1.8.2 (without ``count=1``) the global replace
    rewrote it to ``TOKEN !== '<real-token>'`` — a self-comparison
    that's always false, breaking ``send()``."""
    module, comp = web_module
    _install_chat_html_dat(comp, _TWO_PLACEHOLDER_HTML)
    served = _serve_root(module, comp)
    # The JS sentinel literal must still read the placeholder string,
    # NOT the real token.
    assert "TOKEN !== '__TDPILOT_TOKEN__'" in served, (
        "JS sentinel was rewritten by the substitution — chat would "
        "refuse to send because HAS_VALID_TOKEN evaluates to false"
    )


def test_get_root_substitutes_exactly_once(web_module):
    """Defensive: count the occurrences of the literal placeholder
    in the served body. The meta-tag occurrence is replaced; the JS
    sentinel occurrence is preserved → exactly one occurrence remains."""
    module, comp = web_module
    _install_chat_html_dat(comp, _TWO_PLACEHOLDER_HTML)
    served = _serve_root(module, comp)
    occurrences = served.count("__TDPILOT_TOKEN__")
    assert occurrences == 1, (
        f"expected exactly 1 occurrence of __TDPILOT_TOKEN__ in served "
        f"HTML (the JS sentinel) but found {occurrences}"
    )


def test_real_chat_html_has_meta_and_sentinel_placeholder():
    """Sanity: the actual chat HTML on disk must contain BOTH
    substitution sites — the meta-tag content (line ~14) and the JS
    sentinel comparison (around line 723). Comments mentioning the
    placeholder string are fine and non-functional; we just need both
    of the functional sites present so the substitution-surgery
    assumption holds."""
    chat_path = MODULE_PATH.parent / "tdpilot_api_chat.html"
    body = chat_path.read_text(encoding="utf-8")
    assert '<meta name="tdpilot-token" content="__TDPILOT_TOKEN__"' in body, (
        "meta-tag substitution target missing from chat HTML"
    )
    assert "TOKEN !== '__TDPILOT_TOKEN__'" in body, "JS sentinel comparison missing from chat HTML"
    # The meta tag MUST appear before the JS sentinel — count=1 in the
    # GET / handler relies on `replace` substituting the FIRST
    # occurrence (the meta tag). If a future edit moves the meta tag
    # below the sentinel, this canary fires before the substitution
    # silently rewrites the sentinel instead.
    meta_idx = body.find('<meta name="tdpilot-token" content="__TDPILOT_TOKEN__"')
    sentinel_idx = body.find("TOKEN !== '__TDPILOT_TOKEN__'")
    assert 0 <= meta_idx < sentinel_idx, (
        "meta tag must appear before the JS sentinel for `count=1` to substitute the right occurrence"
    )


def test_real_chat_html_substitution_preserves_js_sentinel(web_module):
    """End-to-end: load the real chat HTML, run it through the
    actual GET / handler, and verify the JS sentinel survives."""
    module, comp = web_module
    chat_path = MODULE_PATH.parent / "tdpilot_api_chat.html"
    real_html = chat_path.read_text(encoding="utf-8")
    _install_chat_html_dat(comp, real_html)
    served = _serve_root(module, comp)
    assert "TOKEN !== '__TDPILOT_TOKEN__'" in served, (
        "real-chat-HTML substitution rewrote the JS sentinel — chat would refuse to send"
    )
    # And the meta tag DOES carry a real token.
    token = comp.fetch("tdpilot_api_session_token")
    assert f'<meta name="tdpilot-token" content="{token}"' in served
