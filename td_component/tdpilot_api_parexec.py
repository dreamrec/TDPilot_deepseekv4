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
    return


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
