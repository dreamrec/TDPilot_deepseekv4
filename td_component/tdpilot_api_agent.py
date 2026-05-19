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
import random
import ssl
import threading
import time
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


# ---------------------------------------------------------------------------
# v2.4 / Phase A.5 — retry-with-backoff for DeepSeek 429 / 5xx.
# Pre-2.4 any 429 raised AgentError and killed the turn; users had to manually
# resend. The retry path handles transient rate-limits + upstream-service
# degradation without surfacing as a user-visible failure on the first hit.
# Cumulative wait is bounded by max_retries × (_MAX_BACKOFF_SECONDS + per-call
# request_timeout) so a misbehaving Retry-After: 9999 can't wedge us forever.
# ---------------------------------------------------------------------------

_MAX_BACKOFF_SECONDS = 60.0
# Codes that should NEVER be retried — they reflect client-side configuration
# bugs (bad key, missing endpoint, malformed body) that retry won't fix.
_NON_RETRYABLE_HTTP_CODES = frozenset({400, 401, 403, 404})


# ---------------------------------------------------------------------------
# v2.4 / Phase B.1 — screenshot vision pipeline (feature-flagged).
# DeepSeek's Anthropic-compat /v1/messages endpoint cannot surface base64
# inside ``tool_result`` blocks — the model literally cannot see screenshots
# taken via td_screenshot today. The vision pipeline:
#   1. Strips ``data_base64`` from the tool_result that lands in self.messages
#      (saves cached-prefix tokens + keeps the JSON small).
#   2. Injects a proper ``image`` content block in the SAME user turn (image
#      blocks must live in user content per Anthropic spec; interleaving
#      tool_result + image keeps role alternation clean).
#   3. Still broadcasts the full base64 to the chat UI via the WS path —
#      that surface lives in the runtime layer, untouched.
#
# Ships behind ``enable_vision_pipeline`` (default False) until a live
# DeepSeek call confirms its compat layer accepts ``image`` blocks. If
# DeepSeek returns 400 on the first call with an image block, set the
# flag back to False and revert to the legacy embedded-base64 behavior.
# ---------------------------------------------------------------------------


def _split_screenshot_payload(tool_name: str, result: Any) -> tuple[Any, str | None, str]:
    """Return ``(slim_result, b64_or_None, media_type)``.

    For non-screenshot tools or non-dict results, returns the input unchanged
    + ``None`` base64. For screenshot dicts carrying ``data_base64``, returns
    a copy with that field replaced by a small ``image_omitted_for_compat``
    marker, plus the raw base64 string the caller can pack into an image
    content block.

    ``media_type`` is a best-effort guess from the result's ``format`` field
    (defaults to ``"image/jpeg"`` which matches handle_screenshot's current
    output).
    """
    if tool_name != "td_screenshot" or not isinstance(result, dict):
        return result, None, ""
    b64 = result.get("data_base64")
    if not isinstance(b64, str) or not b64:
        return result, None, ""
    fmt = str(result.get("format") or "jpeg").lower().lstrip(".")
    media_type = "image/png" if fmt == "png" else "image/jpeg"
    slim = {k: v for k, v in result.items() if k != "data_base64"}
    slim["image_omitted_for_compat"] = True
    slim["content_type"] = media_type
    return slim, b64, media_type


