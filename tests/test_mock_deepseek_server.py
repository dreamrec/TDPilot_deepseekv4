"""Meta-tests for the Mock-DeepSeek server (PR-20).

These exercise the mock infrastructure itself — fixture loading,
request matching, thinking-block echo enforcement, lifecycle. They
use synthesized inline exchanges (``ResponseBuilder``) because they
test the mock, not the agent.

Real-DeepSeek-captured fixtures are exercised by
``test_agent_evals_mock.py`` which is the user-facing eval suite.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

import pytest
from _mock_deepseek import (
    MockDeepSeek,
    ResponseBuilder,
    fixture_path,
    load_fixture,
)

# ---------------------------------------------------------------------------
# Fixture-loader tests
# ---------------------------------------------------------------------------


def test_load_fixture_missing_raises_with_capture_hint(tmp_path, monkeypatch):
    """Missing fixtures emit a clear error pointing at the recorder."""
    monkeypatch.setattr("_mock_deepseek._FIXTURE_DIR", tmp_path / "doesnt_exist")
    with pytest.raises(FileNotFoundError) as exc_info:
        load_fixture("nonexistent_scenario")
    msg = str(exc_info.value)
    assert "nonexistent_scenario" in msg
    assert "capture_deepseek_fixtures.py" in msg


def test_load_fixture_validates_top_level_shape(tmp_path, monkeypatch):
    monkeypatch.setattr("_mock_deepseek._FIXTURE_DIR", tmp_path)
    bad_path = tmp_path / "broken.json"
    bad_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        load_fixture("broken")


def test_load_fixture_rejects_empty_exchanges(tmp_path, monkeypatch):
    monkeypatch.setattr("_mock_deepseek._FIXTURE_DIR", tmp_path)
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"exchanges": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="no exchanges"):
        load_fixture("empty")


def test_load_fixture_rejects_malformed_exchange_entries(tmp_path, monkeypatch):
    monkeypatch.setattr("_mock_deepseek._FIXTURE_DIR", tmp_path)
    p = tmp_path / "missing_response.json"
    p.write_text(
        json.dumps({"exchanges": [{"request": {"a": 1}}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exchange #0"):
        load_fixture("missing_response")


def test_fixture_path_resolves_under_fixtures_dir():
    """fixture_path() must yield ``tests/fixtures/deepseek/<name>.json``."""
    p = fixture_path("inspect_basic_fps")
    parts = p.parts
    assert parts[-3:] == ("fixtures", "deepseek", "inspect_basic_fps.json")


# ---------------------------------------------------------------------------
# Server lifecycle tests
# ---------------------------------------------------------------------------


def _post(url: str, body: dict) -> tuple[int, dict]:
    """Helper — POST JSON, return (status, parsed-or-raw-string)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url=url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=data,
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            payload = {"raw": str(exc)}
        return exc.code, payload


