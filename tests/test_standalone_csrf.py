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
# Insecure bypass (TDPILOT_API_INSECURE=1) — escape hatch for external tools
# ---------------------------------------------------------------------------


def test_insecure_mode_bypasses_token_and_origin(web_module, monkeypatch):
    module, _ = web_module
    monkeypatch.setenv("TDPILOT_API_INSECURE", "1")
    # Same request that's normally rejected with 403/401 must pass.
    err = module._check_auth(
        "POST",
        "/send",
        {"origin": "https://attacker.example.com"},
    )
    assert err is None


# ---------------------------------------------------------------------------
# WebSocket handshake gate
# ---------------------------------------------------------------------------


def test_ws_token_extraction_from_uri(web_module):
    module, _ = web_module
    assert module._ws_token_from_uri("/?t=abc123") == "abc123"
    assert module._ws_token_from_uri("/path?foo=bar&t=xyz&z=1") == "xyz"
    assert module._ws_token_from_uri("/no-query") == ""
    assert module._ws_token_from_uri("") == ""


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
