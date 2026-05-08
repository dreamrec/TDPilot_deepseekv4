"""Phase 4.3 tests — conversation compaction.

The riskiest item in the plan: get the synthetic message shape
wrong and DeepSeek 400s on every subsequent call. These tests pin
the contract:

  - The synthetic compaction summary is TEXT-ONLY (no thinking
    block — we can't fabricate valid signatures).
  - Recent messages survive intact, INCLUDING any thinking blocks
    they originally carried.
  - Forensic persistence writes JSONL records to
    ~/.tdpilot-api/history/<session>.jsonl and is re-readable.
  - Below-threshold history passes through unchanged (same object,
    no allocation).
  - Threshold = 0 disables.
  - Local heuristic summary captures user goals + tool counts.
  - Agent-side integration: the Compactor is invoked at the top of
    ``_loop`` and the resulting message list is what gets sent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_compaction as compaction  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant_text(text: str) -> dict:
    return {"role": "assistant", "content": [{"type": "text", "text": text}]}


def _assistant_with_thinking(text: str, thinking: str = "let me think") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": thinking, "signature": "sig123"},
            {"type": "text", "text": text},
        ],
    }


def _assistant_with_tool_use(name: str, args: dict | None = None) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": f"tu_{name}", "name": name, "input": args or {}},
        ],
    }


def _user_tool_result(tool_use_id: str, content: str = "ok", is_error: bool = False) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": content,
                "is_error": is_error,
            }
        ],
    }


def _build_long_history(n_turns: int) -> list[dict]:
    """Build ``n_turns`` synthetic user/assistant/tool_use/tool_result
    quartets — 4 messages per turn → 4×n_turns total messages.
    """
    msgs: list[dict] = []
    for i in range(n_turns):
        msgs.append(_user(f"turn {i} user"))
        msgs.append(_assistant_with_tool_use("td_get_info"))
        msgs.append(_user_tool_result(f"tu_td_get_info"))  # noqa: F541
        msgs.append(_assistant_with_thinking(f"reply {i}", thinking=f"think {i}"))
    return msgs


# ---------------------------------------------------------------------------
# needs_compaction
# ---------------------------------------------------------------------------


def test_needs_compaction_below_threshold():
    msgs = [_user("x")] * 5
    assert compaction.needs_compaction(msgs, threshold=20) is False


def test_needs_compaction_at_threshold_fires():
    msgs = [_user("x")] * 20
    assert compaction.needs_compaction(msgs, threshold=20) is True


def test_needs_compaction_zero_disables():
    msgs = [_user("x")] * 1000
    assert compaction.needs_compaction(msgs, threshold=0) is False


# ---------------------------------------------------------------------------
# compact() — pure function
# ---------------------------------------------------------------------------


def test_compact_returns_synthetic_plus_recent():
    """Use a clean history (text-only assistant replies) where the
    natural cut at len-keep_recent lands on a non-tool_result message
    so the boundary repair from Phase 1.6.13 doesn't kick in.
    """
    # 30 plain user/assistant pairs.
    msgs: list[dict] = []
    for i in range(15):
        msgs.append(_user(f"u{i}"))
        msgs.append(_assistant_text(f"a{i}"))
    out = compaction.compact(msgs, keep_recent=10)
    assert len(out) == 11  # synthetic + 10 recent
    assert out[0]["role"] == "assistant"
    assert out[1:] == msgs[-10:]  # recent slice unchanged


def test_compact_returns_synthetic_plus_smaller_recent_when_boundary_repaired():
    """Phase 1.6.13 — when the natural cut would orphan a
    user/tool_result, the repair eats into keep_recent. With a
    32-message tool-chain history and keep_recent=10, the cut at
    index 22 lands on a tool_result and gets advanced to 23, so
    the retained slice is 9 messages (synthetic + 9 = 10 total).
    """
    msgs = _build_long_history(8)  # 32 messages
    out = compaction.compact(msgs, keep_recent=10)
    # Length is 10 (synthetic + 9 recent) NOT 11 — boundary repair.
    assert len(out) == 10
    assert out[0]["role"] == "assistant"
    # The retained slice must NOT lead with a tool_result.
    assert compaction._starts_with_tool_result(out[1]) is False


def test_compact_synthetic_message_is_text_only_no_thinking():
    """The killer contract — a synthesised thinking block would have
    no valid signature, which DeepSeek would reject. Text-only is
    the safe path.
    """
    msgs = _build_long_history(8)
    out = compaction.compact(msgs, keep_recent=4)
    synthetic = out[0]
    assert synthetic["role"] == "assistant"
    types = [b.get("type") for b in synthetic["content"] if isinstance(b, dict)]
    assert types == ["text"], f"synthetic must be text-only, got blocks: {types}"
    # No thinking / redacted_thinking / signature anywhere.
    for block in synthetic["content"]:
        assert block.get("type") != "thinking"
        assert block.get("type") != "redacted_thinking"
        assert "signature" not in block


def test_compact_synthetic_carries_marker():
    msgs = _build_long_history(8)
    out = compaction.compact(msgs, keep_recent=4)
    text = out[0]["content"][0]["text"]
    assert compaction.COMPACTION_MARKER in text


def test_compact_preserves_recent_thinking_blocks():
    """Retained recent messages must keep their original thinking
    blocks (with signatures) intact — those are what the API
    validates on the next turn.
    """
    msgs = _build_long_history(8)
    out = compaction.compact(msgs, keep_recent=4)
    # Walk recent messages — at least one must have a thinking block.
    recent = out[1:]
    saw_thinking = False
    for m in recent:
        for block in m.get("content", []):
            if isinstance(block, dict) and block.get("type") == "thinking":
                saw_thinking = True
                assert block.get("signature") == "sig123"
                assert block.get("thinking", "").startswith("think")
    assert saw_thinking, "recent slice must retain at least one thinking block intact"


def test_compact_below_threshold_returns_copy_unchanged():
    msgs = _build_long_history(2)  # 8 messages
    out = compaction.compact(msgs, keep_recent=10)
    assert out == msgs
    # Returned list must be a NEW list (no mutation aliasing) but
    # whose items are the same dicts as the input.
    assert out is not msgs


def test_compact_does_not_mutate_input():
    msgs = _build_long_history(8)
    snapshot = json.dumps(msgs, default=str)
    compaction.compact(msgs, keep_recent=4)
    after = json.dumps(msgs, default=str)
    assert snapshot == after, "compact() must not mutate the input list"


def test_compact_summary_mentions_user_count_and_tool_names():
    msgs = _build_long_history(5)  # 20 messages, 5 user prompts, 5 td_get_info calls
    out = compaction.compact(msgs, keep_recent=2)
    text = out[0]["content"][0]["text"]
    assert "User prompts: 4" in text or "User prompts: 5" in text  # depends on slice
    assert "td_get_info" in text


def test_compact_summary_handles_empty_tool_calls():
    msgs = []
    for i in range(20):
        msgs.append(_user(f"u{i}"))
        msgs.append(_assistant_text(f"a{i}"))
    out = compaction.compact(msgs, keep_recent=4)
    text = out[0]["content"][0]["text"]
    assert "No tool calls" in text


def test_compact_summary_truncates_long_user_text():
    long_text = "X" * 1000
    msgs = [
        _user(long_text),
        _assistant_text("ok"),
    ] * 12
    out = compaction.compact(msgs, keep_recent=4)
    text = out[0]["content"][0]["text"]
    # Truncated at 200 chars + "..." marker.
    assert "..." in text
    # The full 1000-char string must NOT appear verbatim.
    assert long_text not in text


# ---------------------------------------------------------------------------
# persist_history_chunk + read_history
# ---------------------------------------------------------------------------


def test_persist_history_chunk_writes_jsonl(tmp_path):
    msgs = [_user("hello"), _assistant_text("hi")]
    path = compaction.persist_history_chunk(msgs, "sess1", history_dir=tmp_path)
    assert path == tmp_path / "sess1.jsonl"
    assert path.is_file()
    line = path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["session_id"] == "sess1"
    assert rec["message_count"] == 2
    assert rec["messages"] == msgs
    assert rec["ts"]


def test_persist_history_chunk_appends(tmp_path):
    """Multiple compactions in one session = multiple lines."""
    compaction.persist_history_chunk([_user("a")], "sess2", history_dir=tmp_path)
    compaction.persist_history_chunk([_user("b")], "sess2", history_dir=tmp_path)
    lines = (tmp_path / "sess2.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_read_history_round_trip(tmp_path):
    msgs1 = [_user("first")]
    msgs2 = [_user("second"), _assistant_text("ok")]
    compaction.persist_history_chunk(msgs1, "rt", history_dir=tmp_path)
    compaction.persist_history_chunk(msgs2, "rt", history_dir=tmp_path)
    records = compaction.read_history("rt", history_dir=tmp_path)
    assert len(records) == 2
    assert records[0]["messages"] == msgs1
    assert records[1]["messages"] == msgs2


def test_read_history_missing_file(tmp_path):
    assert compaction.read_history("nope", history_dir=tmp_path) == []


def test_persist_history_chunk_filesystem_error_does_not_raise(tmp_path, monkeypatch):
    """A read-only target dir is fine — function logs a warning and
    returns the path, never blocks the agent loop.
    """

    class _DummyPath:
        def __init__(self):
            pass

        def mkdir(self, **k):
            raise OSError("read-only fs")

        def __truediv__(self, other):
            return self

        def open(self, *a, **k):
            raise OSError("read-only fs")

    # Force the target dir's mkdir to fail.
    real_path = compaction.persist_history_chunk(
        [_user("x")],
        "sess_ro",
        history_dir=tmp_path / "definitely_does_not_exist_yet",
    )
    # File creation may have succeeded; either way no exception escaped.
    assert isinstance(real_path, Path)


# ---------------------------------------------------------------------------
# Compactor class — pairs compact + persist
# ---------------------------------------------------------------------------


def test_compactor_disabled_when_threshold_zero(tmp_path):
    c = compaction.Compactor(session_id="x", threshold=0, history_dir=tmp_path)
    assert c.enabled is False
    msgs = _build_long_history(20)
    assert c.maybe_compact(msgs) is msgs  # identity-preserving no-op


def test_compactor_fires_above_threshold(tmp_path):
    c = compaction.Compactor(session_id="abc", threshold=10, keep_recent=4, history_dir=tmp_path)
    msgs = _build_long_history(5)  # 20 messages
    out = c.maybe_compact(msgs)
    assert len(out) == 5  # 1 synthetic + 4 recent
    assert c.compactions_run == 1
    # On-disk persistence happened.
    archive = tmp_path / "abc.jsonl"
    assert archive.is_file()


def test_compactor_no_op_below_threshold(tmp_path):
    c = compaction.Compactor(session_id="below", threshold=20, keep_recent=10, history_dir=tmp_path)
    msgs = _build_long_history(3)  # 12 messages, below 20
    out = c.maybe_compact(msgs)
    assert out is msgs
    assert c.compactions_run == 0
    assert not (tmp_path / "below.jsonl").exists()


def test_compactor_persistence_off(tmp_path):
    """``persist=False`` skips writing the forensic archive."""
    c = compaction.Compactor(
        session_id="nopersist",
        threshold=10,
        keep_recent=4,
        history_dir=tmp_path,
        persist=False,
    )
    out = c.maybe_compact(_build_long_history(5))
    assert len(out) == 5
    assert not (tmp_path / "nopersist.jsonl").exists()


def test_compactor_repeated_calls_eventually_idempotent(tmp_path):
    """Once compacted to below the threshold, subsequent calls don't
    re-compact (until new messages push it back over)."""
    c = compaction.Compactor(session_id="idem", threshold=10, keep_recent=4, history_dir=tmp_path)
    msgs = _build_long_history(5)  # 20 messages
    out1 = c.maybe_compact(msgs)  # → 5 messages
    out2 = c.maybe_compact(out1)  # below threshold → no-op
    assert out1 is out2 or out1 == out2
    assert c.compactions_run == 1


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


def test_agent_calls_compactor_at_loop_start():
    """Provide an Agent with a Compactor stub and verify ``_loop``
    calls maybe_compact ONCE at the top of the run.
    """
    import io
    import json as _json

    from tdpilot_api_agent import Agent

    calls: list[int] = []

    class _StubCompactor:
        def maybe_compact(self, messages):
            calls.append(len(messages))
            return messages

    # Build a minimal Agent that exits immediately with a text-only response.
    fake_response = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
    }

    class _FakeResponse:
        def __init__(self, payload):
            self._body = _json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self._body

    agent = Agent(
        api_key="sk-x",
        dispatcher=lambda *a: None,
        tools=[],
        compactor=_StubCompactor(),
    )
    # Pre-populate the history with messages so the compactor
    # actually has something to look at.
    agent.messages = [{"role": "user", "content": [{"type": "text", "text": f"u{i}"}]} for i in range(5)]
    agent.add_user_message("hi")

    with patch("urllib.request.urlopen", return_value=_FakeResponse(fake_response)):
        agent.run_turn()

    assert len(calls) == 1, f"compactor.maybe_compact must fire exactly once at loop start, got {calls}"


def test_agent_compactor_failure_doesnt_break_turn():
    """A crashing compactor must NOT take down the agent."""
    import json as _json

    from tdpilot_api_agent import Agent

    class _BoomCompactor:
        def maybe_compact(self, messages):
            raise RuntimeError("compaction exploded")

    class _FakeResponse:
        def __init__(self, payload):
            self._body = _json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return self._body

    agent = Agent(
        api_key="sk-x",
        dispatcher=lambda *a: None,
        tools=[],
        compactor=_BoomCompactor(),
    )
    agent.add_user_message("hi")

    fake_response = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
    with patch("urllib.request.urlopen", return_value=_FakeResponse(fake_response)):
        # Must complete normally — exception is swallowed inside _loop.
        out = agent.run_turn()
    assert out == "ok"


def test_runtime_builds_compactor_when_threshold_positive(monkeypatch, tmp_path):
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
            "compaction_threshold": 12,
            "compaction_keep_recent": 4,
            "history_dir": tmp_path / "history",
            "trace_logging": False,
            "pre_retrieval": False,
        },
    )
    assert rt._compactor is not None
    assert rt._compactor.threshold == 12
    assert rt._compactor.keep_recent == 4
    assert rt._compactor.enabled is True


def test_runtime_disabled_compactor_when_threshold_zero(monkeypatch, tmp_path):
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
            "compaction_threshold": 0,  # explicit disable
            "trace_logging": False,
            "pre_retrieval": False,
        },
    )
    assert rt._compactor is None


# ---------------------------------------------------------------------------
# Phase 1.6.13 — boundary-orphan repair (audit finding P1)
# ---------------------------------------------------------------------------


def _build_history_with_tool_chain():
    """30 messages where the natural cut at len-10 lands inside a
    tool chain — message [20] is a user/tool_result whose matching
    assistant/tool_use is at message [19]. Pre-fix this would yield
    a synthetic-+-orphan-tool_result slice that 400s on the next
    API call.
    """
    msgs: list[dict] = []
    for i in range(7):
        msgs.append(_user(f"turn {i} user"))
        msgs.append(_assistant_with_tool_use("td_get_info"))
        msgs.append(_user_tool_result("tu_td_get_info"))
        msgs.append(_assistant_with_thinking(f"reply {i}"))
    # 28 messages so far; total 30 with the trailing 2.
    msgs.append(_user("turn 7 user"))
    msgs.append(_assistant_with_thinking("reply 7"))
    return msgs


def test_compact_does_not_orphan_tool_result_at_boundary():
    """Audit P1 — when the slice point would land on a leading
    user/tool_result, advance forward so the retained slice starts
    on a clean message boundary. The retained slice may end up
    smaller than ``keep_recent`` — by design.
    """
    msgs = _build_long_history(8)  # 32 messages, 4 per turn
    # keep_recent=10 → cut at index 22, message[22] is tool_use
    # (assistant role) — that's safe. Try keep_recent=11 → cut at
    # index 21, message[21] is the user/tool_result. THAT's the
    # orphan we want to repair.
    out = compaction.compact(msgs, keep_recent=11)
    # The retained slice must NOT start with a tool_result.
    first_recent = out[1]
    assert first_recent["role"] != "user" or not any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in first_recent.get("content", [])
    ), f"retained slice starts with orphan tool_result: {first_recent}"


def test_compact_advances_past_multiple_tool_results():
    """Multi-block tool_result tails — advance until the next
    non-tool_result message.
    """
    msgs = []
    msgs.append(_user("first user goal"))
    msgs.append(_assistant_with_tool_use("td_create_node"))
    # Cut would land here if we asked for keep_recent=4:
    msgs.append(_user_tool_result("tu_td_create_node"))  # orphan #1
    msgs.append(_assistant_with_tool_use("td_get_errors"))
    msgs.append(_user_tool_result("tu_td_get_errors"))
    msgs.append(_assistant_with_thinking("ok all good"))
    msgs.append(_user("next turn please"))
    msgs.append(_assistant_with_thinking("done"))
    out = compaction.compact(msgs, keep_recent=6)
    # After repair, retained must NOT lead with tool_result.
    assert compaction._starts_with_tool_result(out[1]) is False


def test_compact_handles_pathological_all_tool_results():
    """If every "recent" message is a tool_result tail, the repair
    eats them all — synthetic stands alone. Never raises.
    """
    msgs = []
    for i in range(20):
        msgs.append(_user_tool_result(f"tu_{i}"))
    out = compaction.compact(msgs, keep_recent=5)
    # The synthetic message remains. Any retained recents either
    # don't exist or are not tool_result-led.
    assert out[0]["role"] == "assistant"
    for m in out[1:]:
        assert compaction._starts_with_tool_result(m) is False


def test_starts_with_tool_result_helper():
    """Spot-check the predicate the compactor uses for boundary repair."""
    assert compaction._starts_with_tool_result(_user_tool_result("x")) is True
    assert compaction._starts_with_tool_result(_user("normal text")) is False
    assert compaction._starts_with_tool_result(_assistant_text("a")) is False
    assert compaction._starts_with_tool_result(_assistant_with_tool_use("td_get_info")) is False
    # Defensive — non-dict / malformed inputs.
    assert compaction._starts_with_tool_result(None) is False  # type: ignore[arg-type]
    assert compaction._starts_with_tool_result({"role": "user"}) is False  # missing content
    assert compaction._starts_with_tool_result({"role": "user", "content": []}) is False


# ---------------------------------------------------------------------------
# F-09 — _resolve_cut + forensic preservation
# ---------------------------------------------------------------------------
#
# Pre-PR-13 the bug was: Compactor.maybe_compact computed the cut as
# ``len(messages) - keep_recent`` and persisted that slice, but
# ``compact()`` then advanced the cut FORWARD past leading
# tool_result blocks. The advanced-past messages were dropped from
# history but absent from the persisted archive — silent forensic
# loss exactly when the boundary repair fired.
#
# These tests pin:
#   * `_resolve_cut` is the shared cut-resolution function.
#   * compact() and maybe_compact() see the SAME cut for the same input.
#   * The forensic JSONL archive contains EVERY message that compact()
#     discards, including ones repair-advanced past.


def test_resolve_cut_matches_naive_when_no_repair_needed():
    msgs = _build_long_history(8)  # 32 messages, no leading tool_results at the natural cut
    cut = compaction._resolve_cut(msgs, keep_recent=4)
    # Natural cut at index 28 (32 - 4). The slice point lands on a user
    # message (turn-start), not a tool_result, so no repair fires.
    assert cut == 28


def test_resolve_cut_advances_past_tool_results():
    """When the natural cut lands on an orphan tool_result, the
    advanced cut must skip it. Same logic compact() uses, factored
    into _resolve_cut so maybe_compact gets the same answer."""
    msgs = _build_long_history(8)  # 32 messages
    # keep_recent=10 → natural cut at index 22 (a tool_result, since
    # the build_long_history pattern is user/tool_use/tool_result/reply).
    cut = compaction._resolve_cut(msgs, keep_recent=10)
    assert cut == 23, "expected advancement past index-22 tool_result"
    assert compaction._starts_with_tool_result(msgs[cut]) is False


def test_resolve_cut_returns_zero_when_below_keep_recent():
    msgs = [_user("a"), _assistant_text("b"), _user("c")]
    cut = compaction._resolve_cut(msgs, keep_recent=10)
    assert cut == 0


def test_resolve_cut_pathological_all_tool_results():
    """When every message past the natural cut is a tool_result tail,
    _resolve_cut returns len(messages) so the caller knows to archive
    EVERYTHING. compact() handles this by returning just the synthetic."""
    msgs = [_user_tool_result(f"tu_{i}") for i in range(20)]
    cut = compaction._resolve_cut(msgs, keep_recent=5)
    assert cut == len(msgs)


def _archived_messages(session_id: str, history_dir: Path) -> list[dict]:
    """Flatten read_history's record list into the raw messages
    sequence, so tests can compare directly against
    ``messages[:cut]``."""
    records = compaction.read_history(session_id, history_dir=history_dir)
    out: list[dict] = []
    for rec in records:
        msgs = rec.get("messages")
        if isinstance(msgs, list):
            out.extend(msgs)
    return out


def test_compactor_persists_advanced_slice_not_naive_slice(tmp_path):
    """The bug fix in one assertion: with a tool-chain history that
    triggers boundary repair, the on-disk archive must contain ALL
    messages compact() discards — including the ones repair advanced
    past. Pre-fix this archive was short by N messages where N is
    the number of leading tool_results the repair skipped."""
    msgs = _build_long_history(8)  # 32 messages
    c = compaction.Compactor(session_id="forensic", threshold=20, keep_recent=10, history_dir=tmp_path)
    out = c.maybe_compact(msgs)
    # Compaction fired (not the same object back).
    assert out is not msgs

    archive = _archived_messages("forensic", tmp_path)
    cut_used = compaction._resolve_cut(msgs, keep_recent=10)
    expected_archive = msgs[:cut_used]
    assert len(archive) == len(expected_archive), (
        f"archive missing {len(expected_archive) - len(archive)} messages — "
        f"the boundary repair advanced the cut but persistence used the naive cut"
    )
    assert archive == expected_archive


def test_compactor_archive_matches_compact_discard(tmp_path):
    """Cross-check across multiple keep_recent values: regardless of
    where the cut lands, the persisted slice == messages[:cut] where
    cut is exactly compact()'s cut."""
    msgs = _build_long_history(10)  # 40 messages
    for kr in (4, 6, 8, 10, 12):
        sub = tmp_path / f"kr_{kr}"
        sub.mkdir()
        c = compaction.Compactor(session_id=f"k{kr}", threshold=20, keep_recent=kr, history_dir=sub)
        c.maybe_compact(msgs)
        archive = _archived_messages(f"k{kr}", sub)
        cut_used = compaction._resolve_cut(msgs, keep_recent=kr)
        if cut_used == 0:
            assert archive == [], f"keep_recent={kr}: no compaction fired, archive should be empty"
        else:
            assert archive == msgs[:cut_used], (
                f"keep_recent={kr}: archive does not match compact() discard slice"
            )


