"""Web ingestion tool — ``td_ingest_url``. v2.6.3 first slice (v2.5.5 ship).

Lets the agent fetch a public HTTPS page and pipe it through markitdown
so the resulting Markdown can be cited as context inline. Mirrors
``td_ocr_image``'s shape: structured result on success, structured
advisory on missing-optional-dep, structured error on each known
failure class.

Side-effect-imported from ``tool_registry.py`` like every other
``tools_*`` submodule.
"""

from __future__ import annotations

import urllib.error
from typing import Annotated, Any

from mcp.server.fastmcp import Context
from pydantic import Field

# Intentional cycle — see registry/__init__.py.
from td_mcp import tool_registry as _tr  # noqa: E402
from td_mcp.tool_registry import mcp  # noqa: E402
from td_mcp.web.ingest import (
    IngestTimeout,
    IngestTooLarge,
    UrlNotAllowed,
    WebIngestUnavailable,
    fetch_and_convert,
)


@mcp.tool(name="td_ingest_url")
async def td_ingest_url(
    ctx: Context,
    url: Annotated[
        str,
        Field(
            description=(
                "HTTPS URL to fetch + convert to Markdown. file://, http://, "
                "javascript:, data:, ftp://, gopher:// schemes are blocked. "
                "Loopback / RFC1918 / link-local hosts are also blocked "
                "(SSRF guard). Redirects are NOT followed — a 30x response "
                "returns the Location header in the body so the caller can "
                "re-validate and retry."
            ),
            min_length=1,
            max_length=2048,
        ),
    ],
) -> str:
    """Fetch a public HTTPS page and convert to Markdown via markitdown.

    Pairs naturally with the knowledge corpus: an agent that wants to
    consult external docs (TouchDesigner docs, POPX examples, Stack
    Overflow answers) can ``td_ingest_url`` the page and either cite the
    Markdown inline or hand it to ``td_knowledge_save`` to persist for
    later runs.

    Sandbox + caps:

      * **HTTPS only.** Other schemes (``file://``, ``http://``,
        ``javascript:``, ``data:``, ``ftp://``, ``gopher://``, etc.)
        rejected before any network call.
      * **No redirects.** Following ``https://attacker.example.com/`` →
        ``http://localhost/admin`` would defeat the host allowlist; the
        no-redirect handler surfaces 30x responses to the caller instead.
      * **Loopback / RFC1918 / link-local rejected.** ``localhost``,
        ``127.0.0.1``, ``10.x``, ``192.168.x``, ``172.16-31.x``,
        ``169.254.x`` (incl. the cloud metadata service literal), IPv6
        ``::1`` / ``fc00::/7`` / ``fe80::/10`` — all blocked. NOTE: DNS
        rebinding is NOT defended.
      * **Per-request timeout.** ``TDPILOT_INGEST_TIMEOUT`` env override
        (default 30 s).
      * **Max response size.** ``TDPILOT_INGEST_MAX_BYTES`` (default
        5 MB). Aborts mid-read; no partial Markdown returned.

    Returns a JSON dict::

        {
          "url": "https://...",
          "title": "...",            # markitdown's <title> if found, else ""
          "markdown": "...",         # the converted body
          "elapsed_ms": 412,
          "content_type": "text/html; charset=utf-8",
          "fetched_bytes": 17483,
          "final_status": 200
        }

    On error returns ``{"error": "<type>", "details": "...", "advice": "..."}``.

    Optional dep — install via the ``[web]`` extras::

        pip install -e .[web]
    """
    finish = _tr._start_tool(ctx, "td_ingest_url")
    try:
        result = fetch_and_convert(url)
        payload: dict[str, Any] = {"schema_version": 1, **result.to_dict()}
        return _tr._as_json_output(payload)
    except WebIngestUnavailable as exc:
        _tr._record_tool_error(ctx, "td_ingest_url")
        return _tr._as_json_output(
            {
                "error": "web_extras_not_installed",
                "advice": (
                    "td_ingest_url requires the [web] optional extras. "
                    "Install via `pip install -e .[web]` in the "
                    "tdpilot-dpsk4 install directory (or "
                    "`pip install markitdown` into the same venv as the "
                    "MCP server)."
                ),
                "details": str(exc),
            }
        )
    except UrlNotAllowed as exc:
        # Sandbox refusal (scheme, host, malformed). Return a clear
        # structured advisory instead of dragging the agent into a
        # retry loop on a URL that will never work.
        _tr._record_tool_error(ctx, "td_ingest_url")
        return _tr._as_json_output(
            {
                "error": "url_not_allowed",
                "details": str(exc),
                "advice": (
                    "td_ingest_url only fetches HTTPS URLs whose host is "
                    "not loopback / RFC1918 / link-local. Check the URL "
                    "scheme + host and retry; do NOT attempt to bypass "
                    "the sandbox with redirects or DNS tricks."
                ),
            }
        )
    except IngestTimeout as exc:
        _tr._record_tool_error(ctx, "td_ingest_url")
        return _tr._as_json_output(
            {
                "error": "ingest_timeout",
                "details": str(exc),
                "advice": (
                    "Fetch took longer than TDPILOT_INGEST_TIMEOUT (default "
                    "30 s). Check the target host or set the env var to a "
                    "larger value if the target is known-slow."
                ),
            }
        )
    except IngestTooLarge as exc:
        _tr._record_tool_error(ctx, "td_ingest_url")
        return _tr._as_json_output(
            {
                "error": "ingest_too_large",
                "details": str(exc),
                "advice": (
                    "Response exceeded TDPILOT_INGEST_MAX_BYTES (default "
                    "5 MB). Either point at a smaller page or raise the "
                    "env var if you genuinely need the larger payload."
                ),
            }
        )
    except urllib.error.URLError as exc:
        _tr._record_tool_error(ctx, "td_ingest_url")
        return _tr._as_json_output(
            {
                "error": "ingest_network_error",
                "details": str(exc),
                "advice": "DNS / TLS / connection failure. Check the URL.",
            }
        )
    except Exception as exc:  # noqa: BLE001
        _tr._record_tool_error(ctx, "td_ingest_url")
        from td_mcp.errors import format_tool_error

        return format_tool_error(exc)
    finally:
        finish()
