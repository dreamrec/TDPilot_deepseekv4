"""v2.4 / B-009 — false 'idle (timeout)' fix.

Live-debug 2026-05-13: user observed the chat panel showing
'idle (timeout)' while the agent was clearly still working (tool
calls streaming into the chat history). Root cause:

  * urllib.request.urlopen blocks the worker thread for the FULL
    duration of a DeepSeek API call (no streaming on this code path).
  * Pro extended-thinking turns routinely take 60-180s for a single
    /v1/messages round trip.
  * The frontend's activity watchdog (TURN_END_SAFETY_MS) was 90s and
    only re-armed on incoming WS events — none arrive during a urlopen.
  * After 90s of silence the JS timer fired ``idle (timeout)``, even
    though the runtime was alive and tool calls would start streaming
    seconds later.

Three-layer fix:

  Frontend (tdpilot_api_chat.html):
    * TURN_END_SAFETY_MS: 90000 → 240000 (covers all observed pro
      single-call durations).
    * applyMessage(): treat tool_call / tool_result / append as
      unambiguous proof-of-life; if a false timeout already fired,
      restore the working state (set awaitingTurnEnd back to true,
      re-arm watchdog) instead of leaving the user staring at
      'idle (timeout)' while tool calls stream visibly.

  Agent backend (tdpilot_api_agent.py):
    * New ``on_heartbeat`` callback + ``heartbeat_interval`` config.
    * Wrap urlopen with a daemon heartbeat thread that pulses
      on_heartbeat every heartbeat_interval seconds while the call
      is pending. Stops on the finally branch regardless of exit.

  Runtime wiring (tdpilot_api_runtime.py):
    * on_heartbeat=lambda: self._push(EV_STATE, "thinking")
    * Re-uses the EV_STATE event path → broadcasts a status event
      over WS → frontend re-arms watchdog.

These tests pin the agent / runtime contract. The frontend pieces
are tested manually by sending a "Build kaleidoscope feedback loop"
prompt and watching the badge stay populated.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _make_agent(**kwargs):
    from tdpilot_api_agent import Agent

    base = {
        "api_key": "sk-fake",
        "dispatcher": lambda *a, **k: {"ok": True},
        "tools": [],
        "system_prompt": "test",
    }
    base.update(kwargs)
    return Agent(**base)


# =====================================================================
# Agent surface — heartbeat callback + interval clamping
# =====================================================================


def test_b009_default_on_heartbeat_is_noop():
    """Default ``on_heartbeat`` is a no-op so tests + offline imports
    don't need TD bindings. Calling it must not raise."""
    a = _make_agent()
    # Just invoke — no side effects expected from the _noop default.
    a.on_heartbeat()


def test_b009_heartbeat_interval_default_is_30s():
    """30s is well inside the bumped JS watchdog (240s) and gives
    ~6 heartbeats during the longest observed pro thinking turn."""
    a = _make_agent()
    assert a.heartbeat_interval == 30.0


def test_b009_heartbeat_interval_clamped_to_min_1s():
    """A misconfigured COMP param (e.g. 0.001) would otherwise spawn
    a heartbeat-storm thread. Clamp prevents that."""
    a = _make_agent(heartbeat_interval=0.001)
    assert a.heartbeat_interval == 1.0


def test_b009_heartbeat_interval_accepts_custom_value():
    """Custom intervals between 1.0 and reasonable upper bounds pass
    through verbatim — only the lower clamp triggers."""
    a = _make_agent(heartbeat_interval=15.0)
    assert a.heartbeat_interval == 15.0


def test_b009_on_heartbeat_callable_stored_on_agent():
    """The injected callable is stored on self.on_heartbeat so the
    urlopen wrapper can invoke it from the heartbeat thread."""
    calls: list[float] = []
    a = _make_agent(on_heartbeat=lambda: calls.append(time.monotonic()))
    # Manual invocation simulates what the heartbeat thread does.
    a.on_heartbeat()
    a.on_heartbeat()
    assert len(calls) == 2


