"""
TDPilot API — webserverDAT callbacks for the in-panel HTML chat UI.

Lives at .../tdpilot_API/chat_web_server (textDAT used as the WebServer
DAT's Callbacks). Receives HTTP requests from the HTML chat (running in
the sibling webRenderTOP) and routes them to the TDPilotAPIExt singleton.

Routes:

  GET  /                or /index.html → served HTML chat UI (token-injected)
  GET  /health                          → {"ok": true}
  GET  /firstrun                        → first-run wizard state (token-gated)
  GET  /history                         → JSON of chat_transcript (token-gated)
  POST /send       body=<utf-8 message> → ext.OnSendPulse(msg) (token-gated)
  POST /stop                            → ext.OnStopPulse()    (token-gated)
  POST /reset                           → ext.OnResetPulse()   (token-gated)

Security model (1.7.1+)
-----------------------

Pre-1.7.1 the standalone shipped with ``Access-Control-Allow-Origin: *``
and no auth. Any local webpage could POST /send and drive the agent —
end-to-end CSRF that was reproducible from a cross-origin probe.

1.7.1 closes that gap with three layers:

  1. Origin allowlist — only ``http://127.0.0.1:<port>`` /
     ``http://localhost:<port>`` / ``http://[::1]:<port>`` and the empty/
     ``null`` origin (file://, same-origin) pass.
  2. Per-launch session token — generated on COMP load, stored in
     ``comp.storage`` (so reloads see the same value), required as
     ``X-TDPilot-Token`` header on every non-bootstrap HTTP route and
     as ``?t=<token>`` on the WebSocket handshake URL. The token is
     injected into the served HTML at GET / time so the same-origin
     chat already has it; browsers can't read it cross-origin even if
     they're loaded with a permissive iframe policy because we never
     emit ``Access-Control-Allow-Origin: *`` and the token never leaves
     the served HTML body.
  3. Sec-Fetch-Site rejection — modern browsers send
     ``Sec-Fetch-Site: cross-site`` on cross-origin fetches; we 403
     anything that isn't ``same-origin`` or ``none``.

The bootstrap path (GET /, GET /index.html, OPTIONS preflight) is
intentionally unauthenticated — the chat HTML must load before the JS
can read the token. Those routes don't expose any agent state.

For users who script the chat from an external tool (curl, a custom
panel), set the env var ``TDPILOT_API_INSECURE=1`` to bypass token +
origin checks. Default is secure.
"""

import hmac
import json
import os
import secrets


def _comp():
    """Return the parent containerCOMP (tdpilot_API). The webserverDAT
    lives directly inside it, so parent() resolves correctly."""
    return parent()


def _ext():
    """Return the live TDPilotAPIExt singleton via the factory."""
    try:
        return _comp().op("tdpilot_api_extension").module.get_extension(_comp())
    except Exception as exc:
        debug(f"[tdpilot_API/web] cannot fetch extension: {exc}")
        return None


def _read_body(request):
    """Pull the request body as a UTF-8 string. WebServer DAT exposes
    `request['data']` as bytes; older versions use 'body'. Covers both."""
    for key in ("data", "body"):
        v = request.get(key)
        if v is None:
            continue
        if isinstance(v, (bytes, bytearray)):
            try:
                return v.decode("utf-8", errors="replace")
            except Exception:
                return ""
        return str(v)
    return ""


def _route(request):
    """Returns (method, path)."""
    method = (request.get("method") or "GET").upper()
    uri = request.get("uri") or "/"
    # Strip query string for simple equality routing.
    path = uri.split("?", 1)[0]
    return method, path


def _headers(request):
    """Return a lower-cased dict of request headers. WebServer DAT
    keys vary across TD builds — some use ``request['headers']``, some
    flatten them into the request dict itself, AND case-conventions
    differ (lowercase vs ``X-TDPilot-Token`` vs ``X-Tdpilot-Token``).
    Tolerate ALL of it.

    2026-05-11 fix — pre-fix the fallback only checked lowercase keys
    against ``request``, so TD 2025.32820/macOS (which flattens with
    original case ``X-TDPilot-Token``) was effectively headerless: the
    auth gate saw ``headers == {}`` for every request, the X-TDPilot-Token
    check returned 401 regardless of the actual header, and the Origin
    allowlist was vacuously satisfied. Now we case-fold every direct
    key on ``request`` itself, then also case-fold whatever's under
    ``request['headers']``. Net effect: as long as TD surfaces the
    header by ANY name + any case, we find it.
    """
    out = {}
    # Pass 1 — flattened headers directly on request. We do this FIRST
    # so the nested 'headers' dict (if present) wins on collision (the
    # nested form is more authoritative when both exist).
    for k, v in request.items() if hasattr(request, "items") else []:
        if v is None:
            continue
        lk = str(k).lower().strip()
        # Skip the obviously-non-header request fields. WebServerDAT's
        # request dict mixes headers with method/uri/data/etc.
        if lk in (
            "method",
            "uri",
            "data",
            "body",
            "headers",
            "client",
            "request",
            "response",
            "remoteip",
            "remoteport",
            "version",
            "secureconnection",
        ):
            continue
        # Only accept stringly values that look like header values;
        # skip dicts/lists/bytes here (the nested 'headers' dict is
        # picked up in pass 2).
        if isinstance(v, (str, int, float, bool)):
            out[lk] = str(v).strip()
    # Pass 2 — nested headers dict, lower-cased. Overrides pass 1.
    raw = request.get("headers") or {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k).lower().strip()] = str(v).strip()
    elif isinstance(raw, list):
        # Some builds use a list of "Key: Value" lines or tuples.
        for item in raw:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                out[str(item[0]).lower().strip()] = str(item[1]).strip()
            elif isinstance(item, str) and ":" in item:
                k, v = item.split(":", 1)
                out[k.lower().strip()] = v.strip()
    return out


