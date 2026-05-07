"""Shared helpers for the Phase 4.2 agent-quality eval suite.

Each eval file under ``tests/agent_evals/`` uses these helpers to
talk to the running tdpilot_API webserver. The harness deliberately
does NOT spin up its own TD instance — these are integration tests
designed to run against a real .tox loaded in TouchDesigner.

Skipped automatically when:
  - The webserver isn't reachable at the configured URL (TD not
    running, .tox not loaded, port blocked).
  - No API key is configured at ``~/.tdpilot-api/config.json``.

Run them with:
    pytest -m agent_eval

The default pytest invocation excludes the ``agent_eval`` marker
(see addopts in pyproject.toml).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from typing import Any

import pytest

DEFAULT_BASE_URL = "http://127.0.0.1:9987"
DEFAULT_TURN_TIMEOUT_SEC = 60.0
POLL_INTERVAL_SEC = 0.5


# ---------------------------------------------------------------------------
# Reachability — gates the whole suite when the .tox isn't running.
# ---------------------------------------------------------------------------


def _webserver_alive(base_url: str = DEFAULT_BASE_URL, timeout: float = 1.5) -> bool:
    target = base_url.rstrip("/") + "/health"
    try:
        with urllib.request.urlopen(target, timeout=timeout) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


@pytest.fixture(scope="session")
def base_url() -> str:
    """Webserver URL for the live .tox. Override with ``TDPILOT_API_URL``
    env var if you've changed the COMP's port.
    """
    import os

    return os.environ.get("TDPILOT_API_URL", DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(autouse=True)
def _require_live_webserver(request, base_url):
    """Skip every agent_eval if the webserver isn't reachable. Lets the
    suite stay green in CI / on a developer machine without TD running.
    """
    if "agent_eval" not in request.node.keywords:
        return
    if not _webserver_alive(base_url):
        pytest.skip(f"tdpilot_API not reachable at {base_url}/health — start TD with the .tox loaded")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post(base_url: str, path: str, body: dict | str | None = None) -> tuple[int, str]:
    """POST to the webserver, return (status, body_text). Body can be a
    dict (JSON-encoded) or a raw string (UTF-8 encoded).
    """
    target = base_url.rstrip("/") + path
    if body is None:
        data = b""
        headers: dict[str, str] = {}
    elif isinstance(body, dict):
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        data = str(body).encode("utf-8")
        headers = {"Content-Type": "text/plain; charset=utf-8"}
    req = urllib.request.Request(target, method="POST", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _get(base_url: str, path: str) -> tuple[int, str]:
    target = base_url.rstrip("/") + path
    try:
        with urllib.request.urlopen(target, timeout=10.0) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Public eval helpers
# ---------------------------------------------------------------------------


def reset_session(base_url: str) -> None:
    """Clear the agent's conversation history before an eval starts so
    prior turns don't poison the assertions.
    """
    _post(base_url, "/reset")


def send_prompt(base_url: str, text: str) -> None:
    """Issue a user prompt to the agent. Returns when the request was
    accepted (the agent's worker thread runs asynchronously — use
    :func:`wait_for_turn_complete` to know when it's done).
    """
    status, body = _post(base_url, "/send", text)
    if status >= 400:
        raise RuntimeError(f"/send returned {status}: {body}")


def history(base_url: str) -> list[dict]:
    """Return the chat transcript rows: list of {role, message}."""
    status, body = _get(base_url, "/history")
    if status != 200:
        raise RuntimeError(f"/history returned {status}: {body}")
    payload = json.loads(body or "{}")
    return list(payload.get("rows", []))


def _last_role(rows: Iterable[dict]) -> str:
    last = ""
    for r in rows:
        last = r.get("role", "")
    return last


def wait_for_turn_complete(
    base_url: str,
    *,
    timeout: float = DEFAULT_TURN_TIMEOUT_SEC,
    poll: float = POLL_INTERVAL_SEC,
) -> list[dict]:
    """Block until the agent finishes the current turn or ``timeout``
    expires. Returns the final history rows.

    Termination signals (in order of precedence):
      - the most recent row's role is ``"hint"`` after an assistant row
        — Phase 1.3 emits the hint at turn-end.
      - the most recent row's role is ``"assistant"`` AND no new rows
        appear for ``stable_for`` seconds.
      - the most recent row's role is ``"error"``.

    Raises ``TimeoutError`` if neither condition is reached.
    """
    deadline = time.monotonic() + timeout
    last_count = -1
    stable_since = time.monotonic()
    stable_for = 1.0  # 1s of no new rows after assistant text counts as done

    while time.monotonic() < deadline:
        rows = history(base_url)
        if rows and rows[-1].get("role") == "error":
            return rows
        if len(rows) != last_count:
            last_count = len(rows)
            stable_since = time.monotonic()
        else:
            # No new rows since last poll. If the last row is assistant
            # text and we've been stable long enough, the turn is done.
            last_role = rows[-1].get("role", "") if rows else ""
            if last_role == "assistant" and time.monotonic() - stable_since >= stable_for:
                return rows
            if last_role == "hint":  # validation hint = end of turn
                return rows
        time.sleep(poll)

    raise TimeoutError(
        f"agent turn did not complete within {timeout}s; last rows: {rows[-3:] if rows else 'none'}"
    )


# ---------------------------------------------------------------------------
# Assertion helpers — give failure messages that name the offender
# ---------------------------------------------------------------------------


def tool_calls_in(rows: Iterable[dict]) -> list[str]:
    """Return tool-call names from the transcript, in order."""
    out: list[str] = []
    for r in rows:
        if r.get("role") != "tool_call":
            continue
        msg = r.get("message", "")
        # The chat HTML renders tool calls as "name(<args>)" — strip args.
        name = msg.split("(", 1)[0].strip()
        if name:
            out.append(name)
    return out


def assistant_replies(rows: Iterable[dict]) -> list[str]:
    return [r.get("message", "") for r in rows if r.get("role") == "assistant"]


def hint_messages(rows: Iterable[dict]) -> list[str]:
    return [r.get("message", "") for r in rows if r.get("role") == "hint"]


def assert_tool_in_sequence(rows: Iterable[dict], expected: Iterable[str]) -> None:
    """Assert every tool in ``expected`` was called at least once.

    Order-independent: callers usually don't care whether the agent
    called td_get_info or td_get_errors first as long as both fired.
    """
    actual = tool_calls_in(rows)
    missing = [t for t in expected if t not in actual]
    assert not missing, f"missing tool calls {missing}; saw: {actual}"


def assert_reply_contains(rows: Iterable[dict], needle: str, *, case_insensitive: bool = True) -> None:
    """Assert at least one assistant reply contains ``needle``."""
    haystack = "\n".join(assistant_replies(rows))
    if case_insensitive:
        ok = needle.lower() in haystack.lower()
    else:
        ok = needle in haystack
    assert ok, f"reply did not mention {needle!r}; final transcript:\n{haystack[-600:]}"


def assert_no_error_event(rows: Iterable[dict]) -> None:
    errors = [r.get("message", "") for r in rows if r.get("role") == "error"]
    assert not errors, f"agent emitted error events: {errors}"


# ---------------------------------------------------------------------------
# Default eval pipeline — used by every spec table eval.
# ---------------------------------------------------------------------------


def run_eval_turn(base_url: str, prompt: str, *, timeout: float = DEFAULT_TURN_TIMEOUT_SEC) -> list[dict]:
    """Reset → send prompt → wait for completion → return history rows.

    The standard one-turn evaluation flow most evals follow.
    """
    reset_session(base_url)
    send_prompt(base_url, prompt)
    return wait_for_turn_complete(base_url, timeout=timeout)
