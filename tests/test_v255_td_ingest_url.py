"""v2.5.5 — td_ingest_url tests (v2.6.3 first slice).

Covers the URL-sandbox + fetch path. Markitdown conversion is mocked
since the [web] extras are optional; one real-network test is marked
skip-by-default and runs only with pytest --run-network.
"""

from __future__ import annotations

import io
import socket
import urllib.error
import urllib.request
from unittest import mock

import pytest

from td_mcp.web.ingest import (
    IngestResult,
    IngestTimeout,
    IngestTooLarge,
    UrlNotAllowed,
    WebIngestUnavailable,
    _host_is_private_literal,
    fetch_url,
    html_to_markdown,
    validate_url,
)

# ---------------------------------------------------------------------------
# URL sandbox — validate_url
# ---------------------------------------------------------------------------


class TestValidateUrlSchemeAllowlist:
    def test_https_allowed(self):
        assert validate_url("https://example.com/path") == "https://example.com/path"

    def test_http_rejected(self):
        with pytest.raises(UrlNotAllowed) as exc:
            validate_url("http://example.com")
        assert "http" in str(exc.value).lower()

    def test_file_scheme_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("file:///etc/passwd")

    def test_javascript_scheme_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("javascript:alert(1)")

    def test_data_scheme_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("data:text/html,<script>alert(1)</script>")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("ftp://example.com/file")

    def test_gopher_scheme_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("gopher://example.com/")

    def test_empty_url_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("")

    def test_whitespace_url_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("   ")


class TestValidateUrlHostBlocklist:
    def test_localhost_rejected(self):
        with pytest.raises(UrlNotAllowed) as exc:
            validate_url("https://localhost:9985/")
        assert "localhost" in str(exc.value)

    def test_loopback_v4_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://127.0.0.1/")

    def test_loopback_v6_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://[::1]/")

    def test_unspecified_v4_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://0.0.0.0/")

    def test_rfc1918_10_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://10.0.0.5/")

    def test_rfc1918_192_168_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://192.168.1.5/")

    def test_rfc1918_172_16_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://172.16.5.5/")

    def test_rfc1918_172_31_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://172.31.255.255/")

    def test_172_15_allowed_just_outside_rfc1918(self):
        # 172.15.x is OUTSIDE RFC1918 — must NOT be blocked.
        assert validate_url("https://172.15.0.1/")

    def test_172_32_allowed_just_outside_rfc1918(self):
        # 172.32.x is OUTSIDE RFC1918 — must NOT be blocked.
        assert validate_url("https://172.32.0.1/")

    def test_link_local_v4_rejected(self):
        # Cloud metadata service literal.
        with pytest.raises(UrlNotAllowed):
            validate_url("https://169.254.169.254/latest/meta-data/")

    def test_ipv6_ula_fc_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://[fc00::1]/")

    def test_ipv6_ula_fd_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://[fd00::1]/")

    def test_ipv6_link_local_rejected(self):
        with pytest.raises(UrlNotAllowed):
            validate_url("https://[fe80::1]/")


class TestHostIsPrivateLiteralHelper:
    """Direct unit-test the host-classifier in case it's reused elsewhere."""

    def test_known_locals(self):
        for h in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            assert _host_is_private_literal(h)

    def test_public_hosts_pass(self):
        for h in ("example.com", "github.com", "api.deepseek.com", "8.8.8.8"):
            assert not _host_is_private_literal(h)


# ---------------------------------------------------------------------------
# fetch_url — mocked HTTPS
# ---------------------------------------------------------------------------


def _fake_response(body: bytes, content_type: str = "text/html", status: int = 200):
    """Build a minimal HTTPResponse-like mock for urllib."""
    resp = mock.MagicMock()
    resp.read.side_effect = [body, b""]
    resp.headers = {"Content-Type": content_type}
    resp.status = status
    resp.getcode.return_value = status
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = None
    return resp