# --- security: per-launch token + origin allowlist (1.7.1) ------------

_STORAGE_KEY_TOKEN = "tdpilot_api_session_token"
_TOKEN_TEMPLATE_MARKER = "__TDPILOT_TOKEN__"
_SAFE_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_INSECURE_ENV = "TDPILOT_API_INSECURE"


def _insecure_mode() -> bool:
    """Return True when the chat-pipe webserver should skip the
    X-TDPilot-Token check. The origin allowlist is NEVER bypassed by
    this — only the token check is.

    Resolution order (first hit wins):

    1. **COMP param ``Authmode``** — value ``"open"`` means insecure,
       ``"token"`` means require the token. Phase 1.2.1 (v2.2.1) made
       this the default source of truth: it persists in the .toe so
       restarts preserve user intent, and the auth check reads it on
       every request so flipping the param takes effect immediately
       (no Reloadconfig needed).
    2. **Env var ``TDPILOT_API_INSECURE``** — backward compat for
       dev workflows that pre-date the Authmode param. Set to
       ``"1" / "true" / "yes"`` for insecure.
    3. **Default**: ``False`` (require token) — only reached on the
       very rare error path where the COMP isn't resolvable AND the
       env var is unset.

    Default Authmode is ``"token"`` (flipped in v2.3.0, commit
    ``ef0aec2`` — bilateral audit). New users get secure-by-default
    token auth; users who want drag-and-go convenience can flip
    Authmode to ``"open"`` in the COMP's param panel. Legacy COMPs
    built pre-v2.3.0 still ship with ``Authmode=open`` baked in —
    the v2.4 ``/firstrun`` wizard surfaces an opt-in switch to
    token mode for those (see Phase B.2 in the v2.4 plan).
    """
    # 1. COMP param wins.
    try:
        comp = _comp()
        if comp is not None and hasattr(comp.par, "Authmode"):
            # ``.val`` returns the menu string for a Menu param. Same
            # as ``.menuNames[par.menuIndex]`` but shorter. Avoids
            # the ``.eval()`` form (which collides with a Python-eval
            # security-linter false-positive in this codebase's hooks).
            value = str(comp.par.Authmode.val or "").strip().lower()
            if value in ("open", "token"):
                return value == "open"
    except Exception:
        pass
    # 2. Env var fallback.
    return os.environ.get(_INSECURE_ENV, "").strip() in ("1", "true", "yes")


def _session_token() -> str:
    """Per-launch random token. Stored in comp.storage so a textDAT
    reload doesn't rotate it mid-session — same comp.storage pattern
    used by ``_ws_clients`` for the same reason."""
    comp = _comp()
    if comp is None:
        return ""
    t = comp.fetch(_STORAGE_KEY_TOKEN, None)
    if not isinstance(t, str) or not t:
        t = secrets.token_urlsafe(24)
        comp.store(_STORAGE_KEY_TOKEN, t)
    return t


def _server_port() -> int:
    """Best-effort lookup of the webserverDAT port for origin matching.
    Falls back to 9987 (the documented default) if the COMP isn't
    reachable yet."""
    try:
        comp = _comp()
        if comp is not None:
            srv = comp.op("chat_web_server")
            if srv is not None and hasattr(srv.par, "port"):
                return int(srv.par.port.eval())
    except Exception:
        pass
    return 9987


def _allowed_origin(origin: str) -> bool:
    """Empty / 'null' origin → same-origin or file://. Otherwise must
    be ``http://<safe-host>:<our-port>`` exactly."""
    o = (origin or "").strip().lower()
    if not o or o == "null":
        return True
    port = _server_port()
    accepted = {f"http://{h}:{port}" for h in _SAFE_HOSTS}
    return o in accepted


