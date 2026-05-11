"""
TDPilot API — standalone agent loop running inside TouchDesigner.

DeepSeek v4 via Anthropic-compatible /v1/messages endpoint. Implements
the tool-use → tool_result loop until the model emits stop_reason
'end_turn' or the turn budget is exhausted.

This module is sync. The TD-side caller (the agent COMP's extension)
runs `Agent.run_turn()` on a worker thread and marshals callbacks back
to the cook thread via td.run() — see tdpilot_api_runtime.py.

No external dependencies — pure stdlib (urllib, json, threading) so it
works against TD's stock Python 3.11 with zero install steps.

Streaming is NOT implemented in this revision. The first call returns
the full response, including any tool_use blocks. Streaming will be
added in a follow-up once non-streaming is verified end-to-end against
DeepSeek's compat layer.
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

# F-12 — soft-import the tool-error sentinel helper. The dispatcher
# module owns the canonical predicate; the agent loop just calls it.
# Soft-import so a stripped-down test embed without the dispatcher
# module still loads. The fallback mirrors the dispatcher's v2.0
# semantics: only the explicit ``_tool_error`` sentinel marks failure.
try:
    from tdpilot_api_dispatcher import is_tool_error_result  # type: ignore[import-not-found]
except ImportError:

    def is_tool_error_result(result):  # type: ignore[misc]
        if not isinstance(result, dict):
            return False
        if "_tool_error" in result:
            return bool(result["_tool_error"])
        return False

# ---------------------------------------------------------------------------
# SSL setup — TouchDesigner's bundled Python varies by platform on whether it
# can locate a default CA bundle. On Windows there is no file-based CA bundle
# at all (the OS uses CryptoAPI / the Windows certificate store), so file-
# path searches like the previous implementation always failed and Windows
# users got CERTIFICATE_VERIFY_FAILED. On Linux distros without a curl-style
# bundle in /etc/ssl, the same thing happened. The fix below tries strategies
# in priority order so HTTPS verification works out of the box on
# Win + Mac + Linux without the user installing anything.
# ---------------------------------------------------------------------------

_CA_CANDIDATE_PATHS = (
    "/etc/ssl/cert.pem",  # macOS default
    "/etc/ssl/certs/ca-certificates.crt",  # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",  # RHEL / CentOS
    "/usr/local/etc/openssl@3/cert.pem",  # Homebrew Intel
    "/opt/homebrew/etc/openssl@3/cert.pem",  # Homebrew Apple Silicon
    "/usr/local/etc/openssl/cert.pem",  # Homebrew older
)


def _resolve_ca_file() -> str | None:
    """Return the first CA bundle PATH that exists. Used as a last-resort
    fallback — on most platforms ``ssl.create_default_context()`` already
    loads the right certs without an explicit path."""
    explicit = os.environ.get("SSL_CERT_FILE", "").strip()
    if explicit and os.path.isfile(explicit):
        return explicit
    try:
        import certifi  # type: ignore[import-not-found]

        path = certifi.where()
        if os.path.isfile(path):
            return path
    except ImportError:
        pass
    for path in _CA_CANDIDATE_PATHS:
        if os.path.isfile(path):
            return path
    try:
        defaults = ssl.get_default_verify_paths()
        if defaults.cafile and os.path.isfile(defaults.cafile):
            return defaults.cafile
    except Exception:
        pass
    return None


def _build_ssl_context() -> ssl.SSLContext:
    """Build an SSLContext that can verify https://api.deepseek.com on every
    supported platform.

    Strategy (first that works wins):
      1. ``certifi.where()`` if certifi is installed — most reliable, ships
         a known-good Mozilla bundle.
      2. ``ssl.create_default_context()`` + ``load_default_certs()`` — the
         stdlib's platform-aware default. On Windows this pulls from the
         CryptoAPI / Schannel certificate store (no file paths involved).
         On macOS it uses the Keychain via /etc/ssl/cert.pem. On Linux it
         walks /etc/ssl + the OPENSSLDIR. This is the path that fixes the
         "Windows users get CERTIFICATE_VERIFY_FAILED" bug — the previous
         implementation went straight to file-path searching, which
         windows has no answer for.
      3. Explicit cafile from the candidate list (last-resort for stripped-
         down Linux containers without OS-default verify paths).

    Raises only if all three fail, with a clear message.
    """
    # 1. certifi
    try:
        import certifi  # type: ignore[import-not-found]

        certifi_path = certifi.where()
        if os.path.isfile(certifi_path):
            return ssl.create_default_context(cafile=certifi_path)
    except ImportError:
        pass

    # 2. OS-default — works on Win (cert store), Mac, most Linux.
    try:
        ctx = ssl.create_default_context()
        # On some platforms create_default_context loads certs lazily;
        # force them now and verify the context actually has CAs available.
        try:
            ctx.load_default_certs()
        except Exception:
            pass
        # Don't trust an empty store — fall through to explicit paths.
        if ctx.get_ca_certs():
            return ctx
    except Exception:
        pass

    # 3. Explicit cafile fallback.
    cafile = _resolve_ca_file()
    if cafile is not None:
        return ssl.create_default_context(cafile=cafile)

    raise AgentError(
        "No CA bundle found for HTTPS verification. On Windows this should "
        "never happen — check that TouchDesigner's bundled Python can read "
        "the system certificate store. On Linux/Mac, set SSL_CERT_FILE to "
        "a CA bundle path or `pip install certifi` into TD's Python."
    )


class AgentError(Exception):
    pass


class TurnBudgetExceeded(AgentError):
    pass


# 2.1.3 — explicit-intent overrides for ``_resolve_model``. Pre-2.1.3
# the auto-tier heuristic was the only way to get pro on a per-turn
# basis; users who wrote "use pro model" in a short prompt scored 0
# (no build-keyword, no code fence, no tool keywords, len<300) and
# kept routing to flash. These regexes catch the common ways a user
# tells the agent which tier to run on, e.g. "use pro", "use the pro
# model", "switch to pro", "force flash", "deepseek-v4-pro", etc.
#
# False-positive guard: ``\b…\b`` ensures "pro" doesn't match
# "professional" / "prompt" / "produce" and "flash" doesn't match
# "flashlight". The override also requires a verb cue ("use", "force",
# "switch to", "with", "via", "run in/with", "in") OR a noun-phrase
# context ("pro model", "flash tier") OR the full model id, so plain
# mentions like "I'll review the pro version of this scene" don't
# falsely trigger.
import re  # noqa: E402 — placed here to keep the module's primary imports up top

_PRO_OVERRIDE_RE = re.compile(
    r"\b(?:use|force|switch\s+to|with|via|run\s+(?:in|with))\s+(?:the\s+)?pro\b"
    r"|\bin\s+pro\s+(?:model|tier|mode)\b"
    r"|\bpro\s+(?:model|tier|mode)\b"
    r"|\bdeepseek[-_]?v4[-_]?pro\b",
    re.IGNORECASE,
)
_FLASH_OVERRIDE_RE = re.compile(
    r"\b(?:use|force|switch\s+to|with|via|run\s+(?:in|with))\s+(?:the\s+)?flash\b"
    r"|\bin\s+flash\s+(?:model|tier|mode)\b"
    r"|\bflash\s+(?:model|tier|mode)\b"
    r"|\bdeepseek[-_]?v4[-_]?flash\b",
    re.IGNORECASE,
)

# 2.3.1 — session-scope cue. Combined with _PRO_OVERRIDE_RE / _FLASH_OVERRIDE_RE
# this promotes the per-turn override into a sticky tier so the user's intent
# survives subsequent short turns that would otherwise auto-route to flash.
# Reproduces the user complaint that motivated the fix: "I said 'use only pro
# mode this session' and he executed one answer in pro then switched to flash."
_SESSION_SCOPE_RE = re.compile(
    r"\bthis\s+session\b"
    r"|\bfrom\s+now\s+on\b"
    r"|\bgoing\s+forward\b"
    r"|\balways\b"
    r"|\bevery\s+(?:turn|message|reply)\b"
    r"|\bonly\s+use\b"
    r"|\bstay\s+(?:on|in)\b"
    r"|\block(?:\s+(?:to|on|in))?\b"
    r"|\bfor\s+the\s+rest\s+of\s+(?:this\s+)?session\b"
    r"|\bonly\s+(?:pro|flash)\b",
    re.IGNORECASE,
)
# Explicit "back to auto" reset — independent of pro/flash mention.
_TIER_RESET_RE = re.compile(
    r"\bback\s+to\s+auto\b"
    r"|\bauto\s+(?:tier|mode|routing)\b"
    r"|\breset\s+(?:tier|mode|routing)\b",
    re.IGNORECASE,
)


# Default callbacks are no-ops so callers can opt into the events they need.
def _noop(*_a, **_kw):  # noqa: ANN001
    return None


class Agent:
    """Stateful conversation + tool-use loop.

    Typical use:
        agent = Agent(api_key=..., dispatcher=..., tools=TOOL_SCHEMAS,
                      system_prompt="You are operating TouchDesigner.")
        agent.add_user_message("Create a noise TOP and connect it to a level TOP.")
        agent.run_turn()  # blocks until end_turn or budget exhausted

    The conversation state lives in `self.messages` (Anthropic format).
    Multiple turns can be run by calling add_user_message + run_turn again;
    the loop preserves history.
    """

    def __init__(
        self,
        api_key: str,
        dispatcher: Callable[[str, dict], Any],
        tools: list[dict] | None = None,
        system_prompt: str = "",
        model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com/anthropic",
        max_tokens: int = 4096,
        temperature: float = 0.7,
        turn_budget: int = 10,
        request_timeout: float = 120.0,
        # Sprint 4.3 — multi-model routing.
        #   "auto"  → heuristic picks flash for simple lookups, pro for
        #             complex builds (DeepSeek auto-cache stays warm
        #             within a turn because we pin the model from
        #             user-message arrival until end_turn).
        #   "flash" → force deepseek-v4-flash (cheap, faster TTFT)
        #   "pro"   → force the configured ``model`` (default v4-pro)
        # Override at the COMP via Modeltier param.
        model_tier: str = "auto",
        flash_model: str = "deepseek-v4-flash",
        # on_model_change(reason, picked_model) — surfaced to the COMP
        # Status line so the user can see which tier each turn used.
        on_model_change: Callable[[str, str], None] = _noop,
        # 2.3.1 — on_tier_change(new_tier) fires when _maybe_promote_tier
        # mutates ``self.model_tier`` in response to a session-scope phrase
        # ("this session", "from now on", "always", "only pro", ...). The
        # AgentRuntime wires this to the COMP ``Modeltier`` param so the
        # promotion survives a chat-panel reload. Distinct from
        # on_model_change, which fires per-turn for the picked model.
        on_tier_change: Callable[[str], None] = _noop,
        # Phase 0.1 — cache-stable dynamic-context slot. Callable invoked
        # ONCE per API call (not once per turn — the same turn can fire
        # multiple API calls during tool-use chains). It returns a list
        # of synthetic messages (typically [user, assistant]) prepended
        # to ``self.messages`` for that one call. The system prompt
        # stays byte-stable; this is where volatile retrieval / index
        # context lives WITHOUT busting DeepSeek's auto-cache prefix.
        # The provider's output is NOT persisted to ``self.messages`` so
        # it's free to vary turn-to-turn (memory_save propagates here,
        # not in the system prompt).
        dynamic_context_provider: Callable[[], list[dict]] | None = None,
        # Phase 4.3 — conversation compaction. Optional; when set, the
        # agent calls ``compactor.maybe_compact(self.messages)`` at the
        # top of each ``_loop`` iteration. Set None to disable.
        compactor: Any | None = None,
        # Callbacks — all optional. Receive primitive args.
        on_text: Callable[[str], None] = _noop,
        on_tool_call: Callable[[str, dict], None] = _noop,
        on_tool_result: Callable[[str, Any, bool], None] = _noop,
        on_turn_done: Callable[[str], None] = _noop,
        on_error: Callable[[BaseException], None] = _noop,
        # Phase 2 (1.8.0) — surface DeepSeek's per-call token usage so
        # the chat status bar can render a token meter. Fires once
        # per API call (multiple times per turn during tool-use chains).
        # Keys typically: input_tokens, output_tokens, cache_read_input_tokens.
        # Treat all keys as optional — DeepSeek's compat layer may omit
        # some fields depending on the model version.
        on_usage: Callable[[dict], None] = _noop,
        # Phase 1.1 (v2.2.0) — auto-rollback on error regression.
        # Factory takes ``(dispatcher, tool_names)`` and returns a
        # context manager (e.g. ``AutoRollbackGuard``) used to wrap each
        # tool batch in ``_loop``. ``None`` disables the feature entirely
        # — the loop runs unwrapped, identical to pre-v2.2.0 behaviour.
        # Wired by AgentRuntime so it can honour ``TDPILOT_DISABLE_AUTO_ROLLBACK``.
        rollback_guard_factory: Callable[..., Any] | None = None,
        # Phase 1.2 (v2.2.0) — cycle detection. Factory takes zero
        # args and returns a fresh ``CycleLedger`` (one per turn) or
        # None. Agent._loop checks the ledger before each tool
        # dispatch; when the count reaches the ledger's threshold,
        # ``CycleDetected`` is raised, propagating to run_turn's
        # BaseException catch and out via ``on_error`` → ``EV_ERROR``.
        # Wired by AgentRuntime to honour ``TDPILOT_DISABLE_CYCLE_DETECTION``.
        cycle_ledger_factory: Callable[[], Any] | None = None,
    ) -> None:
        if not api_key:
            raise AgentError("api_key is required")
        if dispatcher is None:
            raise AgentError("dispatcher is required")
        self.api_key = api_key
        self.dispatcher = dispatcher
        # Sort tools alphabetically by name so the JSON sent to DeepSeek is
        # byte-stable across turns. DeepSeek's server-side prefix cache
        # (~50× cheaper input on hits) only kicks in if the prefix is
        # identical — unsorted tools would shuffle on every cook (Python
        # dict iteration order is insertion-stable but the AgentRuntime
        # builds its tool list opportunistically), missing the cache and
        # paying full price every turn.
        raw_tools = list(tools or [])
        try:
            self.tools = sorted(raw_tools, key=lambda t: t.get("name", ""))
        except Exception:
            self.tools = raw_tools
        self.system_prompt = system_prompt
        self.model = model
        self.flash_model = flash_model
        self.model_tier = (model_tier or "auto").strip().lower()
        if self.model_tier not in ("auto", "flash", "pro"):
            self.model_tier = "auto"
        # Active model for the in-flight turn. Set by ``_resolve_model``
        # at the top of each ``_loop`` call and held stable for the
        # entire tool-use chain so DeepSeek's auto-cache (model-keyed)
        # doesn't bust mid-turn.
        self._active_model: str = self.model
        self.on_model_change = on_model_change
        self.on_tier_change = on_tier_change
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.turn_budget = turn_budget
        self.request_timeout = request_timeout

        self.dynamic_context_provider = dynamic_context_provider
        self.compactor = compactor

        self.on_text = on_text
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_turn_done = on_turn_done
        self.on_error = on_error
        self.on_usage = on_usage
        self.rollback_guard_factory = rollback_guard_factory
        self.cycle_ledger_factory = cycle_ledger_factory

        self.messages: list[dict] = []
        self._stop_flag = threading.Event()

    # ------------------------------------------------------------------
    # Public state mutators
    # ------------------------------------------------------------------

    def add_user_message(self, text: str) -> None:
        """Append a user message, idempotent against accidental double
        sends (PR-18 / F-13).

        Pre-1.8.1 a UI double-click or a transient retry could append
        the same user text twice in a row. The duplicate then turned
        into two consecutive ``user`` blocks, which DeepSeek's compat
        layer rejects with ``messages: roles must alternate``. The
        guard checks the most recent message and no-ops if it's an
        identical user/text duplicate.

        Invariant: only blocks an EXACT-text repeat of the immediately
        preceding user message. Different text, or any non-user
        message in between, allows the append normally — so legitimate
        same-text re-sends after an assistant turn still go through.
        """
        if self.messages:
            last = self.messages[-1]
            if isinstance(last, dict) and last.get("role") == "user":
                last_content = last.get("content")
                if isinstance(last_content, list) and last_content:
                    first_block = last_content[0]
                    if (
                        isinstance(first_block, dict)
                        and first_block.get("type") == "text"
                        and first_block.get("text") == text
                    ):
                        return
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def reset(self) -> None:
        """Clear conversation history.

        Phase 1.6.13 — does NOT clear ``_stop_flag``. If a stop was
        requested before reset, that signal must propagate so the
        worker exits its loop. Caller (AgentRuntime.reset) is
        responsible for joining/retiring any in-flight worker BEFORE
        invoking this method, then clearing the stop flag manually
        once the old worker is gone.
        """
        self.messages.clear()

    def stop(self) -> None:
        """Cooperative cancellation. Checked between API calls."""
        self._stop_flag.set()

    def clear_stop(self) -> None:
        """Reset the stop flag. ONLY safe to call when the previous
        worker has been joined — otherwise the old worker can keep
        running into a fresh session.
        """
        self._stop_flag.clear()

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------

    def run_turn(self) -> str | None:
        """Run the tool-use loop until the model emits text + end_turn.

        Returns the final assistant text, or None if cancelled.
        Raises AgentError on protocol/network failure.
        """
        try:
            return self._loop()
        except BaseException as exc:  # noqa: BLE001 — fan out to callback
            self.on_error(exc)
            raise

    def _maybe_promote_tier(self, user_text: str) -> bool:
        """Mutate ``self.model_tier`` if the user expressed session-scope intent.

        Returns True if the tier was changed. Called by ``_loop`` BEFORE
        ``_resolve_model`` each turn. Fires on three conditions:

          * explicit pro override + session-scope cue → tier := 'pro'
          * explicit flash override + session-scope cue → tier := 'flash'
          * explicit "back to auto" reset phrase → tier := 'auto'

        ``_resolve_model`` itself remains pure and per-turn; the session-
        sticky promotion is the side-effect that lifts user intent across
        turns.
        """
        text = user_text or ""
        if not text:
            return False
        new_tier: str | None = None
        if _TIER_RESET_RE.search(text):
            new_tier = "auto"
        elif _SESSION_SCOPE_RE.search(text):
            # When session-scope intent is already established by phrases
            # like "stay on" / "always" / "this session", the per-turn
            # override regex is too strict (it requires a verb cue). Fall
            # back to a word-bounded mention of pro/flash. Both present
            # → ambiguous, skip rather than guess.
            has_pro = bool(_PRO_OVERRIDE_RE.search(text)) or bool(
                re.search(r"\bpro\b", text, re.IGNORECASE)
            )
            has_flash = bool(_FLASH_OVERRIDE_RE.search(text)) or bool(
                re.search(r"\bflash\b", text, re.IGNORECASE)
            )
            if has_pro and not has_flash:
                new_tier = "pro"
            elif has_flash and not has_pro:
                new_tier = "flash"
        if new_tier is None or new_tier == self.model_tier:
            return False
        self.model_tier = new_tier
        try:
            self.on_tier_change(new_tier)
        except Exception:
            pass
        return True

    def _resolve_model(self, user_text: str) -> str:
        """Pure function. Pick a model for this turn based on the most
        recent user message + the configured tier override.

        Resolution order (highest precedence first):
          1. **Explicit per-turn override in user_text** (2.1.3). Phrases
             like "use pro", "use the pro model", "switch to pro",
             "force flash", "deepseek-v4-pro", etc. flip the tier for
             this turn regardless of the COMP's Modeltier param. Pro
             takes precedence on ties (since the user complaint that
             motivated the feature was "I asked for pro and got
             flash"). The override is per-turn — the next turn falls
             back to the configured tier.
          2. **Pinned tier** ('flash' / 'pro') from the COMP param.
          3. **Auto heuristic** — score 1 point each for:
                * len(user_text) > 300 chars
                * pro-leaning verbs (build/create/fix/...)
                * fenced code block (```)
                * ≥2 tool-name keywords
             score >= 2 → pro, else flash

        With the 75% promo (through 2026-05-31), the cost gap is ~3×
        but the latency gap is the bigger UX win — flash's lower TTFT
        feels qualitatively snappier on lookup-style prompts. Cascade
        routing (try flash, escalate on low-confidence) is rejected
        for v1 — extra round trips eat the savings at our usage volume.
        """
        # 1. Explicit override wins.
        text = user_text or ""
        if _PRO_OVERRIDE_RE.search(text):
            return self.model
        if _FLASH_OVERRIDE_RE.search(text):
            return self.flash_model
        # 2. Configured tier pin.
        if self.model_tier == "flash":
            return self.flash_model
        if self.model_tier == "pro":
            return self.model
        # auto
        text = (user_text or "").lower()
        score = 0
        if len(text) > 300:
            score += 1
        # Imperative / build-leaning verbs — RouteLLM features (arXiv:2406.18665)
        pro_keywords = (
            "build",
            "create",
            "design",
            "fix",
            "refactor",
            "implement",
            "debug",
            "optimize",
            "rewrite",
            "rewire",
        )
        if any(kw in text for kw in pro_keywords):
            score += 1
        # Code fences signal a code-generation intent.
        if "```" in text:
            score += 1
        # Multi-tool prediction — the prompt mentions enough tool-aligned
        # actions that the agent will likely chain ≥3 tool calls. Pro
        # handles long chains better.
        tool_keywords = ("create node", "set param", "connect", "wire", "inspect", "screenshot", "patch")
        if sum(1 for kw in tool_keywords if kw in text) >= 2:
            score += 1
        return self.model if score >= 2 else self.flash_model

    def _loop(self) -> str | None:
        # Phase 4.3 — compact the conversation history if it has grown
        # past the threshold. Runs ONCE at turn start, BEFORE the
        # model's tier is resolved (the model decision works on the
        # last user message which is preserved in the recent slice).
        # The compactor is responsible for forensic persistence
        # before slicing, so a "lost detail" debug session can recover
        # the original messages from ~/.tdpilot-api/history/.
        if self.compactor is not None:
            try:
                self.messages = self.compactor.maybe_compact(self.messages)
            except Exception as exc:  # noqa: BLE001 — compaction must never break a turn
                print(f"[tdpilot_API/agent] compaction failed: {exc}")

        # Pick the model ONCE at turn start. Stays pinned for the entire
        # tool-use chain (mid-turn switching busts DeepSeek's auto-cache
        # AND risks flash failing to finish what pro started). Resolve
        # against the LAST user message — that's the new instruction.
        last_user_text = ""
        for msg in reversed(self.messages):
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            last_user_text = block.get("text", "")
                            break
                elif isinstance(content, str):
                    last_user_text = content
                if last_user_text:
                    break
        # 2.3.1 — session-sticky tier promotion. Mutates self.model_tier
        # before _resolve_model runs so the per-turn pick honours any
        # session-scope intent ("this session", "from now on", ...).
        self._maybe_promote_tier(last_user_text)
        chosen = self._resolve_model(last_user_text)
        if chosen != self._active_model:
            try:
                self.on_model_change(self.model_tier, chosen)
            except Exception:
                pass
        self._active_model = chosen

        # Phase 1.2 (v2.2.0) — per-turn cycle-detection ledger. Built
        # ONCE at the top of _loop (before the first API call), reused
        # across every batch of tool dispatches in this turn. Factory
        # returns None → feature disabled for this turn; the ``if
        # cycle_ledger is not None`` check inside the inner for-loop
        # makes the whole path a no-op in that case.
        #
        # ``CycleDetected`` + ``_format_cycle_args`` are late-imported
        # from tdpilot_api_cycle_detector here because that module
        # imports ``AgentError`` from THIS module — top-level import
        # would create a circular dependency. The cost of the deferred
        # lookup is one dict access per turn (Python caches the
        # resolved module after the first call).
        cycle_ledger = None
        CycleDetected = None  # noqa: N806 — late-imported class, treated as constant in this scope
        _format_cycle_args = None  # noqa: N806 — late-imported helper
        if self.cycle_ledger_factory is not None:
            try:
                from tdpilot_api_cycle_detector import (  # noqa: PLC0415
                    CycleDetected as _CycleDetected,
                )
                from tdpilot_api_cycle_detector import (
                    _format_args_summary as _fas,
                )

                cycle_ledger = self.cycle_ledger_factory()
                CycleDetected = _CycleDetected  # noqa: N806
                _format_cycle_args = _fas  # noqa: N806
            except Exception as exc:  # noqa: BLE001 — factory must never break a turn
                print(f"[tdpilot_API/agent] cycle_ledger setup failed: {exc}")
                cycle_ledger = None
                CycleDetected = None  # noqa: N806

        for _turn in range(self.turn_budget):
            if self._stop_flag.is_set():
                return None

            response = self._call_api()
            content = response.get("content", []) or []
            stop_reason = response.get("stop_reason")
            # Phase 2 (1.8.0) — surface per-call token usage to the chat
            # status bar. Best-effort: a missing/exotic shape is dropped
            # rather than raised so the agent loop is unaffected.
            usage = response.get("usage")
            if isinstance(usage, dict) and usage:
                try:
                    self.on_usage(usage)
                except Exception:  # noqa: BLE001
                    pass

            # Append assistant turn to history. ``_strip_reasoning`` only
            # removes ``reasoning_content`` SUB-KEYS — it KEEPS
            # ``thinking`` content blocks, which DeepSeek's compat layer
            # requires to be echoed back in the next turn (a 400 fires
            # otherwise). See the long comment on _strip_reasoning for
            # the field-by-field rules.
            self.messages.append({"role": "assistant", "content": _strip_reasoning(content)})

            text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
            text_blob = "".join(text_parts)
            if text_blob:
                self.on_text(text_blob)

            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if not tool_uses:
                # No tools requested → conversation turn is complete.
                self.on_turn_done(text_blob)
                return text_blob

            # Execute tools, collect results, send back as a user turn.
            # Phase 1.1 (v2.2.0) — auto-rollback wrap: capture baseline
            # errors + open a TD undo block before the batch; after the
            # batch, recheck errors and either close the block (clean
            # path) or roll it back (regression path). The factory may
            # return None or a no-op guard if disabled via env var; the
            # ``with`` block is always safe to enter.
            tool_names_in_batch = [tu.get("name", "") for tu in tool_uses]
            rollback_guard = None
            if self.rollback_guard_factory is not None:
                try:
                    rollback_guard = self.rollback_guard_factory(
                        self.dispatcher,
                        tool_names_in_batch,
                    )
                except Exception as exc:  # noqa: BLE001 — factory must never break a turn
                    print(f"[tdpilot_API/agent] rollback_guard_factory raised: {exc}")
                    rollback_guard = None

            results_block: list[dict] = []
            try:
                if rollback_guard is not None:
                    rollback_guard.__enter__()
                for tu in tool_uses:
                    tool_id = tu.get("id", "")
                    tool_name = tu.get("name", "")
                    tool_args = tu.get("input", {}) or {}
                    # Phase 1.2 — cycle detection. Check BEFORE
                    # ``on_tool_call`` fires so a blocked call doesn't
                    # leak an EV_TOOL_CALL event that gets superseded
                    # by the EV_ERROR a tick later. The raise
                    # propagates through the outer try/finally (so the
                    # rollback guard's __exit__ still runs), out of
                    # _loop, into run_turn's BaseException catch, into
                    # on_error → EV_ERROR + EV_STATE:idle.
                    if cycle_ledger is not None:
                        count = cycle_ledger.record(tool_name, tool_args)
                        if count >= cycle_ledger.threshold:
                            raise CycleDetected(
                                tool_name=tool_name,
                                count=count,
                                args_summary=_format_cycle_args(tool_args),
                            )
                    self.on_tool_call(tool_name, tool_args)
                    try:
                        result = self.dispatcher(tool_name, tool_args)
                        # F-12: the explicit `_tool_error` sentinel is the
                        # only failure signal post-v2.0. Internal handlers
                        # that emit `{"error": "..."}` get auto-stamped
                        # with `_tool_error: True` by `recovery.attach_hint()`
                        # inside the dispatcher pipeline.
                        is_error = is_tool_error_result(result)
                    except Exception as exc:  # noqa: BLE001
                        result = {
                            "_tool_error": True,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        is_error = True
                    self.on_tool_result(tool_name, result, is_error)
                    results_block.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": _stringify(result),
                            "is_error": is_error,
                        }
                    )
            finally:
                if rollback_guard is not None:
                    # __exit__ runs the post-batch check + decides rollback;
                    # we always want this to fire even if the batch raised.
                    try:
                        rollback_guard.__exit__(None, None, None)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[tdpilot_API/agent] rollback_guard.__exit__ raised: {exc}")

            # Phase 1.1 — append any rollback hint emitted by the guard
            # onto the last tool_result + surface it through on_text.
            # Logic extracted into ``_apply_rollback_hint`` for direct
            # unit-testing (Codex P2 followup on PR #34).
            self._apply_rollback_hint(rollback_guard, results_block)

            self.messages.append({"role": "user", "content": results_block})

            if stop_reason == "end_turn":
                # Defensive — model said end_turn but also issued tool calls.
                # Run the next turn to surface its follow-up text, but don't loop forever.
                continue

        raise TurnBudgetExceeded(f"Tool-use loop exceeded turn_budget={self.turn_budget}")

    # ------------------------------------------------------------------
    # Phase 1.1 — auto-rollback hint plumbing
    # ------------------------------------------------------------------

    def _apply_rollback_hint(self, rollback_guard: Any, results_block: list[dict]) -> None:
        """Append a rollback hint to the last tool_result + surface via
        ``on_text``. No-op if ``rollback_guard`` is None, has no
        ``hint_text``, or ``results_block`` is empty.

        Codex P2 review on PR #34 (2026-05-11) flagged that the prior
        in-line condition keyed on ``rollback_fired`` — which is False
        in the degraded path where the guard detected a regression but
        couldn't actually open / undo the block. The bug dropped the
        only signal the LLM would receive about that failure mode,
        leaving it to continue from a broken graph state with no
        feedback. Keying on ``hint_text`` (which the guard populates in
        BOTH the success and the degraded paths) surfaces both.

        Insertion strategy: the hint text-block gets appended to the
        LAST tool_result's content (which may be a string or a list of
        blocks). This preserves Anthropic's alternating user/assistant
        constraint AND pairs the hint with the failing batch's
        results — exactly where the LLM is most likely to attend on
        its next API call. Also surfaced to the chat UI via the
        ``on_text`` callback so the user sees a yellow inline notice
        in the assistant bubble.
        """
        if rollback_guard is None:
            return
        hint = getattr(rollback_guard, "hint_text", "")
        if not hint or not results_block:
            return
        last = results_block[-1]
        existing = last.get("content")
        if isinstance(existing, str):
            last["content"] = existing + "\n\n" + hint
        elif isinstance(existing, list):
            last["content"] = list(existing) + [{"type": "text", "text": hint}]
        else:
            last["content"] = hint
        try:
            self.on_text(hint)
        except Exception:  # noqa: BLE001 — chat-side callback must never break the agent loop
            pass

    # ------------------------------------------------------------------
    # Dynamic context (Phase 0.1)
    # ------------------------------------------------------------------

    def _materialise_dynamic_context(self) -> list[dict]:
        """Invoke ``dynamic_context_provider`` and validate its output.

        Returns a list of message dicts to prepend to the API request.
        Failures (provider raises, returns junk) degrade to an empty
        list and log; the agent never crashes on a bad provider.
        """
        provider = self.dynamic_context_provider
        if provider is None:
            return []
        try:
            raw = provider() or []
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/agent] dynamic_context_provider raised: {exc}")
            return []
        if not isinstance(raw, list):
            print(
                "[tdpilot_API/agent] dynamic_context_provider returned non-list "
                f"({type(raw).__name__}); ignoring"
            )
            return []
        out: list[dict] = []
        for msg in raw:
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in ("user", "assistant"):
                continue
            if "content" not in msg:
                continue
            out.append(msg)
        return out

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _call_api(self) -> dict:
        # _active_model was pinned at turn start by _loop(). Using it
        # (instead of self.model) is what makes Sprint 4.3 routing
        # actually take effect — flash for simple lookups, pro for
        # complex builds.
        #
        # Phase 0.1 — prepend dynamic context (per-turn retrievals /
        # session indexes) WITHOUT mutating self.messages. The system
        # prompt stays byte-stable so DeepSeek's auto-cache hits on it.
        # The dynamic context busts cache only on the per-turn portion
        # (small compared to system prompt + tools schema).
        dynamic = self._materialise_dynamic_context()
        body: dict[str, Any] = {
            "model": self._active_model or self.model,
            "max_tokens": self.max_tokens,
            "messages": [*dynamic, *self.messages],
            "temperature": self.temperature,
        }
        if self.system_prompt:
            body["system"] = self.system_prompt
        if self.tools:
            body["tools"] = self.tools

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/v1/messages",
            method="POST",
            headers={
                "Content-Type": "application/json",
                # Anthropic SDK convention. DeepSeek's compat layer accepts this;
                # if a future variant requires Authorization: Bearer, we'll add a
                # 401-fallback here.
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            data=data,
        )
        try:
            ctx = _build_ssl_context()
            with urllib.request.urlopen(req, timeout=self.request_timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = ""
            raise AgentError(f"HTTP {exc.code} from /v1/messages: {detail}") from exc
        except urllib.error.URLError as exc:
            raise AgentError(f"Network error to {self.base_url}: {exc.reason}") from exc
        except (TimeoutError, OSError) as exc:
            raise AgentError(f"I/O error talking to {self.base_url}: {exc}") from exc


def _stringify(result: Any) -> str:
    """Tool-result content must be a string (or list of content blocks).
    JSON-encode dicts/lists; coerce primitives to str.
    """
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _strip_reasoning(blocks: list) -> list:
    """Strip reasoning-only SUB-FIELDS from content blocks.

    Two reasoning-related artefacts can show up in DeepSeek responses,
    and they require OPPOSITE handling on the way back:

      * ``thinking`` and ``redacted_thinking`` content blocks (Anthropic-
        format) — DeepSeek REQUIRES these to be passed back in the next
        turn. The error you'll see if you strip them is:
            HTTP 400: ``The content[].thinking in the thinking mode must
            be passed back to the API.``
        We KEEP these blocks as-is.

      * ``reasoning_content`` top-level KEYS (OpenAI-format) — these
        get returned alongside content but the API REJECTS them on
        echo. We strip them as a defensive measure if they ever slip
        through onto a block; the typical place they live is at the
        message-root level (we don't construct messages with that
        field, so this is belt-and-suspenders).

    Net effect: blocks pass through unchanged with the rare exception
    of having a ``reasoning_content`` sub-key removed. Thinking blocks
    are preserved — that's what the API requires.
    """
    out: list[Any] = []
    for block in blocks:
        if not isinstance(block, dict):
            out.append(block)
            continue
        # KEEP all block types including thinking/redacted_thinking —
        # DeepSeek's compat layer requires them in the next turn.
        if "reasoning_content" in block:
            block = {k: v for k, v in block.items() if k != "reasoning_content"}
        out.append(block)
    return out
