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
    flatten them into the request dict itself. Tolerate both."""
    raw = request.get("headers") or {}
    if not isinstance(raw, dict):
        raw = {}
    out = {str(k).lower(): str(v) for k, v in raw.items()}
    # Some builds put 'origin' / 'sec-fetch-site' on the request dict
    # directly. Only adopt them when not already in the headers map.
    for k in ("origin", "sec-fetch-site", "x-tdpilot-token", "authorization"):
        if k not in out and k in request:
            out[k] = str(request.get(k) or "")
    return out


# --- security: per-launch token + origin allowlist (1.7.1) ------------

_STORAGE_KEY_TOKEN = "tdpilot_api_session_token"
_TOKEN_TEMPLATE_MARKER = "__TDPILOT_TOKEN__"
_SAFE_HOSTS = ("127.0.0.1", "localhost", "[::1]", "::1")
_INSECURE_ENV = "TDPILOT_API_INSECURE"


def _insecure_mode() -> bool:
    """Off-switch for users who drive the chat from external tooling.
    Set ``TDPILOT_API_INSECURE=1`` in the environment to disable the
    token + origin checks entirely. Default is secure."""
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
    """
    if _insecure_mode():
        return None
    if method == "OPTIONS":
        return None
    if method == "GET" and path in ("/", "/index.html", "/health"):
        return None
    if not _allowed_origin(headers.get("origin", "")):
        return (403, "cross-origin request blocked")
    sec_fetch = headers.get("sec-fetch-site", "").strip().lower()
    if sec_fetch and sec_fetch not in ("same-origin", "none"):
        return (403, f"cross-site fetch blocked (Sec-Fetch-Site={sec_fetch})")
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
    when echoed back to a same-origin caller."""
    if _insecure_mode():
        # Insecure mode trades safety for tooling compat; widen CORS
        # accordingly so curl / external panels keep working.
        response["Access-Control-Allow-Origin"] = "*"
    elif request_origin and _allowed_origin(request_origin):
        response["Access-Control-Allow-Origin"] = request_origin
    else:
        response["Access-Control-Allow-Origin"] = "null"
    response["Access-Control-Allow-Headers"] = "Content-Type, X-TDPilot-Token, Authorization"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Vary"] = "Origin"


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

    if method == "GET" and path in ("/", "/index.html"):
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

    ext = _ext()
    if ext is None:
        _text(response, 503, "extension not ready", request_origin=request_origin)
        return response

    if method == "POST" and path == "/send":
        body = _read_body(request).strip()
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


def _warn_state():
    comp = _comp()
    w = comp.fetch(_STORAGE_KEY_WARN, None)
    if w is None:
        w = {"last_count": -1}
        comp.store(_STORAGE_KEY_WARN, w)
    return w


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
    """Pull the ``?t=<token>`` from the WS handshake URI. Browsers
    don't permit custom WebSocket headers, so the token rides on the
    query string instead."""
    if "?" not in (uri or ""):
        return ""
    qs = uri.split("?", 1)[1]
    for kv in qs.split("&"):
        if not kv.startswith("t="):
            continue
        # urldecode minimum — tokens are URL-safe base64 already so
        # %xx escapes are exotic here. Stay stdlib-free for TD compat.
        return kv[2:].replace("%3D", "=").replace("%2F", "/").replace("%2B", "+")
    return ""


def onWebSocketOpen(webServerDAT, client, uri):
    if not _insecure_mode():
        expected = _session_token()
        got = _ws_token_from_uri(uri)
        if not expected or not got or not hmac.compare_digest(got, expected):
            print(f"[tdpilot_API/web] WS open REJECTED (bad/missing token) uri={uri!r}")
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
    print(f"[tdpilot_API/web] WS open uri={uri!r} total={len(clients)}")
    _send_full_history(webServerDAT, client)


def onWebSocketClose(webServerDAT, client):
    clients = _ws_clients()
    clients.discard(client)
    print(f"[tdpilot_API/web] WS close total={len(clients)}")


def onWebSocketReceiveText(webServerDAT, client, data):
    return


def onWebSocketReceiveBinary(webServerDAT, client, data):
    return


def onServerStart(webServerDAT):
    """Fires when the webserverDAT activates — that's the most reliable
    "the .tox finished loading" hook we have, and it works on both build-
    script-driven installs AND on plain drag-and-drop.

    We use it to force-cook the executor, which is the canonical fix for
    TD 2025.32460's pull-cooking quirk: programmatically-created
    executeDATs don't fire onFrameStart until something pulls them into
    the cook chain at least once. Without this, drag-dropping the .tox
    into a fresh project would land you with a dead drain (chat UI
    stuck on "thinking…" forever, even though tools execute). The build
    script does the same force-cook at build time for the live install
    path; doing it again here covers .tox-load for everyone else.
    """
    try:
        comp = _comp()
        executor = comp.op("tdpilot_api_executor")
        if executor is not None:
            executor.cook(force=True)
            print("[tdpilot_API/web] onServerStart -> executor.cook(force=True)")
    except Exception as exc:
        print(f"[tdpilot_API/web] onServerStart force-cook failed: {exc}")
    return


def onServerStop(webServerDAT):
    _ws_clients().clear()
