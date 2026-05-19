"""v2.5.2 — cycle-detect orphan tool_use regression.

Pre-fix behavior (Bug A from the 2026-05-19 live audit):
    When ``CycleDetected`` was raised inside the dispatch for-loop in
    ``Agent._loop``, the synthetic ``tool_result`` blocks for the pending
    ``tool_use`` ids were never appended to ``agent.messages``. The
    persisted conversation (``~/.tdpilot-api/history/<session>.jsonl``)
    then had an orphan ``tool_use`` block with no matching
    ``tool_result`` immediately after — which Anthropic-format
    ``/v1/messages`` rejects with HTTP 400. The chat-pipe became stuck
    until TouchDesigner restart (Reinit Extensions, table clear, and
    JSONL move-aside were all insufficient).

Fix (v2.5.2): synthesize one ``tool_result`` block per pending
``tool_use`` (the offending one PLUS any un-dispatched batch entries
after it), append the resulting ``user``-role message to
``agent.messages`` BEFORE raising ``CycleDetected``. The persisted
conversation stays API-valid and the next ``/send`` succeeds.

These tests pin the message-structure invariant so the bug can't
silently recur.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


# ---------------------------------------------------------------------------
# Minimal urlopen stub (reused pattern from test_tdpilot_api_agent.py)
# ---------------------------------------------------------------------------


class _CtxMgr:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self._value

    def __exit__(self, *exc_info):
        return False


def _mk_response(payload: dict) -> Any:
    body = json.dumps(payload).encode("utf-8")
    return _CtxMgr(SimpleNamespace(read=lambda: body))


def _repeat_tool_call(tool_name: str, args: dict, tu_id: str = "tu_1") -> dict:
    """One assistant turn that emits a single tool_use the model will
    insist on repeating with identical args."""
    return {
        "content": [
            {"type": "text", "text": "Calling tool."},
            {"type": "tool_use", "id": tu_id, "name": tool_name, "input": args},
        ],
        "stop_reason": "tool_use",
    }


def _batched_tool_calls(tool_name: str, args: dict, ids: list[str]) -> dict:
    """One assistant turn with multiple tool_use blocks (a batch)."""
    blocks = [{"type": "text", "text": "Batched call."}]
    for tu_id in ids:
        blocks.append({"type": "tool_use", "id": tu_id, "name": tool_name, "input": args})
    return {"content": blocks, "stop_reason": "tool_use"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cycle_detect_appends_synthetic_tool_result_before_raising():
    """After CycleDetected raises, every assistant tool_use_id in
    agent.messages MUST be matched by a tool_result block in the next
    user-role message."""
    from tdpilot_api_agent import Agent, AgentError
    from tdpilot_api_cycle_detector import CycleDetected, CycleLedger

    # Threshold 2 → fires on the 2nd identical call.
    ledger = CycleLedger(threshold=2)

    def dispatcher(name, args):
        # Always returns OK — the cycle-detect halt happens BEFORE
        # dispatch on the 2nd hit, so this body only runs once.
        return {"ok": True}

    # Model insists on the same tool with same args repeatedly.
    responses = iter(
        [
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_first"),
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_second"),
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_third"),
        ]
    )

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        cycle_ledger_factory=lambda: ledger,
    )
    agent.add_user_message("Check errors repeatedly please.")

    raised: list[CycleDetected] = []
    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(responses))
        try:
            agent.run_turn()
        except CycleDetected as exc:
            raised.append(exc)
        except AgentError:
            # AgentError is the parent class — CycleDetected may surface
            # under that name if the run_turn wrapper re-raises.
            pass

    # --- The invariant ---
    # Every assistant message with a tool_use block must be followed by
    # a user message whose tool_result tool_use_ids match.
    assistant_tool_use_ids: list[str] = []
    matched_tool_result_ids: list[str] = []
    for msg in agent.messages:
        if msg["role"] == "assistant":
            for blk in msg.get("content", []):
                if blk.get("type") == "tool_use":
                    assistant_tool_use_ids.append(blk["id"])
        elif msg["role"] == "user":
            for blk in msg.get("content", []):
                if blk.get("type") == "tool_result":
                    matched_tool_result_ids.append(blk.get("tool_use_id"))

    # Critical: NO orphan tool_use ids — all matched.
    unmatched = set(assistant_tool_use_ids) - set(matched_tool_result_ids)
    assert not unmatched, f"orphan tool_use ids (pre-fix bug): {unmatched}; messages = {agent.messages}"


def test_cycle_detect_synthetic_result_marks_is_error_true():
    """The synthesized tool_result for the cycle-killed call carries
    is_error=True and a _tool_error sentinel in its content. Without
    these, downstream recovery hints + activity-ring don't pick the
    failure up correctly."""
    from tdpilot_api_agent import Agent, AgentError
    from tdpilot_api_cycle_detector import CycleDetected, CycleLedger

    ledger = CycleLedger(threshold=2)

    def dispatcher(name, args):
        return {"ok": True}

    responses = iter(
        [
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_first"),
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_second"),
        ]
    )

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        cycle_ledger_factory=lambda: ledger,
    )
    agent.add_user_message("Check errors.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(responses))
        try:
            agent.run_turn()
        except (CycleDetected, AgentError):
            pass

    # Find the tool_result for the 2nd tool_use_id ('tu_second').
    matched = None
    for msg in agent.messages:
        if msg["role"] == "user":
            for blk in msg.get("content", []):
                if blk.get("type") == "tool_result" and blk.get("tool_use_id") == "tu_second":
                    matched = blk
                    break
    assert matched is not None, (
        f"expected synthetic tool_result for tu_second; got messages = {agent.messages}"
    )
    assert matched.get("is_error") is True
    body = json.loads(matched["content"])
    assert body.get("_tool_error") is True
    assert "cycle_detected" in body.get("error", "")
    assert "td_get_errors" in body.get("error", "")


def test_cycle_detect_batched_call_synthesizes_for_remaining_uses_too():
    """If the model batched several tool_use blocks in one response,
    and the FIRST one trips cycle-detect, the runtime must still
    synthesize tool_result blocks for the remaining un-dispatched
    tool_uses in the same batch — otherwise the API would still see
    orphan ids."""
    from tdpilot_api_agent import Agent, AgentError
    from tdpilot_api_cycle_detector import CycleDetected, CycleLedger

    ledger = CycleLedger(threshold=2)

    def dispatcher(name, args):
        return {"ok": True}

    # 1st turn: one tool_use (records count=1).
    # 2nd turn: a batch of 3 tool_uses with the SAME (tool_name, args)
    #   so cycle-detect trips on the 1st of the batch (count=2).
    responses = iter(
        [
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_1"),
            _batched_tool_calls(
                "td_get_errors",
                {"path": "/x"},
                ids=["tu_2a", "tu_2b", "tu_2c"],
            ),
        ]
    )

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        cycle_ledger_factory=lambda: ledger,
    )
    agent.add_user_message("Check errors.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(responses))
        try:
            agent.run_turn()
        except (CycleDetected, AgentError):
            pass

    # All three batch tool_use_ids must have matching tool_result blocks.
    expected_ids = {"tu_2a", "tu_2b", "tu_2c"}
    found_result_ids: set[str] = set()
    for msg in agent.messages:
        if msg["role"] == "user":
            for blk in msg.get("content", []):
                if blk.get("type") == "tool_result":
                    found_result_ids.add(blk.get("tool_use_id"))
    missing = expected_ids - found_result_ids
    assert not missing, (
        f"batch tool_use_ids without matching tool_result (pre-fix would orphan them): {missing}"
    )


def test_cycle_detect_messages_end_in_user_role_not_assistant():
    """The terminal message after CycleDetected MUST be role=user
    (carrying the synthetic tool_result blocks). If the terminal message
    is role=assistant (the model's batch with orphan tool_use), the next
    /v1/messages send will 400 — that's the exact Bug A symptom."""
    from tdpilot_api_agent import Agent, AgentError
    from tdpilot_api_cycle_detector import CycleDetected, CycleLedger

    ledger = CycleLedger(threshold=2)

    def dispatcher(name, args):
        return {"ok": True}

    responses = iter(
        [
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_1"),
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_2"),
        ]
    )

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        cycle_ledger_factory=lambda: ledger,
    )
    agent.add_user_message("Check errors.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(responses))
        try:
            agent.run_turn()
        except (CycleDetected, AgentError):
            pass

    last = agent.messages[-1]
    assert last["role"] == "user", (
        "terminal message after cycle-detect must be user-role (carrying "
        "the synthetic tool_result); if it's assistant, Bug A regressed"
    )
    # And it must contain at least one tool_result block.
    types = [blk.get("type") for blk in last["content"]]
    assert "tool_result" in types


def test_v253_cycle_detect_preserves_rollback_hint_on_terminal_result():
    """v2.5.3 — Codex P2 follow-up on PR #51.

    When a rollback_guard fires during the same batch where cycle-detect
    trips, the synthetic tool_result message MUST carry the guard's
    ``hint_text``. Without this fix, the v2.5.2 code appended messages
    BEFORE the outer try/finally ran ``rollback_guard.__exit__``, so
    ``_apply_rollback_hint`` (which lives AFTER the finally at
    ``tdpilot_api_agent.py:1219``) never got to attach the hint. The
    persisted message claimed the mutation succeeded when it had been
    rolled back — corrupting context for the next /send.

    Fix: run ``rollback_guard.__exit__`` + ``_apply_rollback_hint``
    inside the cycle-detect block BEFORE appending messages, then
    null ``rollback_guard`` so the outer ``finally`` skips a 2nd exit.
    """
    from tdpilot_api_agent import Agent, AgentError
    from tdpilot_api_cycle_detector import CycleDetected, CycleLedger

    class FakeGuard:
        """Minimal AutoRollbackGuard stand-in. Populates hint_text on __exit__
        to mimic the real guard's behavior when it detects a regression
        and runs a rollback."""

        EXPECTED_HINT = (
            "Auto-rollback: 1 critical TD error introduced — reverted via "
            "tdpilot_auto_rollback. Path: /test/x"
        )

        def __init__(self):
            self.entered = False
            self.exited = False
            self.exit_count = 0
            self.hint_text = ""

        def __enter__(self):
            self.entered = True
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            self.exit_count += 1
            self.exited = True
            self.hint_text = self.EXPECTED_HINT
            return False  # do not suppress

    guard = FakeGuard()
    ledger = CycleLedger(threshold=2)

    def dispatcher(name, args):
        return {"ok": True}

    responses = iter(
        [
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_1"),
            _repeat_tool_call("td_get_errors", {"path": "/x"}, tu_id="tu_2"),
        ]
    )

    agent = Agent(
        api_key="sk-fake",
        dispatcher=dispatcher,
        cycle_ledger_factory=lambda: ledger,
        rollback_guard_factory=lambda d, names: guard,
    )
    agent.add_user_message("Check errors.")

    with patch("urllib.request.urlopen") as urlopen:
        urlopen.side_effect = lambda *a, **k: _mk_response(next(responses))
        try:
            agent.run_turn()
        except (CycleDetected, AgentError):
            pass

    # Guard was exited at least once. (The shared guard instance gets
    # entered/exited once per while-loop iteration in _loop — first turn
    # exits normally, second turn's cycle-detect path also runs __exit__
    # via the v2.5.3 fix and then nulls rollback_guard so the outer
    # finally skips a 3rd exit.)
    assert guard.exited is True
    assert guard.exit_count >= 1, "guard.__exit__ must run at least once"

    # The terminal user-role message must contain a tool_result whose content
    # carries the rollback hint text (appended by _apply_rollback_hint).
    last = agent.messages[-1]
    assert last["role"] == "user"
    tool_results = [blk for blk in last["content"] if blk.get("type") == "tool_result"]
    assert tool_results, "expected at least one tool_result in the terminal message"

    # Per _apply_rollback_hint logic, the hint lands on the LAST tool_result.
    # Content may be str (hint appended with "\n\n") or list (hint as text block).
    last_tr = tool_results[-1]
    content = last_tr.get("content")
    if isinstance(content, str):
        assert FakeGuard.EXPECTED_HINT in content, (
            f"rollback hint missing from last tool_result; got content: {content[:300]}"
        )
    elif isinstance(content, list):
        joined = json.dumps(content)
        assert FakeGuard.EXPECTED_HINT in joined, (
            f"rollback hint missing from last tool_result blocks; got: {joined[:300]}"
        )
    else:
        raise AssertionError(f"unexpected tool_result content shape: {type(content).__name__}")
