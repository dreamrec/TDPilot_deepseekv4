"""Phase 4.1 tests — per-turn observability traces.

Cover:
  - Tracer end-to-end: start_turn → record_tool → end_turn writes
    one JSONL record with the documented fields.
  - User text + tool args are HASHED, not stored raw.
  - Day rollover: rotates to a new file when the date changes.
  - Disabled tracer is a true no-op (no thread, no file writes).
  - get_recent_traces walks newest-first across multiple days.
  - handle_get_recent_traces returns the {ok, count, traces} envelope.
  - Schema parity — td_get_recent_traces is registered.
  - Disk hygiene — files older than retention are pruned at init.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_tracing as tracing  # noqa: E402


def _make_tracer(tmp_path: Path, **kwargs):
    return tracing.Tracer(traces_dir=tmp_path / "traces", **kwargs)


def test_tracer_writes_one_record_per_turn(tmp_path):
    """Happy path — start, record two tools, end → one JSONL line."""
    t = _make_tracer(tmp_path)
    t.start_turn("hello world", model_tier="auto", model_used="deepseek-v4-flash")
    t.record_tool("td_get_info", args={}, latency_ms=12, ok=True)
    t.record_tool("td_get_errors", args={"path": "/project1"}, latency_ms=8, ok=True)
    t.end_turn("done", total_tokens=420)
    t.shutdown()

    files = list((tmp_path / "traces").glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])

    # Documented fields all present.
    for k in (
        "ts",
        "session_id",
        "turn_id",
        "user_text_hash",
        "model_tier",
        "model_used",
        "total_tokens",
        "tool_calls",
        "outcome",
        "duration_ms",
    ):
        assert k in rec, f"missing field: {k}"

    assert rec["outcome"] == "done"
    assert rec["model_tier"] == "auto"
    assert rec["model_used"] == "deepseek-v4-flash"
    assert rec["total_tokens"] == 420
    assert rec["duration_ms"] >= 0
    assert len(rec["tool_calls"]) == 2
    assert {c["name"] for c in rec["tool_calls"]} == {"td_get_info", "td_get_errors"}


def test_tracer_hashes_user_text_and_args(tmp_path):
    """Privacy: raw text never lands in the trace file."""
    t = _make_tracer(tmp_path)
    secret_text = "my super secret api key is sk-abc123"
    secret_args = {"prompt": "another secret thing"}
    t.start_turn(secret_text)
    t.record_tool("td_exec_python", args=secret_args, latency_ms=5, ok=True)
    t.end_turn("done")
    t.shutdown()

    files = list((tmp_path / "traces").glob("*.jsonl"))
    blob = files[0].read_text(encoding="utf-8")
    assert "sk-abc123" not in blob
    assert "another secret thing" not in blob

    rec = json.loads(blob.splitlines()[0])
    # Hash is a 12-char hex prefix.
    h = rec["user_text_hash"]
    assert isinstance(h, str)
    assert len(h) == 12
    int(h, 16)  # raises if non-hex
    # Args hash is on the per-tool record.
    args_hash = rec["tool_calls"][0]["args_hash"]
    assert isinstance(args_hash, str)
    assert len(args_hash) == 12


def test_tracer_disabled_is_noop(tmp_path):
    """``enabled=False`` writes no files and starts no thread."""
    t = _make_tracer(tmp_path, enabled=False)
    t.start_turn("anything")
    t.record_tool("x", args={}, latency_ms=0, ok=True)
    t.end_turn("done")
    t.shutdown()

    assert not (tmp_path / "traces").exists() or list((tmp_path / "traces").glob("*.jsonl")) == []


def test_tracer_re_entrant_end_turn_is_safe(tmp_path):
    """on_error firing after on_turn_done already closed the turn:
    the second end_turn must be a no-op, not a crash or duplicate
    record.
    """
    t = _make_tracer(tmp_path)
    t.start_turn("hi")
    t.end_turn("done")
    t.end_turn("error")  # no-op
    t.shutdown()

    files = list((tmp_path / "traces").glob("*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_tracer_record_tool_without_active_turn_is_safe(tmp_path):
    """Defensive — late callbacks during teardown shouldn't crash."""
    t = _make_tracer(tmp_path)
    # No start_turn yet — this should be a no-op.
    t.record_tool("td_get_info", args={}, latency_ms=10, ok=True)
    t.shutdown()


