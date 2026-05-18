"""Regression tests for HEAD route dispatch — surfaced 2026-05-19 by
the 30-test live-audit run.

The auth gate's HEAD whitelist (v2.3.0 Bug 11 fix) lets HEAD on
``/`` / ``/index.html`` / ``/health`` / ``/favicon.ico`` bypass the
token check. But the route dispatcher only had handlers for:

* ``method in ("GET", "HEAD") and path in ("/", "/index.html")`` — covers HEAD
* ``method == "GET" and path == "/health"`` — GET only
* ``method == "GET" and path == "/favicon.ico"`` — GET only

So HEAD /health and HEAD /favicon.ico passed the auth whitelist but
fell through to the catch-all ``404 unknown route``. This breaks
browser cache-revalidation HEAD probes (the v2.3.0 fix's stated
motivation for the whitelist in the first place).

Live evidence at the time of fix:

    $ curl -I http://127.0.0.1:9987/health
    HTTP/1.1 404 Error
    Content-Length: 27

    $ curl -I http://127.0.0.1:9987/favicon.ico
    HTTP/1.1 404 Error
    Content-Length: 32

Fix: add HEAD handlers for /health (200 + empty body) and /favicon.ico
(204 + empty body, mirroring the GET behavior).
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_web_callbacks.py"


# ---------------------------------------------------------------------------
# Mock TD COMP — extends the pattern from test_standalone_csrf.py with a
# chat HTML DAT (required by GET /) and an Authmode param.
# ---------------------------------------------------------------------------


class _FakeStorage(dict):
    def fetch(self, key, default=None):
        return self.get(key, default)

    def store(self, key, value):
        self[key] = value


class _FakeDAT:
    """A textDAT stub that returns a configurable text body."""

    def __init__(self, text: str = ""):
        self.text = text


class _FakeOp:
    """Stand-in for ``op('chat_web_server')`` and friends. Exposes the
    minimal attribute surface the dispatcher pokes at."""

    def __init__(self, name: str):
        self.name = name
        self.par = SimpleNamespace(port=SimpleNamespace(eval=lambda: 9987))


class _FakeComp(_FakeStorage):
    def __init__(self):
        super().__init__()
        self._ops = {
            "chat_web_server": _FakeOp("chat_web_server"),
            "tdpilot_api_chat_html": _FakeDAT(
                "<!doctype html><html><body>chat __TDPILOT_TOKEN__</body></html>"
            ),
        }
        # Authmode defaults to "token" (matches the v2.3.0 ship default).
        self.par = SimpleNamespace(
            Authmode=SimpleNamespace(eval=lambda: "token", val="token"),
        )

    def op(self, name):
        return self._ops.get(name)


@pytest.fixture
def web_module(monkeypatch):
    comp = _FakeComp()
    monkeypatch.setattr(builtins, "parent", lambda: comp, raising=False)
    monkeypatch.setattr(builtins, "debug", lambda *a, **kw: None, raising=False)
    sys.modules.pop("tdpilot_api_web_callbacks_head_test", None)
    spec = importlib.util.spec_from_file_location("tdpilot_api_web_callbacks_head_test", str(MODULE_PATH))
    assert spec and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module, comp


def _make_request(method: str, path: str, headers: dict | None = None, body: str = ""):
    return {
        "method": method,
        "uri": path,
        "headers": dict(headers or {}),
        "data": body.encode("utf-8") if body else b"",
    }


def _make_response():
    return {}


# ---------------------------------------------------------------------------
# HEAD route dispatch — the regression set
# ---------------------------------------------------------------------------


class TestHeadHealthRoute:
    def test_head_health_returns_200_with_empty_body(self, web_module):
        """HEAD /health must mirror GET /health status (200) without body."""
        module, _ = web_module
        req = _make_request("HEAD", "/health")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 200
        assert resp.get("data", b"") == b""

    def test_head_health_does_not_require_token(self, web_module):
        """The HEAD whitelist in _check_auth means no token is needed."""
        module, _ = web_module
        req = _make_request("HEAD", "/health", headers={})  # no auth headers
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        # No 401 — auth bypassed for HEAD on whitelisted paths.
        assert resp["statusCode"] != 401, (
            f"HEAD /health without token should pass the auth gate; got {resp['statusCode']}"
        )
        assert resp["statusCode"] == 200

    def test_get_health_still_returns_200_with_body(self, web_module):
        """Make sure adding the HEAD handler didn't regress GET /health."""
        module, _ = web_module
        req = _make_request("GET", "/health")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 200
        body = resp.get("data", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        assert "ok" in body.lower()


class TestHeadFaviconRoute:
    def test_head_favicon_returns_204(self, web_module):
        """HEAD /favicon.ico must mirror GET behavior — 204 No Content."""
        module, _ = web_module
        req = _make_request("HEAD", "/favicon.ico")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 204
        assert resp.get("data", b"") == b""

    def test_head_favicon_does_not_require_token(self, web_module):
        module, _ = web_module
        req = _make_request("HEAD", "/favicon.ico", headers={})
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] != 401
        assert resp["statusCode"] == 204

    def test_get_favicon_still_returns_204(self, web_module):
        """Regression guard: HEAD addition must not change GET behavior."""
        module, _ = web_module
        req = _make_request("GET", "/favicon.ico")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 204
        assert resp.get("data", b"") == b""


class TestHeadRootRoute:
    """HEAD / and HEAD /index.html were already handled by the
    GET/HEAD dual-path. Lock that in as a regression guard so a future
    refactor that splits the GET handler doesn't drop the HEAD case."""

    def test_head_root_returns_200(self, web_module):
        module, _ = web_module
        req = _make_request("HEAD", "/")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 200

    def test_head_index_html_returns_200(self, web_module):
        module, _ = web_module
        req = _make_request("HEAD", "/index.html")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 200


class TestHeadOnNonWhitelistedPathRequiresToken:
    """The HEAD auth whitelist only covers /, /index.html, /health,
    /favicon.ico. HEAD on any other path must hit the auth gate and
    return 401 without a token. This locks the whitelist scope — a
    future refactor that broadens HEAD auth-bypass to all paths
    would break here.

    Note: this test deliberately stops at the auth check and doesn't
    require a fully-wired extension mock. The dispatcher's downstream
    paths (post-auth) all need ``_ext()`` to return non-None and
    short-circuit with 503 otherwise; that ``extension not ready``
    case is exercised live via the chat-pipe end-to-end test plan
    rather than the unit harness.
    """

    def test_head_unknown_path_without_token_is_401(self, web_module):
        module, _ = web_module
        req = _make_request("HEAD", "/genuinely-not-a-route")
        resp = _make_response()
        module.onHTTPRequest(None, req, resp)
        assert resp["statusCode"] == 401, (
            f"HEAD on non-whitelisted path must require token; got {resp['statusCode']}"
        )
