"""Tests for the cook-thread dispatcher in tdpilot_api_runtime.

The CookThreadDispatcher exists to keep TD API calls off the agent's
worker thread (TD is not thread-safe). These tests simulate the worker
thread vs. cook thread split with two real threads:
  - "worker": calls dispatcher(name, args), expects to block until done.
  - "cook": periodically calls pump() to drain pending requests.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

# tdpilot_api_runtime imports tdpilot_api_agent and tdpilot_api_config at
# module load. Both are pure and importable outside TD.
from tdpilot_api_runtime import CookThreadDispatcher  # noqa: E402


def _start_cook_pump(disp: CookThreadDispatcher, stop: threading.Event, interval: float = 0.005):
    """Background 'cook thread' that pumps the dispatcher until stop is set."""

    def _loop():
        while not stop.is_set():
            disp.pump()
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="fake-cook", daemon=True)
    t.start()
    return t


def test_marshals_call_to_pump_thread():
    """The raw dispatcher must execute on the pump (cook) thread, never on
    the worker thread that originated the call.

    Synchronisation: use a `pump_started` Event so we know the pump
    thread has captured its own tid before we let the worker call return
    or assert on identity. Earlier revisions relied on natural timing
    ("the cookie races first"); the audit flagged it as
    non-deterministic. Now: explicit Event handshake, no timing assumptions.
    """
    raw_thread_seen: list[int] = []
    pump_tid_holder: list[int] = []
    pump_started = threading.Event()

    def raw(name, args):
        raw_thread_seen.append(threading.get_ident())
        return {"echo": args, "name": name}

    disp = CookThreadDispatcher(raw, timeout=2.0)

    def pump_loop():
        pump_tid_holder.append(threading.get_ident())
        pump_started.set()  # explicit handshake — pump tid is captured
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if disp.pump() > 0:
                return
            time.sleep(0.005)

    pump_thread = threading.Thread(target=pump_loop, name="fake-cook-marshall", daemon=True)
    pump_thread.start()
    assert pump_started.wait(timeout=2.0), "pump thread failed to start"

    worker_tid = threading.get_ident()
    result = disp("td_get_info", {"foo": 1})
    pump_thread.join(timeout=2.0)

    assert result == {"echo": {"foo": 1}, "name": "td_get_info"}
    assert raw_thread_seen, "raw dispatcher was never invoked"
    assert raw_thread_seen[0] != worker_tid, "raw dispatcher must not run on the worker thread"
    assert raw_thread_seen[0] == pump_tid_holder[0], "raw dispatcher must run on the pump thread"


def _set_tid_then_pump(disp, set_tid):
    """Legacy helper kept for backwards compatibility with any out-of-tree
    callers; the in-tree marshall test now uses an Event-based handshake
    (see ``test_marshals_call_to_pump_thread``)."""
    set_tid(threading.get_ident())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if disp.pump() > 0:
            return
        time.sleep(0.005)


def test_concurrent_calls_all_resolve():
    """Many worker threads, one pump thread, every call gets its own result."""

    def raw(name, args):
        return {"id": args["id"]}

    disp = CookThreadDispatcher(raw, timeout=3.0)
    stop = threading.Event()
    pump_thread = _start_cook_pump(disp, stop)

    results: dict[int, dict] = {}
    lock = threading.Lock()

    def worker(call_id: int):
        out = disp("td_get_info", {"id": call_id})
        with lock:
            results[call_id] = out

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=4.0)

    stop.set()
    pump_thread.join(timeout=1.0)

    assert len(results) == 20
    for i, r in results.items():
        assert r == {"id": i}


def test_raw_dispatcher_exception_becomes_error_dict():
    def raw(name, args):
        raise RuntimeError("boom")

    disp = CookThreadDispatcher(raw, timeout=1.0)
    stop = threading.Event()
    pump_thread = _start_cook_pump(disp, stop)

    out = disp("td_get_info", {})

    stop.set()
    pump_thread.join(timeout=1.0)

    assert "error" in out
    assert "boom" in out["error"]


def test_timeout_returns_error_dict_when_no_pump():
    """If the cook thread never pumps, we get a timeout error (not a hang)."""
    disp = CookThreadDispatcher(lambda *_a: None, timeout=0.1)
    out = disp("td_get_info", {})
    assert "error" in out
    assert "timed out" in out["error"]


def test_cancel_pending_unblocks_worker():
    """reset()-style cleanup: pending calls return cancellation errors."""
    disp = CookThreadDispatcher(lambda *_a: {"ok": True}, timeout=5.0)

    holder: dict = {}

    def worker():
        holder["result"] = disp("td_get_info", {})

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # Give the worker a moment to enqueue and start waiting.
    time.sleep(0.05)

    disp.cancel_pending()
    t.join(timeout=1.0)

    assert "result" in holder
    assert "error" in holder["result"]
    assert "cancelled" in holder["result"]["error"]


# ---------------------------------------------------------------------------
# Phase 0.1 — cache-stable dynamic-context slot. Runtime side.
# ---------------------------------------------------------------------------


def test_build_system_prompt_excludes_volatile_indexes():
    """build_system_prompt() must NOT contain the per-turn-volatile
    memory / knowledge / recipes index headers — those moved to
    build_dynamic_context() in Phase 0.1.
    """
    from tdpilot_api_runtime import build_system_prompt

    prompt = build_system_prompt()
    # Headers that used to live in the system prompt — gone in 0.1.
    assert "## Memory Index" not in prompt
    assert "## Knowledge Index" not in prompt
    assert "## Recipes" not in prompt
    # The base instructions ARE still in there.
    assert "TDPilot API" in prompt
    assert "Operating protocol" in prompt


def test_system_prompt_includes_trust_tier_rule():
    """Phase 3.2 — system prompt has a rule about how to weight
    search hits by trust_tier. The order ladder + the
    "validate before claiming" instruction must both be present.
    """
    from tdpilot_api_runtime import SYSTEM_PROMPT_BASE

    # Trust ordering ladder.
    assert "official > bundled > personal > community > transcript > experimental" in SYSTEM_PROMPT_BASE
    # The "validate before claiming behavior is correct" instruction
    # — wording matters less than the validators it points to.
    assert "td_get_errors" in SYSTEM_PROMPT_BASE
    # And the trust_tier field name itself, so the model knows what
    # to look for on each match.
    assert "trust_tier" in SYSTEM_PROMPT_BASE


def test_build_system_prompt_byte_stable_when_memory_changes(tmp_path, monkeypatch):
    """Saving a memory / knowledge / recipe entry between calls must NOT
    change the system prompt — it now lives in dynamic context.
    """
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    from tdpilot_api_runtime import build_system_prompt

    # Both ``MEMORY_DIR`` and ``MEMORY_INDEX`` are module-level — patching
    # only the first leaves ``MEMORY_INDEX`` pointing at the developer's
    # real ``~/.tdpilot-api/memory/MEMORY.md``. CI cleanrooms don't have
    # that file so the test fails. Patch both.
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    # Establish baseline.
    before = build_system_prompt()

    # Mutate memory.
    mem.handle_memory_save(
        {
            "name": "test_dynamic_ctx",
            "description": "Phase 0.1 regression",
            "type": "feedback",
            "content": "irrelevant body",
        }
    )

    after = build_system_prompt()
    assert before == after, "system prompt must be insensitive to memory writes"


def test_build_dynamic_context_emits_paired_messages(tmp_path, monkeypatch):
    """When indexes are non-empty, output is a paired user/assistant
    message list with the [[TDPILOT_CONTEXT]] delimiter.
    """
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    from tdpilot_api_runtime import DYNAMIC_CONTEXT_DELIMITER, build_dynamic_context

    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    mem.handle_memory_save(
        {
            "name": "phase01_dyn",
            "description": "marker for dynamic context test",
            "type": "feedback",
            "content": "body",
        }
    )

    msgs = build_dynamic_context()
    assert isinstance(msgs, list)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    user_text = msgs[0]["content"][0]["text"]
    assert user_text.startswith(DYNAMIC_CONTEXT_DELIMITER)
    # The memory we just saved should appear in the user-side context.
    assert "phase01_dyn" in user_text


def test_build_dynamic_context_returns_empty_when_no_indexes(monkeypatch):
    """No memory / knowledge / recipes content → empty list (skip
    prepending). Prevents wasting tokens on empty headers.

    Stubs the three index-hint functions to return ``""`` directly so
    we don't depend on the on-disk layout of dirs / bundled corpora.
    """
    import tdpilot_api_knowledge as kb  # type: ignore[import-not-found]
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    import tdpilot_api_recipes as rec  # type: ignore[import-not-found]
    from tdpilot_api_runtime import build_dynamic_context

    monkeypatch.setattr(mem, "get_memory_index_content", lambda: "")
    monkeypatch.setattr(kb, "get_knowledge_index_hint", lambda: "")
    monkeypatch.setattr(rec, "get_recipes_index_hint", lambda: "")

    msgs = build_dynamic_context()
    assert msgs == []


def test_build_dynamic_context_returns_paired_when_any_index_present(monkeypatch):
    """Even if just ONE index has content, output is a paired
    user/assistant list — required to preserve the alternation
    invariant when the conversation continues with a real user msg.
    """
    import tdpilot_api_knowledge as kb  # type: ignore[import-not-found]
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    import tdpilot_api_recipes as rec  # type: ignore[import-not-found]
    from tdpilot_api_runtime import build_dynamic_context

    monkeypatch.setattr(mem, "get_memory_index_content", lambda: "")
    monkeypatch.setattr(kb, "get_knowledge_index_hint", lambda: "kb_hint_present")
    monkeypatch.setattr(rec, "get_recipes_index_hint", lambda: "")

    msgs = build_dynamic_context()
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert "kb_hint_present" in msgs[0]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Phase 0.1 — cook-thread refresh.
#
# THREAD CONFLICT regression: build_dynamic_context() touches TD globals
# (parent().op('kb') for bundled knowledge). The Agent's worker thread
# must NEVER call it directly. The runtime pre-computes on the cook
# thread via _refresh_dynamic_context and the Agent reads the cached
# snapshot. These tests lock that contract in.
# ---------------------------------------------------------------------------


def test_runtime_refreshes_dynamic_context_on_start_turn(monkeypatch, tmp_path):
    """start_turn must call _refresh_dynamic_context BEFORE the worker
    thread launches — the cook thread is the only safe place to invoke
    the TD-touching bundled-knowledge enumerator.
    """
    import tdpilot_api_runtime as rt_mod  # type: ignore[import-not-found]
    from tdpilot_api_runtime import AgentRuntime

    # ``fetch_api_key`` was imported by name into ``tdpilot_api_runtime``
    # at module load — patching the source module
    # (``tdpilot_api_config``) doesn't affect runtime's already-bound
    # reference. Patch the runtime's copy directly. Locally this masked
    # itself because the developer machine has a real config.json on
    # disk that fetch_api_key successfully reads; CI cleanrooms don't.
    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    refresh_calls: list[str] = []
    original = AgentRuntime._refresh_dynamic_context

    def tracking_refresh(self):
        refresh_calls.append(threading.current_thread().name)
        original(self)

    monkeypatch.setattr(AgentRuntime, "_refresh_dynamic_context", tracking_refresh)

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    # Construction calls _refresh_dynamic_context once for the warmup.
    assert len(refresh_calls) == 1, f"warmup refresh missing: {refresh_calls}"

    # We can't actually run the worker without a real API endpoint, but
    # we can assert the refresh fires. Stub the agent's worker spawn so
    # start_turn returns True without launching a real thread.
    class _FakeAgent:
        messages: list = []

        def add_user_message(self, text):
            self.messages.append({"role": "user", "content": text})

    rt._agent = _FakeAgent()  # type: ignore[assignment]

    # Override the thread spawn — we just want to confirm refresh ran.
    monkeypatch.setattr(
        threading,
        "Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None, "is_alive": lambda self: False})(),
    )

    rt.start_turn("hello")
    # Now we expect 2 calls: 1 warmup + 1 from start_turn.
    assert len(refresh_calls) == 2, f"start_turn didn't refresh: {refresh_calls}"


def test_runtime_dynamic_context_provider_reads_snapshot(monkeypatch):
    """The Agent's dynamic_context_provider must read self._dynamic_context_snapshot,
    NOT call build_dynamic_context() itself. Otherwise the worker thread
    races into TD globals and pops THREAD CONFLICT.
    """
    import tdpilot_api_runtime as rt_mod  # type: ignore[import-not-found]
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])

    # Simulate the cook-thread refresh having captured a known snapshot.
    sentinel = [
        {"role": "user", "content": [{"type": "text", "text": "[[TDPILOT_CONTEXT]] sentinel"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "ack"}]},
    ]
    rt._dynamic_context_snapshot = sentinel

    # Pull the provider that was wired into the Agent (a bound lambda
    # over rt._dynamic_context_snapshot).
    provider = rt._agent.dynamic_context_provider  # type: ignore[union-attr]
    assert provider is not None
    out = provider()
    assert out == sentinel
    # AND the snapshot must be a defensive copy, not the live list — so
    # mutating the agent-side return value cannot leak into the runtime.
    out.append({"role": "user", "content": []})
    assert len(rt._dynamic_context_snapshot) == 2


# ---------------------------------------------------------------------------
# Phase 1.2 — memory saved mid-session must surface in the next turn.
#
# The original concern was: ``Agent._system_prompt`` is set ONCE at
# AgentRuntime.__init__, so memories saved during the session never
# propagated to the model's context. Phase 0.1 implicitly fixed this by
# moving memory/knowledge/recipes indexes OUT of the system prompt and
# into the per-turn dynamic context. Phase 1.2 just locks that
# behaviour in with a regression test that simulates the exact flow:
# turn N → memory_save → turn N+1 sees the new memory.
# ---------------------------------------------------------------------------


def test_memory_saved_mid_session_propagates_to_next_turn(monkeypatch, tmp_path):
    """Phase 1.2 contract: a memory saved during turn N appears in the
    dynamic-context messages sent for turn N+1.
    """
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    import tdpilot_api_runtime as rt_mod  # type: ignore[import-not-found]
    from tdpilot_api_runtime import DYNAMIC_CONTEXT_DELIMITER, AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "memory" / "MEMORY.md")

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])

    # ---- Turn N: snapshot the dynamic context (cook-thread refresh) ----
    rt._refresh_dynamic_context()
    snapshot_n = list(rt._dynamic_context_snapshot)
    text_n = ""
    if snapshot_n:
        text_n = snapshot_n[0]["content"][0]["text"]

    # The new memory's name must NOT be in turn N's snapshot — it
    # hasn't been saved yet. (The dynamic context may be empty here
    # because the test memory dir starts blank; that's fine.)
    assert "phase12_marker" not in text_n

    # ---- Mid-turn: save a memory (simulates an in-turn memory_save call) ----
    mem.handle_memory_save(
        {
            "name": "phase12_marker",
            "description": "Phase 1.2 propagation test",
            "type": "feedback",
            "content": "irrelevant body",
        }
    )

    # ---- Turn N+1: refresh the snapshot again (start_turn does this) ----
    rt._refresh_dynamic_context()
    snapshot_n_plus_1 = list(rt._dynamic_context_snapshot)
    assert snapshot_n_plus_1, "dynamic context for turn N+1 was empty"
    text_n_plus_1 = snapshot_n_plus_1[0]["content"][0]["text"]
    assert text_n_plus_1.startswith(DYNAMIC_CONTEXT_DELIMITER)
    assert "phase12_marker" in text_n_plus_1, (
        "memory saved at turn N didn't appear in turn N+1's dynamic context"
    )


# ---------------------------------------------------------------------------
# Phase 1.3 — severity-tracked validation hints.
#
# Contract: when a turn includes one or more high-severity mutations
# (td_create_node, td_delete_node, td_exec_python, etc.) without a
# follow-up validator call (td_get_errors / td_audit_project /
# td_validate_recipe / patch_validate), emit ``EV_HINT`` so the chat
# UI can render a soft nudge. Never blocks; never fires on low/medium
# turns or on turns that DID validate.
# ---------------------------------------------------------------------------


def _drain_runtime_events(rt) -> list[tuple[str, dict]]:
    """Pop every event from the runtime queue without round-tripping
    through the cook thread. Used to assert hint emission.
    """
    events: list[tuple[str, dict]] = []
    from queue import Empty

    while True:
        try:
            events.append(rt._events.get_nowait())
        except Empty:
            break
    return events


def _build_runtime_for_hint_tests(monkeypatch):
    import tdpilot_api_runtime as rt_mod  # type: ignore[import-not-found]
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    return AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])


def test_severity_lookup_classifies_known_tools_correctly():
    from tdpilot_api_runtime import _tool_severity

    assert _tool_severity("td_create_node") == "high"
    assert _tool_severity("td_delete_node") == "high"
    assert _tool_severity("td_exec_python") == "high"
    assert _tool_severity("td_set_params") == "medium"
    assert _tool_severity("td_get_info") == "low"  # unknown → low
    assert _tool_severity("totally_made_up_tool") == "low"


def test_validation_hint_fires_on_high_severity_without_validator(monkeypatch):
    """A turn that includes td_create_node but NOT td_get_errors must
    emit EV_HINT at turn end.
    """
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)

    # Simulate the worker calling on_tool_result for a single create_node.
    rt._record_tool_call("td_create_node", is_error=False)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    hint_events = [(k, p) for k, p in events if k == EV_HINT]
    assert len(hint_events) == 1
    payload = hint_events[0][1]
    assert payload["kind"] == "missing_validation"
    assert "td_create_node" in payload["tools"]
    assert "td_get_errors" in payload["message"]


def test_validation_hint_suppressed_when_validator_called(monkeypatch):
    """A turn that paired td_create_node with td_get_errors must NOT
    emit a hint.
    """
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)
    rt._record_tool_call("td_create_node", is_error=False)
    rt._record_tool_call("td_get_errors", is_error=False)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    assert not any(k == EV_HINT for k, _ in events)


def test_validation_hint_suppressed_for_low_severity_only_turns(monkeypatch):
    """Read-only turns (td_get_info, td_get_nodes, etc.) must never
    fire the hint regardless of validator presence.
    """
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)
    rt._record_tool_call("td_get_info", is_error=False)
    rt._record_tool_call("td_get_nodes", is_error=False)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    assert not any(k == EV_HINT for k, _ in events)


def test_validation_hint_suppressed_for_medium_severity_only(monkeypatch):
    """td_set_params is medium severity — by itself, no hint."""
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)
    rt._record_tool_call("td_set_params", is_error=False)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    assert not any(k == EV_HINT for k, _ in events)


def test_validation_hint_ignores_failed_tool_calls(monkeypatch):
    """A failed td_create_node (is_error=True) didn't actually mutate
    state — it shouldn't count toward the high-severity tally.
    """
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)
    # Failed call — must not be tracked.
    rt._record_tool_call("td_create_node", is_error=True)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    assert not any(k == EV_HINT for k, _ in events)


def test_validation_hint_lists_all_high_severity_tools(monkeypatch):
    """Multiple distinct high-severity calls in one turn → all listed."""
    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)
    rt._record_tool_call("td_create_node", is_error=False)
    rt._record_tool_call("td_create_node", is_error=False)  # dedup
    rt._record_tool_call("td_exec_python", is_error=False)
    rt._maybe_emit_validation_hint()

    events = _drain_runtime_events(rt)
    hint_events = [(k, p) for k, p in events if k == EV_HINT]
    assert len(hint_events) == 1
    tools = hint_events[0][1]["tools"]
    assert tools == ["td_create_node", "td_exec_python"]  # sorted, deduped


def test_validation_hint_cleared_between_turns(monkeypatch):
    """Per-turn ledger reset on start_turn — hint from turn N doesn't
    leak into turn N+1's check.
    """
    import threading

    from tdpilot_api_runtime import EV_HINT

    rt = _build_runtime_for_hint_tests(monkeypatch)

    # Simulate turn 1 ending with a high-severity mutation.
    rt._record_tool_call("td_create_node", is_error=False)
    rt._maybe_emit_validation_hint()
    _drain_runtime_events(rt)  # flush the hint event from turn 1

    # Turn 2 starts. We need to trigger start_turn's ledger clear.
    # Bypass the worker thread spawn by stubbing it.
    import tdpilot_api_runtime as rt_module

    monkeypatch.setattr(
        rt_module.threading,
        "Thread",
        lambda *a, **k: type("T", (), {"start": lambda self: None, "is_alive": lambda self: False})(),
    )

    class _FakeAgent:
        messages: list = []

        def add_user_message(self, _):
            pass

    rt._agent = _FakeAgent()  # type: ignore[assignment]
    rt.start_turn("turn 2")
    assert rt._turn_tool_calls == [], "ledger should be cleared at turn start"

    # Turn 2 only does a read — no hint.
    rt._record_tool_call("td_get_info", is_error=False)
    rt._maybe_emit_validation_hint()
    events = _drain_runtime_events(rt)
    assert not any(k == EV_HINT for k, _ in events)
    # threading reference touched to satisfy the linter — actual import
    # was needed inside this scope.
    _ = threading.Lock


# ---------------------------------------------------------------------------
# Phase 3.1 — trigger-based skill loading.
#
# When a user message contains a keyword listed in a skill's
# ``triggers:`` frontmatter, the runtime auto-loads that skill: its
# body lands in the dynamic context for THIS turn and every subsequent
# turn until reset(). Idempotent — re-triggering doesn't duplicate.
# ---------------------------------------------------------------------------


def test_find_triggered_skills_word_boundary_for_short_keywords():
    """Short triggers (< 5 chars) use word boundaries — 'pop' must NOT
    match 'population' / 'popular'.
    """
    import tdpilot_api_skills as skills

    fake_entries = [
        {
            "name": "popx-mode",
            "triggers": ["pop", "popx"],
            "text": "popx body",
        },
    ]
    # Patch the discovery to return our fake entry — keeps the test
    # independent of bundled / user state on disk.
    import unittest.mock as mock

    with mock.patch.object(skills, "_all_entries", return_value=fake_entries):
        # Direct hit on the short trigger via word boundary.
        assert skills.find_triggered_skills("looking at popx particles") == fake_entries
        assert skills.find_triggered_skills("just need a POP gun")[0]["name"] == "popx-mode"
        # Words that contain "pop" as a prefix must NOT trigger.
        assert skills.find_triggered_skills("the popular library") == []
        assert skills.find_triggered_skills("population growth") == []


def test_find_triggered_skills_substring_for_long_keywords():
    import unittest.mock as mock

    import tdpilot_api_skills as skills

    fake_entries = [
        {
            "name": "performance-mode",
            "triggers": ["optimize", "performance"],
            "text": "perf body",
        },
    ]
    with mock.patch.object(skills, "_all_entries", return_value=fake_entries):
        assert skills.find_triggered_skills("optimize this") == fake_entries
        assert skills.find_triggered_skills("a performance issue") == fake_entries
        assert skills.find_triggered_skills("nothing relevant") == []


def test_find_triggered_skills_case_insensitive():
    import unittest.mock as mock

    import tdpilot_api_skills as skills

    fake_entries = [{"name": "x", "triggers": ["FPS"], "text": "x"}]
    with mock.patch.object(skills, "_all_entries", return_value=fake_entries):
        assert skills.find_triggered_skills("low fps") == fake_entries
        assert skills.find_triggered_skills("Low FPS Now") == fake_entries


def test_find_triggered_skills_empty_or_invalid():
    import unittest.mock as mock

    import tdpilot_api_skills as skills

    fake_entries = [{"name": "x", "triggers": ["foo"], "text": "x"}]
    with mock.patch.object(skills, "_all_entries", return_value=fake_entries):
        assert skills.find_triggered_skills("") == []
        assert skills.find_triggered_skills(None) == []  # type: ignore[arg-type]


def test_runtime_check_skill_triggers_activates_match(monkeypatch):
    """Triggering populates ``_session_skills_activated`` and emits an
    EV_HINT(kind=skill_activated) event so the user sees the auto-load.
    """
    import unittest.mock as mock

    import tdpilot_api_runtime as rt_mod
    import tdpilot_api_skills as skills_mod
    from tdpilot_api_runtime import EV_HINT, AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    fake_entries = [
        {"name": "popx-mode", "triggers": ["popx"], "text": "POPX BODY"},
    ]
    with mock.patch.object(skills_mod, "_all_entries", return_value=fake_entries):
        rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
        # Initial state — nothing activated.
        assert rt._session_skills_activated == {}
        rt._check_skill_triggers("show me how to use POPX")
        assert "popx-mode" in rt._session_skills_activated
        assert rt._session_skills_activated["popx-mode"] == "POPX BODY"

    events = _drain_runtime_events(rt)
    activated = [(k, p) for k, p in events if k == EV_HINT and p.get("kind") == "skill_activated"]
    assert len(activated) == 1
    assert activated[0][1]["name"] == "popx-mode"


def test_runtime_check_skill_triggers_idempotent(monkeypatch):
    """Re-triggering an already-active skill must NOT duplicate the
    body or emit a second EV_HINT.
    """
    import unittest.mock as mock

    import tdpilot_api_runtime as rt_mod
    import tdpilot_api_skills as skills_mod
    from tdpilot_api_runtime import EV_HINT, AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")

    fake_entries = [{"name": "popx-mode", "triggers": ["popx"], "text": "POPX BODY"}]
    with mock.patch.object(skills_mod, "_all_entries", return_value=fake_entries):
        rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
        rt._check_skill_triggers("popx")
        rt._check_skill_triggers("popx again")  # second hit — no-op
        rt._check_skill_triggers("more popx")  # third — still no-op

    assert len(rt._session_skills_activated) == 1
    events = _drain_runtime_events(rt)
    activated = [(k, p) for k, p in events if k == EV_HINT and p.get("kind") == "skill_activated"]
    assert len(activated) == 1, f"hint fired {len(activated)} times, want 1"


def test_runtime_active_skills_section_appears_in_dynamic_context(monkeypatch):
    """Once a skill activates its body is in the dynamic-context
    snapshot for every subsequent turn.
    """
    import unittest.mock as mock

    import tdpilot_api_runtime as rt_mod
    import tdpilot_api_skills as skills_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    fake_entries = [
        {"name": "popx-mode", "triggers": ["popx"], "text": "POPX SKILL BODY"},
    ]
    with mock.patch.object(skills_mod, "_all_entries", return_value=fake_entries):
        rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
        # No skill activated yet — section absent.
        rt._refresh_dynamic_context()
        text_before = ""
        if rt._dynamic_context_snapshot:
            text_before = rt._dynamic_context_snapshot[0]["content"][0]["text"]
        assert "POPX SKILL BODY" not in text_before

        # Activate by trigger, then refresh.
        rt._check_skill_triggers("learn popx")
        rt._refresh_dynamic_context()
        msgs = rt._dynamic_context_snapshot
        assert msgs, "dynamic context empty after skill activation"
        text_after = msgs[0]["content"][0]["text"]
        assert "POPX SKILL BODY" in text_after
        assert "Active Skills" in text_after


def test_runtime_reset_clears_session_skills(monkeypatch):
    """``reset()`` is a fresh session — auto-loaded skills must NOT
    persist into the new conversation.
    """
    import unittest.mock as mock

    import tdpilot_api_runtime as rt_mod
    import tdpilot_api_skills as skills_mod
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    fake_entries = [{"name": "popx-mode", "triggers": ["popx"], "text": "BODY"}]
    with mock.patch.object(skills_mod, "_all_entries", return_value=fake_entries):
        rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
        rt._check_skill_triggers("popx")
        assert rt._session_skills_activated

    rt.reset()
    assert rt._session_skills_activated == {}


def test_build_dynamic_context_extra_sections_listed_first(monkeypatch):
    """Skill bodies are anchored above the volatile indexes in the
    rendered context block.
    """
    import tdpilot_api_knowledge as kb_mod
    import tdpilot_api_memory as mem_mod
    import tdpilot_api_recipes as rec_mod
    from tdpilot_api_runtime import build_dynamic_context

    monkeypatch.setattr(mem_mod, "get_memory_index_content", lambda: "")
    monkeypatch.setattr(kb_mod, "get_knowledge_index_hint", lambda: "kb_hint")
    monkeypatch.setattr(rec_mod, "get_recipes_index_hint", lambda: "")

    msgs = build_dynamic_context(extra_sections=["## Active Skills\n\nA SKILL"])
    assert msgs
    text = msgs[0]["content"][0]["text"]
    skill_pos = text.find("Active Skills")
    kb_pos = text.find("Knowledge Index")
    assert 0 <= skill_pos < kb_pos, "extra_sections must come before volatile indexes"


def test_system_prompt_is_unchanged_by_mid_session_memory_save(monkeypatch, tmp_path):
    """Phase 1.2 + 0.1 jointly: saving a memory mid-session MUST NOT
    mutate the Agent's system_prompt. Otherwise the DeepSeek auto-cache
    busts on the next turn.
    """
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    import tdpilot_api_runtime as rt_mod  # type: ignore[import-not-found]
    from tdpilot_api_runtime import AgentRuntime

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "memory" / "MEMORY.md")

    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    before = rt._agent.system_prompt  # type: ignore[union-attr]

    mem.handle_memory_save(
        {
            "name": "phase12_cache_check",
            "description": "system prompt must stay byte-stable",
            "type": "feedback",
            "content": "x",
        }
    )

    # No call to ``_build_agent`` between save and check — saving a
    # memory does not, and must not, rebuild the Agent. The system
    # prompt the Agent holds in self.system_prompt is the byte-stable
    # one captured at construction.
    after = rt._agent.system_prompt  # type: ignore[union-attr]
    assert before == after
