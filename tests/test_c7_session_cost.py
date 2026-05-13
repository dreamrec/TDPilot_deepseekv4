"""v2.4 / Phase C.7 — per-session cost tracking.

Tests pin:
  * Session accumulators start at 0 and stamp started_at on construction.
  * _handle_usage accumulates input/output tokens.
  * Cache classification: read>0 → hit; input>0 AND read==0 → miss.
  * EV_USAGE_SESSION fires after each EV_USAGE with full payload.
  * _session_totals_payload shape matches the schema /stats returns.
  * reset() zeros all accumulators + refreshes started_at.
  * USD estimate is conservative (assumes uncached input).
"""

from __future__ import annotations

import sys
from pathlib import Path
from queue import Empty

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _build_runtime(monkeypatch):
    """Build an AgentRuntime with stub Agent so we can exercise the
    cost-tracking machinery without TD or DeepSeek wiring."""
    import tdpilot_api_runtime as rt_mod

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    from tdpilot_api_runtime import AgentRuntime

    return AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])


def _drain_events(rt) -> list[tuple[str, object]]:
    events = []
    while True:
        try:
            events.append(rt._events.get_nowait())
        except Empty:
            break
    return events


def test_c7_session_accumulators_start_at_zero(monkeypatch):
    rt = _build_runtime(monkeypatch)
    assert rt._session_input_tokens == 0
    assert rt._session_output_tokens == 0
    assert rt._session_cache_hits == 0
    assert rt._session_cache_misses == 0
    assert isinstance(rt._session_started_iso, str) and rt._session_started_iso


def test_c7_handle_usage_accumulates(monkeypatch):
    """Three EV_USAGE calls → totals are the sum."""
    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0})
    rt._handle_usage({"input_tokens": 200, "output_tokens": 75, "cache_read_input_tokens": 180})
    rt._handle_usage({"input_tokens": 300, "output_tokens": 100, "cache_read_input_tokens": 250})
    assert rt._session_input_tokens == 600
    assert rt._session_output_tokens == 225


def test_c7_cache_hit_classification(monkeypatch):
    """cache_read_input_tokens > 0 → counts as hit."""
    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 80})
    rt._handle_usage({"input_tokens": 200, "output_tokens": 75, "cache_read_input_tokens": 180})
    assert rt._session_cache_hits == 2
    assert rt._session_cache_misses == 0


def test_c7_cache_miss_classification(monkeypatch):
    """input_tokens > 0 AND cache_read_input_tokens == 0 → counts as miss."""
    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0})
    rt._handle_usage({"input_tokens": 200, "output_tokens": 75, "cache_read_input_tokens": 0})
    assert rt._session_cache_hits == 0
    assert rt._session_cache_misses == 2


def test_c7_zero_input_keepalive_not_classified(monkeypatch):
    """A keep-alive with input=0 and read=0 doesn't bump either counter."""
    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0})
    assert rt._session_cache_hits == 0
    assert rt._session_cache_misses == 0


def test_c7_ev_usage_session_pushed_after_each_call(monkeypatch):
    """Every EV_USAGE → EV_USAGE_SESSION push with rolling totals."""
    from tdpilot_api_runtime import EV_USAGE, EV_USAGE_SESSION

    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 0})
    rt._handle_usage({"input_tokens": 200, "output_tokens": 75, "cache_read_input_tokens": 180})

    events = _drain_events(rt)
    kinds = [k for k, _ in events]
    # Each handle → one EV_USAGE + one EV_USAGE_SESSION
    assert kinds.count(EV_USAGE) == 2
    assert kinds.count(EV_USAGE_SESSION) == 2

    # Latest EV_USAGE_SESSION should reflect the cumulative totals.
    session_payloads = [p for k, p in events if k == EV_USAGE_SESSION]
    assert session_payloads[-1]["input_tokens"] == 300
    assert session_payloads[-1]["output_tokens"] == 125
    assert session_payloads[-1]["cache_hits"] == 1
    assert session_payloads[-1]["cache_misses"] == 1


def test_c7_session_totals_payload_shape(monkeypatch):
    """_session_totals_payload returns the contract shape /stats serves."""
    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 1000, "output_tokens": 500, "cache_read_input_tokens": 0})
    payload = rt._session_totals_payload()

    required_keys = {
        "input_tokens",
        "output_tokens",
        "cache_hits",
        "cache_misses",
        "approx_usd",
        "started_at",
        "model_pricing_version",
    }
    assert required_keys.issubset(payload.keys()), f"missing keys: {required_keys - set(payload.keys())}"
    assert isinstance(payload["approx_usd"], float)
    assert payload["approx_usd"] > 0  # 1000 input + 500 output > 0
    assert payload["model_pricing_version"]  # non-empty


def test_c7_reset_zeros_accumulators(monkeypatch):
    """reset() returns all four counters to 0 + refreshes started_at."""
    import time

    rt = _build_runtime(monkeypatch)
    rt._handle_usage({"input_tokens": 500, "output_tokens": 200, "cache_read_input_tokens": 0})
    assert rt._session_input_tokens == 500
    started_before = rt._session_started_iso

    # Tiny sleep so the timestamp changes (iso resolution is microseconds).
    time.sleep(0.001)
    rt.reset()

    assert rt._session_input_tokens == 0
    assert rt._session_output_tokens == 0
    assert rt._session_cache_hits == 0
    assert rt._session_cache_misses == 0
    assert rt._session_started_iso > started_before, (
        f"started_at must refresh on reset; was {started_before}, now {rt._session_started_iso}"
    )


def test_c7_estimate_usd_is_positive_for_real_workload(monkeypatch):
    """Sanity check the USD estimator: 1M input + 500K output ≈ $0.27 + $0.55."""
    from tdpilot_api_runtime import _PRICE_INPUT_FRESH_PER_M, _PRICE_OUTPUT_PER_M, _estimate_usd

    cost = _estimate_usd(1_000_000, 500_000, cache_hits=0)
    expected = _PRICE_INPUT_FRESH_PER_M + (_PRICE_OUTPUT_PER_M / 2)
    assert abs(cost - expected) < 0.001, (
        f"estimate diverged from constants — got {cost}, expected ~{expected}"
    )


def test_c7_estimate_usd_zero_for_empty_session(monkeypatch):
    """Empty session → 0.0 (no false-positive cost)."""
    from tdpilot_api_runtime import _estimate_usd

    assert _estimate_usd(0, 0, cache_hits=0) == 0.0
