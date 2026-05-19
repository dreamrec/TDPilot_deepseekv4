"""Web ingestion — v2.6.3 first slice shipped under v2.5.5.

Exposes ``td_ingest_url`` so the agent can fetch web pages and convert
them to Markdown for context. Mirrors the OCR sidecar's optional-deps
pattern: ``markitdown`` ships via the ``[web]`` extras; without it,
``td_ingest_url`` returns a clear advisory.

Security
========

This is a *minimal viable* SSRF defense. Real hardening (DNS
rebinding resistance, per-redirect re-validation, IPv6 mapped-IPv4
edge cases) is deferred to a follow-up. What v2.5.5 closes:

  * Scheme allowlist — ``https://`` only. File / FTP / JavaScript
    / data / gopher URLs are rejected before any network call.
  * Obvious-local literals rejected — ``localhost``, ``127.0.0.1``,
    ``0.0.0.0``, ``::1``.
  * RFC1918 + link-local IP literals rejected: 10.x, 192.168.x,
    172.16-31.x, 169.254.x, IPv6 ULA (fc00::/7), IPv6 link-local
    (fe80::/10). The metadata-service literal ``169.254.169.254``
    is covered by the link-local check.
  * Redirects DISABLED. urllib follows them by default; we install
    a no-redirect opener so an attacker can't bait the URL allowlist
    into following ``https://attacker.example.com/`` → ``http://localhost``.
    Callers see HTTP 30x via the response status.
  * Per-request timeout (default 30 s, env-overridable).
  * Max response size cap (default 5 MB).
  * Identifying User-Agent so server logs can correlate.
"""

from __future__ import annotations

import os
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tuning knobs — env-overridable.
# ---------------------------------------------------------------------------

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("TDPILOT_INGEST_TIMEOUT", "30"))
MAX_RESPONSE_BYTES = int(os.environ.get("TDPILOT_INGEST_MAX_BYTES", str(5 * 1024 * 1024)))
USER_AGENT = "TDPilot-DPSK4/2.5.5 (+https://github.com/dreamrec/TDPilot_deepseekv4)"

_ALLOWED_SCHEMES = frozenset({"https"})
_REJECTED_HOST_LITERALS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1", "0:0:0:0:0:0:0:1"})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WebIngestUnavailable(RuntimeError):
    """Raised when the [web] extras (markitdown) aren't installed."""


class UrlNotAllowed(RuntimeError):
    """Raised when the URL fails the sandbox check (scheme, host, redirect)."""


class IngestTimeout(RuntimeError):
    """Raised when the fetch exceeds REQUEST_TIMEOUT_SECONDS."""


class IngestTooLarge(RuntimeError):
    """Raised when the response exceeds MAX_RESPONSE_BYTES."""


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class IngestResult:
    """Structured output from a successful ingest."""

    url: str
    markdown: str
    title: str | None = None
    elapsed_ms: int = 0
    content_type: str = ""
    fetched_bytes: int = 0
    final_status: int = 200

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title or "",
            "markdown": self.markdown,
            "elapsed_ms": self.elapsed_ms,
            "content_type": self.content_type,
            "fetched_bytes": self.fetched_bytes,
            "final_status": self.final_status,
        }


# ---------------------------------------------------------------------------
# URL sandbox
# ---------------------------------------------------------------------------


def _host_is_private_literal(host: str) -> bool:
    """Return True if ``host`` is a local-only or private IP literal.

    Conservative — only catches obvious literals. DNS-rebinding-style
    attacks (where the hostname resolves to a public IP at validation
    time and then a private one at fetch time) are NOT defended here.
    """
    if host in _REJECTED_HOST_LITERALS:
        return True
    # IPv4 RFC1918 + link-local prefixes (string match is sufficient since
    # the host comes from urlparse which strips brackets and ports).
    if host.startswith("10.") or host.startswith("192.168.") or host.startswith("169.254."):
        return True
    if host.startswith("172."):
        # 172.16.0.0/12 — second octet 16..31 inclusive.
        parts = host.split(".")
        if len(parts) >= 2:
            try:
                second = int(parts[1])
                if 16 <= second <= 31:
                    return True
            except ValueError:
                pass
    # IPv6 — urlparse strips brackets, so we see "fc00::1" not "[fc00::1]".
    if host.startswith("fc") or host.startswith("fd"):
        # IPv6 ULA fc00::/7 — first two hex chars in fc..fd. Tighter check:
        # the first byte (two hex chars) of an fc/fd address starts with "fc" or "fd".
        return True
    if host.startswith("fe8") or host.startswith("fe9") or host.startswith("fea") or host.startswith("feb"):
        # IPv6 link-local fe80::/10 — first 10 bits 1111111010, so first
        # hex pair fe80..febf.
        return True
    return False