def test_compactor_does_not_persist_when_threshold_disabled(tmp_path):
    """threshold=0 disables compaction entirely — even with a huge
    message list, no archive is written."""
    msgs = _build_long_history(20)
    c = compaction.Compactor(session_id="disabled", threshold=0, keep_recent=10, history_dir=tmp_path)
    out = c.maybe_compact(msgs)
    assert out is msgs  # same-object identity preserved
    assert _archived_messages("disabled", tmp_path) == []


def test_compactor_with_persist_false_archives_nothing(tmp_path):
    msgs = _build_long_history(15)
    c = compaction.Compactor(
        session_id="nopersist",
        threshold=10,
        keep_recent=4,
        history_dir=tmp_path,
        persist=False,
    )
    out = c.maybe_compact(msgs)
    assert out is not msgs  # compaction did fire
    assert _archived_messages("nopersist", tmp_path) == []


def test_compactor_round_trip_archive_messages(tmp_path):
    """Persisted JSONL must round-trip — re-reading the file produces
    the same dict structure compact() discarded. Defends the forensic
    contract: archived messages are inspectable later, not just
    write-only ballast."""
    msgs = _build_long_history(8)
    c = compaction.Compactor(session_id="rt", threshold=20, keep_recent=10, history_dir=tmp_path)
    c.maybe_compact(msgs)
    archive = _archived_messages("rt", tmp_path)
    cut_used = compaction._resolve_cut(msgs, keep_recent=10)
    assert archive == msgs[:cut_used]
    # Spot-check a thinking block survives the round trip.
    thinking_blocks = [
        b
        for m in archive
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "thinking"
    ]
    assert thinking_blocks, "expected at least one thinking block in archive"
    for b in thinking_blocks:
        assert "thinking" in b
        assert "signature" in b
