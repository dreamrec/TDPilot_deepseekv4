"""v2.1.1 paused-TD UX-trap regression tests.

When TouchDesigner playback is paused (``me.time.play = False``), TD's
``onFrameStart`` callback does NOT fire. ``CookThreadDispatcher`` relies
on ``onFrameStart`` pumping each cook to deliver tool-call results to
the worker thread; without pumps, every tool call times out at 60s and
the agent falsely concludes "TD is unresponsive" and tells the user to
restart TD when the actual fix is one keypress (spacebar).

v2.1.1 adds a detect-and-warn check at ``start_turn`` that emits
``EV_HINT(kind="paused_td")`` so the user sees the explanation BEFORE
spending a turn budget watching the worker hit 60s timeouts. The
underlying CookThreadDispatcher pump architecture is untouched —
moving the pump off ``onFrameStart`` is tracked as a separate tech-debt
item (Option B in the v2.1.1 plan).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from queue import Empty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _drain_runtime_events(rt) -> list[tuple[str, object]]:
    """Pop every event from the runtime queue (mirror of helper in
    test_tdpilot_api_runtime.py).
    """
    events: list[tuple[str, object]] = []
    while True:
        try:
            events.append(rt._events.get_nowait())
        except Empty:
            break
    return events


def _build_runtime(monkeypatch):
    """Construct an AgentRuntime with a fake API key (no real TD)."""
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    return AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])


def _stub_thread_spawn(monkeypatch):
    """Replace threading.Thread so start_turn doesn't spawn a real worker.

    The runtime tests in test_tdpilot_api_runtime.py use this same trick
    to keep ``start_turn`` synchronous and observable.
    """
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None, "is_alive": lambda self: False})(),
    )


class _FakeAgent:
    """Stand-in for the real Agent — only ``add_user_message`` is needed
    by ``start_turn``.

    ``messages`` is initialised per-instance (NOT as a class-level
    mutable default) so tests can't accidentally share state through
    a single shared list — a hazard that would only manifest if
    ``add_user_message`` were ever extended to mutate it.
    """

    def __init__(self) -> None:
        self.messages: list = []

    def add_user_message(self, _text: str) -> None:
        pass


def test_paused_td_emits_hint_at_start_turn(monkeypatch):
    """When TD playback is paused, start_turn emits EV_HINT(kind=paused_td)
    BEFORE the worker thread spawns — so the user sees the warning
    immediately rather than blowing a turn budget on 60s timeouts.
    """
    from tdpilot_api_runtime import EV_HINT, AgentRuntime

    rt = _build_runtime(monkeypatch)
    rt._agent = _FakeAgent()  # type: ignore[assignment]

    # Force the paused-state probe to claim TD is paused.
    monkeypatch.setattr(AgentRuntime, "_is_td_paused", lambda self: True)
    _stub_thread_spawn(monkeypatch)

    # Drain construction-time events so the assertion below sees only
    # events from THIS turn.
    _drain_runtime_events(rt)

    rt.start_turn("hello")

    events = _drain_runtime_events(rt)
    paused_hints = [
        (k, p) for k, p in events if k == EV_HINT and isinstance(p, dict) and p.get("kind") == "paused_td"
    ]
    assert len(paused_hints) == 1, f"expected exactly one paused_td hint, got {len(paused_hints)}"
    payload = paused_hints[0][1]
    assert isinstance(payload, dict)
    msg = payload.get("message", "")
    assert "paused" in msg.lower(), f"hint message must mention 'paused', got: {msg!r}"
    # The message has to point at the fix (spacebar / unpause) so the
    # user knows what to do — not just describe the symptom.
    assert "spacebar" in msg.lower() or "unpause" in msg.lower() or "play" in msg.lower(), (
        f"hint must point at the fix (spacebar/unpause/play), got: {msg!r}"
    )


def test_playing_td_emits_no_paused_hint(monkeypatch):
    """When TD is playing, start_turn must NOT emit a paused_td hint
    (otherwise we'd warn on every message).
    """
    from tdpilot_api_runtime import EV_HINT, AgentRuntime

    rt = _build_runtime(monkeypatch)
    rt._agent = _FakeAgent()  # type: ignore[assignment]

    monkeypatch.setattr(AgentRuntime, "_is_td_paused", lambda self: False)
    _stub_thread_spawn(monkeypatch)

    _drain_runtime_events(rt)
    rt.start_turn("hello")

    events = _drain_runtime_events(rt)
    paused_hints = [
        (k, p) for k, p in events if k == EV_HINT and isinstance(p, dict) and p.get("kind") == "paused_td"
    ]
    assert paused_hints == [], f"expected no paused_td hints when TD is playing, got {paused_hints}"


def test_is_td_paused_returns_false_outside_td(monkeypatch):
    """When ``parent()`` is unavailable (test env, headless run),
    ``_is_td_paused`` must return False — never claim "paused" without
    proof. Otherwise CI / unit-test environments would emit phantom
    hints on every turn.
    """
    rt = _build_runtime(monkeypatch)
    # Outside TD, parent() raises NameError; the try/except returns False.
    assert rt._is_td_paused() is False


def test_paused_hint_does_not_block_normal_turn_flow(monkeypatch):
    """The paused-state check is a soft warning — it must NOT short-circuit
    start_turn. The worker still spawns; the model still gets the message.
    Otherwise users with TD paused (e.g. mid-debug) couldn't ask
    anything at all.
    """
    from tdpilot_api_runtime import AgentRuntime

    rt = _build_runtime(monkeypatch)
    rt._agent = _FakeAgent()  # type: ignore[assignment]

    monkeypatch.setattr(AgentRuntime, "_is_td_paused", lambda self: True)
    _stub_thread_spawn(monkeypatch)

    # start_turn must still return True (turn accepted) even though paused.
    ok = rt.start_turn("hello")
    assert ok is True, "start_turn must accept the turn even when TD is paused (warning is non-blocking)"