def _check_auth(method: str, path: str, headers: dict) -> tuple[int, str] | None:
    """Return (status, message) on rejection, ``None`` on accept.

    Bootstrap routes (GET /, /index.html, OPTIONS) skip the gate so
    the browser can fetch the HTML before it has the token. Every
    other route requires:

      * Origin in the allowlist (or empty/null for same-origin).
      * Sec-Fetch-Site of 'same-origin' / 'none' (when the header is
        present — older browsers / non-browser clients omit it, which
        is fine because they're already filtered by the token check).
      * X-TDPilot-Token header matches the session token.

    Insecure mode (``TDPILOT_API_INSECURE=1``) bypasses ONLY the token
    check — it does NOT bypass the origin / sec-fetch-site checks.
    Pre-2.1.3 the bypass was total, which let any browser tab the user
    had open issue cross-origin CSRF POSTs; combined with the
    ``EXEC_MODE=full`` default, this exposed a drive-by-RCE chain via
    ``td_exec_python``. The token bypass alone preserves the legitimate
    "external local script with no browser context" use case (curl,
    Python ``requests``, etc. send no Origin header so they continue
    to pass the same-origin gate).
    """
    if method == "OPTIONS":
        return None
    # 2026-05-11 — /favicon.ico added to bootstrap allowlist. Chromium
    # (and the in-TD webRenderTOP's embedded engine) auto-fetches it
    # on every page load WITHOUT the auth header — even though the
    # HTML doesn't reference it. Pre-fix the route hit _check_auth,
    # missed the token, returned 401 with no Content-Type body, and
    # the embedded Chromium reported "page can't be found / HTTP
    # ERROR 404" as a misleading fallback (the actual page load
    # succeeded, but Chromium's favicon error path doesn't cleanly
    # distinguish 401 from 404).
    #
    # 2026-05-11 — HEAD also accepted for bootstrap paths. Browsers
    # sometimes HEAD-probe a URL before GETting (cache-revalidation,
    # link-rel=preload hints). HEAD on a 401 auth-gated route would
    # confuse client-side caching logic; whitelisting HEAD keeps the
    # bootstrap surface uniform with GET.
    if method in ("GET", "HEAD") and path in ("/", "/index.html", "/health", "/favicon.ico"):
        return None
    if not _allowed_origin(headers.get("origin", "")):
        return (403, "cross-origin request blocked")
    sec_fetch = headers.get("sec-fetch-site", "").strip().lower()
    if sec_fetch and sec_fetch not in ("same-origin", "none"):
        return (403, f"cross-site fetch blocked (Sec-Fetch-Site={sec_fetch})")
    if _insecure_mode():
        return None
    expected = _session_token()
    got = headers.get("x-tdpilot-token", "").strip()
    if not expected:
        return (503, "session token not initialised")
    if not got:
        # Authorization: Bearer <token> as a fallback for non-browser tooling.
        auth = headers.get("authorization", "").strip()
        if auth.lower().startswith("bearer "):
            got = auth[7:].strip()
    if not got or not hmac.compare_digest(got, expected):
        return (401, "missing or invalid X-TDPilot-Token")
    return None