def validate_url(url: str) -> str:
    """Parse + validate ``url``. Returns the canonical URL string on success.

    Raises :class:`UrlNotAllowed` on any failure (unparseable, wrong
    scheme, blocked host).
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlNotAllowed("URL is empty")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UrlNotAllowed(f"unparseable URL: {exc}") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlNotAllowed(
            f"URL scheme {scheme!r} is not allowed; only "
            f"{sorted(_ALLOWED_SCHEMES)} permitted. (file://, http://, "
            f"javascript:, data:, ftp://, gopher:// etc. are blocked.)"
        )
    if not parsed.hostname:
        raise UrlNotAllowed("URL has no hostname")
    host = parsed.hostname.lower()
    if _host_is_private_literal(host):
        raise UrlNotAllowed(
            f"host {host!r} is a loopback / private / link-local literal — "
            f"SSRF guard rejects it before any network call."
        )
    return url


# ---------------------------------------------------------------------------
# Fetch (HTTPS, no redirects, capped size, timeout)
# ---------------------------------------------------------------------------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Surface 30x responses to the caller instead of following them.

    Following redirects would let an attacker bait the URL allowlist
    (``https://attacker.example.com/redirect?to=http://localhost/``).
    With this handler, the redirect target is visible in ``Location``
    and the caller can re-validate before deciding to follow.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None  # raising None aborts the redirect


def _build_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirectHandler())


def _decode_with_content_type_charset(body: bytes, content_type: str) -> str:
    """Decode ``body`` honouring the Content-Type charset hint if present."""
    encoding = "utf-8"
    match = re.search(r"charset=([\w\-]+)", content_type, flags=re.IGNORECASE)
    if match:
        encoding = match.group(1).lower()
    try:
        return body.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def fetch_url(url: str) -> tuple[bytes, str, int]:
    """Validated HTTPS fetch — returns ``(body_bytes, content_type, status)``.

    Raises :class:`UrlNotAllowed`, :class:`IngestTimeout`,
    :class:`IngestTooLarge`, or ``urllib.error.URLError`` on failure.
    """
    safe_url = validate_url(url)
    req = urllib.request.Request(safe_url, headers={"User-Agent": USER_AGENT})
    opener = _build_opener()
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            content_type = resp.headers.get("Content-Type", "").strip()
            status = getattr(resp, "status", None) or resp.getcode() or 200
            chunks = []
            total = 0
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise IngestTooLarge(
                        f"response exceeds {MAX_RESPONSE_BYTES} bytes (read {total} bytes so far)"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            return body, content_type, status
    except urllib.error.HTTPError as exc:
        # urllib raises HTTPError on 4xx/5xx — preserve the status for callers.
        content_type = exc.headers.get("Content-Type", "").strip() if exc.headers else ""
        body = exc.read() if exc.fp else b""
        return body, content_type, exc.code
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            raise IngestTimeout(f"fetch timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc
        raise
    except TimeoutError as exc:
        raise IngestTimeout(f"fetch timed out after {REQUEST_TIMEOUT_SECONDS}s") from exc


# ---------------------------------------------------------------------------
# HTML → Markdown via markitdown (optional dep)
# ---------------------------------------------------------------------------


def html_to_markdown(html: str) -> tuple[str, str | None]:
    """Convert an HTML string to Markdown. Returns ``(markdown, title)``.

    Raises :class:`WebIngestUnavailable` if the ``[web]`` extras are
    not installed.
    """
    try:
        from markitdown import MarkItDown  # type: ignore[import-not-found]
    except ImportError as exc:
        raise WebIngestUnavailable(
            "markitdown is not installed in the MCP server's Python "
            "environment. Install via `pip install -e .[web]` (or "
            f"`pip install tdpilot-dpsk4[web]`). Underlying error: {exc}"
        ) from exc
    import tempfile

    md = MarkItDown()
    # NamedTemporaryFile with delete=False — markitdown needs to open the
    # file by path AFTER our handle closes; we manually unlink in finally.
    # The with-block satisfies SIM115 (proper context-managed open).
    with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8") as tmp:
        tmp.write(html)
        tmp_path = tmp.name
    try:
        result = md.convert(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    markdown = getattr(result, "text_content", None) or str(result)
    title = getattr(result, "title", None)
    return markdown, title


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def fetch_and_convert(url: str) -> IngestResult:
    """Fetch ``url`` (validated, capped, no-redirect) and convert to Markdown.

    Composition of :func:`fetch_url` + :func:`html_to_markdown`.
    """
    start = time.monotonic()
    body, content_type, status = fetch_url(url)
    html = _decode_with_content_type_charset(body, content_type)
    markdown, title = html_to_markdown(html)
    return IngestResult(
        url=url,
        markdown=markdown,
        title=title,
        elapsed_ms=int((time.monotonic() - start) * 1000),
        content_type=content_type,
        fetched_bytes=len(body),
        final_status=status,
    )