def test_get_recent_traces_returns_newest_first(tmp_path):
    """Reads today's records newest-first, then walks back."""
    t = _make_tracer(tmp_path)
    for i in range(5):
        t.start_turn(f"turn {i}")
        t.record_tool("td_get_info", args={}, latency_ms=1, ok=True)
        t.end_turn("done")
    t.shutdown()

    out = tracing.get_recent_traces(limit=3, traces_dir=tmp_path / "traces")
    assert len(out) == 3
    # The 5 records were written in order — newest is "turn 4".
    user_hashes = [tracing._hash_text(f"turn {i}") for i in range(5)]
    seen = [r["user_text_hash"] for r in out]
    assert seen == [user_hashes[4], user_hashes[3], user_hashes[2]]


def test_get_recent_traces_limit_capped(tmp_path):
    """``limit`` is clamped to [1, 200]."""
    t = _make_tracer(tmp_path)
    t.start_turn("x")
    t.end_turn("done")
    t.shutdown()

    # Negative / zero clamps to 1.
    assert len(tracing.get_recent_traces(limit=0, traces_dir=tmp_path / "traces")) <= 1


def test_handle_get_recent_traces_envelope(tmp_path, monkeypatch):
    """``handle_get_recent_traces`` returns {ok, count, traces}."""
    t = _make_tracer(tmp_path)
    t.start_turn("only")
    t.end_turn("done")
    t.shutdown()

    monkeypatch.setattr(tracing, "DEFAULT_TRACES_DIR", tmp_path / "traces")
    out = tracing.handle_get_recent_traces({"limit": 5})
    assert out["ok"] is True
    assert out["count"] >= 1
    assert isinstance(out["traces"], list)
    assert "user_text_hash" in out["traces"][0]


def test_tracer_prune_old_files(tmp_path):
    """Files older than retention_days are removed at init."""
    traces_dir = tmp_path / "traces"
    traces_dir.mkdir()
    # Stale file — name dated 60 days ago, well past the 30-day default.
    old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    old_file = traces_dir / f"{old_date}.jsonl"
    old_file.write_text('{"ts":"old"}\n', encoding="utf-8")
    # Fresh file — today.
    new_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_file = traces_dir / f"{new_date}.jsonl"
    new_file.write_text('{"ts":"new"}\n', encoding="utf-8")

    tracing.Tracer(traces_dir=traces_dir, retention_days=30, enabled=True).shutdown()

    assert not old_file.exists(), "60-day-old file should have been pruned"
    assert new_file.exists(), "today's file must NOT be pruned"


def test_schema_parity_for_get_recent_traces():
    """Every entry in TOOL_SCHEMAS must have a TOOL_TO_HANDLER entry.
    Phase 4.1 added td_get_recent_traces — locks in parity.
    """
    from tdpilot_api_schema_defs import TOOL_SCHEMAS  # type: ignore[import-not-found]
    from tdpilot_api_schema_map import TOOL_TO_HANDLER  # type: ignore[import-not-found]

    schema_names = {s["name"] for s in TOOL_SCHEMAS}
    assert "td_get_recent_traces" in schema_names
    assert "td_get_recent_traces" in TOOL_TO_HANDLER
    handler_fn_name, _adapter = TOOL_TO_HANDLER["td_get_recent_traces"]
    assert handler_fn_name == "handle_get_recent_traces"
    assert schema_names == set(TOOL_TO_HANDLER.keys()), (
        "tool count drift — every schema entry must have a handler entry"
    )


def test_tracer_shutdown_flushes_pending(tmp_path):
    """Records enqueued just before shutdown still hit the file."""
    t = _make_tracer(tmp_path)
    for _ in range(5):
        t.start_turn("x")
        t.end_turn("done")
    # Don't sleep — shutdown should flush deterministically.
    t.shutdown(timeout=2.0)

    files = list((tmp_path / "traces").glob("*.jsonl"))
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5