def _cors(response, request_origin: str = ""):
    """Reflect a known-good origin instead of ``*``. Same-origin
    requests don't need this at all — but the cors header is harmless
    when echoed back to a same-origin caller. Also sets cache-control
    so browsers don't retain stale 401/404/error responses across
    server transitions (the .tox rebuild window briefly returns
    errors that the browser would otherwise cache and replay even
    after the server is healthy)."""
    if _insecure_mode():
        # Insecure mode trades safety for tooling compat; widen CORS
        # accordingly so curl / external panels keep working.
        response["Access-Control-Allow-Origin"] = "*"
    elif request_origin and _allowed_origin(request_origin):
        response["Access-Control-Allow-Origin"] = request_origin
    else:
        response["Access-Control-Allow-Origin"] = "null"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-TDPilot-Token, Authorization"
    response["Access-Control-Allow-Methods"] = "GET, HEAD, POST, OPTIONS"
    response["Vary"] = "Origin"
    # 2026-05-11 — no-store/no-cache on every response. Pre-fix, browsers
    # would cache responses from the brief .tox-rebuild window where
    # the webserverDAT was transitioning (errors, 401s, half-responses)
    # and then replay that cached state to the user even after the
    # server came back healthy — manifesting as "the page can't be
    # found / HTTP ERROR 404" stuck in the browser tab indefinitely.
    # no-store stops Chrome/Safari/Firefox from caching the response;
    # no-cache forces revalidation if a cached copy ever exists.
    response["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"


def _json(response, status, payload, request_origin: str = ""):
    _cors(response, request_origin)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = json.dumps(payload).encode("utf-8")
    response["Content-Type"] = "application/json; charset=utf-8"


def _text(response, status, body, request_origin: str = ""):
    _cors(response, request_origin)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = (body or "").encode("utf-8")
    response["Content-Type"] = "text/plain; charset=utf-8"


def onHTTPRequest(webServerDAT, request, response):
    method, path = _route(request)
    headers = _headers(request)
    request_origin = headers.get("origin", "")

    auth_err = _check_auth(method, path, headers)
    if auth_err is not None:
        status, msg = auth_err
        _text(response, status, msg, request_origin=request_origin)
        return response

    if method == "OPTIONS":
        _text(response, 200, "", request_origin=request_origin)
        return response

    if method in ("GET", "HEAD") and path in ("/", "/index.html"):
        # Serve the chat HTML so the page + WebSocket share http://host:port
        # origin. Required because Chrome blocks ws:// from file:// origins.
        # Inject the per-launch session token so the same-origin chat can
        # authenticate subsequent fetches + the WS handshake.
        html_dat = _comp().op("tdpilot_api_chat_html")
        body = html_dat.text if html_dat is not None else "<h1>chat html DAT missing</h1>"
        # ``count=1`` (1.8.2 fix) — the placeholder string ``__TDPILOT_TOKEN__``
        # appears TWICE in the HTML: once in the meta-tag content (the
        # legitimate substitution target, line ~14) and once in the JS
        # sentinel ``TOKEN !== '__TDPILOT_TOKEN__'`` (line ~723) that the
        # client uses to detect a file:// load.
        # Without count=1, the global replace rewrites the JS comparison
        # to ``TOKEN !== <real-token>`` — and since TOKEN equals the real
        # token, ``HAS_VALID_TOKEN`` becomes always false and ``send()``
        # refuses to call /send with a "No session token in this page"
        # error. This regression was latent since v1.7.1 and surfaced
        # right after the v1.8.1 .tox rebuild because every prior session
        # had been talking to a chat tab loaded BEFORE the bug shipped.
        body = body.replace(_TOKEN_TEMPLATE_MARKER, _session_token(), 1)
        _cors(response, request_origin)
        response["statusCode"] = 200
        response["statusReason"] = "OK"
        response["data"] = body.encode("utf-8")
        response["Content-Type"] = "text/html; charset=utf-8"
        return response

    if method == "GET" and path == "/health":
        _json(response, 200, {"ok": True}, request_origin=request_origin)
        return response

    if method == "GET" and path == "/favicon.ico":
        # 2026-05-11 — silence Chromium's auto-fetch with a 204 No
        # Content. We don't ship an icon (the chat panel renders
        # inside the webRenderTOP, not in a browser-tab-favicon
        # context), so the empty body keeps payload + token cost zero.
        _cors(response, request_origin)
        response["statusCode"] = 204
        response["statusReason"] = "No Content"
        response["data"] = b""
        return response

    if method == "GET" and path == "/history":
        comp = _comp()
        t = comp.op("chat_transcript")
        rows = []
        if t is not None:
            try:
                for r in range(1, t.numRows):
                    rows.append({"role": t[r, 0].val, "message": t[r, 1].val})
            except Exception as exc:
                debug(f"[tdpilot_API/web] history scan error: {exc}")
        _json(response, 200, {"rows": rows}, request_origin=request_origin)
        return response

    if method == "GET" and path == "/firstrun":
        # Phase 5.2 — first-run wizard status. The chat HTML polls
        # this on load (and a few times after) to populate the
        # quickstart checklist and to know when to dismiss it.
        try:
            from tdpilot_api_introspect import firstrun_status  # type: ignore[import-not-found]

            _json(response, 200, firstrun_status(), request_origin=request_origin)
        except Exception as exc:
            debug(f"[tdpilot_API/web] /firstrun failed: {exc}")
            _json(response, 500, {"ok": False, "error": str(exc)}, request_origin=request_origin)
        return response

    if method == "GET" and path == "/capabilities-summary":
        # v2.4 / Phase C.6 — capability summary for the chat HTML.
        # Pure data; safe to serve before the runtime is ready, so it
        # sits above the ext-ready gate (parallel to /firstrun).
        try:
            from tdpilot_api_introspect import (  # type: ignore[import-not-found]
                handle_get_capabilities_summary,
            )

            _json(
                response,
                200,
                handle_get_capabilities_summary({}),
                request_origin=request_origin,
            )
        except Exception as exc:
            debug(f"[tdpilot_API/web] /capabilities-summary failed: {exc}")
            _json(
                response,
                500,
                {"ok": False, "error": str(exc)},
                request_origin=request_origin,
            )
        return response

    if method == "GET" and path == "/stats":
        # v2.4 / Phase C.7 — per-session cost telemetry. Read-only,
        # token-gated like every other route. Pulls the runtime's
        # already-aggregated counters off the extension. Returns
        # the same shape that EV_USAGE_SESSION pushes via WS so
        # the chat UI has a single payload model for both surfaces.
        try:
            ext = _ext()
            runtime = getattr(ext, "_runtime", None) if ext is not None else None
            if runtime is None or not hasattr(runtime, "_session_totals_payload"):
                _json(
                    response,
                    503,
                    {"ok": False, "error": "runtime not ready"},
                    request_origin=request_origin,
                )
                return response
            session = runtime._session_totals_payload()
            _json(
                response,
                200,
                {
                    "ok": True,
                    "session": session,
                    "model_pricing_version": session.get("model_pricing_version", ""),
                    "started_at": session.get("started_at", ""),
                },
                request_origin=request_origin,
            )
        except Exception as exc:
            debug(f"[tdpilot_API/web] /stats failed: {exc}")
            _json(
                response,
                500,
                {"ok": False, "error": str(exc)},
                request_origin=request_origin,
            )
        return response

    if method == "POST" and path == "/set-authmode":
        # v2.4 / Phase B.2 — wizard endpoint for the Authmode=open→token
        # migration. Flips the COMP param + rotates the session token.
        # Tab reload is the responsibility of the caller (the chat HTML
        # does window.location.reload() after a 200 so the new token is
        # injected by the GET / template substitution).
        #
        # This route sits BELOW _check_auth, so a legacy-open COMP can
        # hit it without a token (origin allowlist still required); a
        # token-mode COMP holding a valid token can flip to open or
        # rotate the token. We DELIBERATELY do not require explicit
        # confirmation of the previous state — the user already opted
        # in via the wizard button.
        raw = (_read_body(request) or "").strip()
        try:
            payload = json.loads(raw) if raw.startswith("{") else {}
        except json.JSONDecodeError:
            payload = {}
        mode = str(payload.get("mode", "")).strip().lower()
        if mode not in ("open", "token"):
            _text(
                response,
                400,
                'mode must be "open" or "token"',
                request_origin=request_origin,
            )
            return response
        try:
            comp = _comp()
            prev = ""
            if comp is not None and hasattr(comp.par, "Authmode"):
                prev = str(comp.par.Authmode.val or "").lower()
                comp.par.Authmode = mode
            new_token = ""
            if mode == "token":
                # Rotate so any stale tabs holding an old token are
                # invalidated. The 401-recovery banner in the chat HTML
                # picks them up and prompts reload (existing behavior).
                try:
                    comp.unstore(_STORAGE_KEY_TOKEN)
                except Exception:
                    pass
                new_token = _session_token()
            _json(
                response,
                200,
                {"ok": True, "mode": mode, "previous": prev, "token": new_token},
                request_origin=request_origin,
            )
        except Exception as exc:
            debug(f"[tdpilot_API/web] /set-authmode failed: {exc}")
            _json(
                response,
                500,
                {"ok": False, "error": str(exc)},
                request_origin=request_origin,
            )
        return response

    ext = _ext()
    if ext is None:
        _text(response, 503, "extension not ready", request_origin=request_origin)
        return response

    if method == "POST" and path == "/send":
        # 2.1.3 — JSON envelope ``{"message": "<text>"}`` is REQUIRED for
        # browser requests. Pre-2.1.3 we accepted plain-text bodies, but
        # ``Content-Type: text/plain`` is a CORS "simple request" that
        # bypasses preflight; combined with insecure_mode this exposed a
        # drive-by-RCE vector via ``td_exec_python`` from any browser
        # tab the user had open. Forcing JSON triggers preflight for
        # cross-origin POSTs, which the origin gate then rejects.
        #
        # Local tooling (curl, Python ``requests``, internal scripts)
        # sends no Origin header, so it's NOT the CSRF threat we're
        # defending against — it gets backwards-compat text/plain
        # support so existing automation keeps working. Browsers
        # ALWAYS send Origin on cross-origin POSTs, so the contract
        # binds where it matters.
        # 2026-05-11 — Content-Type-agnostic JSON envelope extraction.
        # Some TD builds (observed on 2025.32820/macOS) don't surface
        # Content-Type into request['headers'] or as a flat key the
        # _headers() fallback can find. Pre-fix, those builds fell into
        # the plain-text branch and stored the literal `{"message":"..."}`
        # string as the user prompt — visible in chat_transcript and
        # wasting cached-prefix tokens on JSON quoting. Now: peek at the
        # raw body shape regardless of Content-Type. If it parses as a
        # JSON object with a "message" string, use that. Otherwise treat
        # as plain text. Robust to header-flattening quirks.
        ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        origin_lower = (request_origin or "").strip().lower()
        is_browser_request = bool(origin_lower) and origin_lower != "null"
        raw = _read_body(request)
        body = ""
        parsed_as_json = False
        raw_stripped = (raw or "").strip()
        if raw_stripped.startswith("{") and raw_stripped.endswith("}"):
            try:
                payload = json.loads(raw_stripped)
                if isinstance(payload, dict) and "message" in payload:
                    # 2026-05-11 — strict type check on "message". Pre-fix
                    # the extraction did ``str(payload.get("message", ""))``
                    # which coerced None→"None", dict→"{'nested': 1}",
                    # int→"0", bool→"True" — all silently accepted as
                    # the user prompt. Agent received garbage. Now: only
                    # str values pass; everything else 400s with a clear
                    # error so the client knows the envelope is wrong.
                    msg_val = payload.get("message")
                    if not isinstance(msg_val, str):
                        _text(
                            response,
                            400,
                            '"message" must be a string (got ' + type(msg_val).__name__ + ")",
                            request_origin=request_origin,
                        )
                        return response
                    body = msg_val.strip()
                    parsed_as_json = True
            except json.JSONDecodeError:
                pass
        if not parsed_as_json:
            if ctype == "application/json":
                # Caller explicitly said JSON but body wasn't a valid
                # {"message":...} envelope. Reject with a clear error
                # rather than silently treating the raw body as a prompt.
                _text(
                    response,
                    400,
                    'JSON body must be an object with a "message" string',
                    request_origin=request_origin,
                )
                return response
            if is_browser_request:
                # Browser caller MUST use JSON — preflight + origin check
                # is the CSRF guard. (Same policy as 2.1.3.)
                _text(
                    response,
                    415,
                    'Browser requests must use Content-Type: application/json with body {"message": "..."}',
                    request_origin=request_origin,
                )
                return response
            # Non-browser (no Origin header) AND not JSON-shaped: legacy
            # plain-text body. Backwards compat for curl-style automation.
            body = raw_stripped
        if not body:
            _text(response, 400, "empty message", request_origin=request_origin)
            return response
        try:
            comp = _comp()
            comp.par.Chatmessage.val = body
            # skip_html_echo=True: the HTML send() already did an optimistic
            # local appendMessage('user', ...) — we don't want to double-
            # render the user's bubble via the WebSocket broadcast.
            ext.OnSendPulse(skip_html_echo=True)
            _text(response, 200, "queued", request_origin=request_origin)
        except Exception as exc:
            debug(f"[tdpilot_API/web] /send failed: {exc}")
            _text(response, 500, str(exc), request_origin=request_origin)
        return response

    if method == "POST" and path == "/stop":
        try:
            ext.OnStopPulse()
            _text(response, 200, "stopped", request_origin=request_origin)
        except Exception as exc:
            _text(response, 500, str(exc), request_origin=request_origin)
        return response

    if method == "POST" and path == "/reset":
        try:
            ext.OnResetPulse()
            _text(response, 200, "reset", request_origin=request_origin)
        except Exception as exc:
            _text(response, 500, str(exc), request_origin=request_origin)
        return response

    _text(response, 404, f"unknown route: {method} {path}", request_origin=request_origin)
    return response


# ---------------------------------------------------------------------------
# WebSocket fan-out — TD pushes events here; every connected browser tab
# receives them so all instances of the chat UI stay in sync.
# ---------------------------------------------------------------------------

# WebSocket-client registry — stored on the COMP via comp.storage so it
# survives this module being reloaded. We hit a real bug where live-
# editing this textDAT's text caused TD to recompile the module while
# the webserverDAT kept routing onWebSocketOpen calls to one module
# instance and ext._broadcast routed broadcast() calls to ANOTHER. Two
# disjoint module-level _ws_clients sets meant clients were registered
# in one and broadcasts iterated the other (which was always empty).
# Symptoms: turns ran, transcript updated, but broadcasts silently
# vanished. Force-reloading the browser tab "fixed" it because the
# refresh-triggered onWebSocketOpen happened to land on whichever
# module ext._broadcast was using — but only by coincidence.
#
# comp.storage is the canonical TD pattern for state that must survive
# module reloads. Both this module and any future reloaded sibling
# read/write the SAME underlying set.
_STORAGE_KEY_CLIENTS = "tdpilot_api_ws_clients"
_STORAGE_KEY_WARN = "tdpilot_api_ws_warned_no_clients"
# 2026-05-11 — per-client last-seen timestamps for the keepalive reaper.
# Map: client-handle -> absTime.seconds at last incoming WS frame from
# that handle. The HTML chat sends {"type":"ping"} every 5s; the reaper
# evicts handles whose last_seen is older than _WS_STALE_S.
_STORAGE_KEY_LAST_SEEN = "tdpilot_api_ws_last_seen"
_WS_STALE_S = 15.0  # 3 missed pings @ 5s cadence


def _ws_clients():
    """Return the live WebSocket-client set, creating it on first use.
    Always read through this accessor — never via a stale module-level
    variable — to dodge the module-reload split bug described above."""
    comp = _comp()
    s = comp.fetch(_STORAGE_KEY_CLIENTS, None)
    if s is None:
        s = set()
        comp.store(_STORAGE_KEY_CLIENTS, s)
    return s


def _ws_last_seen():
    """Per-client last-seen timestamp dict, in comp.storage for the same
    module-reload reasons as ``_ws_clients``."""
    comp = _comp()
    m = comp.fetch(_STORAGE_KEY_LAST_SEEN, None)
    if not isinstance(m, dict):
        m = {}
        comp.store(_STORAGE_KEY_LAST_SEEN, m)
    return m


def _now_seconds() -> float:
    """absTime.seconds when available (cook thread), else 0.0. Using TD's
    own clock matches the executor's cadence and keeps timestamps stable
    across the COMP — no monotonic vs wall-clock confusion."""
    try:
        return float(absTime.seconds)  # type: ignore[name-defined]
    except Exception:
        return 0.0


def _warn_state():
    comp = _comp()
    w = comp.fetch(_STORAGE_KEY_WARN, None)
    if w is None:
        w = {"last_count": -1}
        comp.store(_STORAGE_KEY_WARN, w)
    return w


def reap_dead_ws_clients(webServerDAT) -> int:
    """Reap WS client handles whose connection is dead.

    Mechanism: keepalive age-out. The HTML chat sends a
    ``{"type":"ping"}`` every 5s; ``onWebSocketReceiveText`` updates
    ``last_seen`` per client. Any client whose last_seen is older
    than ``_WS_STALE_S`` (15s = 3 missed pings) is evicted.

    2026-05-11 — pre-fix this function ALSO sent a server-side ping
    to each client as a raise-on-send backstop. That worked in
    theory but TD 2025.32820/macOS's ``webSocketSendText`` is
    silently best-effort against dead sockets so the backstop never
    fired AND every healthy client received a noisy ``{"type":"ping"}``
    in its event stream every 5s. Now: age-out only. Client-driven
    keepalive is the canonical liveness signal; no server-side ping.

    Returns total number of handles reaped.
    """
    clients = _ws_clients()
    if not clients:
        return 0
    last_seen = _ws_last_seen()
    now = _now_seconds()
    dead = []
    # Clients without a last_seen entry get one set to NOW — grace
    # period for a freshly-opened connection that hasn't yet sent
    # its first ping. After that, the 15s stale window applies.
    for client in list(clients):
        ts = last_seen.get(client)
        if ts is None:
            last_seen[client] = now
            continue
        if now > 0.0 and (now - float(ts)) > _WS_STALE_S:
            dead.append(client)
    for d in dead:
        clients.discard(d)
        last_seen.pop(d, None)
    # 2026-05-11 — also sweep stale last_seen entries that no longer
    # correspond to a registered client. These can accumulate when
    # onWebSocketClose races with a late ping, or when a reload re-
    # constructs the clients set but not last_seen. Tiny memory leak
    # otherwise — sweep is O(len(last_seen)) and runs at the same
    # 5s cadence as the rest of the reaper.
    orphans = [c for c in list(last_seen.keys()) if c not in clients]
    for o in orphans:
        last_seen.pop(o, None)
    if dead:
        print(f"[tdpilot_API/web] reaped {len(dead)} dead WS client(s); {len(clients)} remain")
    return len(dead)


def _mark_ws_seen(client) -> None:
    """Refresh last_seen for a client — called from onWebSocketOpen +
    onWebSocketReceiveText. Idempotent."""
    last_seen = _ws_last_seen()
    last_seen[client] = _now_seconds()


def _send_full_history(webServerDAT, client):
    """Snapshot the current transcript for a freshly-connected client."""
    t = _comp().op("chat_transcript")
    rows = []
    if t is not None:
        try:
            for r in range(1, t.numRows):
                rows.append({"role": t[r, 0].val, "message": t[r, 1].val})
        except Exception:
            pass
    payload = json.dumps({"type": "fullSync", "rows": rows})
    try:
        webServerDAT.webSocketSendText(client, payload)
    except Exception as exc:
        print(f"[tdpilot_API/web] send fullSync failed: {exc}")


def broadcast(webServerDAT, payload: dict) -> None:
    """Send a JSON payload to every connected WebSocket client. Called by
    the extension whenever the agent emits an event so all browser tabs
    showing the chat stay in sync.

    Reads the client set from comp.storage (NOT a module-level var) so
    a module reload during live-editing can't split the registry into
    two disjoint sets — see the long comment at the top of the file."""
    clients = _ws_clients()
    warn = _warn_state()
    n = len(clients)
    if n == 0:
        if warn["last_count"] != 0:
            kind = payload.get("type", "?") if isinstance(payload, dict) else "?"
            print(
                f"[tdpilot_API/web] broadcast({kind}) skipped: 0 WebSocket clients "
                f"connected. Open the chat at http://127.0.0.1:9987/ — if it's already "
                f"open, the WS handshake may have failed (check the browser console)."
            )
            warn["last_count"] = 0
        return
    if warn["last_count"] == 0:
        print(f"[tdpilot_API/web] broadcast resumed: {n} WebSocket client(s) connected")
    warn["last_count"] = n

    msg = json.dumps(payload)
    dead = []
    for client in list(clients):
        try:
            webServerDAT.webSocketSendText(client, msg)
        except Exception as exc:
            print(f"[tdpilot_API/web] webSocketSendText failed: {exc}")
            dead.append(client)
    for d in dead:
        clients.discard(d)


def _ws_token_from_uri(uri: str) -> str:
    """Pull the session token from the WS handshake URI. Browsers
    don't permit custom WebSocket headers, so the token rides on the
    URL itself. Supports two encodings:

      * Path-segment form ``ws://host:port/<token>`` — what the live
        HTML client emits (see ``tdpilot_api_chat.html`` ~line 2228).
      * Query-string form ``ws://host:port/?t=<token>`` — legacy /
        external-tool convenience form.

    2026-05-11 fix — pre-fix this only handled query-string form, so the
    path-segment handshake (every browser client) hit the "no token"
    branch which only mattered when ``_insecure_mode()`` was False.
    With Authmode default flipped to "token", path-segment handshakes
    started getting rejected; restore them by reading the path too.
    """
    if not uri:
        return ""
    # Query-string form first — wins if both are present.
    if "?" in uri:
        qs = uri.split("?", 1)[1]
        for kv in qs.split("&"):
            if kv.startswith("t="):
                return kv[2:].replace("%3D", "=").replace("%2F", "/").replace("%2B", "+")
    # Path-segment form: ``/<token>`` (single segment, no further slashes).
    path = uri.split("?", 1)[0]
    if path.startswith("/"):
        path = path[1:]
    # Reject obviously-multi-segment paths and the empty path; a real
    # session token is a urlsafe-base64 string with no slashes.
    if path and "/" not in path:
        return path
    return ""


def _redact_uri(uri: str) -> str:
    """Strip the per-launch session token from a URI before logging.

    v2.0.1 security audit: the WS handshake URI carries
    ``?t=<token>`` because browsers can't set custom WS headers, and
    the previous handler logged ``uri={uri!r}`` on every open + every
    rejected handshake. That landed the token in the TD console log
    where another local user (or a screen-sharing session) could read
    it and impersonate the chat client. Now we redact the ``t``
    parameter to ``t=<redacted>`` before logging; the rest of the URI
    is preserved so debugging stays useful.
    """
    if not uri or "?" not in uri:
        return uri or ""
    base, qs = uri.split("?", 1)
    parts = []
    for kv in qs.split("&"):
        if kv.startswith("t="):
            parts.append("t=<redacted>")
        else:
            parts.append(kv)
    return base + "?" + "&".join(parts)


def onWebSocketOpen(webServerDAT, client, uri):
    redacted = _redact_uri(uri)
    if not _insecure_mode():
        expected = _session_token()
        got = _ws_token_from_uri(uri)
        if not expected or not got or not hmac.compare_digest(got, expected):
            print(f"[tdpilot_API/web] WS open REJECTED (bad/missing token) uri={redacted!r}")
            try:
                webServerDAT.webSocketSendText(
                    client,
                    json.dumps({"type": "error", "message": "unauthorized"}),
                )
            except Exception:
                pass
            return
    clients = _ws_clients()
    clients.add(client)
    # Seed last_seen so the keepalive reaper doesn't immediately age
    # this client out before its first ping arrives.
    _mark_ws_seen(client)
    print(f"[tdpilot_API/web] WS open uri={redacted!r} total={len(clients)}")
    _send_full_history(webServerDAT, client)


def onWebSocketClose(webServerDAT, client):
    clients = _ws_clients()
    clients.discard(client)
    _ws_last_seen().pop(client, None)
    print(f"[tdpilot_API/web] WS close total={len(clients)}")


def onWebSocketReceiveText(webServerDAT, client, data):
    # 2026-05-11 — keepalive ping handler. The HTML chat sends
    # {"type":"ping"} every 5s; we just refresh last_seen for this
    # client so the reaper knows the connection is alive. Don't parse
    # the payload aggressively — any incoming frame is evidence of
    # liveness. Future room for client-driven commands here if
    # needed.
    _mark_ws_seen(client)
    return


def onWebSocketReceiveBinary(webServerDAT, client, data):
    # Same liveness signal as text frames; not currently used by the
    # HTML chat but worth refreshing if any future client sends binary.
    _mark_ws_seen(client)
    return


def onServerStart(webServerDAT):
    """Fires when the webserverDAT activates — that's the most reliable
    "the .tox finished loading" hook we have, and it works on both build-
    script-driven installs AND on plain drag-and-drop.

    Two things happen here:

    1. Force-cook the executor so onFrameStart starts firing (TD 2025
       pull-cooking quirk fix — programmatically-created executeDATs
       don't fire onFrameStart until pulled into the cook chain at
       least once).

    2. Restart the in-TD ``chat_web`` webRenderTOP's Chromium engine
       and re-fetch its URL. Pre-fix, rebuilding the .tox left
       Chromium showing a cached error page (the brief window during
       rebuild where the old webserverDAT was down + the new one not
       yet up returned 404/timeout to Chromium, and Chromium retained
       the error indefinitely even after the server came back up).
       The autorestart pulse nukes the Chromium process and the
       reloadsrc pulse re-fetches the URL — between them, the panel
       is always clean after a rebuild.
    """
    try:
        comp = _comp()
        executor = comp.op("tdpilot_api_executor")
        if executor is not None:
            executor.cook(force=True)
            print("[tdpilot_API/web] onServerStart -> executor.cook(force=True)")
    except Exception as exc:
        print(f"[tdpilot_API/web] onServerStart force-cook failed: {exc}")
    # 2026-05-11 — chat_web auto-recovery on .tox rebuild.
    try:
        comp = _comp()
        chat_web = comp.op("chat_web")
        if chat_web is not None:
            # Restart pulse first — nukes the Chromium process if it was
            # holding a stale error page from a transient rebuild
            # window. Then reloadsrc forces a fresh GET of the URL.
            try:
                chat_web.par.autorestartpulse.pulse()
            except Exception:
                pass
            try:
                chat_web.par.reloadsrc.pulse()
            except Exception:
                pass
            print("[tdpilot_API/web] onServerStart -> chat_web restart + reload")
    except Exception as exc:
        print(f"[tdpilot_API/web] onServerStart chat_web reload failed: {exc}")
    return


def onServerStop(webServerDAT):
    _ws_clients().clear()
