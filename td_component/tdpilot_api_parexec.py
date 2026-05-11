# me  - this DAT
# A parameterexecuteDAT inside the tdpilot_API COMP, listening to pulses
# on the parent COMP's custom parameters. Routes each pulse to the
# matching extension method.
#
# Wired by the build script with: executeloc=here, fromop=parent(),
# pars=*, custom=1, builtin=0, onpulse=1, valuechange=0.
#
# We don't use TD's Extensions-page parameter — see the architecture
# note in tdpilot_api_executor.py. The TDPilotAPIExt instance is fetched
# from a module-level factory in tdpilot_api_extension.


def onPulse(par):
    name = par.name
    comp = par.owner
    try:
        ext = comp.op("tdpilot_api_extension").module.get_extension(comp)
    except Exception as exc:
        debug(f"[tdpilot_API] extension fetch failed for pulse {name}: {exc}")
        return
    if ext is None:
        debug(f"[tdpilot_API] extension is None for pulse {name}")
        return

    handler = {
        "Sendmessage": ext.OnSendPulse,
        "Stopagent": ext.OnStopPulse,
        "Resetconversation": ext.OnResetPulse,
        "Reloadconfig": ext.OnReloadConfigPulse,
        "Saveapikey": ext.OnSaveApiKeyPulse,
        "Openpanelnow": ext.OnOpenPanelPulse,
    }.get(name)

    if handler is None:
        return
    try:
        handler()
    except Exception as exc:
        debug(f"[tdpilot_API] {name} handler error: {exc}")


def onValueChange(par, prev):
    """Phase 1.2.1 (v2.2.1) — auto-route a tiny set of value changes to
    the extension so the user doesn't have to manually pulse follow-up
    actions.

    Currently:

      * ``Apikey`` change → save to disk + Reloadconfig (so the running
        Agent picks up the new key immediately). Drops the
        "type key → pulse Saveapikey → pulse Reloadconfig" 3-step
        ritual to a single param edit.
      * ``Authmode`` change → log the new value. The webserver's auth
        check reads this param on every request, so no rebuild is
        needed; the log is just so the user sees the change land.

    Every other value change is a no-op (cheap early-return) — we
    deliberately keep this narrow to avoid spurious side effects.
    """
    name = par.name
    if name not in ("Apikey", "Authmode"):
        return

    comp = par.owner
    try:
        ext = comp.op("tdpilot_api_extension").module.get_extension(comp)
    except Exception as exc:
        debug(f"[tdpilot_API] extension fetch failed for {name} change: {exc}")
        return
    if ext is None:
        return

    try:
        if name == "Apikey":
            handler = getattr(ext, "OnApikeyValueChange", None)
            if handler is not None:
                handler(par)
        elif name == "Authmode":
            handler = getattr(ext, "OnAuthmodeValueChange", None)
            if handler is not None:
                handler(par, prev)
    except Exception as exc:
        debug(f"[tdpilot_API] {name} value-change handler error: {exc}")


def onValuesChanged(changes):
    return


def onExpressionChange(par, val, prev):
    return


def onExportChange(par, val, prev):
    return


def onEnableChange(par, val, prev):
    return


def onModeChange(par, val, prev):
    return
