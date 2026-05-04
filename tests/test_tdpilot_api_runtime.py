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


def test_build_system_prompt_byte_stable_when_memory_changes(tmp_path, monkeypatch):
    """Saving a memory / knowledge / recipe entry between calls must NOT
    change the system prompt — it now lives in dynamic context.
    """
    import tdpilot_api_memory as mem  # type: ignore[import-not-found]
    from tdpilot_api_runtime import build_system_prompt

    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
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