# =====================================================================
# Heartbeat thread behavior — pulse during blocking call, stop on exit
# =====================================================================


def test_b009_heartbeat_thread_pulses_while_blocked():
    """Simulate the urlopen-wrap structure: spin a heartbeat thread,
    sleep briefly (mimics a pending API call), confirm the thread
    fired the callback at least once before we set stop, then verify
    it stops after stop_event is set.

    This isolates the threading + Event-stop pattern in the agent's
    _call_anthropic without needing to mock urllib.
    """
    fires: list[float] = []
    stop = threading.Event()

    def _pulse():
        while not stop.wait(0.05):  # 50ms cadence for the test
            fires.append(time.monotonic())

    t = threading.Thread(target=_pulse, daemon=True)
    t.start()
    try:
        time.sleep(0.25)  # ≥ 4 cadences
    finally:
        stop.set()
    t.join(timeout=1.0)

    assert len(fires) >= 2, (
        f"heartbeat must pulse at least twice during a 250ms blocking "
        f"call with 50ms cadence — got {len(fires)} fires"
    )
    assert not t.is_alive(), "thread must terminate cleanly on stop"


def test_b009_heartbeat_stops_when_call_returns():
    """The stop_event must reliably halt the heartbeat thread even if
    the cadence is long — daemon=True is a defence in depth, but the
    primary stop path is the Event."""
    fires: list[float] = []
    stop = threading.Event()

    def _pulse():
        while not stop.wait(10.0):  # very long cadence
            fires.append(time.monotonic())

    t = threading.Thread(target=_pulse, daemon=True)
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(timeout=1.5)
    assert not t.is_alive(), "stop_event must wake the long wait"
    # Zero pulses because the 10s cadence never elapsed.
    assert fires == [], "no pulses expected when stop_event is set before the first cadence"


def test_b009_heartbeat_callback_exception_does_not_crash_thread():
    """If on_heartbeat raises, the worker thread must keep going so a
    transient WS broadcast failure (TD event-queue full, etc.) doesn't
    leave the API call without further pulses."""
    fires: list[int] = []
    stop = threading.Event()

    def _pulse():
        # Mirror the agent's exception-swallowing structure.
        while not stop.wait(0.05):
            try:
                fires.append(1)
                if len(fires) == 1:
                    raise RuntimeError("simulated WS broadcast fail")
            except Exception:
                pass

    t = threading.Thread(target=_pulse, daemon=True)
    t.start()
    try:
        time.sleep(0.25)
    finally:
        stop.set()
    t.join(timeout=1.0)

    assert len(fires) >= 2, (
        f"heartbeat must survive callback exception and continue pulsing — got {len(fires)} fires"
    )


# =====================================================================
# Runtime wiring — on_heartbeat must push EV_STATE("thinking")
# =====================================================================


def test_b009_runtime_wires_on_heartbeat_to_ev_state_thinking(monkeypatch):
    """The runtime's _build_agent must inject an on_heartbeat that
    pushes EV_STATE("thinking") onto the event queue. This is what
    re-arms the frontend watchdog during a long urlopen."""
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import EV_STATE, AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    rt._build_agent()
    assert rt._agent is not None
    # Invoke the wired heartbeat — must push EV_STATE("thinking").
    from queue import Empty

    # Drain any pre-existing events first.
    while True:
        try:
            rt._events.get_nowait()
        except Empty:
            break
    rt._agent.on_heartbeat()
    # Now the queue should have exactly one event: EV_STATE("thinking").
    drained = []
    while True:
        try:
            drained.append(rt._events.get_nowait())
        except Empty:
            break
    assert len(drained) == 1, f"heartbeat must push exactly one event, got: {drained}"
    kind, payload = drained[0]
    assert kind == EV_STATE
    assert payload == "thinking"