class TestFetchUrl:
    def test_fetch_returns_body_and_content_type(self, monkeypatch):
        body = b"<html><body>Hello</body></html>"
        opener = mock.MagicMock()
        opener.open.return_value = _fake_response(body)
        monkeypatch.setattr("td_mcp.web.ingest._build_opener", lambda: opener)
        got_body, got_ct, got_status = fetch_url("https://example.com/")
        assert got_body == body
        assert "text/html" in got_ct
        assert got_status == 200

    def test_fetch_aborts_on_size_cap(self, monkeypatch):
        """A response larger than MAX_RESPONSE_BYTES aborts before full read."""
        big_chunk = b"x" * (64 * 1024)
        resp = mock.MagicMock()
        # Stream 200 64-KB chunks → 12.5 MB total (> 5 MB cap).
        resp.read.side_effect = [big_chunk] * 200 + [b""]
        resp.headers = {"Content-Type": "text/plain"}
        resp.status = 200
        resp.getcode.return_value = 200
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        opener = mock.MagicMock()
        opener.open.return_value = resp
        monkeypatch.setattr("td_mcp.web.ingest._build_opener", lambda: opener)
        with pytest.raises(IngestTooLarge):
            fetch_url("https://example.com/big")

    def test_fetch_raises_timeout(self, monkeypatch):
        opener = mock.MagicMock()
        opener.open.side_effect = TimeoutError("timed out")
        monkeypatch.setattr("td_mcp.web.ingest._build_opener", lambda: opener)
        with pytest.raises(IngestTimeout):
            fetch_url("https://example.com/slow")

    def test_fetch_url_error_propagates(self, monkeypatch):
        opener = mock.MagicMock()
        opener.open.side_effect = urllib.error.URLError("DNS failure")
        monkeypatch.setattr("td_mcp.web.ingest._build_opener", lambda: opener)
        with pytest.raises(urllib.error.URLError):
            fetch_url("https://example.com/dns-fail")

    def test_fetch_rejects_invalid_url_before_network_call(self, monkeypatch):
        """The validate_url step must happen BEFORE any network attempt."""
        opener = mock.MagicMock()
        monkeypatch.setattr("td_mcp.web.ingest._build_opener", lambda: opener)
        with pytest.raises(UrlNotAllowed):
            fetch_url("https://127.0.0.1/admin")
        assert opener.open.called is False, "must reject sandboxed URLs without a network call"


# ---------------------------------------------------------------------------
# html_to_markdown — markitdown-availability gate
# ---------------------------------------------------------------------------


class TestHtmlToMarkdownAvailability:
    def test_without_markitdown_raises_advisory(self, monkeypatch):
        """When the [web] extras aren't installed, raise a clear advisory."""
        # Simulate markitdown missing by patching __import__.
        import builtins

        real_import = builtins.__import__

        def block_markitdown(name, *args, **kwargs):
            if name == "markitdown":
                raise ImportError("No module named 'markitdown' (test stub)")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_markitdown)
        with pytest.raises(WebIngestUnavailable) as exc:
            html_to_markdown("<html><body>x</body></html>")
        msg = str(exc.value).lower()
        assert "markitdown" in msg
        assert "[web]" in msg or "extras" in msg


# ---------------------------------------------------------------------------
# Result + tool count
# ---------------------------------------------------------------------------


class TestIngestResultDataclass:
    def test_to_dict_shape(self):
        r = IngestResult(
            url="https://example.com",
            markdown="hello",
            title="t",
            elapsed_ms=10,
            content_type="text/html",
            fetched_bytes=100,
            final_status=200,
        )
        d = r.to_dict()
        assert set(d.keys()) == {
            "url",
            "title",
            "markdown",
            "elapsed_ms",
            "content_type",
            "fetched_bytes",
            "final_status",
        }


class TestToolRegistration:
    """The MCP server must expose td_ingest_url as a real @mcp.tool."""

    def test_td_ingest_url_in_mcp_surface(self):
        import asyncio

        from td_mcp import server

        tools = asyncio.run(server.mcp.list_tools())
        names = {t.name for t in tools}
        assert "td_ingest_url" in names
