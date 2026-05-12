"""v2.4 / B-005 — _run_safe survives TD module-reload class drift.

Symptom seen live: a controlled `CycleDetected` cycle abort produced
TWO error rows in chat instead of one, the second prefixed "Worker
crash:". Root cause: TD's textDAT module reload pattern can make
``CycleDetected`` (defined in tdpilot_api_cycle_detector and inheriting
from ``tdpilot_api_agent.AgentError``) appear as a "foreign" class to
``_run_safe`` if ``tdpilot_api_agent`` was reloaded between
the two modules' load times. The ``isinstance(exc, AgentError)``
check silently fails and the fallback ``except Exception`` branch
fires with the alarming "Worker crash:" prefix.

Fix: name-match agent-side controlled exception types
("AgentError", "TurnBudgetExceeded", "CycleDetected") in the fallback
branch so they still get the quiet idle marker, NOT the user-visible
crash event.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _build_runtime_with_failing_agent(monkeypatch, failure):
    """Build an AgentRuntime stub whose Agent.run_turn raises ``failure``."""
    import tdpilot_api_runtime as rt_mod
    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    from tdpilot_api_runtime import AgentRuntime

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])

    class _StubAgent:
        def run_turn(self_inner):
            raise failure
        def stop(self_inner): pass
        def reset(self_inner): pass
        def clear_stop(self_inner): pass

    rt._agent = _StubAgent()
    return rt


def _drain_events(rt) -> list[tuple[str, object]]:
    from queue import Empty
    out = []
    while True:
        try:
            out.append(rt._events.get_nowait())
        except Empty:
            break
    return out


def test_b005_real_AgentError_caught_quietly(monkeypatch):
    """A genuine AgentError instance is caught by ``except AgentError``
    and only pushes EV_STATE=idle — no EV_ERROR (Agent already emitted)."""
    from tdpilot_api_agent import AgentError
    from tdpilot_api_runtime import EV_ERROR, EV_STATE

    rt = _build_runtime_with_failing_agent(monkeypatch, AgentError("oops"))
    rt._run_safe()
    events = _drain_events(rt)
    kinds = [k for k, _ in events]
    assert EV_ERROR not in kinds, (
        f"AgentError must NOT push a second EV_ERROR (already emitted by Agent), "
        f"got events: {kinds}"
    )
    assert EV_STATE in kinds
    state_payloads = [p for k, p in events if k == EV_STATE]
    assert "idle" in state_payloads


def test_b005_module_reload_impersonator_caught_by_name(monkeypatch):
    """Simulate a CycleDetected from a 'foreign' module — same NAME but
    no inheritance from this module's AgentError. Pre-fix would fall
    through to ``except Exception`` and push "Worker crash:". Post-fix
    the name-match branch catches it."""
    from tdpilot_api_runtime import EV_ERROR, EV_STATE

    # Construct a synthetic CycleDetected that is NOT a subclass of the
    # AgentError currently imported by runtime — mimics the TD module-
    # reload class-identity drift.
    class CycleDetected(Exception):
        def __init__(self):
            super().__init__("Cycle detected: foo ×3 with identical args")

    rt = _build_runtime_with_failing_agent(monkeypatch, CycleDetected())
    rt._run_safe()
    events = _drain_events(rt)
    # CRITICAL: no "Worker crash:" EV_ERROR push.
    error_msgs = [p for k, p in events if k == EV_ERROR]
    assert not any("Worker crash" in str(m) for m in error_msgs), (
        f"name-matched CycleDetected must NOT produce 'Worker crash:' "
        f"event, got: {error_msgs}"
    )
    # State must still go to idle.
    state_payloads = [p for k, p in events if k == EV_STATE]
    assert "idle" in state_payloads


def test_b005_module_reload_impersonator_AgentError_also_caught(monkeypatch):
    """Same defence applies to AgentError + TurnBudgetExceeded by name —
    a hypothetical 'foreign' AgentError must not produce 'Worker crash'."""
    from tdpilot_api_runtime import EV_ERROR

    class AgentError(Exception):  # different class from the runtime's AgentError
        pass

    rt = _build_runtime_with_failing_agent(monkeypatch, AgentError("foreign"))
    rt._run_safe()
    events = _drain_events(rt)
    error_msgs = [p for k, p in events if k == EV_ERROR]
    assert not any("Worker crash" in str(m) for m in error_msgs)


def test_b005_genuine_unexpected_exception_still_surfaces_as_crash(monkeypatch):
    """A real, unexpected exception (ValueError, RuntimeError, etc.)
    still gets the 'Worker crash:' prefix so we don't lose visibility
    on actual bugs."""
    from tdpilot_api_runtime import EV_ERROR

    rt = _build_runtime_with_failing_agent(monkeypatch, ValueError("real bug"))
    rt._run_safe()
    events = _drain_events(rt)
    error_msgs = [p for k, p in events if k == EV_ERROR]
    assert any("Worker crash" in str(m) and "ValueError" in str(m) for m in error_msgs), (
        f"genuine unexpected exception MUST still emit 'Worker crash:' "
        f"so real bugs stay visible. Got: {error_msgs}"
    )