def _parse_retry_after(headers: Any, default: float) -> float:
    """Parse a Retry-After header into seconds, falling back to ``default``.

    Accepts the integer-seconds form ("60", "0.5"). The HTTP-date form is
    rare in practice for rate-limit headers; we fall back to the computed
    backoff if the value isn't a plain number. Returns at least 0.1s so
    callers can use the result as an Event.wait timeout without raising
    on negative values.
    """
    if headers is None:
        return default
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after") or ""
    except Exception:
        return default
    if not raw:
        return default
    try:
        return max(0.1, float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


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

# v2.4 / B-008-T (live-debug 2026-05-13) — TASK-DONE signals. Once
# task-sticky pro is active (entered by a heuristic-induced pro routing
# OR by a CycleDetected escalation), the agent stays on pro until the
# user sends one of these signals — at which point we drop back to the
# normal auto heuristic. Cost-OK rationale: the user explicitly said
# costs are small + transparent (visible in the cost pill), so staying
# on pro across a multi-turn build is the right UX trade-off vs the
# context-thrash of mid-task tier flips.
#
# Conservative set — only fires on CLEAR completion signals, not on
# casual mid-task acks ("ok"/"yes"/"sure"). Negative signals ("still
# broken", "not working") deliberately do NOT match — the agent must
# stay on pro while the user is still asking for help.
_TASK_DONE_RE = re.compile(
    r"\b(?:thanks?(?:\s+you)?|thank\s+you|"
    r"perfect|awesome|excellent|nailed\s+it|"
    r"ship\s+it|"
    r"(?:that|it)\s+works(?:\s+now)?|works\s+now|"
    r"all\s+done|we'?re\s+done|that'?s\s+(?:it|done|all)|"
    r"looks?\s+(?:good|great|perfect)|"
    r"done|finished|completed?)\b"
    # Hard guard: don't fire if the message is asking for MORE work in
    # the same sentence ("thanks, now also fix Y" — keep on pro).
    r"(?!.*\b(?:now|but|also|additionally|next|then|and)\b.+\b"
    r"(?:fix|build|create|add|do|change|update|tweak|adjust)\b)",
    re.IGNORECASE | re.DOTALL,
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
        # v2.5.1 — activity-ring factory. Zero-arg callable returning a
        # fresh ``ActivityRing`` (per-turn) or None to disable. When
        # present, ``_loop`` appends an ActivityRecord per tool dispatch
        # and may inject a ``_read_journal`` hint into tool_results when
        # the same ``(tool_name, args_hash)`` has fired twice this turn
        # (count=2 is the last call before cycle-detect at threshold=3).
        activity_ring_factory: Callable[[], Any] | None = None,
        # v2.5.3 — tool-approval provider. Callable with signature
        # ``(tool_name, tool_args) -> (decision, reason)`` where
        # decision is one of ``approve / deny / timeout / not_required``.
        # When ``not_required`` the dispatcher runs normally; otherwise
        # the dispatcher is SKIPPED and a denial result is injected.
        # ``None`` disables the gate entirely (same as Approvalmode=off).
        # Wired by AgentRuntime via _build_approval_provider.
        approval_provider: Callable[[str, dict], tuple[str, str]] | None = None,
        # v2.4 / Phase A.5 — retry policy for DeepSeek 429 / 5xx. ``max_retries``
        # is the number of retry attempts AFTER the first failure (so
        # ``max_retries=3`` allows up to 4 total HTTP calls). ``initial_backoff``
        # is the base for exponential backoff in seconds: attempt N waits
        # ``initial_backoff * 2**N + jitter`` (capped at _MAX_BACKOFF_SECONDS,
        # or replaced by the server's Retry-After header when present).
        max_retries: int = 3,
        initial_backoff: float = 2.0,
        # v2.4 / Phase A.5 — soft-nudge UI surface. ``on_hint(kind, message)``
        # fires on each retry attempt AND on retry exhaustion before the
        # final raise. AgentRuntime wires this to EV_HINT so the chat UI
        # can render "retrying in 5s…" mid-turn rather than going silent.
        # ``kind`` is a short stable identifier ("api_retry",
        # "api_retry_exhausted"); ``message`` is a human-readable string.
        on_hint: Callable[[str, str], None] = _noop,
        # v2.4 / B-009 (live-debug 2026-05-13) — heartbeat during long API
        # calls. Pro extended-thinking turns can block on urlopen for
        # 60-180s with no events emitted; the frontend's prior 90s activity
        # watchdog tripped 'idle (timeout)' falsely while the agent was
        # still thinking. Fires every ``heartbeat_interval`` seconds while
        # urlopen is pending; the runtime wires this to push EV_STATE
        # ("thinking") onto the event queue, which broadcasts via WS and
        # re-arms the JS watchdog. No-op by default so unit tests don't
        # need TD bindings to instantiate Agent.
        on_heartbeat: Callable[[], None] = _noop,
        # Interval between heartbeats during a single API call. 30s is
        # well inside the bumped JS watchdog window (240s).
        heartbeat_interval: float = 30.0,
        # v2.4 / Phase B.1 — screenshot vision pipeline. When True, the
        # _loop strips ``data_base64`` from td_screenshot tool_results and
        # injects a sibling ``image`` content block in the same user turn
        # so the model can actually see the screenshot. Default False
        # until a live DeepSeek call confirms its compat layer accepts
        # image blocks in user content. The WS broadcast path (chat UI)
        # is untouched — full base64 still reaches the browser.
        enable_vision_pipeline: bool = False,
        # v2.4 / Phase C.9 — extended-thinking budget. When > 0, every
        # /v1/messages request body includes ``"thinking": {"type":
        # "enabled", "budget_tokens": N}``. DeepSeek's compat-layer
        # support for this field is undocumented as of 2026-05 — if
        # the server 400s, set this to 0 to disable. 0 = disabled
        # (legacy pre-v2.4 behaviour, byte-stable cache prefix).
        thinking_budget: int = 0,
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
        # v2.4 / B-008-C (live-debug 2026-05-13) — reactive escalation flag.
        # Set to True by ``run_turn`` when the previous turn died on
        # ``CycleDetected``. Consumed (and reset) by ``_resolve_model`` on
        # the NEXT auto-tier turn to force pro. With B-008-T integrated,
        # this also enters task-sticky pro so subsequent turns of the
        # recovery effort stay on pro until the user signals done.
        self._cycle_escalate_next_turn: bool = False
        # v2.4 / B-008-T (live-debug 2026-05-13, follow-up to A+C) —
        # TASK-STICKY pro. When the auto heuristic OR cycle-escalation
        # picks pro, latch this flag so the rest of the multi-turn task
        # stays on pro (preserves working-memory + DeepSeek auto-cache
        # across tool-chain turns). Cleared by ``_maybe_clear_task_sticky``
        # when the user's message matches ``_TASK_DONE_RE`` (thanks /
        # perfect / done / that works / ...). Explicit user pins
        # (model_tier=flash/pro) and per-turn overrides take precedence
        # — this only governs the auto-tier path.
        self._task_sticky_pro: bool = False
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
        # v2.5.1 — activity ring factory (parallels cycle_ledger_factory).
        self.activity_ring_factory = activity_ring_factory
        # v2.5.3 — approval provider (None disables the gate).
        self.approval_provider = approval_provider

        # v2.4 / Phase A.5 — retry config + hint callback. Clamp to safe
        # values so a bad COMP-param value can't disable retries entirely
        # (negative) or set a sub-millisecond backoff (zero / NaN).
        self.max_retries = max(0, int(max_retries))
        self.initial_backoff = max(0.1, float(initial_backoff))
        self.on_hint = on_hint
        # v2.4 / B-009 (live-debug 2026-05-13) — heartbeat surface.
        self.on_heartbeat = on_heartbeat
        # Clamp to ≥ 1s to prevent pathological per-call thread storms
        # from a misconfigured COMP param.
        self.heartbeat_interval = max(1.0, float(heartbeat_interval))
        # v2.4 / Phase B.1 — screenshot vision flag. Off by default.
        self.enable_vision_pipeline = bool(enable_vision_pipeline)
        # v2.4 / Phase C.9 — thinking budget. Clamped to ≥ 0 so a bad
        # COMP-param value can't produce a negative budget.
        self.thinking_budget = max(0, int(thinking_budget))

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
            # v2.4 / B-008-C (live-debug 2026-05-13) — name-match on
            # CycleDetected (avoids the late-import / module-reload class-
            # identity issue that motivated B-005). Setting the flag here
            # ensures the very next ``_resolve_model`` call promotes auto
            # → pro for one turn. Same name-match defense that B-005's
            # ``_run_safe`` fallback uses in the runtime — keep the two
            # branches in lock-step.
            if type(exc).__name__ == "CycleDetected":
                self._cycle_escalate_next_turn = True
            self.on_error(exc)
            raise

    def _maybe_clear_task_sticky(self, user_text: str) -> bool:
        """Clear ``_task_sticky_pro`` if the user's message signals task done.

        Called by ``_loop`` BEFORE ``_resolve_model`` each turn so that the
        very turn containing the "thanks" / "done" / "that works" signal
        drops back to the auto heuristic — the user's follow-up question
        (often a lookup) gets a cheap flash response.

        Returns True if the flag was cleared, False otherwise. Idempotent
        if the flag was already False (the regex is still evaluated, which
        is fine — sub-millisecond on short messages).

        Guard against "thanks, now also fix Y"-style mixed messages built
        into _TASK_DONE_RE's negative lookahead — those keep the flag.
        """
        if not self._task_sticky_pro:
            return False
        text = user_text or ""
        if not text:
            return False
        if _TASK_DONE_RE.search(text):
            self._task_sticky_pro = False
            return True
        return False

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
            has_pro = bool(_PRO_OVERRIDE_RE.search(text)) or bool(re.search(r"\bpro\b", text, re.IGNORECASE))
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
        # 3. v2.4 / B-008-C (live-debug 2026-05-13) — REACTIVE ESCALATION.
        # If the previous turn died on CycleDetected (caught in run_turn's
        # except), force pro AND latch task-sticky so the multi-turn
        # recovery stays on pro until the user signals done. Pre-B-008-T
        # this was a one-shot escalation; that lost context if the
        # recovery itself took >1 turn (which it usually does).
        if self._cycle_escalate_next_turn:
            self._cycle_escalate_next_turn = False
            self._task_sticky_pro = True
            return self.model
        # 3.5. v2.4 / B-008-T — TASK-STICKY pro. Honored AFTER cycle-
        # escalate (which would set the same flag anyway) and BEFORE the
        # heuristic recomputes. The flag is cleared by
        # _maybe_clear_task_sticky on a task-done message, which runs
        # in _loop BEFORE this method.
        if self._task_sticky_pro:
            return self.model
        # 4. Auto heuristic (improved 2026-05-13).
        text = (user_text or "").lower()
        score = 0
        if len(text) > 300:
            score += 1
        # Imperative / build-leaning verbs — RouteLLM features (arXiv:2406.18665).
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
            "audit",
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
        # v2.4 / B-008-A (live-debug 2026-05-13) — STRUCTURAL-COMPLEXITY
        # NOUNS. Short imperative prompts like "Build a kaleidoscope
        # feedback loop" (3-noun phrase ending in "loop") routed to flash
        # under the pre-2026-05-13 heuristic — verb scored +1 but nothing
        # else hit, so score=1 < threshold=2 → flash. These nouns signal
        # system-shaped work (multi-node wiring, recursion, downstream
        # consumers) which flash struggles with. Combined with the verb
        # signal, "Build a X feedback loop"-style prompts now score 2 →
        # pro, while single-target mutations like "fix the parameter on
        # this op" stay on flash (verb alone scores 1).
        #
        # Design note: deliberately NOT adding a separate imperative-
        # starter bonus on top of the verb-anywhere check — that would
        # double-count the verb on "fix the parameter ..."-style short
        # mutation prompts and erode the cost-tier intent. The verb +
        # structural-noun pair is enough signal for the genuine multi-
        # step build tasks without overshooting.
        structural_nouns = (
            "feedback",
            "loop",
            "chain",
            "network",
            "system",
            "pipeline",
            "graph",
            "tree",
            "rig",
            "shader",
            "renderer",
            "compositor",
            "kaleidoscope",
            "fractal",
            "particle",
        )
        if any(kw in text for kw in structural_nouns):
            score += 1
        picked = self.model if score >= 2 else self.flash_model
        # v2.4 / B-008-T — heuristic-induced pro routing latches task-
        # sticky. The next turn (even a short clarification like "what's
        # the framerate?") stays on pro, preserving working-memory and
        # the DeepSeek auto-cache across the multi-turn build.
        if picked == self.model:
            self._task_sticky_pro = True
        return picked

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
        # v2.4 / B-008-T (live-debug 2026-05-13) — clear task-sticky pro
        # if the user just signalled task completion ("thanks", "done",
        # "that works", ...). Runs BEFORE _resolve_model so the very
        # turn carrying the signal drops to flash for any follow-up
        # lookup, without an extra round-trip on pro.
        self._maybe_clear_task_sticky(last_user_text)
        chosen = self._resolve_model(last_user_text)
        # B-004 (live-debug 2026-05-13) — fire on_model_change on EVERY
        # turn, not only when the tier flips. Pre-fix the badge in the
        # chat UI stayed empty after a tab reload IF the auto-router
        # kept picking the same tier as the last turn (e.g. sticky pro,
        # or a long-running build where every turn routes to pro). The
        # badge depends on EV_MODEL ever firing, so a quiet "same tier
        # again" turn left the user staring at just "thinking" with no
        # idea which model was running. Now we fire every turn; the
        # extension drain de-dupes idempotently (textContent assignment
        # to the same value is a no-op).
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

        # v2.5.1 — activity ring (parallel to cycle_ledger). Per-turn
        # instance; tracks every tool dispatch this turn. Used to emit
        # ``_read_journal`` hints in tool_results at count=2 (one call
        # before cycle-detect at threshold=3 ends the turn).
        activity_ring = None
        _journal_threshold = cycle_ledger.threshold if cycle_ledger is not None else 3
        _build_journal_hint = None  # late-imported below
        if self.activity_ring_factory is not None:
            try:
                from tdpilot_api_activity_log import (  # noqa: PLC0415
                    build_journal_hint as _bjh,
                )

                activity_ring = self.activity_ring_factory()
                _build_journal_hint = _bjh
                if activity_ring is not None and hasattr(activity_ring, "start_turn"):
                    activity_ring.start_turn()
            except Exception as exc:  # noqa: BLE001 — factory must never break a turn
                print(f"[tdpilot_API/agent] activity_ring setup failed: {exc}")
                activity_ring = None
                _build_journal_hint = None

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
            # v2.4 / Phase B.1 — vision-pipeline image-block buffer. Each
            # entry is ``(tool_result_index, image_block)`` so we can
            # interleave after the for-loop closes. Skipped entirely
            # when the feature flag is off — preserves byte-for-byte
            # legacy behavior pre-v2.4.
            pending_image_blocks: list[tuple[int, dict]] = []
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
                            # v2.5.2 — synthesize tool_result blocks for the
                            # offending tool_use AND any remaining un-dispatched
                            # tool_uses in this batch BEFORE raising, so the
                            # persisted conversation stays API-valid. Without
                            # this, orphan tool_use ids cause /v1/messages to
                            # return HTTP 400 on the NEXT /send and the chat
                            # becomes stuck until TD restart. See live audit
                            # 2026-05-19 / BUG_REPORT_cycle_detect_orphan_tool_use.
                            err_content = json.dumps(
                                {
                                    "error": (
                                        f"cycle_detected: '{tool_name}' called "
                                        f"{count}x with identical args this "
                                        "turn — turn ended by runtime"
                                    ),
                                    "_tool_error": True,
                                }
                            )
                            tu_idx = tool_uses.index(tu)
                            for _unproc in tool_uses[tu_idx:]:
                                results_block.append(
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": _unproc.get("id", ""),
                                        "content": err_content,
                                        "is_error": True,
                                    }
                                )
                            # Append to messages BEFORE raising so the
                            # synthetic results survive the unwind through
                            # the surrounding try/finally (which runs
                            # rollback_guard.__exit__) and the message
                            # store stays consistent for the next /send.
                            self.messages.append({"role": "user", "content": results_block})
                            raise CycleDetected(
                                tool_name=tool_name,
                                count=count,
                                args_summary=_format_cycle_args(tool_args),
                            )
                    self.on_tool_call(tool_name, tool_args)
                    # v2.5.1 — wall-clock the dispatch so the activity
                    # record carries duration. ``time`` is module-level
                    # imported in tdpilot_api_agent already.
                    _activity_t0 = time.monotonic()
                    _activity_err: str | None = None
                    # v2.5.3 — tool approval gate. Runs BEFORE dispatch.
                    # If approval is required and the user denies (or
                    # times out), we synthesize a denial tool_result
                    # and SKIP the dispatcher entirely. The denial
                    # looks like a normal _tool_error so the existing
                    # recovery + activity-ring + journal-hint paths
                    # handle it without special-casing.
                    _approval_decision: str | None = None
                    if self.approval_provider is not None:
                        try:
                            from tdpilot_api_approval import (  # noqa: PLC0415
                                DECISION_NOT_REQUIRED,
                                build_denied_result,
                            )

                            decision, reason = self.approval_provider(tool_name, tool_args)
                            if decision != DECISION_NOT_REQUIRED:
                                _approval_decision = decision
                                if decision != "approve":
                                    result = build_denied_result(tool_name, decision, reason)
                                    is_error = True
                                    _activity_err = f"approval={decision}"
                        except Exception as exc:  # noqa: BLE001
                            # Approval mechanism must never break dispatch.
                            print(f"[tdpilot_API/agent] approval_provider raised: {exc}")
                            _approval_decision = None

                    if _approval_decision is not None and _approval_decision != "approve":
                        # Skipped the dispatcher; result already set above.
                        pass
                    else:
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
                            _activity_err = f"{type(exc).__name__}: {exc}"
                    # v2.5.1 — activity ring append + journal-hint inject.
                    # Defensive: every operation here is wrapped so a
                    # broken hint path can never break tool dispatch.
                    if activity_ring is not None:
                        try:
                            _activity_record = activity_ring.record(
                                tool_name,
                                tool_args,
                                int((time.monotonic() - _activity_t0) * 1000),
                                "error" if is_error else "ok",
                                error_msg=_activity_err,
                            )
                            if _build_journal_hint is not None and isinstance(result, dict):
                                hint = _build_journal_hint(
                                    tool_name,
                                    _activity_record.args_hash,
                                    activity_ring,
                                    cycle_threshold=_journal_threshold,
                                )
                                if hint is not None:
                                    # Inject a _read_journal block on the
                                    # tool_result the LLM will see. Don't
                                    # mutate the dispatcher's result in
                                    # place if it's shared — but in
                                    # practice the dispatcher returns
                                    # fresh dicts per call.
                                    result["_read_journal"] = hint
                        except Exception as exc:  # noqa: BLE001
                            print(f"[tdpilot_API/agent] activity_ring/journal hint failed: {exc}")
                    # v2.4 / Phase B.1 — split screenshot payload so the
                    # tool_result that lands in self.messages doesn't carry
                    # base64 (which DeepSeek's compat layer can't decode
                    # and which costs cached-prefix tokens on every turn).
                    # The base64 itself becomes an ``image`` content block
                    # appended right after this tool_result, in the same
                    # user turn.
                    history_result = result
                    image_b64: str | None = None
                    image_mt = ""
                    if self.enable_vision_pipeline:
                        history_result, image_b64, image_mt = _split_screenshot_payload(tool_name, result)
                    self.on_tool_result(tool_name, result, is_error)
                    results_block.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": _stringify(history_result),
                            "is_error": is_error,
                        }
                    )
                    if image_b64:
                        pending_image_blocks.append(
                            (
                                len(results_block) - 1,
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": image_mt or "image/jpeg",
                                        "data": image_b64,
                                    },
                                },
                            )
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

            # v2.4 / Phase B.1 — interleave pending image blocks after
            # their matching tool_results so the user turn looks like
            # [tool_result, image, tool_result, image, ...]. Walk in
            # reverse so the index references stay valid as we insert.
            if pending_image_blocks:
                for tr_idx, image_block in reversed(pending_image_blocks):
                    # Insert AFTER the tool_result at tr_idx.
                    results_block.insert(tr_idx + 1, image_block)

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
        # v2.4 / Phase C.9 — extended-thinking budget. Adding the
        # ``thinking`` block on every request keeps the cache prefix
        # byte-stable as long as the budget value is fixed for the
        # session (it's loaded once at AgentRuntime construction
        # from the Thinkingbudget COMP param). DeepSeek's Anthropic-
        # compat layer support is undocumented as of 2026-05; a 400
        # here means the field isn't accepted — set Thinkingbudget=0
        # to disable.
        if self.thinking_budget > 0:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }

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
        # v2.4 / Phase A.5 — retry-with-backoff for transient HTTP failures.
        # 429 (rate limit) + any 5xx → exponential backoff with jitter, or
        # Retry-After header when the server provides one. Client-side errors
        # (400/401/403/404) raise immediately — they reflect bad config, not
        # transient load. Network/I/O errors raise on the first failure (the
        # plan's scope is HTTP retry only; network-error retry can land in a
        # follow-up if it shows up in usage data).
        ctx = _build_ssl_context()
        attempt = 0
        while True:
            if self._stop_flag.is_set():
                raise AgentError("Interrupted before API call")
            try:
                # v2.4 / B-009 (live-debug 2026-05-13) — heartbeat thread.
                # urlopen blocks the worker thread for the full duration of
                # the DeepSeek call (no streaming on this code path). With
                # pro extended thinking that can be 60-180s. The frontend's
                # activity watchdog only re-arms on incoming WS events, so
                # the long silence used to trip a false 'idle (timeout)'.
                # This thread pulses on_heartbeat → runtime pushes
                # EV_STATE("thinking") → broadcast WS → frontend re-arms.
                # Daemon + Event-stop pair so the heartbeat dies on the
                # finally branch regardless of how urlopen exits (success,
                # HTTPError, URLError, timeout).
                _hb_stop = threading.Event()

                def _heartbeat_loop(stop_event: threading.Event = _hb_stop) -> None:
                    # Bind _hb_stop into the default-arg to avoid late-binding
                    # over the retry-loop iteration variable (ruff B023).
                    while not stop_event.wait(self.heartbeat_interval):
                        try:
                            self.on_heartbeat()
                        except Exception:  # noqa: BLE001 — pulse must never crash worker
                            pass

                _hb_thread = threading.Thread(
                    target=_heartbeat_loop,
                    daemon=True,
                    name="tdpilot-api-heartbeat",
                )
                _hb_thread.start()
                try:
                    with urllib.request.urlopen(req, timeout=self.request_timeout, context=ctx) as resp:
                        return json.loads(resp.read().decode("utf-8"))
                finally:
                    _hb_stop.set()
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8")
                except Exception:
                    detail = ""
                # Non-retryable client error → raise immediately.
                if exc.code in _NON_RETRYABLE_HTTP_CODES:
                    raise AgentError(f"HTTP {exc.code} from /v1/messages: {detail}") from exc
                # Retryable: 429 (rate limit) + any 5xx (server-side).
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable:
                    raise AgentError(f"HTTP {exc.code} from /v1/messages: {detail}") from exc
                if attempt >= self.max_retries:
                    if exc.code == 429:
                        diagnosis = (
                            f"API rate-limited {attempt + 1}× — wait a minute "
                            "and retry. If this keeps happening, your DeepSeek "
                            "key is over its quota or the upstream service is "
                            "having a bad day."
                        )
                    else:
                        diagnosis = (
                            f"API returned HTTP {exc.code} {attempt + 1}× — "
                            "upstream service is degraded. Check "
                            "status.deepseek.com or try again in a minute."
                        )
                    self.on_hint("api_retry_exhausted", diagnosis)
                    raise AgentError(
                        f"HTTP {exc.code} from /v1/messages after "
                        f"{attempt + 1} attempts: {detail}. Diagnosis: "
                        f"{diagnosis}"
                    ) from exc
                # Compute backoff. Prefer server-provided Retry-After
                # over our exponential default; clamp so a misbehaving
                # Retry-After: 9999 can't wedge the cook for hours.
                wait_default = self.initial_backoff * (2**attempt) + random.uniform(0, 0.5)
                wait_seconds = _parse_retry_after(getattr(exc, "headers", None), wait_default)
                wait_seconds = min(wait_seconds, _MAX_BACKOFF_SECONDS)
                self.on_hint(
                    "api_retry",
                    f"HTTP {exc.code} from /v1/messages — retrying in "
                    f"{wait_seconds:.1f}s "
                    f"(attempt {attempt + 1}/{self.max_retries}).",
                )
                # Cooperative cancellation: Event.wait returns True if
                # the flag is set during the sleep window. We honour
                # stop mid-backoff so a user-initiated cancel doesn't
                # have to wait out the full delay.
                if self._stop_flag.wait(timeout=wait_seconds):
                    raise AgentError("Interrupted during retry backoff") from exc
                attempt += 1
                continue
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