def test_server_starts_and_returns_first_exchange():
    exchanges = [
        {
            "request": {"messages": [{"role": "user", "content": "hi"}]},
            "response": ResponseBuilder().with_text("hello back").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        status, body = _post(
            f"{server.base_url}/v1/messages",
            {"messages": [{"role": "user", "content": "hi"}]},
        )
    assert status == 200
    assert body["content"][0]["text"] == "hello back"


def test_server_replays_exchanges_in_order():
    exchanges = [
        {
            "request": {"messages": [{"role": "user", "content": "a"}]},
            "response": ResponseBuilder().with_text("first").build(),
        },
        {
            "request": {"messages": [{"role": "user", "content": "b"}]},
            "response": ResponseBuilder().with_text("second").build(),
        },
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        s1, b1 = _post(
            f"{server.base_url}/v1/messages",
            {"messages": [{"role": "user", "content": "a"}]},
        )
        s2, b2 = _post(
            f"{server.base_url}/v1/messages",
            {"messages": [{"role": "user", "content": "b"}]},
        )
    assert s1 == 200 and b1["content"][0]["text"] == "first"
    assert s2 == 200 and b2["content"][0]["text"] == "second"


def test_server_returns_500_when_exchanges_exhausted():
    exchanges = [
        {
            "request": {},
            "response": ResponseBuilder().with_text("only one").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        s1, _ = _post(f"{server.base_url}/v1/messages", {"messages": []})
        s2, b2 = _post(f"{server.base_url}/v1/messages", {"messages": []})
    assert s1 == 200
    assert s2 == 500
    # Error message should name the scenario so failures surface clearly.
    assert "no more exchanges" in b2["error"]["message"]


def test_server_404s_unknown_paths():
    exchanges = [{"request": {}, "response": ResponseBuilder().with_text("x").build()}]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        status, body = _post(f"{server.base_url}/v1/wrongpath", {})
    assert status == 404
    assert "unknown path" in body["error"]["message"]


def test_server_diagnostic_state_endpoint():
    exchanges = [
        {"request": {}, "response": ResponseBuilder().with_text("x").build()},
        {"request": {}, "response": ResponseBuilder().with_text("y").build()},
    ]
    with MockDeepSeek.from_exchanges(exchanges, scenario="diag_test") as server:
        # Before any requests: state shows fixture loaded, nothing replayed.
        with urllib.request.urlopen(f"{server.root_url}/_mock/state", timeout=5.0) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        assert state["scenario"] == "diag_test"
        assert state["next_index"] == 0
        assert state["exchanges_total"] == 2
        assert state["captured_requests"] == 0

        # Drive one request, re-check.
        _post(f"{server.base_url}/v1/messages", {"messages": []})
        with urllib.request.urlopen(f"{server.root_url}/_mock/state", timeout=5.0) as resp:
            state = json.loads(resp.read().decode("utf-8"))
        assert state["next_index"] == 1
        assert state["captured_requests"] == 1


def test_server_captures_request_bodies_for_introspection():
    exchanges = [{"request": {}, "response": ResponseBuilder().with_text("ok").build()}]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        _post(
            f"{server.base_url}/v1/messages",
            {"model": "deepseek-v4-pro", "messages": [{"role": "user"}]},
        )
        captured = server.captured_requests()
    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].path.endswith("/v1/messages")
    assert captured[0].body["model"] == "deepseek-v4-pro"


def test_server_remaining_counter_decrements():
    exchanges = [
        {"request": {}, "response": ResponseBuilder().with_text("a").build()},
        {"request": {}, "response": ResponseBuilder().with_text("b").build()},
        {"request": {}, "response": ResponseBuilder().with_text("c").build()},
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        assert server.remaining() == 3
        _post(f"{server.base_url}/v1/messages", {})
        assert server.remaining() == 2
        _post(f"{server.base_url}/v1/messages", {})
        assert server.remaining() == 1


def test_server_double_start_raises():
    server = MockDeepSeek.from_exchanges(
        [{"request": {}, "response": ResponseBuilder().with_text("x").build()}]
    )
    server.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            server.start()
    finally:
        server.stop()


def test_base_url_before_start_raises():
    server = MockDeepSeek.from_exchanges(
        [{"request": {}, "response": ResponseBuilder().with_text("x").build()}]
    )
    with pytest.raises(RuntimeError, match="not started"):
        _ = server.base_url


def test_base_url_includes_anthropic_suffix():
    """Agent composes f'{base_url}/v1/messages'; the prefix must include
    /anthropic so the path matches DeepSeek's compat shape."""
    with MockDeepSeek.from_exchanges(
        [{"request": {}, "response": ResponseBuilder().with_text("x").build()}]
    ) as server:
        assert server.base_url.endswith("/anthropic")
        assert server.root_url + "/anthropic" == server.base_url


def test_server_rejects_construction_with_no_exchanges():
    with pytest.raises(ValueError, match="at least one"):
        MockDeepSeek.from_exchanges([])


# ---------------------------------------------------------------------------
# Strict request matching
# ---------------------------------------------------------------------------


def test_strict_match_passes_when_model_and_system_align():
    exchanges = [
        {
            "request": {
                "model": "deepseek-v4-pro",
                "system": "you are helpful",
                "messages": [{"role": "user"}],
            },
            "response": ResponseBuilder().with_text("ok").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges, strict_request_match=True) as server:
        status, body = _post(
            f"{server.base_url}/v1/messages",
            {
                "model": "deepseek-v4-pro",
                "system": "you are helpful",
                "messages": [{"role": "user"}],
            },
        )
    assert status == 200
    assert body["content"][0]["text"] == "ok"


def test_strict_match_400s_on_model_mismatch():
    exchanges = [
        {
            "request": {"model": "deepseek-v4-pro", "messages": []},
            "response": ResponseBuilder().with_text("ok").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges, strict_request_match=True) as server:
        status, body = _post(
            f"{server.base_url}/v1/messages",
            {"model": "deepseek-v4-flash", "messages": []},
        )
    assert status == 400
    assert "model" in body["error"]["message"]


def test_strict_match_400s_on_messages_length_mismatch():
    exchanges = [
        {
            "request": {"model": "x", "messages": [{"role": "user"}]},
            "response": ResponseBuilder().with_text("ok").build(),
        }
    ]
    with MockDeepSeek.from_exchanges(exchanges, strict_request_match=True) as server:
        status, body = _post(
            f"{server.base_url}/v1/messages",
            {
                "model": "x",
                "messages": [{"role": "user"}, {"role": "assistant"}],
            },
        )
    assert status == 400
    assert "messages length" in body["error"]["message"]


# ---------------------------------------------------------------------------
# Thinking-block echo enforcement
# ---------------------------------------------------------------------------


def test_thinking_blocks_echoed_in_history_passes():
    """When the prior response had thinking blocks AND the agent echoed
    them in the next request's messages history, the second request
    should land on a 200 (the response payload is whatever's scripted)."""
    first_response = (
        ResponseBuilder()
        .with_thinking("internal monologue: figuring out the answer")
        .with_text("the answer is 42")
        .build()
    )
    exchanges = [
        {"request": {}, "response": first_response},
        {
            "request": {},
            "response": ResponseBuilder().with_text("turn 2 ack").build(),
        },
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        # Turn 1: user → mock returns first_response with thinking.
        status1, _ = _post(
            f"{server.base_url}/v1/messages",
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}]},
        )
        # Turn 2: agent echoes thinking blocks back (good citizen).
        status2, _ = _post(
            f"{server.base_url}/v1/messages",
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "go"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "internal monologue: figuring out the answer"},
                            {"type": "text", "text": "the answer is 42"},
                        ],
                    },
                    {"role": "user", "content": [{"type": "text", "text": "thanks"}]},
                ]
            },
        )
    assert status1 == 200
    assert status2 == 200
    assert server.thinking_violations() == []