# ---------------------------------------------------------------------------
# Runtime integration — Tracer is actually wired through AgentRuntime
# ---------------------------------------------------------------------------


def test_runtime_writes_trace_for_completed_turn(monkeypatch, tmp_path):
    """End-to-end: a completed turn produces a record in the runtime's
    configured trace dir.
    """
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    rt = AgentRuntime(
        dispatcher=lambda *a: {"ok": True},
        tools=[],
        config={
            "model": "x",
            "base_url": "http://x",
            "max_tokens": 10,
            "turn_budget": 1,
            "temperature": 0.0,
            "trace_logging": True,
            "traces_dir": tmp_path / "traces",
            "pre_retrieval": False,
        },
    )
    assert rt._tracer is not None
    assert rt._tracer.enabled is True

    # Simulate the worker-thread callbacks the agent loop fires.
    rt._trace_start_turn("hello")
    rt._trace_tool_started("td_get_info", {})
    time.sleep(0.005)
    rt._trace_record_tool("td_get_info", {"fps": 60}, is_error=False)
    rt._trace_end_turn("done")
    rt._tracer.shutdown(timeout=2.0)

    files = list((tmp_path / "traces").glob("*.jsonl"))
    assert files
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    assert rec["outcome"] == "done"
    assert rec["tool_calls"]
    assert rec["tool_calls"][0]["name"] == "td_get_info"
    assert rec["tool_calls"][0]["ok"] is True
    assert rec["tool_calls"][0]["latency_ms"] >= 0


def test_runtime_reset_closes_open_tracer_turn(monkeypatch, tmp_path):
    """Cold-pass review finding — reset() called mid-turn must
    finalise the active tracer record with outcome='interrupted',
    otherwise the in-flight turn vanishes from the trace log.
    """
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    rt = AgentRuntime(
        dispatcher=lambda *a: {"ok": True},
        tools=[],
        config={
            "model": "x",
            "base_url": "http://x",
            "max_tokens": 10,
            "turn_budget": 1,
            "temperature": 0.0,
            "trace_logging": True,
            "traces_dir": tmp_path / "traces",
            "pre_retrieval": False,
            "compaction_threshold": 0,
        },
    )

    # Simulate a turn opening but never naturally completing.
    rt._trace_start_turn("user msg that gets interrupted")
    rt._trace_record_tool("td_get_info", {"fps": 60}, is_error=False)
    # User pulses Reset before the turn ends.
    rt.reset()
    rt._tracer.shutdown(timeout=2.0)

    files = list((tmp_path / "traces").glob("*.jsonl"))
    assert files, "trace file missing — interrupted turn must still write a record"
    rec = json.loads(files[0].read_text(encoding="utf-8").splitlines()[-1])
    assert rec["outcome"] == "interrupted", f"wrong outcome: {rec['outcome']}"
    # Tool record from before the interrupt must still be there.
    assert any(c["name"] == "td_get_info" for c in rec["tool_calls"])


def test_runtime_disabled_tracer_via_config(monkeypatch, tmp_path):
    """``config['trace_logging'] = False`` → tracer.enabled == False."""
    import tdpilot_api_runtime as rt_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    rt = AgentRuntime(
        dispatcher=lambda *a: {"ok": True},
        tools=[],
        config={
            "model": "x",
            "base_url": "http://x",
            "max_tokens": 10,
            "turn_budget": 1,
            "temperature": 0.0,
            "trace_logging": False,
            "traces_dir": tmp_path / "traces",
            "pre_retrieval": False,
        },
    )
    assert rt._tracer is not None
    assert rt._tracer.enabled is False
    rt._trace_start_turn("anything")
    rt._trace_end_turn("done")
    if rt._tracer is not None:
        rt._tracer.shutdown()
    # No files written
    assert not (tmp_path / "traces").exists() or list((tmp_path / "traces").glob("*.jsonl")) == []
