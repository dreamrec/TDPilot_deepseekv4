# me - this DAT
# An executeDAT inside the tdpilot_API COMP. Toggles enabled at build time:
# start, framestart.
#
# Architecture note 1: we do NOT use TD's Extensions-page parameter (its
# expression parser rejected every form we tried in TD 2025.32460).
# Instead we hold the TDPilotAPIExt instance via the module-level
# `get_extension(comp)` factory inside tdpilot_api_extension.
#
# Architecture note 2: we do NOT rely on the parameterexecuteDAT to fire
# onPulse for our COMP's custom Pulse params — TD 2025.32460's parexec
# subscription is broken for our COMP (verified across UI clicks and
# programmatic .pulse() calls; identical config to a working DPSK4
# parexec also fails). We bypass parexec by polling a parameterCHOP that
# exposes the pulse params as channels, in onFrameStart below.

# Map pulse parameter name -> extension method name. Mirrors the parexec
# routing dict and is the single source of truth for pulse routing.
_PULSE_HANDLERS = {
    "Sendmessage": "OnSendPulse",
    "Stopagent": "OnStopPulse",
    "Resetconversation": "OnResetPulse",
    "Reloadconfig": "OnReloadConfigPulse",
    "Saveapikey": "OnSaveApiKeyPulse",
    "Openpanelnow": "OnOpenPanelPulse",
}


def _ext():
    """Return the live TDPilotAPIExt for this COMP, or None on failure."""
    try:
        return parent().op("tdpilot_api_extension").module.get_extension(parent())
    except Exception as exc:
        debug(f"[tdpilot_API] cannot fetch extension: {exc}")
        return None


def _ensure_panel_open(comp):
    """Open the chat HTML served from the COMP's WebServer DAT in the
    user's default browser.

    Why http://localhost not file://: Chrome blocks ws:// connections from
    file:// origins (same-origin policy since v92). When the HTML is
    served over HTTP from the same port the WebSocket lives on, the
    browser sees them as same-origin and the WebSocket connects cleanly.

    The /` route in tdpilot_api_web_callbacks now returns the baked HTML
    so we don't need a temp-file dance at all.
    """
    ws = comp.op("chat_web_server")
    port = 9987
    if ws is not None:
        try:
            port = int(ws.par.port.eval())
        except Exception:
            pass
    url = f"http://127.0.0.1:{port}/"
    try:
        import webbrowser

        webbrowser.open(url, new=0)
    except Exception as exc:
        debug(f"[tdpilot_API] webbrowser.open failed: {exc}")


def _sync_status_top_resolution():
    """Match the chat_status textTOP's native resolution to the COMP's
    current panel size so text renders 1:1 — no upscale blur, no
    downscale tiny-text. Without this, a textTOP at 1920×1080 displayed
    in a 600×460 pane shows minuscule text; one at 600×460 displayed in
    a 1200×900 pane shows blurry stretched text. Re-syncing each frame
    is cheap (one comparison + maybe two parameter writes)."""
    comp = parent()
    top = comp.op("chat_status")
    if top is None:
        return
    # Prefer the live panel size (when the COMP is shown in a pane this
    # reflects the user's actual viewport). Fall back to the COMP's own
    # par.w / par.h if `panel.width` isn't accessible.
    panel = getattr(comp, "panel", None)
    w = h = None
    if panel is not None:
        try:
            w = int(panel.width)
            h = int(panel.height)
        except Exception:
            pass
    if w is None or w <= 0:
        try:
            w = int(comp.par.w.eval())
        except Exception:
            w = 600
    if h is None or h <= 0:
        try:
            h = int(comp.par.h.eval())
        except Exception:
            h = 460
    # Only update when the size actually moved — TD parameter writes are
    # cheap but not free, and stable values keep the cook clean.
    try:
        if abs(int(top.par.resolutionw.eval()) - w) > 4:
            top.par.resolutionw = max(w, 100)
        if abs(int(top.par.resolutionh.eval()) - h) > 4:
            top.par.resolutionh = max(h, 100)
    except Exception:
        pass


_input_state = {"last_send_btn": 0.0}


