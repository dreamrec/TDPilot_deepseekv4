"""Mock-DeepSeek HTTP server for agent-eval tests (PR-20).

Replays previously-captured ``/v1/messages`` responses from JSON
fixtures so the agent loop can be exercised in regular CI without
hitting api.deepseek.com — and without TouchDesigner running.

Why a real HTTP server instead of ``patch('urllib.request.urlopen')``?
The existing ``tests/test_tdpilot_api_agent.py`` tests use the patch
approach with synthesized inline dicts. PR-20's brief is different:

  1. Fixtures must be captured from REAL DeepSeek so future schema
     drift surfaces as a diff, not as a hand-written approximation.
  2. The mock must enforce the thinking-block echo constraint —
     DeepSeek's compat endpoint returns HTTP 400 if a tool_result
     turn doesn't carry the prior assistant's ``thinking`` blocks
     in its history. Reproducing that contract requires inspecting
     the request body, which fits more naturally in a server than
     in a side_effect lambda.
  3. The test exercises the real urllib + ssl stack, catching
     contract bugs (e.g. header casing, encoding) that monkey-
     patching would mask.

Fixture format: ``tests/fixtures/deepseek/<scenario>.json`` —
pretty-printed JSON, one file per eval scenario, recording every
``/v1/messages`` exchange in order. See ``capture_deepseek_fixtures.py``
for the recorder + the file-format docstring at the top of every
captured fixture.

Threading model: the mock binds to a random localhost port and runs
a single-threaded ``HTTPServer`` in a daemon thread. Each test gets
a fresh server so fixture state isn't shared across test cases.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------


_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "deepseek"


def fixture_path(scenario: str) -> Path:
    """Resolve a scenario name to its on-disk JSON path."""
    return _FIXTURE_DIR / f"{scenario}.json"


def load_fixture(scenario: str) -> dict:
    """Load and validate a captured fixture.

    Raises FileNotFoundError if the fixture doesn't exist; ValueError
    if the JSON is malformed or missing the required keys.
    """
    path = fixture_path(scenario)
    if not path.exists():
        raise FileNotFoundError(
            f"DeepSeek fixture not found at {path}. "
            f"Run `python scripts/capture_deepseek_fixtures.py {scenario}` "
            f"with TD running to capture it."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "exchanges" not in raw:
        raise ValueError(
            f"Fixture {path} is malformed — expected top-level dict with "
            f"'exchanges' key (a list of {{request, response}} entries)."
        )
    exchanges = raw["exchanges"]
    if not isinstance(exchanges, list) or not exchanges:
        raise ValueError(f"Fixture {path} has no exchanges to replay.")
    for i, ex in enumerate(exchanges):
        if not isinstance(ex, dict) or "request" not in ex or "response" not in ex:
            raise ValueError(
                f"Fixture {path} exchange #{i} is malformed — "
                f"each entry must have 'request' and 'response' keys."
            )
    return raw


# ---------------------------------------------------------------------------
# Thinking-block echo verification
# ---------------------------------------------------------------------------
#
# DeepSeek's Anthropic-compat endpoint requires ``type:thinking`` (and
# ``redacted_thinking``) blocks from the previous assistant turn to be
# echoed back in the next request. Stripping them surfaces as:
#
#   HTTP 400: The content[].thinking in the thinking mode must be
#   passed back to the API.
#
# The mock reproduces this contract so the agent's
# ``_strip_reasoning`` logic stays load-bearing — if anyone tries to
# strip thinking blocks in the future, the mock returns 400 and the
# eval test fails.


_THINKING_TYPES = {"thinking", "redacted_thinking"}


def _has_thinking_in_assistant(messages: list[dict]) -> bool:
    """Did any prior assistant message contain a thinking block?"""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in _THINKING_TYPES:
                return True
    return False


def _last_assistant_thinking_blocks(messages: list[dict]) -> list[dict]:
    """Return the thinking blocks from the most recent assistant message."""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            return []
        return [b for b in content if isinstance(b, dict) and b.get("type") in _THINKING_TYPES]
    return []


def _has_assistant_with_thinking_followed_by_user(messages: list[dict]) -> tuple[bool, int]:
    """If any assistant turn with thinking is followed by a user turn,
    return (True, index_of_assistant). Else (False, -1)."""
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        has_thinking = any(isinstance(b, dict) and b.get("type") in _THINKING_TYPES for b in content)
        if not has_thinking:
            continue
        # Check the next message is a user message — if so we expect the
        # thinking blocks to still be intact in our messages history
        # (not stripped between turns).
        if i + 1 < len(messages) and messages[i + 1].get("role") == "user":
            return (True, i)
    return (False, -1)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


@dataclass
class CapturedRequest:
    """A request observed by the mock during a test."""

    method: str
    path: str
    body: dict
    headers: dict


class MockDeepSeek:
    """An aiohttp-free HTTP server that replays DeepSeek fixtures.

    Construct via :func:`MockDeepSeek.from_fixture` (the common case)
    or pass exchanges directly via :func:`MockDeepSeek.from_exchanges`
    for the meta-tests that exercise the server itself.

    Usage::

        server = MockDeepSeek.from_fixture("inspect_basic_fps")
        server.start()
        try:
            agent = Agent(
                api_key="sk-mock",
                base_url=server.base_url,
                ...
            )
            agent.add_user_message("...")
            agent.run_turn()
        finally:
            server.stop()

    The base_url ends in ``/anthropic`` so the agent's literal
    ``f"{self.base_url}/v1/messages"`` resolves to
    ``http://127.0.0.1:<port>/anthropic/v1/messages`` — the same
    shape as ``https://api.deepseek.com/anthropic/v1/messages``.
    """

    def __init__(
        self,
        exchanges: list[dict],
        *,
        scenario: str = "<inline>",
        enforce_thinking_echo: bool = True,
        strict_request_match: bool = False,
    ) -> None:
        if not exchanges:
            raise ValueError("MockDeepSeek requires at least one exchange")
        self._exchanges = exchanges
        self.scenario = scenario
        self.enforce_thinking_echo = enforce_thinking_echo
        self.strict_request_match = strict_request_match

        self._index = 0
        self._captured: list[CapturedRequest] = []
        self._lock = threading.Lock()
        self._httpd: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._thinking_violations: list[str] = []

    @classmethod
    def from_fixture(
        cls,
        scenario: str,
        *,
        enforce_thinking_echo: bool = True,
        strict_request_match: bool = False,
    ) -> MockDeepSeek:
        data = load_fixture(scenario)
        return cls(
            exchanges=list(data["exchanges"]),
            scenario=scenario,
            enforce_thinking_echo=enforce_thinking_echo,
            strict_request_match=strict_request_match,
        )

    @classmethod
    def from_exchanges(
        cls,
        exchanges: list[dict],
        *,
        scenario: str = "<inline>",
        enforce_thinking_echo: bool = True,
        strict_request_match: bool = False,
    ) -> MockDeepSeek:
        return cls(
            exchanges=exchanges,
            scenario=scenario,
            enforce_thinking_echo=enforce_thinking_echo,
            strict_request_match=strict_request_match,
        )

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Base URL the agent should be configured with.

        The Agent class composes ``f"{base_url}/v1/messages"``, so we
        return the URL without ``/v1/messages`` but with the ``/anthropic``
        prefix — matching DeepSeek's compat-endpoint shape.
        """
        if self._httpd is None:
            raise RuntimeError("MockDeepSeek not started yet")
        host, port = self._httpd.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}/anthropic"

    @property
    def root_url(self) -> str:
        """URL without the ``/anthropic`` suffix — for tests that
        exercise the path-routing logic directly."""
        if self._httpd is None:
            raise RuntimeError("MockDeepSeek not started yet")
        host, port = self._httpd.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode("ascii")
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._httpd is not None:
            raise RuntimeError("already started")
        # Bind to port 0 → kernel picks a free port, no race with other tests.
        owner = self
        # Close over `owner` so the request handler can read fixture state.
        # BaseHTTPRequestHandler instances don't get a reference to the server
        # by default — we attach via the `server` attribute (set by HTTPServer).

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                # Silence the per-request stderr noise during pytest runs.
                return

            def do_POST(self):  # noqa: N802 — http.server convention
                owner._handle_post(self)

            def do_GET(self):  # noqa: N802
                owner._handle_get(self)

        self._httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name=f"MockDeepSeek<{self.scenario}>",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._httpd = None
        self._thread = None

    def __enter__(self) -> MockDeepSeek:
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Request handling
    # ------------------------------------------------------------------

    def _handle_get(self, h: BaseHTTPRequestHandler) -> None:
        # Diagnostic endpoint — useful when a test wants to introspect
        # the mock from outside the agent's _call_api path.
        if h.path == "/_mock/state":
            payload = {
                "scenario": self.scenario,
                "next_index": self._index,
                "exchanges_total": len(self._exchanges),
                "captured_requests": len(self._captured),
                "thinking_violations": list(self._thinking_violations),
            }
            body = json.dumps(payload).encode("utf-8")
            h.send_response(200)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            h.wfile.write(body)
            return
        h.send_response(404)
        h.end_headers()

    def _handle_post(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length", "0") or "0")
        raw = h.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            body = {}

        # Capture every request — tests can introspect via captured_requests().
        with self._lock:
            self._captured.append(
                CapturedRequest(
                    method="POST",
                    path=h.path,
                    body=body,
                    headers={k: v for k, v in h.headers.items()},
                )
            )

        if not h.path.endswith("/v1/messages"):
            self._send_json(h, 404, {"error": {"message": f"unknown path {h.path}"}})
            return

        # Thinking-block echo verification — fires BEFORE the index
        # advance so a violating request still maps cleanly to "request
        # N was missing thinking blocks".
        if self.enforce_thinking_echo:
            messages = body.get("messages", [])
            if isinstance(messages, list):
                violation = self._verify_thinking_echo(messages)
                if violation:
                    self._thinking_violations.append(
                        f"request #{len(self._captured)} ({h.path}): {violation}"
                    )
                    self._send_json(
                        h,
                        400,
                        {
                            "error": {
                                "type": "invalid_request_error",
                                "message": (
                                    "The content[].thinking in the thinking mode "
                                    "must be passed back to the API."
                                ),
                            }
                        },
                    )
                    return

        # Get the next scripted exchange.
        with self._lock:
            if self._index >= len(self._exchanges):
                self._send_json(
                    h,
                    500,
                    {
                        "error": {
                            "message": (
                                f"MockDeepSeek<{self.scenario}>: no more "
                                f"exchanges (already replayed {self._index} "
                                f"of {len(self._exchanges)})"
                            )
                        }
                    },
                )
                return
            exchange = self._exchanges[self._index]
            self._index += 1

        # Optional strict request matching — useful for tests that
        # assert the agent sent EXACTLY the model/system/messages we
        # captured. Skipped by default because tests usually only
        # care about the response side.
        if self.strict_request_match:
            captured_request = exchange.get("request", {})
            mismatch = self._diff_requests(captured_request, body)
            if mismatch:
                self._send_json(
                    h,
                    400,
                    {
                        "error": {
                            "message": f"request mismatch at index {self._index - 1}: {mismatch}",
                        }
                    },
                )
                return

        response = exchange.get("response", {})
        self._send_json(h, 200, response)

    def _verify_thinking_echo(self, messages: list[dict]) -> str:
        """Return a violation message if a prior assistant turn had
        thinking blocks but they're missing from the messages history.

        Rules:
          - If ANY assistant message in messages has thinking blocks,
            the request is fine — the agent did its job.
          - If a prior turn's response (per the captured fixture) had
            thinking blocks but the agent's request to us doesn't echo
            them, that's a violation.

        We can't see the response side here directly, but we can detect
        a STRIPPED echo: if there are >=2 assistant messages and any of
        them has tool_use+text but no thinking adjacent, AND we've sent
        thinking blocks in any prior response, that's the violation.

        Practical implementation: after we send a response with thinking
        blocks, the NEXT request must contain those exact thinking blocks
        in the messages history at the corresponding position.
        """
        # Count how many assistant messages we've seen in the history.
        assistant_count = sum(1 for m in messages if m.get("role") == "assistant")
        if assistant_count == 0:
            return ""  # First request, nothing to verify yet.

        # For each assistant message in the history, check that it has
        # thinking blocks IF the prior response we sent had them.
        for prior_response_idx in range(min(assistant_count, self._index)):
            prior_response = self._exchanges[prior_response_idx].get("response", {})
            response_content = prior_response.get("content", [])
            if not isinstance(response_content, list):
                continue
            response_has_thinking = any(
                isinstance(b, dict) and b.get("type") in _THINKING_TYPES for b in response_content
            )
            if not response_has_thinking:
                continue
            # Find the corresponding assistant message in the history.
            assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
            if prior_response_idx >= len(assistant_msgs):
                continue
            history_msg = assistant_msgs[prior_response_idx]
            history_content = history_msg.get("content", [])
            if not isinstance(history_content, list):
                history_content = []
            history_has_thinking = any(
                isinstance(b, dict) and b.get("type") in _THINKING_TYPES for b in history_content
            )
            if not history_has_thinking:
                return (
                    f"prior response #{prior_response_idx} contained thinking "
                    f"blocks but the echoed assistant message in the request "
                    f"history dropped them"
                )
        return ""

    def _diff_requests(self, captured: dict, current: dict) -> str:
        """Return a one-line diff if the captured and current requests
        differ in load-bearing fields. Empty string = match."""
        for field_name in ("model", "system"):
            cap_val = captured.get(field_name)
            cur_val = current.get(field_name)
            if cap_val != cur_val:
                return f"{field_name}: captured={cap_val!r} got={cur_val!r}"
        cap_msgs = captured.get("messages") or []
        cur_msgs = current.get("messages") or []
        if len(cap_msgs) != len(cur_msgs):
            return f"messages length: captured={len(cap_msgs)} got={len(cur_msgs)}"
        return ""

    def _send_json(self, h: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def captured_requests(self) -> list[CapturedRequest]:
        """Return a snapshot of every POST observed since start()."""
        with self._lock:
            return list(self._captured)

    def thinking_violations(self) -> list[str]:
        """Diagnostic violations triggered during this session.

        If the mock returned HTTP 400 for thinking-block echo failures,
        each rejected request leaves a violation message here. Tests
        assert ``mock.thinking_violations() == []`` to enforce the
        agent's strip-reasoning contract.
        """
        with self._lock:
            return list(self._thinking_violations)

    def remaining(self) -> int:
        """How many exchanges in the fixture haven't been replayed yet."""
        with self._lock:
            return len(self._exchanges) - self._index


# ---------------------------------------------------------------------------
# Fixture builder for inline tests
# ---------------------------------------------------------------------------


@dataclass
class ResponseBuilder:
    """Convenience for building a single ``response`` payload.

    The on-disk fixture format puts the raw DeepSeek response under
    ``exchanges[i].response``. For tests that need synthesized responses
    (the meta-tests for the mock itself), this builder produces a
    DeepSeek-shaped dict without the test having to remember every
    field name.
    """

    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = "deepseek-v4-pro"
    usage: dict = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 50})

    def with_text(self, text: str) -> ResponseBuilder:
        self.content.append({"type": "text", "text": text})
        return self

    def with_thinking(self, thinking: str) -> ResponseBuilder:
        self.content.append({"type": "thinking", "thinking": thinking})
        return self

    def with_tool_use(self, tool_id: str, name: str, args: dict) -> ResponseBuilder:
        self.content.append({"type": "tool_use", "id": tool_id, "name": name, "input": args})
        self.stop_reason = "tool_use"
        return self

    def build(self) -> dict:
        return {
            "id": "msg_mock",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": list(self.content),
            "stop_reason": self.stop_reason,
            "stop_sequence": None,
            "usage": dict(self.usage),
        }
