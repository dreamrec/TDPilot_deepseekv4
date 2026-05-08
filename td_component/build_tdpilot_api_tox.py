"""
TDPilot API — build the standalone tdpilot_API container COMP and export tdpilot_API.tox.

Run from TD Textport once the .py source files are in place:

    runfile = "/ABS/PATH/td_component/build_tdpilot_api_tox.py"
    with open(runfile) as f: source = f.read()
    exec(compile(source, runfile, "exec"), globals(), globals())

What it produces (drag-in target):

    /tdpilot_API                       containerCOMP, panel 600x460
        Custom param pages:
          API     (api key, model, base url, save-key pulse, behavior knobs)
          Chat    (message field, send/stop/reset pulses)
          Status  (readonly status + last tool)
        Children:
          tdpilot_api_agent       textDAT
          tdpilot_api_dispatcher  textDAT
          tdpilot_api_config      textDAT
          tdpilot_api_schema      textDAT
          tdpilot_api_runtime     textDAT
          tdpilot_api_extension   textDAT  (Extensions page references this)
          tdpilot_api_executor    executeDAT  (start + framestart triggers)
          tdpilot_api_parexec     parameterexecuteDAT  (custom-pulse routing)
          mcp_webserver_callbacks textDAT  (the TD-side handlers, baked in)
          chat_transcript         table DAT  (role, message)
          chat_status             text TOP  (panel background; renders transcript)

Override behaviour with env vars:

    TD_MCP_REPO_ROOT    /ABS/PATH/repo                  (auto-detected if unset)
    TD_MCP_PARENT_PATH  /local                          ('' to skip live install)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Path + legacy helper bootstrap (mirrors build_tdpilot_tox.py)
# ---------------------------------------------------------------------------


def _resolve_this_dir():
    repo_root = os.environ.get("TD_MCP_REPO_ROOT")
    if repo_root:
        candidate = os.path.join(repo_root, "td_component")
        if os.path.isfile(os.path.join(candidate, "build_export_mcp_tox.py")):
            return candidate
    try:
        candidate = os.path.dirname(os.path.abspath(__file__))
        if os.path.isfile(os.path.join(candidate, "build_export_mcp_tox.py")):
            return candidate
    except NameError:
        pass
    raise RuntimeError(
        "Could not locate td_component/. Set TD_MCP_REPO_ROOT before exec'ing "
        "build_tdpilot_api_tox.py from Textport."
    )


_THIS_DIR = _resolve_this_dir()
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)


def _load_legacy_module():
    """Reuse the existing build_export_mcp_tox helpers without triggering its
    auto-build. Same trick as build_tdpilot_tox.py — see its docstring for why
    we go through types.ModuleType instead of `import`.
    """
    import re as _re
    import types as _types

    legacy_path = os.path.join(_THIS_DIR, "build_export_mcp_tox.py")
    with open(legacy_path, encoding="utf-8") as f:
        legacy_src = f.read()
    legacy_src = _re.sub(r"\nbuild_and_export\(\)\s*$", "\n", legacy_src)
    legacy_module = _types.ModuleType("build_export_mcp_tox")
    legacy_module.__file__ = legacy_path
    caller_globals = globals()
    if "__builtins__" in caller_globals:
        legacy_module.__dict__["__builtins__"] = caller_globals["__builtins__"]
    exec(compile(legacy_src, legacy_path, "exec"), legacy_module.__dict__)
    return legacy_module


_legacy = _load_legacy_module()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Install path — the legacy `_resolve_install_parent_comp()` reads the
# env var TD_MCP_PARENT_PATH directly (defaults to /local when unset),
# so we must SET the env var, not just compute a Python variable. The
# standalone API COMP belongs in /project1 so it saves with the user's
# .toe; explicit /local overrides via the env var still work.
_env_parent = os.environ.get("TD_MCP_PARENT_PATH")
if _env_parent is None or not _env_parent.strip():
    os.environ["TD_MCP_PARENT_PATH"] = "/project1"
INSTALL_PARENT_PATH = os.environ["TD_MCP_PARENT_PATH"]
COMP_NAME = "tdpilot_API"
TEMP_CONTAINER_NAME = "__tdpilot_api_tox_export__"
EXPORT_TOX_PATH = ""  # empty = repo_root/td_component/tdpilot_API.tox
PANEL_W = 900  # native panel width
PANEL_H = 600  # native panel height
WEB_PORT = 9987  # port for the in-COMP WebServer DAT (HTML→TD bridge)

# DeepSeek brand purple — matches the chat HTML accent (#6B47FF) so the
# COMP node, its panel, and the browser chat all share visual identity.
# RGB (0.42, 0.28, 1.0) ≈ #6B47FF.
COMP_COLOR = (0.42, 0.28, 1.0)


# Custom-param schema. Each tuple: (name, kind, label, default-or-None).
_API_PAGE = [
    ("Apihdr", "Header", "DeepSeek API", None),
    ("Apikey", "Str", "API Key", ""),
    ("Saveapikey", "Pulse", "Save Key to ~/.tdpilot-api/", None),
    ("Model", "Str", "Model", "deepseek-v4-pro"),
    ("Baseurl", "Str", "Base URL", "https://api.deepseek.com/anthropic"),
    # Sprint 4.3 — multi-model routing
    ("Modeltier", "Menu", "Model Tier", ("auto", "flash", "pro")),
    ("Flashmodel", "Str", "Flash Model Name", "deepseek-v4-flash"),
    ("Behaviorhdr", "Header", "Behavior", None),
    # DeepSeek v4 capabilities (verified 2026-05):
    #   * 1,000,000 token context window (Pro and Flash)
    #   * 384,000 token max output per call
    # Defaults are a generous-but-not-extreme starting point. User can crank
    # Max Tokens up to 384000 in the param panel for long-form responses.
    ("Maxtokens", "Int", "Max Tokens (out, max 384000)", 32768),
    ("Temperature", "Float", "Temperature", 0.7),
    ("Turnbudget", "Int", "Turn Budget (tool rounds)", 100),
    ("Soundondone", "Toggle", "Sound on completion", True),
    ("Soundvolume", "Float", "Sound Volume (0-1)", 0.7),
    ("Autoopenpanel", "Toggle", "Auto-open chat in browser on load", True),
    ("Openpanelnow", "Pulse", "Open Chat in Browser", None),
    ("Reloadconfig", "Pulse", "Reload Config", None),
]

_CHAT_PAGE = [
    ("Chathdr", "Header", "Chat", None),
    ("Chatmessage", "Str", "Message", ""),
    ("Sendmessage", "Pulse", "Send", None),
    ("Stopagent", "Pulse", "Stop", None),
    ("Resetconversation", "Pulse", "Reset", None),
]

_STATUS_PAGE = [
    ("Statushdr", "Header", "Status", None),
    ("Status", "Str", "Status", "uninitialized"),
    ("Lasttool", "Str", "Last Tool", "—"),
    # Sprint 4.3 — written by the extension's EV_MODEL handler so the
    # user sees which model tier was routed for the most recent turn.
    ("Activemodel", "Str", "Active Model", "—"),
]

# The five custom Pulse parameter names whose pulses we want routed to the
# extension. Single source of truth: keep in sync with the executor's
# _PULSE_HANDLERS dict in tdpilot_api_executor.py.
_PULSE_PARAM_NAMES = (
    "Sendmessage",
    "Stopagent",
    "Resetconversation",
    "Reloadconfig",
    "Saveapikey",
    "Openpanelnow",
)


# Source files baked in as textDATs. Order matters only for layout.
# (DAT name, op-type, source path relative to repo root).
_SOURCE_FILES = (
    ("tdpilot_api_agent", "textDAT", "td_component/tdpilot_api_agent.py"),
    ("tdpilot_api_dispatcher", "textDAT", "td_component/tdpilot_api_dispatcher.py"),
    ("tdpilot_api_config", "textDAT", "td_component/tdpilot_api_config.py"),
    # Phase 3 (F-18) — single-source COMP/extension/runtime lookup helpers.
    # Pure module, no TD calls at import time, so it can be loaded
    # before anything that depends on it.
    ("tdpilot_api_lookup", "textDAT", "td_component/tdpilot_api_lookup.py"),
    # Schema is split across three textDATs (defs + map + shim re-export).
    # Order matters for the shim's import-time resolve: defs and map must
    # be in sys.modules before the shim is imported, so list them first.
    ("tdpilot_api_schema_defs", "textDAT", "td_component/tdpilot_api_schema_defs.py"),
    ("tdpilot_api_schema_map", "textDAT", "td_component/tdpilot_api_schema_map.py"),
    ("tdpilot_api_schema", "textDAT", "td_component/tdpilot_api_schema.py"),
    ("tdpilot_api_runtime", "textDAT", "td_component/tdpilot_api_runtime.py"),
    ("tdpilot_api_extension", "textDAT", "td_component/tdpilot_api_extension.py"),
    ("tdpilot_api_bm25", "textDAT", "td_component/tdpilot_api_bm25.py"),
    ("tdpilot_api_memory", "textDAT", "td_component/tdpilot_api_memory.py"),
    ("tdpilot_api_knowledge", "textDAT", "td_component/tdpilot_api_knowledge.py"),
    ("tdpilot_api_recipes", "textDAT", "td_component/tdpilot_api_recipes.py"),
    ("tdpilot_api_skills", "textDAT", "td_component/tdpilot_api_skills.py"),
    ("tdpilot_api_patches", "textDAT", "td_component/tdpilot_api_patches.py"),
    ("tdpilot_api_user_tools", "textDAT", "td_component/tdpilot_api_user_tools.py"),
    ("tdpilot_api_subagents", "textDAT", "td_component/tdpilot_api_subagents.py"),
    ("tdpilot_api_macros", "textDAT", "td_component/tdpilot_api_macros.py"),
    ("tdpilot_api_official_docs", "textDAT", "td_component/tdpilot_api_official_docs.py"),
    ("tdpilot_api_td2025", "textDAT", "td_component/tdpilot_api_td2025.py"),
    ("tdpilot_api_introspect", "textDAT", "td_component/tdpilot_api_introspect.py"),
    ("tdpilot_api_batch", "textDAT", "td_component/tdpilot_api_batch.py"),
    ("tdpilot_api_recovery", "textDAT", "td_component/tdpilot_api_recovery.py"),
    ("tdpilot_api_tracing", "textDAT", "td_component/tdpilot_api_tracing.py"),
    ("tdpilot_api_compaction", "textDAT", "td_component/tdpilot_api_compaction.py"),
    ("tdpilot_api_chat_html", "textDAT", "td_component/tdpilot_api_chat.html"),
    ("tdpilot_api_web_callbacks", "textDAT", "td_component/tdpilot_api_web_callbacks.py"),
    # PR-16 (v1.8.3): mcp_webserver_callbacks is now composed from the
    # callbacks/ split package rather than a single source file. The build
    # loop below detects the special "<COMPOSE>" sentinel and routes to
    # _legacy._read_callbacks_source() for the composed body.
    ("mcp_webserver_callbacks", "textDAT", "<COMPOSE>"),
    ("tdpilot_api_executor", "executeDAT", "td_component/tdpilot_api_executor.py"),
    ("tdpilot_api_parexec", "parameterexecuteDAT", "td_component/tdpilot_api_parexec.py"),
)


# ---------------------------------------------------------------------------
# v2.1.1 — API .tox freshness tracking (parallel to the dpsk4 .tox gate
# in scripts/check_tox_freshness.py).
#
# Why: pre-2.1.1 only tdpilot-dpsk4.tox had a CI gate; tdpilot_API.tox
# went stale silently if a contributor edited runtime.py / chat.html /
# anywhere in the API source tree without rebuilding inside TD. Users
# installing the plugin would get an old binary while CI stayed green.
#
# How: at build time we compute a sha256 over the byte content of every
# source file that contributes to the API .tox, and write it alongside
# the existing dpsk4 hash file under td_component/.tox-api-source-hash.json.
# scripts/check_tox_api_freshness.py recomputes the hash from the current
# tree and fails CI if it doesn't match what's stored.
# ---------------------------------------------------------------------------


# Direct embeds — pulled from _SOURCE_FILES (skip the `<COMPOSE>` sentinel
# whose content comes from the callbacks/ split package below).
_API_TOX_SOURCE_FILES = tuple(rel for _, _, rel in _SOURCE_FILES if rel != "<COMPOSE>") + (
    # Composed mcp_webserver_callbacks textDAT pulls its body from the
    # callbacks/ split package via _legacy._read_callbacks_source(). Any
    # byte change in any of these files changes the composed text that
    # gets baked into the API .tox, so the hash must cover them too.
    # NOTE: this list overlaps with check_tox_freshness.py's SOURCE_FILES
    # by design — both .tox files embed the composed callbacks body.
    "td_component/callbacks/_composer.py",
    "td_component/callbacks/__init__.py",
    "td_component/callbacks/_header.py",
    "td_component/callbacks/router.py",
    "td_component/callbacks/auth.py",
    "td_component/callbacks/serializers.py",
    "td_component/callbacks/handlers/__init__.py",
    "td_component/callbacks/handlers/nodes.py",
    "td_component/callbacks/handlers/exec_and_custom_params.py",
    "td_component/callbacks/handlers/exec_python.py",
    "td_component/callbacks/handlers/inspect.py",
    "td_component/callbacks/handlers/search.py",
    "td_component/callbacks/handlers/lifecycle.py",
    "td_component/callbacks/handlers/pulse.py",
    "td_component/callbacks/handlers/monitor.py",
    "td_component/callbacks/handlers/analyze_frame.py",
    # Build script bytes — same reasoning as the dpsk4 gate (any change
    # to how the .tox is laid out forces a rebuild signal even if no
    # embedded source changed).
    "td_component/build_tdpilot_api_tox.py",
)


def _compute_api_tox_source_hash(repo_root):
    """Return sha256 over the bytes of every file that feeds tdpilot_API.tox.

    Single source of truth for API .tox freshness. The matching list
    in scripts/check_tox_api_freshness.py must stay aligned — a comment
    in each file points at the other.
    """
    h = hashlib.sha256()
    for rel in _API_TOX_SOURCE_FILES:
        path = os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            continue
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        with open(path, "rb") as f:
            h.update(f.read())
        h.update(b"\x00")
    return h.hexdigest()


def _write_api_tox_source_hash(repo_root):
    """Record the API .tox source hash so CI can detect drift after edits."""
    manifest = {
        "tox_source_hash": _compute_api_tox_source_hash(repo_root),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "source_files": list(_API_TOX_SOURCE_FILES),
    }
    out_path = os.path.join(repo_root, "td_component", ".tox-api-source-hash.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[tdpilot_API] Wrote {out_path}")


# ---------------------------------------------------------------------------
# Custom-param helpers
# ---------------------------------------------------------------------------


def _append_param(comp, page_name, name, kind, label, default):
    page = comp.appendCustomPage(page_name)
    if kind == "Header":
        try:
            page.appendHeader(name, label=label)
            return
        except Exception:
            par = page.appendStr(name, label=label)[0]
            par.readOnly = True
            return
    if kind == "Str":
        par = page.appendStr(name, label=label)[0]
        if default is not None:
            par.default = default
            par.val = default
        return
    if kind == "Pulse":
        page.appendPulse(name, label=label)
        return
    if kind == "Int":
        par = page.appendInt(name, label=label)[0]
        if default is not None:
            par.default = int(default)
            par.val = int(default)
        return
    if kind == "Float":
        par = page.appendFloat(name, label=label)[0]
        if default is not None:
            par.default = float(default)
            par.val = float(default)
        # Force 0-1 clamping for params whose name implies a normalised
        # value (Volume, Mix, etc.). Keeps the param panel slider usable.
        if name.lower().endswith(("volume", "mix", "amount", "level")):
            try:
                par.normMin = 0.0
                par.normMax = 1.0
                par.min = 0.0
                par.max = 1.0
                par.clampMin = True
                par.clampMax = True
            except Exception:
                pass
        return
    if kind == "Toggle":
        par = page.appendToggle(name, label=label)[0]
        if default is not None:
            par.default = bool(default)
            par.val = bool(default)
        return
    if kind == "Menu":
        # default is a tuple of menu options; first is the initial value.
        # Format: ("auto", "flash", "pro") or similar — both menuNames and
        # menuLabels get the same list (machine-readable + display same).
        par = page.appendMenu(name, label=label)[0]
        opts = list(default) if isinstance(default, (list, tuple)) else ["auto"]
        if not opts:
            opts = ["auto"]
        try:
            par.menuNames = opts
            par.menuLabels = opts
            par.default = opts[0]
            par.val = opts[0]
        except Exception:
            pass
        return
    raise ValueError(f"Unknown param kind: {kind}")


def _build_custom_params(comp):
    for name, kind, label, default in _API_PAGE:
        _append_param(comp, "API", name, kind, label, default)
    for name, kind, label, default in _CHAT_PAGE:
        _append_param(comp, "Chat", name, kind, label, default)
    for name, kind, label, default in _STATUS_PAGE:
        _append_param(comp, "Status", name, kind, label, default)
    # Status fields are readonly — the extension writes to them.
    for ro in ("Status", "Lasttool", "Activemodel"):
        try:
            comp.par[ro].readOnly = True
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Children
# ---------------------------------------------------------------------------


def _create_dat_with_source(parent_comp, name, op_type, source_text):
    fallbacks = (op_type,)
    if op_type == "parameterexecuteDAT":
        fallbacks = ("parameterexecuteDAT", "parexecDAT")
    dat = _legacy._create_with_fallback(parent_comp, fallbacks, name)
    dat.text = source_text

    if op_type == "executeDAT":
        # Mirror the DPSK4 executor: enable the triggers our callback module
        # actually defines functions for. Untouched toggles stay False.
        for trig in ("start", "create", "exit", "framestart"):
            _legacy._set_first_par(dat, (trig,), True)
    return dat


def _wire_parexec(parexec_dat, parent_comp):
    """parameterexecuteDAT listens to pulses on the parent COMP's custom params.
    We listen ONLY to onPulse — value/expression/mode changes are ignored to
    avoid spurious pulse routing."""
    _legacy._set_first_par(parexec_dat, ("executeloc",), "here")
    try:
        par = parexec_dat.par.fromop
        par.expr = "parent()"
        par.mode = parexec_dat.par.executeloc.mode.__class__.EXPRESSION
    except Exception:
        try:
            parexec_dat.par.fromop = parent_comp
        except Exception:
            pass
    _legacy._set_first_par(parexec_dat, ("pars",), "*")
    _legacy._set_first_par(parexec_dat, ("custom",), 1)
    _legacy._set_first_par(parexec_dat, ("builtin",), 0)
    _legacy._set_first_par(parexec_dat, ("onpulse",), 1)
    for off in (
        "valuechange",
        "valueschanged",
        "expressionchange",
        "exportchange",
        "enablechange",
        "modechange",
    ):
        _legacy._set_first_par(parexec_dat, (off,), 0)
    _legacy._set_first_par(parexec_dat, ("active",), 1)


def _wire_extension(comp):
    """No-op in the current architecture.

    We tried wiring TD's Extensions parameter to construct TDPilotAPIExt
    via ``mod('tdpilot_api_extension').TDPilotAPIExt(me)`` and a half-
    dozen variants — all hit one of two TD 2025.32460 bugs:
      * dotted-attribute access (`me.op`, `mod.foo`) raised SyntaxError
        even though the strings are valid Python and work in Textport.
      * function-call forms (`mod('foo')`, `op('foo').module...`) parsed
        cleanly but resolved to the wrong scope (sibling, not child) so
        the resulting instance was None.
    We bypass TD's Extensions page entirely now: the TDPilotAPIExt is
    held as a per-COMP singleton via tdpilot_api_extension.get_extension,
    and both the executeDAT and parameterexecuteDAT call that factory
    directly. The function below is kept as a no-op so older callers
    that import it don't break.
    """
    return


def _create_chat_transcript(parent_comp):
    dat = _legacy._create_with_fallback(parent_comp, ("tableDAT",), "chat_transcript")
    dat.clear()
    dat.appendRow(["role", "message"])
    return dat


def _create_skills_corpus(parent_comp, repo_root):
    """Bake every td_component/skills/*.md into a textDAT inside a
    `skills` baseCOMP child. Thin wrapper around the shared corpus
    baker — see build_tdpilot_api_corpus.bake_md_corpus for the
    implementation.

    The runtime's skills module reads these on demand via
    skill_list / skill_get / skill_load. User-added skills in
    ~/.tdpilot-api/skills/ merge with the bundled set at runtime
    (user names override bundled names).
    """
    from build_tdpilot_api_corpus import bake_md_corpus  # type: ignore[import-not-found]

    return bake_md_corpus(
        parent_comp,
        _legacy,
        repo_root,
        src_subdir="skills",
        container_name="skills",
        dat_prefix="skill_",
        node_xy=(1000, 200),
        safe_stem=True,
    )


def _create_knowledge_corpus(parent_comp, repo_root):
    """Bake every td_component/knowledge/*.md into a textDAT inside a
    `kb` baseCOMP child. Thin wrapper — see
    build_tdpilot_api_corpus.bake_md_corpus.

    Why textDATs (not files on disk): keeps the .tox a single drag-drop
    artefact. No filesystem dependency for the bundled corpus. Users can
    still drop their own .md files into ~/.tdpilot-api/knowledge/ —
    those merge with the bundled set at runtime.
    """
    from build_tdpilot_api_corpus import bake_md_corpus  # type: ignore[import-not-found]

    return bake_md_corpus(
        parent_comp,
        _legacy,
        repo_root,
        src_subdir="knowledge",
        container_name="kb",
        dat_prefix="kb_",
        node_xy=(800, 200),
        safe_stem=False,
    )
    return kb


def _create_chat_web(parent_comp, repo_root):
    """The HTML chat UI lives in tdpilot_api_chat.html (baked into a sibling
    textDAT). At cook time we write its content to a temp file and point a
    webRenderTOP at it. Why temp file rather than data: URL? webRenderTOP's
    URL parameter consumes file:// URIs cleanly; data: URLs are limited and
    inconsistent across TD builds. Temp file = simple + reliable.
    """
    web = _legacy._create_with_fallback(
        parent_comp,
        # TD ships this operator under multiple names across versions.
        # Verified in TD 2025.32460: 'webrenderTOP' works (lowercase r).
        ("webRenderTOP", "webrenderTOP", "webclientTOP", "webBrowserTOP"),
        "chat_web",
    )
    # Native res — the panel-bg path downsamples cleanly, and the HTML
    # uses 100vh/100vw flex layout to fill whatever the parent panel is.
    for k, v in (("resolutionw", 1280), ("resolutionh", 800)):
        _legacy._set_first_par(web, (k,), v)
    # CRITICAL: webrenderTOP defaults active=False on creation in TD 2025.
    # Without this, the URL loads but the page never paints to the TOP and
    # the panel-bg stays black. Force it on at build time so the user
    # doesn't have to manually toggle anything.
    _legacy._set_first_par(web, ("active",), True)
    # Make the rendered TOP interactive (clickable + typeable) inside a
    # Panel pane.
    try:
        web.par.viewer = True
    except Exception:
        pass
    try:
        web.viewer = True
        web.display = True
    except Exception:
        pass
    # URL is set at runtime by the executor's onStart (writes the baked
    # HTML to a temp file with #port=N in the hash so JS knows where to
    # POST). Leave blank at build time.
    try:
        web.par.url = ""
    except Exception:
        pass
    try:
        web.nodeX, web.nodeY = -400, 100
    except Exception:
        pass
    return web


def _create_chat_web_server(parent_comp):
    """The webserverDAT that listens on WEB_PORT for HTML→TD POSTs.

    Callbacks are routed to the sibling tdpilot_api_web_callbacks textDAT.
    """
    ws = _legacy._create_with_fallback(parent_comp, ("webserverDAT",), "chat_web_server")
    _legacy._set_first_par(ws, ("port",), WEB_PORT)
    _legacy._set_first_par(ws, ("active",), True)
    # Wire the Callbacks DAT param to our callbacks textDAT. Param name
    # varies by TD version — try the common ones.
    cb_dat = parent_comp.op("tdpilot_api_web_callbacks")
    if cb_dat is not None:
        for attr in ("callbacks", "callbackdat"):
            par = getattr(ws.par, attr, None)
            if par is None:
                continue
            try:
                par.val = cb_dat
                break
            except Exception:
                try:
                    par.expr = "op('tdpilot_api_web_callbacks')"
                    par.mode = par.mode.__class__.EXPRESSION
                    break
                except Exception:
                    continue
    try:
        ws.nodeX, ws.nodeY = -400, -100
    except Exception:
        pass
    return ws


# Note: native textTOP chat-status / fieldCOMP-input-bar helpers were
# removed during the 2026-05-04 audit. The chat UI lives entirely in the
# HTML panel served by chat_web (webRenderTOP → http://127.0.0.1:9987),
# never in native TD widgets. The legacy helpers referenced an undefined
# INPUT_BAR_H constant and were never invoked from the populator — see
# git history for the full prior implementation if needed.


# ---------------------------------------------------------------------------
# Populator
# ---------------------------------------------------------------------------


def _populate_comp(comp, repo_root, info_text):
    version = _legacy._get_version(repo_root)
    comp.comment = f"TDPilot API v{version} — standalone (in-TD) agent. {info_text}"

    _legacy._set_first_par(comp, ("w",), PANEL_W)
    _legacy._set_first_par(comp, ("h",), PANEL_H)
    try:
        comp.viewer = True
        comp.display = True
        comp.render = True
    except Exception:
        pass
    try:
        comp.color = COMP_COLOR
    except Exception:
        pass

    # Wipe existing children so reruns land clean.
    for child in list(comp.children):
        child.destroy()

    _build_custom_params(comp)

    # Conversation log table.
    transcript = _create_chat_transcript(comp)
    try:
        transcript.nodeX, transcript.nodeY = -200, 200
    except Exception:
        pass

    # Bundled knowledge corpus — one textDAT per .md file in
    # td_component/knowledge/, all under a `kb` baseCOMP child. Read by
    # tdpilot_api_knowledge.py at runtime.
    _create_knowledge_corpus(comp, repo_root)
    # Bundled skills corpus — same pattern, under a `skills` baseCOMP
    # child. Read by tdpilot_api_skills.py at runtime.
    _create_skills_corpus(comp, repo_root)

    # parameterCHOP that exposes the COMP's custom Pulse params as channels.
    # The executor's onFrameStart polls this each frame to detect pulses,
    # bypassing TD 2025.32460's broken parameterexecuteDAT subscription.
    pulse_chop = _legacy._create_with_fallback(comp, ("parameterCHOP",), "pulse_chop")
    try:
        # Use an expression so the .tox is portable — the path resolves to
        # the COMP's live path at cook time, regardless of where the COMP
        # is dragged in.
        pulse_chop.par.ops.expr = "parent().path"
        pulse_chop.par.ops.mode = pulse_chop.par.ops.mode.__class__.EXPRESSION
    except Exception:
        try:
            pulse_chop.par.ops = comp.path
        except Exception:
            pass
    try:
        pulse_chop.par.parameters = " ".join(_PULSE_PARAM_NAMES)
    except Exception:
        pass
    for tog, val in (("custom", True), ("builtin", False)):
        _legacy._set_first_par(pulse_chop, (tog,), val)
    try:
        pulse_chop.nodeX, pulse_chop.nodeY = -200, 100
    except Exception:
        pass

    # Source files baked in as DATs.
    layout = {
        "tdpilot_api_agent": (200, 200),
        "tdpilot_api_dispatcher": (400, 200),
        "tdpilot_api_config": (200, 100),
        "tdpilot_api_lookup": (300, 100),
        "tdpilot_api_schema": (400, 100),
        "tdpilot_api_runtime": (200, 0),
        "tdpilot_api_extension": (400, 0),
        "tdpilot_api_memory": (600, 0),
        "tdpilot_api_knowledge": (800, 0),
        "tdpilot_api_recipes": (1000, 0),
        "tdpilot_api_skills": (1200, 0),
        "tdpilot_api_patches": (1400, 0),
        "tdpilot_api_user_tools": (1600, 0),
        "tdpilot_api_subagents": (1800, 0),
        "tdpilot_api_macros": (2000, 0),
        "mcp_webserver_callbacks": (600, 100),
        "tdpilot_api_executor": (200, -100),
        "tdpilot_api_parexec": (400, -100),
    }
    created = {}
    for name, op_type, rel_path in _SOURCE_FILES:
        if rel_path == "<COMPOSE>":
            # PR-16 (v1.8.3): mcp_webserver_callbacks is composed from the
            # callbacks/ split package; share the legacy helper so the
            # standalone + CLI builds use the same composed body.
            source = _legacy._read_callbacks_source(repo_root)
        else:
            source = _legacy._read_repo_file(repo_root, rel_path)
        dat = _create_dat_with_source(comp, name, op_type, source)
        created[name] = dat
        try:
            dat.nodeX, dat.nodeY = layout.get(name, (0, 0))
        except Exception:
            pass

    # parexec wiring (after the param pages exist so pars=* matches).
    _wire_parexec(created["tdpilot_api_parexec"], comp)

    # WebRender TOP that loads the HTML chat UI. Becomes the panel-bg
    # so the COMP's Panel surface IS the chat. Created AFTER the source
    # textDATs so the html-bake textDAT exists.
    chat_web = _create_chat_web(comp, repo_root)
    try:
        comp.par.top = chat_web
    except Exception:
        pass

    # WebServer DAT that listens on WEB_PORT for HTML→TD POSTs.
    _create_chat_web_server(comp)

    # Hook the extension class. After this, op('tdpilot_API').ext.TDPilotAPIExt
    # is the live agent driver.
    _wire_extension(comp)

    # CRITICAL: force the executor into the cook chain so its
    # ``onFrameStart`` callback actually fires. TD 2025.32460's pull-
    # cooking leaves a programmatically-created executeDAT inert by
    # default — its ``active`` and ``framestart`` toggles are both On,
    # but the callback never fires because nothing downstream pulls
    # the op into a cook. Calling ``.cook(force=True)`` once at build
    # time registers it with TD's frame ticker and the callback then
    # fires every frame for the lifetime of the COMP.
    #
    # Without this, the entire chat pipe is dead: ``DrainEvents`` never
    # runs, the agent's worker thread blocks on the cook-thread tool
    # dispatcher, and LLM responses never reach the browser. We
    # discovered this the hard way — see the community research note
    # ("Execute DAT not always run", forum.derivative.ca/t/.../10750)
    # which calls out exactly this symptom and prescribes force-cook
    # as the canonical fix.
    executor = created.get("tdpilot_api_executor")
    if executor is not None:
        try:
            executor.cook(force=True)
            print("[tdpilot_API] executor.cook(force=True) — onFrameStart registered")
        except Exception as exc:
            print(f"[tdpilot_API] executor force-cook failed: {exc}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _resolve_export_path(repo_root):
    if EXPORT_TOX_PATH:
        out = os.path.abspath(os.path.expanduser(EXPORT_TOX_PATH))
    else:
        out = os.path.join(repo_root, "td_component", "tdpilot_API.tox")
    out_dir = os.path.dirname(out)
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    return out


def _build_info_text(repo_root, export_path):
    timestamp = datetime.now(timezone.utc).isoformat()
    tox_name = os.path.basename(export_path)
    repo_label = os.path.basename(os.path.abspath(repo_root.rstrip(os.sep))) or "TDPilot"
    return f"Generated UTC {timestamp}; repo {repo_label}; export {tox_name}"


def build_and_export():
    repo_root = _legacy._guess_repo_root()
    if not repo_root:
        raise RuntimeError("Could not auto-detect repo root. Set TD_MCP_REPO_ROOT first.")

    export_path = _resolve_export_path(repo_root)
    info_text = _build_info_text(repo_root, export_path)

    export_host = _legacy._resolve_export_host()
    temp_parent = export_host.op(TEMP_CONTAINER_NAME)
    if temp_parent is not None:
        temp_parent.destroy()
    temp_parent = export_host.create("baseCOMP", TEMP_CONTAINER_NAME)
    try:
        temp_parent.nodeX, temp_parent.nodeY = 1000, -400
    except Exception:
        pass

    try:
        existing = temp_parent.op(COMP_NAME)
        if existing is not None:
            existing.destroy()
        export_comp = temp_parent.create("containerCOMP", COMP_NAME)
        _populate_comp(export_comp, repo_root, info_text)
        export_comp.save(export_path)
    finally:
        try:
            temp_parent.destroy()
        except Exception:
            pass

    # Resolve install parent: respect INSTALL_PARENT_PATH (set at the top
    # of this module from the env var, default '/project1') instead of
    # delegating to the legacy module's hard-coded /local default. The
    # standalone API COMP must end up at /project1 by default so it
    # serializes with the user's .toe.
    install_parent = None
    if INSTALL_PARENT_PATH:
        install_parent = op(INSTALL_PARENT_PATH)
        if install_parent is None:
            print(
                f"[tdpilot_API] WARNING: install path {INSTALL_PARENT_PATH} not found; "
                "falling back to legacy default"
            )
    if install_parent is None:
        install_parent = _legacy._resolve_install_parent_comp()
    if install_parent is not None:
        live = install_parent.op(COMP_NAME)
        if live is not None:
            live.destroy()
        live = install_parent.create("containerCOMP", COMP_NAME)
        _populate_comp(live, repo_root, info_text)
        print(f"[tdpilot_API] installed at {live.path}")

    version = _legacy._get_version(repo_root)
    print(f"[tdpilot_API] built v{version}")
    print(f"[tdpilot_API] exported TOX: {export_path}")
    # v2.1.1 — refresh the API .tox source-hash manifest so CI's
    # check_tox_api_freshness gate stays green.
    _write_api_tox_source_hash(repo_root)
    if install_parent is None:
        print(f"[tdpilot_API] drag {export_path} into a TD project to install.")
    return export_path


build_and_export()