def _poll_input_widgets():
    """Sync the in-panel chat_input field text into Chatmessage and detect
    Send-button clicks (or Enter in the field) to fire Sendmessage.

    Polled instead of subscribed because TD 2025.32460's panelExecuteDAT
    has the same subscription bug we worked around for parameterexecuteDAT.
    Trailing newline in the field text = Enter pressed → strip + send.
    """
    comp = parent()
    bar = comp.op("chat_input_bar")
    if bar is None:
        return
    field = bar.op("chat_input")
    btn = bar.op("chat_send")

    # Find which attr holds the field's text — varies by TD version.
    text_par = None
    if field is not None:
        for attr in ("field", "text"):
            text_par = getattr(field.par, attr, None)
            if text_par is not None:
                break

    enter_fired = False
    if text_par is not None:
        try:
            cur = str(text_par.eval() or "")
            # Enter detection: trailing newline => send + clear.
            if cur and cur != cur.rstrip("\r\n"):
                stripped = cur.rstrip("\r\n").strip()
                if stripped:
                    comp.par.Chatmessage.val = stripped
                    text_par.val = ""
                    comp.par.Sendmessage.pulse()
                    enter_fired = True
                else:
                    # Empty + just newline — wipe.
                    text_par.val = ""
            else:
                # Live-sync field text into the Chatmessage param so
                # OnSendPulse can read whatever's currently typed.
                if cur != comp.par.Chatmessage.eval():
                    comp.par.Chatmessage.val = cur
        except Exception:
            pass

    if enter_fired:
        return

    # Send-button click: 0->1 transition fires Sendmessage and clears field.
    if btn is not None:
        v = 0.0
        try:
            v = float(btn.panel.click.val)
        except Exception:
            for attr in ("value", "state"):
                p = getattr(btn.par, attr, None)
                if p is not None:
                    try:
                        v = float(p.eval())
                        break
                    except Exception:
                        continue
        last = _input_state["last_send_btn"]
        if v >= 0.5 and last < 0.5:
            try:
                comp.par.Sendmessage.pulse()
            except Exception:
                pass
            if text_par is not None:
                try:
                    text_par.val = ""
                except Exception:
                    pass
        _input_state["last_send_btn"] = v


_PANEL_PREFIX = {
    "user": "> ",
    "assistant": "< ",
    "tool_call": "  * ",
    "tool_result": "  = ",
    "error": "  ! ",
}

# Cached state for the panel renderer so we re-render only when the
# transcript actually changes. Module-level so survives across frames.
_panel_state = {"rows": 0}


def _render_panel():
    """Update the chat_status TOP's text to show the latest conversation
    rows. Runs each frame from onFrameStart but no-ops when the transcript
    row count is unchanged."""
    transcript = parent().op("chat_transcript")
    status_top = parent().op("chat_status")
    if transcript is None or status_top is None:
        return
    n = transcript.numRows
    if n == _panel_state["rows"]:
        return
    _panel_state["rows"] = n

    if n <= 1:
        text = (
            "tdpilot_API ready\n\n"
            "Open the COMP's parameters (P).\n"
            "API page  -> paste key, pulse Save Key (once).\n"
            "Chat page -> type Message, pulse Send.\n"
        )
    else:
        # Show ALL rows. The chat_status TOP is bottom-anchored (set in the
        # build script style), so the newest line is always visible at the
        # bottom; older rows extend upward and may clip off the top of a
        # small panel. Full scrollback is available in the chat_transcript
        # Table DAT (right-click -> View Data). We deliberately removed the
        # earlier 15-row cap and per-line truncation — the user wants to
        # see the actual conversation, not a summary.
        lines = []
        for r in range(1, n):
            role = transcript[r, 0].val
            msg = transcript[r, 1].val
            lines.append(_PANEL_PREFIX.get(role, "  ") + msg)
        text = "\n".join(lines)
    try:
        status_top.par.text = text
    except Exception:
        pass


def _poll_pulses(ext_inst):
    """Detect pulses by reading pulse_chop channels. On any channel >= 0.5,
    fire the matching extension handler and reset the parameter to 0 so the
    pulse doesn't re-trigger every subsequent frame.

    parameterCHOP latches pulse values at 1.0 indefinitely on programmatic
    par.pulse() calls (TD 2025 quirk). Without the reset, the channel would
    stay high and we'd dispatch the handler every frame.
    """
    chop = parent().op("pulse_chop")
    if chop is None:
        return
    chans = chop.chans() if hasattr(chop, "chans") else []
    if not chans:
        return
    comp = parent()
    for ch in chans:
        try:
            if ch.eval() < 0.5:
                continue
            handler_name = _PULSE_HANDLERS.get(ch.name)
            par = comp.par[ch.name] if ch.name in (p.name for p in comp.pars()) else None
            # Reset BEFORE dispatch so a handler that re-pulses doesn't loop.
            if par is not None:
                try:
                    par.val = 0
                except Exception:
                    pass
            if handler_name is not None and hasattr(ext_inst, handler_name):
                getattr(ext_inst, handler_name)()
        except Exception as exc:
            debug(f"[tdpilot_API] pulse {ch.name} dispatch error: {exc}")


_WEB_PORT_DEFAULT = 9987

# Tracks one-shot autodock state across frames. Reset whenever the
# executor module is re-loaded (rebuild, project reload).
_autodock_state = {"frames": 0, "done": False}


