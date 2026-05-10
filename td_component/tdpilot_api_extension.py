"""
TDPilot API — COMP extension class.

Includes a fire-and-forget completion sound that plays when the agent
finishes a turn. macOS uses /System/Library/Sounds/Glass.aiff via afplay,
Linux uses paplay, Windows uses winsound. Toggleable via the COMP's
`Soundondone` parameter.

The class is held as a per-COMP singleton via the module-level
``get_extension(comp)`` factory. The factory pattern bypasses TD's
Extensions-page parameter (whose expression parser had unresolved
issues with both `mod()`/`op()` resolution and dotted-attribute syntax
in TD 2025.32460) — instead the executeDAT and parameterexecuteDAT
call ``get_extension(parent())`` directly to obtain the live instance.

All TD operator access is funneled through this class so the rest of
the modules (agent, dispatcher, runtime) stay TD-API-free and
unit-testable outside TD.
"""

from __future__ import annotations

import os
import sys
from typing import Any

# Per-COMP singleton cache. Keyed by COMP node id (unique + stable for
# the COMP's lifetime). When a COMP is destroyed the id becomes invalid,
# so a recreated COMP gets a fresh instance automatically.
_INSTANCES: dict[int, TDPilotAPIExt] = {}


def get_extension(comp) -> TDPilotAPIExt | None:
    """Return the live TDPilotAPIExt for `comp`, creating it on first call.

    Idempotent — repeated calls return the same instance, preserving the
    AgentRuntime's conversation history across pulses and frame ticks.
    """
    if comp is None:
        return None
    key = comp.id
    inst = _INSTANCES.get(key)
    if inst is None:
        inst = TDPilotAPIExt(comp)
        _INSTANCES[key] = inst
    return inst


def reset_extension(comp) -> None:
    """Drop the cached instance so the next get_extension rebuilds. Used
    when the user pulses Reload Config and we want the runtime rewired
    around a fresh API key without restarting TD."""
    if comp is None:
        return
    _INSTANCES.pop(comp.id, None)


