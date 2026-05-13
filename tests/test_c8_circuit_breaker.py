"""v2.4 / Phase C.8 — CookThreadDispatcher circuit breaker.

When TD is paused mid-turn, the cook thread doesn't fire ``pump`` and
every queued tool call hangs to its full timeout. A 10-tool turn = 600s
of dead waiting. The breaker trips on the first timeout: cancel_pending
drains anything already queued, the tripped flag short-circuits
subsequent calls, and the on_breaker_trip callback lets the runtime
push an EV_HINT explaining the symptom.

Tests pin:
  * First call times out at the configured timeout (legacy behaviour).
  * Second call after a trip returns IMMEDIATELY with a clear error.
  * cancel_pending drains queued work as part of the trip.
  * on_breaker_trip callback fires once with a meaningful reason.
  * reset_breaker re-arms the dispatcher.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

from tdpilot_api_runtime import CookThreadDispatcher  # noqa: E402


def test_c8_first_timeout_returns_timeout_error():
    """Without pump running, the dispatcher's __call__ returns a clear
    timeout error after the configured window."""
    d = CookThreadDispatcher(lambda name, args: {"ok": True}, timeout=0.1)
    started = time.monotonic()
    result = d("td_get_info", {"path": "/project1"})
    elapsed = time.monotonic() - started
    assert "timed out" in result["error"].lower()
    assert 0.05 <= elapsed <= 0.6, f"expected ~0.1s, got {elapsed:.3f}s"


def test_c8_second_call_after_trip_returns_immediately():
    """After the breaker trips, subsequent __call__ entries return
    almost instantly (no wait) with a clear breaker-tripped error."""
    d = CookThreadDispatcher(lambda name, args: {"ok": True}, timeout=0.1)
    # First call → times out → trips breaker
    d("td_get_info", {"path": "/project1"})
    assert d._tripped is True

    # Second call MUST return without waiting the full timeout.
    started = time.monotonic()
    result = d("td_get_errors", {"path": "/project1"})
    elapsed = time.monotonic() - started
    assert "breaker" in result["error"].lower() or "suspended" in result["error"].lower()
    assert result.get("_tool_error") is True
    assert elapsed < 0.05, f"breaker-tripped call should be near-instant, got {elapsed:.3f}s"


def test_c8_on_breaker_trip_callback_fires_with_reason():
    """The runtime-installed callback receives a reason string when
    the breaker trips."""
    fired: list[str] = []
    d = CookThreadDispatcher(
        lambda name, args: {"ok": True},
        timeout=0.05,
        on_breaker_trip=fired.append,
    )
    d("td_get_info", {})
    assert len(fired) == 1, f"callback should fire exactly once on trip, got {len(fired)}"
    assert "td_get_info" in fired[0] or "timed out" in fired[0]


def test_c8_on_breaker_trip_callback_idempotent():
    """Subsequent calls after trip do NOT re-fire the callback —
    one trip = one diagnosis."""
    fired: list[str] = []
    d = CookThreadDispatcher(
        lambda name, args: {"ok": True},
        timeout=0.05,
        on_breaker_trip=fired.append,
    )
    d("td_get_info", {})
    d("td_get_errors", {})
    d("td_screenshot", {})
    assert len(fired) == 1, f"breaker should only fire callback once across multiple calls, got {len(fired)}"


def test_c8_reset_breaker_rearms_dispatcher():
    """After reset_breaker, a fresh call goes through the queue path
    again (not the fast-fail path)."""
    fired: list[str] = []
    d = CookThreadDispatcher(
        lambda name, args: {"ok": True},
        timeout=0.05,
        on_breaker_trip=fired.append,
    )
    d("td_get_info", {})
    assert d._tripped is True

    d.reset_breaker()
    assert d._tripped is False

    # After reset, a call where pump actually runs should succeed.
    # Simulate cook-thread pump by running pump in a sibling thread.
    def run_pump():
        time.sleep(0.02)
        d.pump()

    threading.Thread(target=run_pump, daemon=True).start()
    result = d("td_get_info", {"path": "/project1"})
    assert result.get("ok") is True, f"after reset, call should succeed when pumped: {result}"


def test_c8_cancel_pending_drains_on_trip():
    """When the breaker trips, anything queued (e.g., from another
    worker) gets drained with cancellation errors so those callers
    don't hang."""
    d = CookThreadDispatcher(lambda name, args: {"ok": True}, timeout=0.05)

    # Manually queue a fake pending call (simulates a second worker
    # that's already added to the queue but hasn't started waiting).
    import uuid

    call_id = uuid.uuid4().hex
    d._pending.put((call_id, "td_screenshot", {}))

    # Trigger trip via primary call
    d("td_get_info", {})

    # Pending queue should be empty (drained by cancel_pending_locked)
    assert d._pending.empty(), "trip should drain pending queue"
    # The drained call's result must be a cancellation error
    assert call_id in d._results
    assert "cancelled" in d._results[call_id]["error"]


def test_c8_normal_path_unaffected_by_breaker_machinery():
    """When pump runs in time, no trip happens and the call succeeds."""
    fired: list[str] = []
    d = CookThreadDispatcher(
        lambda name, args: {"ok": True, "echo": name},
        timeout=1.0,
        on_breaker_trip=fired.append,
    )

    def pump_soon():
        time.sleep(0.02)
        d.pump()

    threading.Thread(target=pump_soon, daemon=True).start()
    result = d("td_get_info", {"path": "/project1"})

    assert result == {"ok": True, "echo": "td_get_info"}
    assert d._tripped is False
    assert fired == []  # no trip → no callback


def test_c8_breaker_safe_when_callback_raises():
    """If the on_breaker_trip callback raises, the dispatcher must
    not propagate — it must still trip and return the error."""

    def bad_callback(reason):
        raise RuntimeError("simulated callback failure")

    d = CookThreadDispatcher(
        lambda name, args: {"ok": True},
        timeout=0.05,
        on_breaker_trip=bad_callback,
    )
    # This must NOT raise; the dispatcher's trip-locked must catch.
    result = d("td_get_info", {})
    assert "timed out" in result["error"]
    assert d._tripped is True