def _materialize_chat_html():
    """Point the webRenderTOP at the WebServer DAT's GET / route.

    Why http:// (not file://): the chat HTML uses a WebSocket back to
    ws://127.0.0.1:<port>/ for live LLM-response fan-out. Chromium (which
    powers the webRenderTOP) BLOCKS WebSocket connections from a file://
    origin to a ws:// host — same-origin policy treats them as cross-
    scheme. Loading the HTML over HTTP from the SAME port the WebSocket
    lives on makes them same-origin and the connection succeeds.

    The /` route in tdpilot_api_web_callbacks already serves the baked
    HTML on this exact URL, so there's nothing to materialize on disk.
    """
    comp = parent()
    web = comp.op("chat_web")
    ws = comp.op("chat_web_server")
    if web is None or ws is None:
        return
    try:
        port = _WEB_PORT_DEFAULT
        try:
            port = int(ws.par.port.eval())
        except Exception:
            pass
        url = f"http://127.0.0.1:{port}/"
        try:
            cur = (web.par.url.eval() or "").strip()
        except Exception:
            cur = ""
        if cur != url:
            web.par.url = url
        # Force a reload so it picks up the latest baked HTML when the
        # textDAT changes. Cheap when URL is unchanged.
        try:
            web.par.unload.pulse()
        except Exception:
            pass
        try:
            web.par.url.pulse()
        except Exception:
            pass
    except Exception as exc:
        debug(f"[tdpilot_API] HTML materialize failed: {exc}")


def onStart():
    e = _ext()
    if e is not None:
        debug("[tdpilot_API] extension initialized")
    try:
        _materialize_chat_html()
    except Exception as exc:
        debug(f"[tdpilot_API] chat HTML init failed: {exc}")
    try:
        if parent().par.Autoopenpanel.eval():
            _ensure_panel_open(parent())
    except Exception as exc:
        debug(f"[tdpilot_API] auto-open panel failed: {exc}")


def onCreate():
    return


def onExit():
    return


def onFrameStart(frame):
    e = _ext()
    if e is None:
        return
    # 1. Drain the agent's event queue. EV_TEXT / EV_TOOL_* events get
    #    appended to chat_transcript AND broadcast over WebSocket here.
    #    Without this, neither the TD-side panel nor the browser ever see
    #    LLM responses.
    try:
        e.DrainEvents()
    except Exception as exc:
        debug(f"[tdpilot_API/exec] DrainEvents failed: {exc}")
    # 2. Re-render the in-panel chat_status TOP from the freshly-updated
    #    transcript. No-ops when row count is unchanged, so cheap to call
    #    every frame.
    try:
        _render_panel()
    except Exception as exc:
        debug(f"[tdpilot_API/exec] _render_panel failed: {exc}")
    # 3. Match the chat_status TOP resolution to the live panel size so
    #    text stays crisp at any pane size.
    try:
        _sync_status_top_resolution()
    except Exception as exc:
        debug(f"[tdpilot_API/exec] _sync_status_top_resolution failed: {exc}")
    # 4. Sync the in-panel chat_input field into Chatmessage and detect
    #    Send-button / Enter-key clicks (the panelExecuteDAT subscription
    #    is broken in TD 2025.32460, so we poll).
    try:
        _poll_input_widgets()
    except Exception as exc:
        debug(f"[tdpilot_API/exec] _poll_input_widgets failed: {exc}")
    # 5. Pulse-poll fallback (the parameterexecuteDAT subscription is
    #    broken in TD 2025.32460 — see architecture note at top of file).
    try:
        _poll_pulses(e)
    except Exception as exc:
        debug(f"[tdpilot_API/exec] _poll_pulses failed: {exc}")
    # One-shot fallback: ensure the chat_web URL is set even if onStart
    # didn't fire after a fresh build (TD doesn't always re-fire onStart
    # on a just-created executeDAT). Cheap to check each frame; once the
    # URL is non-empty the branch is skipped.
    try:
        web = parent().op("chat_web")
        if web is not None:
            cur_url = (web.par.url.eval() or "").strip()
            if not cur_url:
                _materialize_chat_html()
    except Exception:
        pass
    # Same one-shot for the auto-dock — only fires until a Panel pane with
    # this COMP exists. _ensure_panel_open is idempotent so re-entering
    # the branch after first dock is harmless.
    try:
        if parent().par.Autoopenpanel.eval():
            _autodock_state["frames"] += 1
            if _autodock_state["frames"] <= 60 and not _autodock_state["done"]:
                if _autodock_state["frames"] >= 5:
                    _ensure_panel_open(parent())
                    _autodock_state["done"] = True
    except Exception:
        pass


def onPlayStateChange(state):
    return


def onDeviceChange():
    return


def onProjectPreSave():
    return


def onProjectPostSave():
    return
