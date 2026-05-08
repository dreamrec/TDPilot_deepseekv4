"""v2.1.1 bugfix regression tests — paused-TD detection + new recovery hints.

These tests pin two classes of fixes:

  1. paused-TD detection in start_turn — pre-v2.1.1 a paused TD
     (me.time.play == False) caused every tool call to wait the
     full 60s DEFAULT_TOOL_TIMEOUT before timing out, because the
     CookThreadDispatcher only pumps from onFrameStart and TD
     stops calling that callback when playback is paused. The agent
     then saw consecutive timeouts and falsely concluded
     "TouchDesigner is completely unresponsive". v2.1.1 catches this
     at the entry point and emits an EV_HINT so the user knows to
     press spacebar.

  2. Four new recovery_hints patterns — surfaced from a 184-message
     lighting-redesign turn with 11 tool_result errors (all agent
     learning errors, zero TD-side bugs). The patterns target real
     misuses: td.Par.rawVal (deprecated TD-2022 attribute),
     renderTOP attribute typos (cooking / numCooks / xres / yres),
     tdu.Matrix.translation (use .tx/.ty/.tz), and
     ParCollection.children (it's the param list, not children).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from queue import Empty, Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_recovery as recovery  # noqa: E402
import tdpilot_api_runtime as runtime  # noqa: E402

# ---------------------------------------------------------------------------
# Fixture: minimal AgentRuntime that exercises start_turn without TD globals
# ---------------------------------------------------------------------------


def _drain(events: Queue) -> list[tuple[str, object]]:
    """Pull every event currently queued, return as list of (kind, payload)."""
    out: list[tuple[str, object]] = []
    while True:
        try:
            kind, payload = events.get_nowait()
        except Empty:
            break
        out.append((kind, payload))
    return out


@pytest.fixture
def stub_runtime(monkeypatch):
    """Build a minimal AgentRuntime that bypasses __init__'s TD coupling.

    AgentRuntime.__init__ calls _build_agent() and
    _refresh_dynamic_context() at construction, both of which touch
    TD globals. We bypass with object.__new__ and populate only the
    fields start_turn actually reads, plus stub out the heavyweight
    helpers it calls into.
    """
    rt = object.__new__(runtime.AgentRuntime)
    rt._events = Queue()
    rt._agent = MagicMock()
    rt._agent.add_user_message = MagicMock()
    rt._agent.model_tier = "auto"
    rt._worker = None
    rt._lock = threading.Lock()
    rt._session_skills_activated = {}
    rt._turn_tool_calls = []
    rt._tool_call_starts = {}
    rt._tool_started_monotonic = {}
    rt._dynamic_context_snapshot = []
    rt._config = {}

    # Stub the TD-touching helpers that start_turn calls into.
    monkeypatch.setattr(rt, "_refresh_dynamic_context", lambda **_kw: None)
    monkeypatch.setattr(rt, "_check_skill_triggers", lambda _t: None)
    monkeypatch.setattr(rt, "_trace_start_turn", lambda _t: None)

    # Don't spawn a real worker thread — just record that start would have run.
    fake_thread = MagicMock()
    fake_thread.is_alive = MagicMock(return_value=False)
    monkeypatch.setattr(runtime.threading, "Thread", lambda *a, **kw: fake_thread)

    return rt


# ---------------------------------------------------------------------------
# Fix 1 — paused-TD detection in start_turn
# ---------------------------------------------------------------------------


def test_start_turn_emits_paused_hint_when_playback_paused(stub_runtime, monkeypatch):
    """When me.time.play is False, start_turn must emit an EV_HINT
    warning the user that tool calls will time out — BEFORE the
    worker thread spawns and starts wedging on dispatcher timeouts."""
    fake_me = SimpleNamespace(time=SimpleNamespace(play=False))
    monkeypatch.setattr(runtime, "me", fake_me, raising=False)

    started = stub_runtime.start_turn("hello")

    assert started is True
    events = _drain(stub_runtime._events)
    hint_events = [(k, p) for k, p in events if k == runtime.EV_HINT]
    assert hint_events, f"expected an EV_HINT event, got: {events}"
    # Payload may be string or dict — extract message text either way.
    payload = hint_events[0][1]
    msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
    assert "paused" in msg.lower()


def test_start_turn_does_not_emit_paused_hint_when_playback_running(stub_runtime, monkeypatch):
    """When me.time.play is True, no paused-hint should fire. Other
    EV_HINT events (e.g. skill-activation) may still happen — we only
    assert that no hint mentions 'paused'."""
    fake_me = SimpleNamespace(time=SimpleNamespace(play=True))
    monkeypatch.setattr(runtime, "me", fake_me, raising=False)

    stub_runtime.start_turn("hello")

    events = _drain(stub_runtime._events)
    for kind, payload in events:
        if kind != runtime.EV_HINT:
            continue
        msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
        assert "paused" not in msg.lower(), f"unexpected paused hint with playback running: {msg}"


def test_start_turn_survives_missing_me_global(stub_runtime, monkeypatch):
    """In non-TD contexts (pytest fixtures, standalone runs) the `me`
    global is absent. start_turn must not crash — the paused-detect
    block degrades to a silent no-op."""
    # Ensure `me` is NOT defined as a module attribute. Use raising=False
    # to handle the case where it's already absent.
    monkeypatch.delattr(runtime, "me", raising=False)

    started = stub_runtime.start_turn("hello")

    assert started is True
    events = _drain(stub_runtime._events)
    # We don't care about other events — just that start_turn didn't raise
    # AND no paused-hint was emitted (since we couldn't read the play state).
    for kind, payload in events:
        if kind != runtime.EV_HINT:
            continue
        msg = payload.get("message", "") if isinstance(payload, dict) else str(payload)
        assert "paused" not in msg.lower(), f"hint fired despite missing me: {msg}"


# ---------------------------------------------------------------------------
# Fix 2 — Four new recovery_hints patterns (v2.1.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error_msg, expected_hint_substring",
    [
        # td.Par.rawVal — deprecated TD-2022 name, removed in TD 2025
        ("'td.Par' object has no attribute 'rawVal'", "par.eval"),
        # renderTOP attribute typos — multiple variants share one pattern
        ("'td.renderTOP' object has no attribute 'cooking'", "cookCount"),
        ("'td.renderTOP' object has no attribute 'numCooks'", "cookCount"),
        ("'td.renderTOP' object has no attribute 'xres'", "resolutionw"),
        ("'td.renderTOP' object has no attribute 'yres'", "resolutionh"),
        # tdu.Matrix.translation — actual fields are .tx/.ty/.tz
        ("'tdu.Matrix' object has no attribute 'translation'", ".tx"),
        # ParCollection.children — it's the parameter list, not children
        ("'td.ParCollection' object has no attribute 'children'", "op.children"),
    ],
)
def test_recovery_hints_v211_patterns(error_msg, expected_hint_substring):
    """Each of the v2.1.1 AttributeError patterns gets a specific
    actionable hint pointing at the right TD API."""
    enriched = recovery.attach_hint({"error": error_msg})
    assert "recovery_hint" in enriched, f"no hint attached for: {error_msg}"
    assert expected_hint_substring.lower() in enriched["recovery_hint"].lower()
