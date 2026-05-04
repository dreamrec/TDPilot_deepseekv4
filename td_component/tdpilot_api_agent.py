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
        # Callbacks — all optional. Receive primitive args.
        on_text: Callable[[str], None] = _noop,
        on_tool_call: Callable[[str, dict], None] = _noop,
        on_tool_result: Callable[[str, Any, bool], None] = _noop,
        on_turn_done: Callable[[str], None] = _noop,
        on_error: Callable[[BaseException], None] = _noop,
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
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.turn_budget = turn_budget
        self.request_timeout = request_timeout

        self.on_text = on_text
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_turn_done = on_turn_done
        self.on_error = on_error

        self.messages: list[dict] = []
        self._stop_flag = threading.Event()

    # ------------------------------------------------------------------
    # Public state mutators
    # ------------------------------------------------------------------

    def add_user_message(self, text: str) -> None:
        self.messages.append({"role": "user", "content": [{"type": "text", "text": text}]})

    def reset(self) -> None:
        self.messages.clear()
        self._stop_flag.clear()

    def stop(self) -> None:
        """Cooperative cancellation. Checked between API calls."""
        self._stop_flag.set()

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

    def _resolve_model(self, user_text: str) -> str:
        """Pure function. Pick a model for this turn based on the most
        recent user message + the configured tier override.

        Heuristic (only runs when tier='auto'):
          score 1 point each for:
            * len(user_text) > 300 chars
            * presence of pro-leaning verbs (build/create/fix/...)
            * fenced code block (`)
            * 2+ tool-name keywords in the text
          score >= 2 → pro, else flash

        With the 75% promo (through 2026-05-31), the cost gap is ~3×
        but the latency gap is the bigger UX win — flash's lower TTFT
        feels qualitatively snappier on lookup-style prompts. Cascade
        routing (try flash, escalate on low-confidence) is rejected
        for v1 — extra round trips eat the savings at our usage volume.
        """
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
        chosen = self._resolve_model(last_user_text)
        if chosen != self._active_model:
            try:
                self.on_model_change(self.model_tier, chosen)
            except Exception:
                pass
        self._active_model = chosen

        for _turn in range(self.turn_budget):
            if self._stop_flag.is_set():
                return None

            response = self._call_api()
            content = response.get("content", []) or []
            stop_reason = response.get("stop_reason")

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
            results_block = []
            for tu in tool_uses:
                tool_id = tu.get("id", "")
                tool_name = tu.get("name", "")
                tool_args = tu.get("input", {}) or {}
                self.on_tool_call(tool_name, tool_args)
                try:
                    result = self.dispatcher(tool_name, tool_args)
                    is_error = isinstance(result, dict) and "error" in result
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}"}
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
            self.messages.append({"role": "user", "content": results_block})

            if stop_reason == "end_turn":
                # Defensive — model said end_turn but also issued tool calls.
                # Run the next turn to surface its follow-up text, but don't loop forever.
                continue

        raise TurnBudgetExceeded(f"Tool-use loop exceeded turn_budget={self.turn_budget}")

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _call_api(self) -> dict:
        # _active_model was pinned at turn start by _loop(). Using it
        # (instead of self.model) is what makes Sprint 4.3 routing
        # actually take effect — flash for simple lookups, pro for
        # complex builds.
        body: dict[str, Any] = {
            "model": self._active_model or self.model,
            "max_tokens": self.max_tokens,
            "messages": self.messages,
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