def test_thinking_blocks_stripped_returns_400():
    """If the agent strips thinking blocks between turns, the mock
    must return 400 with the canonical DeepSeek error message."""
    first_response = ResponseBuilder().with_thinking("hidden reasoning").with_text("partial answer").build()
    exchanges = [
        {"request": {}, "response": first_response},
        {
            "request": {},
            "response": ResponseBuilder().with_text("never reached").build(),
        },
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        # Turn 1.
        _post(
            f"{server.base_url}/v1/messages",
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "go"}]}]},
        )
        # Turn 2: STRIPPED thinking — should 400.
        status2, body2 = _post(
            f"{server.base_url}/v1/messages",
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "go"}]},
                    {
                        "role": "assistant",
                        "content": [
                            # Thinking block is GONE.
                            {"type": "text", "text": "partial answer"},
                        ],
                    },
                    {"role": "user", "content": [{"type": "text", "text": "more"}]},
                ]
            },
        )
    assert status2 == 400
    assert (
        body2["error"]["message"]
        == "The content[].thinking in the thinking mode must be passed back to the API."
    )
    violations = server.thinking_violations()
    assert len(violations) == 1
    assert "dropped" in violations[0]


def test_thinking_echo_enforcement_can_be_disabled():
    """Some tests may want to exercise behavior with stripped thinking
    blocks (e.g. testing the agent's fall-through path); the mock
    accepts ``enforce_thinking_echo=False`` to skip the check."""
    exchanges = [
        {
            "request": {},
            "response": ResponseBuilder().with_thinking("x").with_text("y").build(),
        },
        {"request": {}, "response": ResponseBuilder().with_text("z").build()},
    ]
    with MockDeepSeek.from_exchanges(exchanges, enforce_thinking_echo=False) as server:
        _post(f"{server.base_url}/v1/messages", {"messages": []})
        status2, _ = _post(
            f"{server.base_url}/v1/messages",
            {
                "messages": [
                    {"role": "assistant", "content": [{"type": "text", "text": "y"}]},
                    {"role": "user", "content": []},
                ]
            },
        )
    assert status2 == 200
    assert server.thinking_violations() == []


def test_thinking_echo_skipped_when_no_prior_response_had_thinking():
    """If no prior response carried thinking blocks, the request is
    fine even without thinking blocks in history."""
    exchanges = [
        {
            "request": {},
            "response": ResponseBuilder().with_text("turn 1").build(),
        },
        {
            "request": {},
            "response": ResponseBuilder().with_text("turn 2").build(),
        },
    ]
    with MockDeepSeek.from_exchanges(exchanges) as server:
        _post(f"{server.base_url}/v1/messages", {"messages": []})
        status2, _ = _post(
            f"{server.base_url}/v1/messages",
            {
                "messages": [
                    {"role": "user", "content": []},
                    {"role": "assistant", "content": []},
                    {"role": "user", "content": []},
                ]
            },
        )
    assert status2 == 200
    assert server.thinking_violations() == []
