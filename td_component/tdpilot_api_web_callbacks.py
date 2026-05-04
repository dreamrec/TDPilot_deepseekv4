"""
TDPilot API — webserverDAT callbacks for the in-panel HTML chat UI.

Lives at .../tdpilot_API/chat_web_server (textDAT used as the WebServer
DAT's Callbacks). Receives HTTP requests from the HTML chat (running in
the sibling webRenderTOP) and routes them to the TDPilotAPIExt singleton.

Routes (the HTML page only uses /send today, but the others are wired so
the web UI can grow):

  POST /send       body=<utf-8 user message>     → ext.OnSendPulse(msg)
  POST /stop                                      → ext.OnStopPulse()
  POST /reset                                     → ext.OnResetPulse()
  GET  /history                                   → JSON of chat_transcript
  GET  /health                                    → {"ok": true}

CORS is enabled wide open because the HTML is loaded from a file:// URL
(or http://localhost) and browsers refuse cross-origin POSTs without it.
The webRenderTOP is sandboxed inside TD so there's no real attack surface.
"""

import json


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


def _cors(response):
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Headers"] = "Content-Type, *"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"


def _json(response, status, payload):
    _cors(response)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = json.dumps(payload).encode("utf-8")
    response["Content-Type"] = "application/json; charset=utf-8"


def _text(response, status, body):
    _cors(response)
    response["statusCode"] = status
    response["statusReason"] = "OK" if status == 200 else "Error"
    response["data"] = (body or "").encode("utf-8")
    response["Content-Type"] = "text/plain; charset=utf-8"


def onHTTPRequest(webServerDAT, request, response):
    method, path = _route(request)
    if method == "OPTIONS":
        _text(response, 200, "")
        return response

    if method == "GET" and path in ("/", "/index.html"):
        # Serve the chat HTML so the page + WebSocket share http://host:port
        # origin. Required because Chrome blocks ws:// from file:// origins.
        html_dat = _comp().op("tdpilot_api_chat_html")
        body = html_dat.text if html_dat is not None else "<h1>chat html DAT missing</h1>"
        _cors(response)
        response["statusCode"] = 200
        response["statusReason"] = "OK"
        response["data"] = body.encode("utf-8")
        response["Content-Type"] = "text/html; charset=utf-8"
        return response

    if method == "GET" and path == "/health":
        _json(response, 200, {"ok": True})
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
        _json(response, 200, {"rows": rows})
        return response

    ext = _ext()
    if ext is None:
        _text(response, 503, "extension not ready")
        return response

    if method == "POST" and path == "/send":
        body = _read_body(request).strip()
        if not body:
            _text(response, 400, "empty message")
            return response
        try:
            comp = _comp()
            comp.par.Chatmessage.val = body
            # skip_html_echo=True: the HTML send() already did an optimistic
            # local appendMessage('user', ...) — we don't want to double-
            # render the user's bubble via the WebSocket broadcast.
            ext.OnSendPulse(skip_html_echo=True)
            _text(response, 200, "queued")
        except Exception as exc:
            debug(f"[tdpilot_API/web] /send failed: {exc}")
            _text(response, 500, str(exc))
        return response

    if method == "POST" and path == "/stop":
        try:
            ext.OnStopPulse()
            _text(response, 200, "stopped")
        except Exception as exc:
            _text(response, 500, str(exc))
        return response

    if method == "POST" and path == "/reset":
        try:
            ext.OnResetPulse()
            _text(response, 200, "reset")
        except Exception as exc:
            _text(response, 500, str(exc))
        return response

    _text(response, 404, f"unknown route: {method} {path}")
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


def onWebSocketOpen(webServerDAT, client, uri):
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