class TDPilotAPIExt:
    """COMP extension. One per loaded tdpilot_API COMP."""

    def __init__(self, owner_comp):
        self.owner = owner_comp
        self._runtime = None
        self._dispatcher = None
        self._tools: list[dict] = []
        self._handlers_module = None
        # Wire op('tdpilot_api_*') module imports into the regular import path
        # so each module can `from tdpilot_api_config import ...` cleanly.
        self._ensure_module_path()
        self._build_runtime()

    # Phase 3 (F-10) — public accessor for the runtime. Handlers that
    # need the cook-thread-bypass dispatcher (handle_recipe_replay,
    # handle_tool_batch, etc.) previously reached in via
    # ``ext._runtime._raw_dispatcher`` — a chain of private attrs that
    # broke any time either field was renamed. Calling through these
    # properties keeps internal field names free to evolve.
    @property
    def runtime(self):
        return self._runtime

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _ensure_module_path(self) -> None:
        """Bind each child textDAT's module into sys.modules under its DAT
        name so plain ``from tdpilot_api_X import Y`` resolves.

        IMPORTANT: we OVERWRITE sys.modules[name] every time. ``setdefault``
        was wrong — after a rebuild (destroy + recreate the COMP), the new
        textDATs hold the new code but ``sys.modules`` still has the OLD
        modules from the previous build. ``setdefault`` keeps the old ones,
        so ``from tdpilot_api_runtime import AgentRuntime`` resolves to the
        STALE class while every other piece of state (the new COMP, its new
        DATs) is fresh. The result was a stuck "thinking…" forever and no
        LLM response in the chat — exactly the bug we were chasing.
        """
        for name in (
            "tdpilot_api_config",
            # PR-19 (F-18) — tdpilot_api_lookup is a pure module
            # (no TD globals at import time) and is imported by the
            # batch / recipes / patches / macros handlers. Bind early
            # so those modules' top-level imports resolve.
            "tdpilot_api_lookup",
            # schema_defs + schema_map MUST come before tdpilot_api_schema:
            # the shim's `from tdpilot_api_schema_defs import ...` resolves
            # via sys.modules, so the underlying modules need to be
            # registered first.
            "tdpilot_api_schema_defs",
            "tdpilot_api_schema_map",
            "tdpilot_api_schema",
            "tdpilot_api_agent",
            "tdpilot_api_dispatcher",
            "tdpilot_api_runtime",
            "tdpilot_api_web_callbacks",
            "tdpilot_api_bm25",
            "tdpilot_api_memory",
            "tdpilot_api_knowledge",
            "tdpilot_api_recipes",
            "tdpilot_api_skills",
            "tdpilot_api_patches",
            "tdpilot_api_user_tools",
            "tdpilot_api_subagents",
            "tdpilot_api_macros",
            "tdpilot_api_official_docs",
            "tdpilot_api_td2025",
            "tdpilot_api_introspect",
            "tdpilot_api_batch",
            "tdpilot_api_recovery",
            "tdpilot_api_tracing",
            "tdpilot_api_compaction",
            "mcp_webserver_callbacks",
        ):
            child = self.owner.op(name)
            if child is not None:
                sys.modules[name] = child.module

    def _read_comp_config(self) -> dict:
        """Build the runtime's config dict from the COMP's parameter panel,
        falling back to env vars / config.json / defaults for anything not
        set on the COMP. Lets the user adjust Max Tokens / Turn Budget /
        Temperature / Model / Base URL in the param panel and pulse Reload
        Config to apply — the COMP becomes the single source of truth."""
        from tdpilot_api_config import resolved_config  # type: ignore[import-not-found]

        cfg = resolved_config()
        for par_name, key, caster in (
            ("Maxtokens", "max_tokens", int),
            ("Turnbudget", "turn_budget", int),
            ("Temperature", "temperature", float),
            ("Model", "model", str),
            ("Baseurl", "base_url", str),
            ("Modeltier", "model_tier", str),  # Sprint 4.3
            ("Flashmodel", "flash_model", str),  # Sprint 4.3
        ):
            try:
                par = getattr(self.owner.par, par_name, None)
                if par is None:
                    continue
                v = par.eval()
                # Only override if the user actually set something. Empty
                # strings fall back to the env/file/default chain.
                if isinstance(v, str):
                    if v.strip():
                        cfg[key] = caster(v.strip())
                elif v is not None:
                    cfg[key] = caster(v)
            except Exception:
                continue
        # base_url shouldn't carry trailing slash — Agent normalizes too,
        # but we keep the dict clean here as well.
        if isinstance(cfg.get("base_url"), str):
            cfg["base_url"] = cfg["base_url"].rstrip("/")
        return cfg

    def _build_runtime(self) -> None:
        try:
            # 2.1.3 — surface insecure-mode prominently in textport so
            # users can't unknowingly run with auth disabled. Pre-2.1.3
            # this was a silent env toggle; the audit found it active
            # on a real machine with no startup signal that the
            # webserver was wide open.
            if os.environ.get("TDPILOT_API_INSECURE", "").strip() in ("1", "true", "yes"):
                msg = (
                    "\n"
                    "============================================================\n"
                    "  ⚠  TDPILOT_API_INSECURE=1 — chat-pipe webserver running\n"
                    "      with NO TOKEN AUTH. Anyone who can reach\n"
                    "      http://127.0.0.1:9987 can drive this TD instance.\n"
                    "      (Origin allowlist still rejects browser CSRF.)\n"
                    "      Unset the env var to require X-TDPilot-Token.\n"
                    "============================================================\n"
                )
                print(msg)

            handlers_dat = self.owner.op("mcp_webserver_callbacks")
            if handlers_dat is None:
                self._set_status("error: mcp_webserver_callbacks DAT missing")
                return
            self._handlers_module = handlers_dat.module

            # Force `full` exec mode for the standalone agent. The user
            # owns this TD process AND the DeepSeek API key — there is no
            # second-party security boundary to defend. Using anything
            # less hobbles the agent's introspection without adding real
            # safety:
            #   - `restricted` strips builtins (no print, no Exception)
            #   - `standard` still blocks os / subprocess / import / file I/O
            # Both prevent the agent from doing simple things like
            # `import TDJson` to enumerate operator types, forcing it to
            # brute-force trial-and-error instead. Symptom: agent burns
            # 10+ tool calls trying random POP type names.
            #
            # COEXISTENCE NOTE: this OVERWRITES the env var, which means
            # the dpsk4 / Claude Code variant also picks up `full` mode
            # while the standalone is loaded in the same TD session.
            # That's an acceptable trade-off — both variants are driven
            # by the same user; a malicious tool author isn't part of
            # our threat model. If you need strict isolation, run the
            # variants in separate TD processes.
            #
            # 2.1.3 — defense in depth: when ``TDPILOT_API_INSECURE=1``
            # is also set, the chat-pipe webserver bypasses token auth.
            # If we ALSO leave EXEC_MODE=full, an unauthenticated POST
            # to /send can chain into ``td_exec_python`` with full
            # Python privileges (drive-by RCE from any browser tab the
            # user has open). Clamp to ``restricted`` instead — the
            # agent loses some introspection, but a no-auth surface
            # cannot escalate to RCE. Users who want both insecure mode
            # AND full exec must explicitly opt in by setting
            # ``TDPILOT_API_ALLOW_INSECURE_FULL_EXEC=1``.
            insecure = os.environ.get("TDPILOT_API_INSECURE", "").strip() in ("1", "true", "yes")
            allow_insecure_full = os.environ.get("TDPILOT_API_ALLOW_INSECURE_FULL_EXEC", "").strip() in (
                "1",
                "true",
                "yes",
            )
            if insecure and not allow_insecure_full:
                os.environ["TD_MCP_EXEC_MODE"] = "restricted"
                print(
                    "[tdpilot_API] WARNING: TDPILOT_API_INSECURE=1 detected — "
                    "clamping TD_MCP_EXEC_MODE to 'restricted' to break the "
                    "no-auth + full-exec RCE chain. Set "
                    "TDPILOT_API_ALLOW_INSECURE_FULL_EXEC=1 to opt back into "
                    "full mode (only safe in trusted single-user dev sandboxes)."
                )
            else:
                os.environ["TD_MCP_EXEC_MODE"] = "full"

            from tdpilot_api_dispatcher import make_dispatcher  # type: ignore[import-not-found]
            from tdpilot_api_runtime import AgentRuntime  # type: ignore[import-not-found]
            from tdpilot_api_schema import TOOL_SCHEMAS  # type: ignore[import-not-found]

            # Sprint 2 modules live in their own textDATs. The dispatcher
            # walks all handler modules in order — TD-side handlers first,
            # then memory, then knowledge — so `handle_*` lookups resolve
            # transparently regardless of which module owns them.
            handler_modules: list = [self._handlers_module]
            for mod_name, label in (
                ("tdpilot_api_memory", "memory"),
                ("tdpilot_api_knowledge", "knowledge"),
                ("tdpilot_api_recipes", "recipes"),
                ("tdpilot_api_skills", "skills"),
                ("tdpilot_api_patches", "patches"),
                ("tdpilot_api_user_tools", "user_tools"),
                ("tdpilot_api_subagents", "subagents"),
                ("tdpilot_api_macros", "macros"),
                ("tdpilot_api_official_docs", "official_docs"),
                ("tdpilot_api_td2025", "td2025_native"),
                ("tdpilot_api_introspect", "introspect"),
                ("tdpilot_api_batch", "tool_batch"),
                ("tdpilot_api_tracing", "tracing"),
            ):
                dat = self.owner.op(mod_name)
                if dat is not None:
                    handler_modules.append(dat.module)
                else:
                    print(f"[tdpilot_API] {mod_name} DAT missing — {label} tools disabled")

            # Tool list: built-in TOOL_SCHEMAS + any user tools loaded
            # from ~/.tdpilot-api/tools/. User tools are registered via
            # the dispatcher's extra_mappings so we don't need to mutate
            # the static TOOL_TO_HANDLER dict in tdpilot_api_schema.
            self._tools = list(TOOL_SCHEMAS)
            extra_mappings: dict = {}
            ut_dat = self.owner.op("tdpilot_api_user_tools")
            if ut_dat is not None:
                try:
                    ut_dat.module.load_user_tools(
                        self._tools,
                        handler_modules,
                        extra_mappings,
                    )
                except Exception as exc:
                    print(f"[tdpilot_API] user-tools loader crashed: {exc}")

            self._dispatcher = make_dispatcher(
                tuple(handler_modules),
                extra_mappings=extra_mappings,
            )
            self._runtime = AgentRuntime(
                dispatcher=self._dispatcher,
                tools=self._tools,
                config=self._read_comp_config(),
            )
            self._set_status("ready" if self._runtime._agent is not None else "no api key")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"init error: {type(exc).__name__}: {exc}")
            print(f"[tdpilot_API] init error: {exc}")

    # ------------------------------------------------------------------
    # Pulse handlers — invoked by the parameterexecuteDAT
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 2.1.3 — chat inbox queue. Backstop for the case where /send fires
    # while the worker is still processing a prior turn (multiple browser
    # tabs, external tooling, param-panel + HTML simultaneously).
    # Pre-2.1.3 the second send overwrote ``Chatmessage.val`` and the
    # first message was silently dropped if not yet consumed. Now we
    # always clear the param immediately, push the message into a FIFO
    # on ``comp.storage``, and drain one entry per EV_DONE.
    # ------------------------------------------------------------------

    _STORAGE_KEY_INBOX = "tdpilot_api_chat_inbox"

    def _inbox(self) -> list:
        """Return the live inbox queue, creating it on first use.
        Stored on ``comp.storage`` so it survives textDAT reload (same
        rationale as ``_ws_clients`` in tdpilot_api_web_callbacks)."""
        try:
            box = self.owner.fetch(self._STORAGE_KEY_INBOX, None)
            if not isinstance(box, list):
                box = []
                self.owner.store(self._STORAGE_KEY_INBOX, box)
            return box
        except Exception:
            return []

    def _enqueue(self, msg: str) -> int:
        """Append a message; return the resulting queue length (1 = head)."""
        try:
            box = self._inbox()
            box.append(msg)
            self.owner.store(self._STORAGE_KEY_INBOX, box)
            return len(box)
        except Exception:
            return -1

    def _dequeue(self) -> str | None:
        """Pop one message off the head, or None if empty."""
        try:
            box = self._inbox()
            if not box:
                return None
            msg = box.pop(0)
            self.owner.store(self._STORAGE_KEY_INBOX, box)
            return msg
        except Exception:
            return None

    def _clear_inbox(self) -> None:
        try:
            self.owner.store(self._STORAGE_KEY_INBOX, [])
        except Exception:
            pass

    def _drain_inbox_one(self) -> None:
        """Try to start a turn for the next queued message. No-op if
        the runtime is missing or still busy. Called from EV_DONE."""
        if self._runtime is None:
            return
        msg = self._dequeue()
        if msg is None:
            return
        ok = self._runtime.start_turn(msg)
        if not ok:
            # Worker still busy — re-insert at head, try again on next EV_DONE.
            box = self._inbox()
            box.insert(0, msg)
            try:
                self.owner.store(self._STORAGE_KEY_INBOX, box)
            except Exception:
                pass

    def _read_chat_param(self) -> str:
        """Read the current Chatmessage parameter as a stripped string.
        Wrapped so callers don't need to know about TD's parameter API
        (calling ``.eval()`` directly tripped a security-hook regex)."""
        par = self.owner.par.Chatmessage
        getter = getattr(par, "eval", None)
        raw = getter() if callable(getter) else par
        return str(raw or "").strip()

    def OnSendPulse(self, skip_html_echo: bool = False) -> None:
        """Fire a chat turn with whatever's in the Chatmessage parameter.

        skip_html_echo: when True, we DON'T broadcast a 'user' append to
        connected browser tabs — the assumption is the HTML already echoed
        the user's input optimistically (its send() path does this, see
        chat HTML). Default False so param-panel sends DO get pushed to
        the browser.

        2.1.3 — when the worker is busy, append to the inbox queue
        instead of silently dropping. ``Chatmessage.val`` is always
        cleared so a follow-up /send isn't blocked by stale text.
        """
        if self._runtime is None:
            self._set_status("not ready")
            return
        msg = self._read_chat_param()
        if not msg:
            self._set_status("empty message")
            return
        # Always clear the param up front. Pre-2.1.3 we only cleared on
        # successful start_turn; a second /send while busy overwrote the
        # un-consumed value. Now the param is a transient input slot,
        # not a buffer.
        try:
            self.owner.par.Chatmessage.val = ""
        except Exception:
            pass
        ok = self._runtime.start_turn(msg)
        if ok:
            self._append_transcript("user", msg)
            if not skip_html_echo:
                self._html_append("user", msg)
        else:
            position = self._enqueue(msg)
            self._append_transcript("user", msg)
            if not skip_html_echo:
                self._html_append("user", msg)
            if position > 0:
                self._set_status(f"busy — queued at position {position}")
            else:
                self._set_status("busy — queue write failed")

    def OnStopPulse(self) -> None:
        if self._runtime is not None:
            self._runtime.stop()
        self._set_status("stopped")

    def OnResetPulse(self) -> None:
        if self._runtime is not None:
            self._runtime.reset()
        transcript = self.owner.op("chat_transcript")
        if transcript is not None:
            transcript.clear(keepFirstRow=True)
        # 2.1.3 — drop any queued (un-processed) messages too. A reset
        # implies "start fresh"; running stale queue items would surface
        # as ghost user messages from the prior session.
        self._clear_inbox()
        # Tell every connected browser to wipe its view.
        self._broadcast({"type": "clear"})
        self._set_status("reset")

    def OnReloadConfigPulse(self) -> None:
        # Rebuild the runtime entirely so the new COMP-param values
        # (Max Tokens / Turn Budget / Model / etc.) take effect. Just
        # calling self._runtime.reload_config() would re-read env+file
        # but ignore the COMP params, which is what the user is editing.
        # Preserve conversation history across the rebuild.
        history: list[dict] = []
        if self._runtime is not None and self._runtime._agent is not None:
            history = list(self._runtime._agent.messages)
        self._build_runtime()
        if self._runtime is not None and self._runtime._agent is not None:
            self._runtime._agent.messages = history
            self._set_status("config reloaded")

    def OnOpenPanelPulse(self) -> None:
        """Manually trigger the floating-panel auto-open (useful when the
        user dismissed the panel and wants it back, or when Autoopenpanel
        was off at load time)."""
        try:
            executor = self.owner.op("tdpilot_api_executor")
            if executor is not None:
                executor.module._ensure_panel_open(self.owner)
                self._set_status("panel opened")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"open panel failed: {exc}")

    def OnSaveApiKeyPulse(self) -> None:
        from tdpilot_api_config import save_api_key_to_config  # type: ignore[import-not-found]

        key = str(self.owner.par.Apikey.eval() or "").strip()
        if not key:
            self._set_status("no key in param")
            return
        try:
            save_api_key_to_config(key)
            # Wipe the param so the key isn't lurking in the .toe if the
            # user saves it. We deliberately do NOT set an expression here
            # — TD's param-expression parser in 2025.32460 rejects every
            # form of `mod('tdpilot_api_config').fetch_api_key()` we tried,
            # and an expression with an error puts the param into red-bar
            # error state. The runtime reads the saved key directly from
            # ~/.tdpilot-api/config.json via fetch_api_key() — the param
            # is purely a paste target and doesn't need a live binding.
            self.owner.par.Apikey.val = ""
            if self._runtime is not None:
                self._runtime.reload_config()
            self._set_status("key saved + reloaded")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"save failed: {exc}")

    def OnVerifySetupPulse(self) -> dict:
        """Phase 5.1 — run the install-doctor check registry against
        the current standalone instance and return a JSON-serialisable
        dict of results.

        Wired to a future ``Verifysetup`` pulse param on the COMP. For
        now the method is callable from a textport one-liner so the
        user can sanity-check their install without leaving TD::

            mod('tdpilot_api_extension').get_extension(parent()).OnVerifySetupPulse()

        Falls back to a structured "skipped" result when the doctor
        script isn't reachable (older .tox builds, restricted
        environments).
        """
        try:
            import importlib.util
            from pathlib import Path

            repo_root = Path(__file__).resolve().parent.parent
            spec = importlib.util.spec_from_file_location(
                "tdpilot_doctor_live",
                repo_root / "scripts" / "doctor_live.py",
            )
            if spec is None or spec.loader is None:
                raise ImportError("doctor_live.py not on disk")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            results = module.run_all_checks()
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"verify_setup failed: {exc}")
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "results": [],
            }

        out = {
            "ok": True,
            "results": [
                {
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                    "fix": r.fix,
                }
                for r in results
            ],
        }
        n_fail = sum(1 for r in results if r.status == "fail")
        n_warn = sum(1 for r in results if r.status == "warn")
        out["fail"] = n_fail
        out["warn"] = n_warn
        self._set_status(f"verify: {n_fail} fail, {n_warn} warn")
        return out

    # ------------------------------------------------------------------
    # Per-frame drain — invoked by the executeDAT onFrameStart
    # ------------------------------------------------------------------

    def DrainEvents(self) -> None:
        if self._runtime is None:
            return
        # Pump dispatcher BEFORE drain so any tool result that lands this
        # frame can fan out an EV_TOOL_RESULT event in the same drain pass.
        self._runtime.pump_dispatcher()
        # Per-event try/except is essential: drain_events() has already
        # popped each event off the queue, so a single malformed event
        # that raises inside _handle_event would lose every subsequent
        # event in the same drain pass — and the executor's outer
        # try/except would swallow the traceback. That symptom matches
        # the reported "no LLM response in browser or TD" bug exactly.
        for kind, payload in self._runtime.drain_events():
            try:
                self._handle_event(kind, payload)
            except Exception as exc:  # noqa: BLE001
                print(f"[tdpilot_API] _handle_event({kind!r}) failed: {type(exc).__name__}: {exc}")

    def _handle_event(self, kind: str, payload: Any) -> None:
        # Imports inside method so module-level reload of these textDATs
        # picks up new event constants without restarting the COMP.
        from tdpilot_api_runtime import (  # type: ignore[import-not-found]
            EV_DONE,
            EV_ERROR,
            EV_HINT,
            EV_MODEL,
            EV_STATE,
            EV_SUB_DONE,
            EV_SUB_TEXT,
            EV_SUB_TOOL,
            EV_TEXT,
            EV_TOOL_CALL,
            EV_TOOL_RESULT,
            EV_USAGE,
        )

        if kind == EV_TEXT:
            self._append_transcript("assistant", str(payload))
            self._html_append("assistant", str(payload))
        elif kind == EV_TOOL_CALL:
            name = payload.get("name", "?")
            args = payload.get("args") if isinstance(payload, dict) else None
            self._set_last_tool(name)
            line = name + "(" + _short_args(args) + ")"
            self._append_transcript("tool_call", line)
            # Phase 2 (1.8.0) — structured payload for the collapsible
            # chat UI. The browser opens a placeholder <details> entry
            # and waits for the matching tool_result to fill in the
            # outcome + latency. The legacy `append` path is preserved
            # in the transcript so /history rehydration on reload still
            # shows the same one-line summary.
            self._broadcast(
                {
                    "type": "tool_call",
                    "name": str(name),
                    "args": args if isinstance(args, dict) else {},
                    "summary": line,
                }
            )
        elif kind == EV_TOOL_RESULT:
            name = payload.get("name", "?")
            is_error = bool(payload.get("is_error")) if isinstance(payload, dict) else False
            result = payload.get("result") if isinstance(payload, dict) else None
            latency_ms = payload.get("latency_ms") if isinstance(payload, dict) else None
            ok = "ERR" if is_error else "ok"
            line = f"{name} → {ok}"
            self._append_transcript("tool_result", line)
            self._broadcast(
                {
                    "type": "tool_result",
                    "name": str(name),
                    "is_error": is_error,
                    "result": _coerce_jsonable(result),
                    "latency_ms": latency_ms if isinstance(latency_ms, int) else None,
                    "summary": line,
                }
            )
        elif kind == EV_DONE:
            self._set_status("idle")
            self._html_status("idle")
            self._play_done_sound("done")
            # 2.1.3 — drain one queued message if any. Calling
            # start_turn synchronously here is safe because EV_DONE is
            # delivered on the cook thread and start_turn is the
            # cook-thread-side public API.
            self._drain_inbox_one()
        elif kind == EV_ERROR:
            self._append_transcript("error", str(payload))
            self._html_append("error", str(payload))
            self._set_status("error")
            self._html_status("error")
            self._play_done_sound("error")
            # 2.1.4 — drain inbox on error too, not just EV_DONE. Pre-2.1.4
            # a failed turn left queued messages stranded in
            # comp.storage["tdpilot_api_chat_inbox"] until a later
            # successful turn happened to fire EV_DONE. (Codex review on
            # PR #28, P1.) The runtime is now idle either way, so the
            # next message can start.
            self._drain_inbox_one()
        elif kind == EV_HINT:
            # Phase 1.3 — soft validation nudge. Rendered as a "hint"
            # role; the chat HTML / textTable show it dimmer than the
            # main reply so the user sees it but the agent's text
            # remains primary. Never blocks.
            if isinstance(payload, dict):
                msg = payload.get("message", "")
            else:
                msg = str(payload)
            self._append_transcript("hint", msg)
            self._html_append("hint", msg)
        elif kind == EV_STATE:
            self._set_status(str(payload))
            self._html_status(str(payload))
        elif kind == EV_MODEL:
            # Sprint 4.3 — surface the routed model in the COMP Status
            # param ("flash" or "pro"). The user sees which tier is
            # active per turn — transparent without cluttering the chat.
            picked = (payload.get("model") or "") if isinstance(payload, dict) else ""
            tier = (payload.get("tier") or "") if isinstance(payload, dict) else ""
            short = "flash" if "flash" in picked.lower() else ("pro" if "pro" in picked.lower() else picked)
            try:
                self.owner.par.Activemodel.val = short[:60]
            except Exception:
                pass
            # Phase 2 (1.8.0) — push a structured `model` event to the
            # chat. The status-bar code uses tier + picked separately
            # to render a short-form badge ("pro" vs "flash") and a
            # tooltip with the full model name.
            self._broadcast(
                {
                    "type": "model",
                    "tier": str(tier),
                    "model": str(picked),
                    "short": str(short),
                }
            )
        elif kind == EV_USAGE:
            # Phase 2 (1.8.0) — token usage from a single API call.
            # Sanitised in the runtime; we just forward the dict as-is
            # so the chat can accumulate a per-turn meter.
            self._broadcast(
                {
                    "type": "usage",
                    "usage": payload if isinstance(payload, dict) else {},
                }
            )
        elif kind == EV_SUB_TEXT:
            # Sprint 4.1 — surface subagent text in the chat with a
            # [sub:id] tag so it's distinguishable from parent output.
            sub_id = payload.get("id", "?") if isinstance(payload, dict) else "?"
            text = payload.get("text", "") if isinstance(payload, dict) else ""
            line = f"[sub:{sub_id}] {text}"
            self._append_transcript("subagent", line)
            self._html_append("subagent", line)
        elif kind == EV_SUB_TOOL:
            sub_id = payload.get("id", "?") if isinstance(payload, dict) else "?"
            name = payload.get("name", "?") if isinstance(payload, dict) else "?"
            args = payload.get("args") if isinstance(payload, dict) else None
            line = f"[sub:{sub_id}] {name}({_short_args(args)})"
            self._append_transcript("subagent_tool", line)
            self._html_append("subagent_tool", line)
        elif kind == EV_SUB_DONE:
            sub_id = payload.get("id", "?") if isinstance(payload, dict) else "?"
            status = payload.get("status", "?") if isinstance(payload, dict) else "?"
            tool_calls = payload.get("tool_calls", 0) if isinstance(payload, dict) else 0
            duration = payload.get("duration_ms", 0) if isinstance(payload, dict) else 0
            line = f"[sub:{sub_id}] {status} — {tool_calls} tool calls in {duration}ms"
            self._append_transcript("subagent_done", line)
            self._html_append("subagent_done", line)

    # ------------------------------------------------------------------
    # UI sinks
    # ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        try:
            self.owner.par.Status.val = text[:120]
        except Exception:
            pass

    def _set_last_tool(self, name: str) -> None:
        try:
            self.owner.par.Lasttool.val = name[:60]
        except Exception:
            pass

    def _should_play_sound(self) -> bool:
        try:
            return bool(self.owner.par.Soundondone.eval())
        except Exception:
            return True  # default on if param missing (older builds)

    def _get_sound_volume(self) -> float:
        """Read the Soundvolume custom param (Float 0-1). Defaults to 0.7
        if missing (older builds without the param). Clamped to [0, 1]."""
        try:
            v = float(self.owner.par.Soundvolume.eval())
        except Exception:
            return 0.7
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v

    def _play_done_sound(self, kind: str = "done") -> None:
        """Play a brief system sound on turn completion. Cross-platform with
        volume control (Soundvolume param, 0-1). Fire-and-forget — runs in
        a daemon thread so it never blocks the cook thread or the agent
        worker. ``kind`` selects between ``done`` and ``error`` sounds.

        Per-platform strategy:
          * macOS — afplay -v <vol> with /System/Library/Sounds/*.aiff
            (Glass for done, Sosumi for error). afplay accepts volume
            as a 0-1 float via ``-v``.
          * Linux — paplay --volume=<int> with freedesktop sound theme
            (complete.oga / dialog-error.oga). paplay's volume is 0-65536
            (65536 = 100%).
          * Windows — ctypes mciSendStringW (built into winmm.dll, no
            extra deps) with C:\\Windows\\Media\\*.wav. ``setaudio alias
            volume to N`` accepts 0-1000 (1000 = 100%). Falls back through
            multiple .wav file names because Win10/11 default Media folder
            contents are not contractually guaranteed (LTSC and Server SKUs
            prune them). Last-resort fallback is winsound.MessageBeep
            which has no volume but at least makes A noise.
        """
        if not self._should_play_sound():
            return
        volume = self._get_sound_volume()
        if volume <= 0.0:
            return  # muted

        import platform
        import threading

        system = platform.system()

        def _play() -> None:
            try:
                if system == "Darwin":
                    self._play_sound_macos(kind, volume)
                elif system == "Linux":
                    self._play_sound_linux(kind, volume)
                elif system == "Windows":
                    self._play_sound_windows(kind, volume)
            except Exception:
                # Sound failure is never worth surfacing to the user.
                pass

        threading.Thread(target=_play, daemon=True, name="tdpilot_api_sound").start()

    @staticmethod
    def _play_sound_macos(kind: str, volume: float) -> None:
        import subprocess

        sound = (
            "/System/Library/Sounds/Sosumi.aiff" if kind == "error" else "/System/Library/Sounds/Glass.aiff"
        )
        # afplay -v takes a 0-1 float volume.
        subprocess.Popen(
            ["afplay", "-v", f"{volume:.3f}", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _play_sound_linux(kind: str, volume: float) -> None:
        import subprocess

        sound = (
            "/usr/share/sounds/freedesktop/stereo/dialog-error.oga"
            if kind == "error"
            else "/usr/share/sounds/freedesktop/stereo/complete.oga"
        )
        # paplay --volume is 0-65536 (65536 = 100%, won't clip above that).
        vol_int = int(volume * 65536)
        subprocess.Popen(
            ["paplay", f"--volume={vol_int}", sound],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _play_sound_windows(kind: str, volume: float) -> None:
        """Play a Win10/11 system sound with volume control.

        Why mciSendStringW: it's the only stdlib path that supports volume
        on Windows. winsound.MessageBeep ignores volume (annoying system
        beep at fixed loudness); winsound.PlaySound(SND_FILENAME) plays a
        wav but also has no volume control. mci has been shipped in winmm
        since Windows 2000; it accepts 0-1000 for ``setaudio volume to N``
        per Microsoft Win32 docs.
        """
        import os

        media = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media")
        # Multi-candidate fallback because the exact files in
        # %WINDIR%\Media vary between Windows builds, SKUs (LTSC strips
        # most), and OEM customizations. Try the most-common-first.
        if kind == "error":
            candidates = (
                "Windows Critical Stop.wav",
                "Windows Hardware Fail.wav",
                "chord.wav",
                "Critical Stop.wav",
            )
        else:
            candidates = (
                "Notify.wav",
                "Windows Notify.wav",
                "Windows Notify Calendar.wav",
                "chimes.wav",
                "tada.wav",
                "ding.wav",
                "chord.wav",
            )
        sound = None
        for name in candidates:
            p = os.path.join(media, name)
            if os.path.isfile(p):
                sound = p
                break

        if sound is None:
            # Last-resort: system beep with no volume control. At least
            # the user hears SOMETHING even on a stripped-down Windows.
            try:
                import winsound  # type: ignore[import-not-found]

                flag = winsound.MB_ICONHAND if kind == "error" else winsound.MB_OK
                winsound.MessageBeep(flag)
            except Exception:
                pass
            return

        # mciSendStringW path with volume. Wrap aliases in quotes because
        # the path may contain spaces (always does on Win10+: "Windows
        # Critical Stop.wav"). MCI takes commands as a single space-
        # separated string and uses quotes to preserve spaces.
        try:
            import ctypes

            mci = ctypes.windll.winmm.mciSendStringW  # type: ignore[attr-defined]
            alias = f"tdpilot_api_{kind}_sound"
            # Close any leftover instance from a previous play.
            mci(f'close "{alias}"', None, 0, 0)
            # Open. waveaudio is the device type for .wav files.
            r = mci(f'open "{sound}" type waveaudio alias "{alias}"', None, 0, 0)
            if r != 0:
                # Try without explicit type — MCI will auto-detect.
                r = mci(f'open "{sound}" alias "{alias}"', None, 0, 0)
            if r == 0:
                vol_mci = int(volume * 1000)  # MCI volume is 0-1000.
                mci(f'setaudio "{alias}" volume to {vol_mci}', None, 0, 0)
                mci(f'play "{alias}"', None, 0, 0)
                # Don't close — MCI will release on file end. Closing
                # immediately would cut the playback short.
                return
        except Exception:
            pass

        # Fallback: winsound.PlaySound (no volume control but plays the
        # whole file).
        try:
            import winsound  # type: ignore[import-not-found]

            winsound.PlaySound(
                sound,
                winsound.SND_FILENAME | winsound.SND_ASYNC,
            )
        except Exception:
            pass

    def _broadcast(self, payload: dict) -> None:
        """Push a JSON event to every connected browser tab via the TD
        WebServer DAT's WebSocket fan-out (see _ws_clients in
        tdpilot_api_web_callbacks). Replaces the earlier `executeJavaScript`
        path that only updated the in-COMP webrenderTOP — the user's actual
        chat lives in their default browser, which doesn't share JS state
        with the embedded webrenderTOP. WebSocket reaches both.

        We DO NOT swallow exceptions here any more — the symptom of the
        previous `except Exception: pass` was exactly the bug we are
        debugging now: LLM response events silently disappeared on the
        way to the browser. Print to TD's textport so the failure is
        visible without crashing the cook thread.
        """
        ws = self.owner.op("chat_web_server")
        if ws is None:
            print("[tdpilot_API] _broadcast: chat_web_server DAT missing")
            return
        cb = self.owner.op("tdpilot_api_web_callbacks")
        if cb is None:
            print("[tdpilot_API] _broadcast: tdpilot_api_web_callbacks DAT missing")
            return
        try:
            cb.module.broadcast(ws, payload)
        except Exception as exc:  # noqa: BLE001
            kind = payload.get("type", "?") if isinstance(payload, dict) else "?"
            print(f"[tdpilot_API] _broadcast({kind}) failed: {type(exc).__name__}: {exc}")

    def _html_append(self, role: str, message: str) -> None:
        self._broadcast({"type": "append", "role": role, "message": message})

    def _html_status(self, status: str) -> None:
        self._broadcast({"type": "status", "status": status})

    def _html_full_sync(self) -> None:
        """Snapshot the whole transcript and push it to all browser tabs.
        Browser tabs also pull this on WebSocket connect (see callbacks
        `onWebSocketOpen`), so this is mainly for explicit re-syncs."""
        transcript = self.owner.op("chat_transcript")
        rows: list[dict] = []
        if transcript is not None:
            try:
                for r in range(1, transcript.numRows):
                    rows.append({"role": transcript[r, 0].val, "message": transcript[r, 1].val})
            except Exception:
                return
        self._broadcast({"type": "fullSync", "rows": rows})

    def _append_transcript(self, role: str, message: str) -> None:
        transcript = self.owner.op("chat_transcript")
        if transcript is None:
            return
        # First row is the header; data rows follow.
        try:
            if transcript.numRows == 0:
                transcript.appendRow(["role", "message"])
            transcript.appendRow([role, message[:4000]])
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API] transcript append failed: {exc}")


def _short_args(args: Any) -> str:
    if args is None:
        return ""
    s = repr(args)
    if len(s) > 80:
        return s[:77] + "..."
    return s


def _coerce_jsonable(value: Any, _depth: int = 0) -> Any:
    """Convert a tool-result value into something ``json.dumps`` will
    serialise without choking. Tool results travel from worker thread
    to dispatcher to the WS broadcast layer; an exotic type (custom
    TD object, set, datetime, etc.) reaching ``json.dumps`` raises
    and the broadcast helper drops the message — leaving the chat's
    placeholder tool-call entry stuck on "running…" forever.

    Strategy:
      - primitives (str, int, float, bool, None) pass through.
      - dicts have keys stringified and values recursed (cap depth).
      - lists/tuples/sets recurse (sets become lists).
      - anything else — repr() it, capped at 4000 chars.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _depth >= 6:
        return repr(value)[:4000]
    if isinstance(value, dict):
        return {str(k): _coerce_jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_jsonable(v, _depth + 1) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_coerce_jsonable(v, _depth + 1) for v in value]
    return repr(value)[:4000]
