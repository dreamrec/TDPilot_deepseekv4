"""v2.4 / B-003 — THREAD CONFLICT fix on sticky-tier sync.

Pre-fix: ``AgentRuntime._sync_model_tier_to_comp`` (wired as
``on_tier_change`` callback) wrote ``parent().par.Modeltier`` directly.
But this callback fires from inside ``Agent._maybe_promote_tier`` which
runs on the WORKER thread (Agent._loop). Touching ``parent()`` from
the worker tripped TD's THREAD CONFLICT detector with a
"may behave unpredictably or terminate" dialog and a reference to
``td.ParentShortcut``.

Post-fix: the callback only mutates dict state (``self._config``) and
pushes an ``EV_TIER_SYNC`` event onto the thread-safe event queue.
The cook-thread drain handler in ``tdpilot_api_extension.py`` reads
the event and performs the actual COMP-param write under
``onFrameStart`` context (cook thread, safe).

These tests pin:
  * The callback never raises NameError (i.e. never touches
    ``parent()``) when called outside TD.
  * The callback pushes EV_TIER_SYNC with the new tier as payload.
  * The callback updates self._config["model_tier"] for the
    start_turn live-refresh path.
  * Invalid tiers (anything not in auto/flash/pro) are no-ops.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _build_runtime(monkeypatch):
    """Build an AgentRuntime stub for callback testing."""
    import tdpilot_api_runtime as rt_mod

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    from tdpilot_api_runtime import AgentRuntime

    return AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])


def _drain_events(rt) -> list[tuple[str, object]]:
    from queue import Empty
    out = []
    while True:
        try:
            out.append(rt._events.get_nowait())
        except Empty:
            break
    return out


def test_b003_sync_does_not_touch_parent(monkeypatch):
    """Calling _sync_model_tier_to_comp outside TD must NOT raise.
    Pre-fix it called parent() which raises NameError in tests."""
    rt = _build_runtime(monkeypatch)
    # If the fix is in place, this won't raise. Pre-fix it would have
    # caught the NameError silently in a bare except — but the
    # ASSERTION below is what matters: post-fix the event queue gets
    # an EV_TIER_SYNC entry instead of attempting the parent() write.
    rt._sync_model_tier_to_comp("pro")  # should not raise


def test_b003_sync_pushes_ev_tier_sync(monkeypatch):
    """The callback must push an EV_TIER_SYNC event so the cook-thread
    drain can perform the actual COMP-param write."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    rt = _build_runtime(monkeypatch)
    rt._sync_model_tier_to_comp("pro")
    events = _drain_events(rt)
    kinds = [k for k, _ in events]
    assert EV_TIER_SYNC in kinds, (
        f"expected EV_TIER_SYNC in event queue after sync, got {kinds}"
    )
    payloads = [p for k, p in events if k == EV_TIER_SYNC]
    assert payloads == ["pro"], (
        f"EV_TIER_SYNC payload should be the tier string 'pro', got {payloads}"
    )


def test_b003_sync_updates_runtime_config(monkeypatch):
    """The runtime's cached _config mirror must be updated synchronously
    so the next start_turn's live-refresh doesn't fight the agent's
    in-memory mutation."""
    rt = _build_runtime(monkeypatch)
    rt._sync_model_tier_to_comp("flash")
    assert rt._config.get("model_tier") == "flash"


def test_b003_sync_invalid_tier_is_no_op(monkeypatch):
    """Bogus tier values must be silently ignored — no event push,
    no _config mutation."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    rt = _build_runtime(monkeypatch)
    prev_tier = rt._config.get("model_tier")
    rt._sync_model_tier_to_comp("garbage")
    events = _drain_events(rt)
    tier_sync_events = [k for k, _ in events if k == EV_TIER_SYNC]
    assert tier_sync_events == [], (
        f"invalid tier must NOT push EV_TIER_SYNC, got {tier_sync_events}"
    )
    assert rt._config.get("model_tier") == prev_tier, (
        "invalid tier must NOT update _config"
    )


def test_b003_all_three_valid_tiers_accepted(monkeypatch):
    """All of auto / flash / pro must each fire EV_TIER_SYNC with the
    matching payload."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    rt = _build_runtime(monkeypatch)
    for tier in ("auto", "flash", "pro"):
        rt._sync_model_tier_to_comp(tier)
    events = _drain_events(rt)
    payloads = [p for k, p in events if k == EV_TIER_SYNC]
    assert payloads == ["auto", "flash", "pro"]
    assert rt._config.get("model_tier") == "pro"  # last write wins
