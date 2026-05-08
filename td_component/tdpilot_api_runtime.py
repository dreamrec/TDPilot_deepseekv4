"""
TDPilot API — runtime glue between the agent loop and TD's cook thread.

The Agent class (tdpilot_api_agent) is sync and pure-Python. It must run
on a worker thread because urllib.urlopen blocks. But all TD API calls
(op(), CHOP/DAT writes, parameter updates) MUST happen on the cook
thread, or TD will crash or silently corrupt state.

This module provides:
  - AgentRuntime: holds an Agent instance, a Queue of UI events, and a
    drain function the COMP polls each frame to apply events safely.
  - start_turn(text): adds a user message and kicks off the worker.
  - drain_events(): called from a Timer/Execute DAT each cook to flush
    UI updates marshalled from the worker.

The COMP's extension wires:
  - Send button pulse        → AgentRuntime.start_turn(input_field.text)
  - Frame execute callback   → AgentRuntime.drain_events()
  - Stop pulse               → AgentRuntime.stop()
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from typing import Any


def _make_thread_safe(value: Any) -> Any:
    """Convert a handler result into a JSON-safe nested structure.

    TD operators are not safe to reference from non-cook threads — even
    `str(td_op)` triggers TD's THREAD CONFLICT dialog. The CookThreadDispatcher
    runs handlers on the cook thread and stores their return values for the
    worker thread to pick up; if a handler returns a dict containing TD ops
    (or other unhashable/non-JSON objects), the worker would crash or pop
    the THREAD CONFLICT dialog when it later json.dumps() the result for the
    model. Doing the round-trip here, on the cook thread, captures the string
    form of every TD-op reference safely.
    """
    try:
        return json.loads(json.dumps(value, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        # Last resort — coerce to string. Still happens on the cook thread
        # so any TD-op references are stringified safely here.
        return {"_unserializable": str(value)}


from tdpilot_api_agent import Agent, AgentError  # type: ignore[import-not-found]
from tdpilot_api_config import (  # type: ignore[import-not-found]
    fetch_api_key,
    redact,
    resolved_config,
)

# ---------------------------------------------------------------------------
# Cook-thread dispatcher
# ---------------------------------------------------------------------------
# TouchDesigner's Python API (op(), parent(), parameter writes, etc.) is NOT
# thread-safe. The agent loop runs on a worker thread because urlopen blocks;
# tool dispatch must therefore marshal back onto the cook thread before
# touching TD. We use a producer/consumer queue: the worker submits
# (call_id, name, args) and waits on a Condition; the cook thread (driven by
# AgentRuntime.pump_dispatcher() each frame) pops requests, runs the raw
# dispatcher, and signals completion.
#
# This trades a frame of latency per tool call for thread safety. With
# typical TD cook rates (60 FPS) that's ~16ms per tool call — negligible
# compared to the multi-hundred-ms API roundtrip the loop is already paying.

DEFAULT_TOOL_TIMEOUT = 60.0  # seconds — generous because td_screenshot can be slow.


class CookThreadDispatcher:
    """Wraps a raw dispatcher so worker-thread calls execute on the cook thread.

    Construct with the dispatcher that does the actual TD work (typically
    `make_dispatcher(handlers_module)` from tdpilot_api_dispatcher). Pass
    THIS object to the Agent as its dispatcher.
    """

    def __init__(self, raw_dispatcher: Callable[[str, dict], Any], timeout: float = DEFAULT_TOOL_TIMEOUT):
        self._raw = raw_dispatcher
        self._timeout = timeout
        self._pending: Queue = Queue()  # (call_id, name, args)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._results: dict[str, Any] = {}

    def __call__(self, name: str, args: dict) -> Any:
        """Called from the worker thread. Blocks until cook-thread pump runs the tool."""
        call_id = uuid.uuid4().hex
        self._pending.put((call_id, name, args))
        with self._cond:
            deadline = time.monotonic() + self._timeout
            while call_id not in self._results:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"error": f"Tool {name} timed out after {self._timeout}s"}
                self._cond.wait(timeout=remaining)
            return self._results.pop(call_id)

    def pump(self, max_per_pump: int = 8) -> int:
        """Called from the cook thread once per frame. Drains up to max calls.

        The handler result is eagerly converted to a JSON-safe nested
        structure HERE on the cook thread — see _make_thread_safe. This
        prevents TD's THREAD CONFLICT detector from firing when the worker
        thread later inspects or stringifies the result.
        """
        ran = 0
        for _ in range(max_per_pump):
            try:
                call_id, name, args = self._pending.get_nowait()
            except Empty:
                break
            try:
                raw = self._raw(name, args)
                result = _make_thread_safe(raw)
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"{type(exc).__name__}: {exc}"}
            with self._cond:
                self._results[call_id] = result
                self._cond.notify_all()
            ran += 1
        return ran

    def cancel_pending(self) -> None:
        """Wake any worker blocked on a tool call. Used when the agent is reset."""
        # Drain the queue and produce timeout-style errors for everything
        # currently outstanding, so the worker returns instead of hanging.
        with self._cond:
            while True:
                try:
                    call_id, name, _args = self._pending.get_nowait()
                except Empty:
                    break
                self._results[call_id] = {"error": f"Tool {name} cancelled"}
            self._cond.notify_all()


# Event types pushed by the worker, drained by the cook thread.
EV_TEXT = "text"  # payload: str
EV_TOOL_CALL = "tool_call"  # payload: {"name": str, "args": dict}
EV_TOOL_RESULT = "tool_res"  # payload: {"name": str, "is_error": bool, "result": Any}
EV_DONE = "done"  # payload: str (final text)
EV_ERROR = "error"  # payload: str (redacted message)
EV_STATE = "state"  # payload: str ("idle"|"calling"|"thinking")
EV_MODEL = "model"  # payload: {"tier": str, "model": str} — Sprint 4.3
# Phase 2 (1.8.0) — per-API-call token usage. Fires multiple times per
# turn (one per tool-use round trip). Frontend accumulates into a
# per-turn meter and resets on EV_DONE / EV_ERROR. All fields optional
# — DeepSeek's compat layer may omit some depending on model version.
EV_USAGE = "usage"  # payload: {"input_tokens": int, "output_tokens": int, "cache_read_input_tokens": int}
# Phase 1.3 — severity-tracked validation hint. Emitted at turn end
# when high-severity mutations went out without a follow-up validator
# call. Soft signal; chat UI renders as a subtle nudge below the
# final assistant text.
EV_HINT = "hint"  # payload: {"kind": str, "message": str, "tools": list[str]}
# Sprint 4.1 — subagent events. Forwarded by SubagentManager into the
# parent runtime's event queue so the chat UI can display sub-task
# progress under collapsible [worker:<id>] sections.
EV_SUB_TEXT = "subagent_text"  # payload: {"id": str, "text": str}
EV_SUB_TOOL = "subagent_tool"  # payload: {"id": str, "name": str, "args": dict}
EV_SUB_DONE = (
    "subagent_done"  # payload: {"id": str, "status": str, "result", "error", "tool_calls", "duration_ms"}
)


# Phase 1.3 — mutation-severity classifier. The runtime tracks tool
# calls per turn; if a turn included one or more HIGH-severity
# mutations without a follow-up validator call, EV_HINT fires at
# turn end. Soft signal — never blocks the conversation.
#
# Severity rationale:
#   - high: changes that can leave a network in a broken state if the
#     model's mental model diverges from the TD reality. exec_python
#     in particular can do anything; create_node + delete_node +
#     wire/unwire mutate topology.
#   - medium: parameter changes that may or may not have downstream
#     effects depending on the operator. Currently we don't emit hints
#     for medium — the noise/signal ratio is too low.
#   - low: reads. Inspections.
#
# Validators that satisfy a high-severity mutation:
#   td_get_errors  - canonical post-mutation check.
#   td_audit_project - whole-project sanity sweep.
#   td_validate_recipe - asserts recipe consistency.
_TOOL_SEVERITY: dict[str, str] = {
    "td_create_node": "high",
    "td_delete_node": "high",
    "td_disconnect": "high",
    "td_connect_nodes": "high",
    "td_exec_python": "high",
    "td_set_content": "high",
    "td_copy_node": "high",
    "td_rename_node": "high",
    "td_create_macro": "high",
    "patch_begin": "high",
    "patch_commit": "high",
    "recipe_replay": "high",
    "td_set_params": "medium",
    "td_pulse_param": "medium",
    "td_custom_parameters": "medium",
}

_VALIDATOR_TOOLS: frozenset[str] = frozenset(
    (
        "td_get_errors",
        "td_audit_project",
        "td_validate_recipe",
        "patch_validate",
    )
)


def _tool_severity(name: str) -> str:
    """Return ``"high" | "medium" | "low"`` for a tool name. Unknown
    tools default to ``"low"`` (read-only assumption — they don't
    contribute to the validation-hint signal). Severity is data, not
    policy: callers decide what to do with it.
    """
    return _TOOL_SEVERITY.get(name, "low")


# Phase 2 (1.8.0) — fields the chat token meter can render. Anything
# else in the upstream usage dict is dropped: status-bar payloads
# travel over the WS many times per turn, so we ship a known-safe
# subset rather than forwarding whatever DeepSeek's compat layer
# happens to attach this week.
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _sanitise_usage(usage: Any) -> dict[str, int]:
    """Coerce a raw usage dict to ``{field: int}`` with non-int and
    missing fields dropped to 0. The frontend treats every key as
    optional; this guarantees the on-the-wire payload is JSON-stable
    and never leaks an arbitrary value (an experimental field name
    DeepSeek might flip on someday)."""
    if not isinstance(usage, dict):
        return {}
    out: dict[str, int] = {}
    for k in _USAGE_FIELDS:
        v = usage.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            out[k] = v
        elif isinstance(v, float):
            out[k] = int(v)
    return out


SYSTEM_PROMPT_BASE = (
    "You are TDPilot API, an AI assistant operating inside TouchDesigner. "
    "You have direct access to the TD network through tools.\n\n"
    "Operating protocol:\n"
    "  1. Inspect before mutating — prefer td_get_info / td_get_nodes / "
    "td_get_node_detail before creating or modifying anything.\n"
    "  2. Build in small verifiable steps: create -> wire -> parameterize -> verify.\n"
    "  3. After a multi-step mutation, call td_get_errors on the affected root "
    "and surface any remaining warnings.\n"
    "  4. Be token-frugal: avoid td_screenshot unless explicitly needed.\n"
    "  5. For risky multi-step builds, call td_project_lifecycle action=save "
    "FIRST so you can undo back if something breaks.\n\n"
    "Critical rules for TouchDesigner type names:\n"
    "  * Operator types ALWAYS include the family suffix in camelCase: "
    "'noiseTOP', 'levelTOP', 'boxSOP', 'sphereSOP', 'gridSOP', "
    "'constantCHOP', 'lfoCHOP', 'textDAT', 'tableDAT', 'phongMAT', "
    "'geometryCOMP', 'cameraCOMP', 'lightCOMP', 'baseCOMP', 'containerCOMP'.\n"
    "  * NEVER use 'box', 'sphere', 'noise', 'level' on their own.\n"
    "  * Type names are case-sensitive. If td_create_node returns 'Unknown "
    "operator type', the name is wrong — DON'T retry the same name. Call "
    "td_list_families to discover valid op types in that family.\n"
    "  * Common pitfalls: video input is 'videodeviceinTOP' (all lowercase "
    "between 'video' and 'TOP'), NOT 'videoDeviceInTOP'. Movie file in is "
    "'moviefileinTOP'. Audio device in is 'audiodeviceinCHOP'. When unsure, "
    "td_list_families.\n\n"
    "Critical rules for paths in td_exec_python:\n"
    "  * Inside td_exec_python, `parent()` resolves to the COMP that runs "
    "the agent — typically /project1/tdpilot_API. It is NOT the project root.\n"
    "  * To target the project root, ALWAYS use `op('/project1')` with an "
    "absolute path. Never rely on `parent()` for places outside the agent COMP.\n"
    "  * Prefer the dedicated tools (td_create_node, td_set_params, "
    "td_connect_nodes, td_delete_node) over td_exec_python for normal "
    "operations — they're more reliable and don't have scope traps.\n\n"
    "Memory protocol:\n"
    "  * You have a persistent memory at ~/.tdpilot-api/memory/. The "
    "MEMORY.md index below this section is auto-injected each turn.\n"
    "  * SAVE memory whenever you learn something non-obvious — user "
    "preferences, validated approaches, project context, references. Use "
    "memory_save with the right type (user/feedback/project/reference). "
    "Record from BOTH corrections AND successes: if you only save "
    "corrections you'll drift away from approaches the user has already "
    "validated.\n"
    "  * RECALL memory when relevant — call memory_get on a specific "
    "indexed entry, or memory_recall(query) for BM25 search. Do this "
    "BEFORE asking the user clarifying questions when you suspect the "
    "answer is already remembered.\n"
    "  * Memory file format: frontmatter (name, description, type) + "
    "markdown body. For feedback/project entries, structure the body as: "
    "rule/fact, then **Why:** line, then **How to apply:** line.\n"
    "  * If a memory turns out wrong, use memory_delete or save an updated "
    "version with the same name — same name overwrites.\n\n"
    "Knowledge protocol:\n"
    "  * You have a bundled TD knowledge corpus (operator families, "
    "Python idioms, common pitfalls) plus user-added entries in "
    "~/.tdpilot-api/knowledge/. The list below this section is the index.\n"
    "  * SEARCH knowledge BEFORE guessing on TD specifics — operator-type "
    "capitalisation, type names, Python idioms, threading rules, pull-"
    "cooking gotchas. Call knowledge_search(query) first; if a hit looks "
    "promising, knowledge_get(name) for full content.\n"
    "  * If you discover a TD specific that's NOT in the corpus and "
    "would be valuable for future sessions, save it via knowledge_add "
    "(persists in ~/.tdpilot-api/knowledge/, NOT in memory).\n"
    "  * Trust order on search hits (every match carries a "
    "`trust_tier` field): official > bundled > personal > community > "
    "transcript > experimental. Official docs and live TD state "
    "answer facts. Community / transcript hits suggest approaches — "
    "validate via td_get_errors, td_screenshot, or td_get_operator_doc "
    "before claiming behavior is correct.\n\n"
    "Recipe protocol:\n"
    "  * After a multi-step build (>3 tool calls) that the user might want "
    "to reproduce later, OFFER to save it as a recipe via recipe_save. "
    "Capture the exact tool-call sequence in the `replay` field — this "
    "is what makes recipe_replay work later.\n"
    '  * When the user references prior work ("do that thing again", '
    '"that audio reactive setup", "the noise field we built"), call '
    "recipe_recall(query) FIRST. If a match looks right, recipe_get to "
    "show the user the steps, then recipe_replay to reproduce.\n"
    "  * Before recipe_replay, consider calling td_project_lifecycle "
    "action=save so you can undo if a step fails (replay aborts on first "
    "error — no automatic rollback yet).\n\n"
    "Skills protocol:\n"
    "  * Skills are on-demand behaviour modulators — discipline rules + "
    "protocols for specific workflows (POPx particles, performance "
    "optimization, etc.). The Skills Index below this section lists all "
    "available ones with their triggers.\n"
    "  * When the user's task matches a skill's triggers (e.g. user "
    "mentions POPs → popx-mode; user reports lag → performance-mode), "
    "call skill_load(name). The returned content is AUTHORITATIVE for "
    "the rest of the turn — it layers on top of your base discipline.\n"
    "  * skill_get(name) reads a skill without committing to follow it; "
    "use when you want to consult but the user's task isn't a strong fit.\n\n"
    "Safety / patch protocol:\n"
    "  * For risky multi-step builds the user can't easily reproduce, "
    "snapshot_save BEFORE starting — that's a full .toe save the user "
    "can manually restore if everything goes wrong.\n"
    "  * For transactional safety WITHIN a turn, use patch_begin to "
    "open a TD undo block, do your operations normally, patch_validate "
    "to spot-check errors, then patch_commit on success or "
    "patch_rollback on failure. Rollback reverts the entire sequence "
    "atomically — no half-built network left behind.\n"
    "  * recipe_replay(transactional=true) does the same for recipe "
    "execution — strongly recommended for any recipe with >5 tool calls.\n"
    "  * Only ONE patch session can be active at a time. Always "
    "commit or rollback before patch_begin'ing again."
)


def build_system_prompt() -> str:
    """Return the byte-stable system prompt for the session.

    Phase 0.1 contract: this string MUST be byte-identical across every
    turn within a session so DeepSeek's auto-cache (~50× discount on
    cached input tokens) hits. Anything that varies turn-to-turn lives
    in :func:`build_dynamic_context`, NOT here.

    What's allowed in here:
      - SYSTEM_PROMPT_BASE (immutable string literal).
      - Skills index hint + auto-loaded skill bodies. Skills are loaded
        from disk at COMP startup; mid-session skill additions are rare
        enough that we treat the index as session-stable. (If a user
        DOES add skills mid-session, they take effect on the next .tox
        load — acceptable trade-off for cache stability.)

    What's NOT allowed:
      - Memory index — changes every time ``memory_save`` runs.
      - Knowledge index — changes every time ``knowledge_add`` runs.
      - Recipes index — changes every time ``recipe_save`` runs.
      Those are emitted by :func:`build_dynamic_context` instead and
      injected as synthetic messages right after the system prompt.
    """
    parts = [SYSTEM_PROMPT_BASE]

    # Skills: index hint (always) + auto-load skill bodies (when a skill
    # has auto_load=true in frontmatter). Auto-load skills are rare but
    # let the user/maintainer pin always-on disciplines. Both are
    # session-stable — they're read from disk once at COMP startup.
    try:
        from tdpilot_api_skills import (  # type: ignore[import-not-found]
            get_auto_load_skills_text,
            get_skills_index_hint,
        )

        skills_hint = get_skills_index_hint()
        if skills_hint and skills_hint.strip():
            parts.append("\n\n## Skills Index — call skill_load(name) to activate one\n\n" + skills_hint)
        auto_skills = get_auto_load_skills_text()
        if auto_skills and auto_skills.strip():
            parts.append("\n\n## Auto-loaded Skills (always active)\n\n" + auto_skills)
    except Exception as exc:
        print(f"[tdpilot_API/runtime] skills injection failed: {exc}")

    return "".join(parts)


# Delimiter that flags a synthetic context message to the model. Phase
# 0.1 — keeping it as a literal lets the LLM recognise these blocks as
# ambient state rather than user instructions, and lets future tooling
# grep for them. Intentionally non-secret; the model can echo it freely.
DYNAMIC_CONTEXT_DELIMITER = "[[TDPILOT_CONTEXT]]"


def build_dynamic_context(extra_sections: list[str] | None = None) -> list[dict]:
    """Return a list of synthetic messages carrying volatile session state.

    Phase 0.1 — these messages are prepended to the conversation history
    on EVERY API call. They are NOT persisted in ``Agent.messages``, so
    the model sees a fresh snapshot each turn while the conversation
    history itself stays cache-friendly.

    Returns either an empty list (no volatile state worth injecting) or
    a paired ``[user, assistant]`` so that the user→user/assistant
    alternation invariant of the Anthropic message format is preserved
    when the conversation continues.

    Collected sections:
      - ``extra_sections`` (Phase 3.1) — caller-supplied prepended
        blocks, currently used for triggered-skill bodies the runtime
        wants to keep active across turns. Listed FIRST so they
        anchor the model's attention before the volatile indexes.
      - Memory index (``memory_save`` writes invalidate it).
      - Knowledge index (``knowledge_add`` writes invalidate it).
      - Recipes index (``recipe_save`` writes invalidate it).
    """
    sections: list[str] = list(extra_sections or [])

    try:
        from tdpilot_api_memory import get_memory_index_content  # type: ignore[import-not-found]

        mem_index = get_memory_index_content()
        if mem_index and mem_index.strip():
            sections.append(
                "## Memory Index — files you can recall via memory_get / memory_recall\n\n"
                + mem_index.strip()
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] dynamic memory index failed: {exc}")

    try:
        from tdpilot_api_knowledge import get_knowledge_index_hint  # type: ignore[import-not-found]

        kb_hint = get_knowledge_index_hint()
        if kb_hint and kb_hint.strip():
            sections.append(
                "## Knowledge Index — call knowledge_search(query) or knowledge_get(name)\n\n"
                + kb_hint.strip()
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] dynamic knowledge index failed: {exc}")

    try:
        from tdpilot_api_recipes import get_recipes_index_hint  # type: ignore[import-not-found]

        recipes_hint = get_recipes_index_hint()
        if recipes_hint and recipes_hint.strip():
            sections.append(
                "## Recipes — call recipe_recall(query) / recipe_replay(name)\n\n" + recipes_hint.strip()
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] dynamic recipes index failed: {exc}")

    if not sections:
        return []

    body = DYNAMIC_CONTEXT_DELIMITER + "\n\n" + "\n\n".join(sections)
    return [
        {"role": "user", "content": [{"type": "text", "text": body}]},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Acknowledged — I'll use those indexes alongside the conversation.",
                }
            ],
        },
    ]


# Backwards-compat alias — older code referenced SYSTEM_PROMPT directly.
# New code should call build_system_prompt() (byte-stable) and rely on
# build_dynamic_context() for per-turn state.
SYSTEM_PROMPT = SYSTEM_PROMPT_BASE


class AgentRuntime:
    """One per chat session. Owns a worker thread and an event queue."""

    def __init__(
        self,
        dispatcher,  # callable(name, args) -> result. Wrapped in CookThreadDispatcher.
        tools: list[dict],
        system_prompt: str | None = None,
        config: dict | None = None,
    ) -> None:
        # Default to dynamic system-prompt construction so the memory
        # index gets re-injected on every runtime rebuild. Callers can
        # still pass an explicit string for tests or special-purpose
        # agents.
        if system_prompt is None:
            system_prompt = build_system_prompt()
        self._events: Queue = Queue()
        # The Agent always sees the cook-thread-safe wrapper so it never
        # touches TD from the worker thread.
        self._cook_dispatcher = CookThreadDispatcher(dispatcher)
        self._dispatcher = self._cook_dispatcher
        # Keep the RAW dispatcher reachable for handlers that need to
        # invoke other tools synchronously (e.g. handle_recipe_replay,
        # handle_tool_batch). The recipe / batch handlers run on the
        # cook thread already; calling ``self._dispatcher`` (the
        # cook-thread wrapper) would deadlock the cook thread waiting
        # for its own queue to drain. Public access is via the
        # ``raw_dispatcher`` property — see the property below.
        self._raw_dispatcher = dispatcher
        self._tools = tools
        self._system_prompt = system_prompt
        self._config = config or resolved_config()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

        # Phase 1.3 — per-turn validation tracking. Worker thread fills
        # ``_turn_tool_calls`` via the on_tool_result callback; cook
        # thread reads it at turn end (in the on_turn_done handler) to
        # decide whether to emit EV_HINT. The list is cleared on every
        # ``start_turn``. Race is benign: both reads and writes happen
        # in producer/consumer order around the worker's lifecycle.
        self._turn_tool_calls: list[str] = []

        # Phase 3.1 — triggered skill bodies, persisted across turns
        # within this session. Once a skill activates (its trigger
        # keyword appeared in a user message) its body lives here and
        # gets prepended to the dynamic context every turn until
        # ``reset()``. ``dict[name → body]`` so re-triggering the same
        # skill is a no-op. Initialised BEFORE the first
        # ``_refresh_dynamic_context`` call below — the refresh reads
        # from this dict.
        self._session_skills_activated: dict[str, str] = {}

        # Phase 2.2 — pre-turn retrieval. Default on. Disabled via
        # ``config["pre_retrieval"] = False`` (settable from a COMP
        # toggle param). When enabled, ``start_turn`` runs cheap
        # local retrieval (memory_recall + recipe_recall +
        # knowledge_search) and prepends the top hits to the dynamic
        # context — the model gets context-aware retrieval without
        # having to spend a tool round-trip on it.
        self._pre_retrieval_enabled: bool = bool(self._config.get("pre_retrieval", True))

        # Phase 4.1 — per-turn observability traces. Records turn
        # timing + every tool call's name/args_hash/latency/ok into
        # ~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl. Async writer so
        # the cook thread never blocks. Disable via
        # ``config["trace_logging"] = False``.
        self._tool_call_starts: dict[str, float] = {}
        # Phase 2 (1.8.0) — always-on per-tool latency clock for the
        # chat UI's "(123ms)" badge. Lives independently of the tracer
        # so the latency stays available even when trace_logging is
        # disabled. Drained on every matching tool_result.
        self._tool_started_monotonic: dict[str, float] = {}
        self._trace_dir_override: Path | None = self._config.get("traces_dir")  # type: ignore[assignment]
        self._tracer = self._build_tracer()

        # Phase 4.3 — conversation compaction. When the agent's
        # message history grows past the threshold, the oldest portion
        # gets summarised into a single synthetic assistant message
        # and the most-recent ``compaction_keep_recent`` messages are
        # kept verbatim (preserving their original thinking blocks).
        # Disable by setting ``config["compaction_threshold"] = 0``.
        # See td_component/tdpilot_api_compaction.py for the design
        # rationale on the thinking-block contract.
        self._compactor = self._build_compactor()

        # Phase 0.1 — dynamic context snapshot. ``build_dynamic_context``
        # touches TD globals (parent().op('kb') for bundled knowledge
        # entries) so it MUST run on the cook thread. We refresh this
        # field on every ``start_turn`` (which IS the cook thread — the
        # COMP extension calls it from a button pulse / param exec) and
        # the worker-thread Agent reads the cached snapshot via
        # ``_dynamic_context_snapshot``. No locking needed: each turn's
        # write happens-before its worker-thread reads.
        self._dynamic_context_snapshot: list[dict] = []
        self._refresh_dynamic_context()

        self._agent: Agent | None = None
        self._build_agent()

    # ------------------------------------------------------------------
    # Agent lifecycle
    # ------------------------------------------------------------------

    def _build_agent(self) -> None:
        api_key = fetch_api_key()
        if not api_key:
            self._agent = None
            return
        cfg = self._config
        self._agent = Agent(
            api_key=api_key,
            dispatcher=self._dispatcher,
            tools=self._tools,
            system_prompt=self._system_prompt,
            # Phase 0.1 — volatile per-turn context (memory / knowledge /
            # recipes indexes) is injected as synthetic messages at API
            # call time so the system prompt itself stays byte-stable
            # (DeepSeek auto-cache hits at ~50× discount on cached
            # input tokens). The snapshot is built on the cook thread
            # in ``_refresh_dynamic_context`` (called from start_turn);
            # the Agent worker thread just reads the cached list, never
            # touching TD globals like parent().op('kb').
            dynamic_context_provider=lambda: list(self._dynamic_context_snapshot),
            # Phase 4.3 — conversation compaction. The agent calls
            # ``maybe_compact`` at the top of each ``_loop`` iteration.
            # None (no compactor) means the history grows without
            # bound — fine for short sessions, risky for 50+ turn ones.
            compactor=self._compactor,
            model=cfg["model"],
            base_url=cfg["base_url"],
            max_tokens=cfg["max_tokens"],
            temperature=cfg["temperature"],
            turn_budget=cfg["turn_budget"],
            # Sprint 4.3 — multi-model routing. tier from COMP param
            # (auto/flash/pro); Agent picks model at turn-start based
            # on user message + tier.
            model_tier=cfg.get("model_tier", "auto"),
            flash_model=cfg.get("flash_model", "deepseek-v4-flash"),
            on_text=lambda s: self._push(EV_TEXT, s),
            on_tool_call=lambda n, a: (
                self._trace_tool_started(n, a),
                self._push_tool_call_event(n, a),
            ),
            on_tool_result=lambda n, r, e: (
                self._record_tool_call(n, e, r),
                self._trace_record_tool(n, r, e),
                self._push_tool_result_event(n, r, e),
            ),
            on_turn_done=lambda s: (
                self._maybe_emit_validation_hint(),
                self._trace_end_turn("done"),
                self._push(EV_DONE, s),
                self._push(EV_STATE, "idle"),
            ),
            on_error=lambda exc: (
                self._trace_end_turn("error"),
                self._push(EV_ERROR, redact(f"{type(exc).__name__}: {exc}")),
                self._push(EV_STATE, "idle"),
            ),
            # Surface routed model so the COMP Status param can show
            # `model: flash · thinking…` etc. Goes through the same event
            # queue + drain → extension wires this to a Status line.
            on_model_change=lambda tier, picked: (
                self._trace_update_model(tier, picked),
                self._push(EV_MODEL, {"tier": tier, "model": picked}),
            ),
            # Phase 2 (1.8.0) — per-call token usage to the chat
            # status bar. Sanitised to int-or-zero so the frontend
            # never sees a stray non-numeric field.
            on_usage=lambda usage: self._push(EV_USAGE, _sanitise_usage(usage)),
        )

    # ------------------------------------------------------------------
    # Phase 4.1 — observability tracer
    # ------------------------------------------------------------------

    def _build_compactor(self) -> Any:
        """Construct the per-runtime Compactor. Returns ``None`` when
        the compaction module is unavailable (older .tox builds) or
        when the threshold is set to 0 (explicit disable).
        """
        try:
            import tdpilot_api_compaction as compaction_mod  # type: ignore[import-not-found]
        except ImportError:
            return None
        threshold = int(self._config.get("compaction_threshold", compaction_mod.DEFAULT_THRESHOLD))
        if threshold <= 0:
            return None
        keep_recent = int(self._config.get("compaction_keep_recent", compaction_mod.DEFAULT_KEEP_RECENT))
        history_dir = self._config.get("history_dir")
        # Re-use the tracer's session id if a tracer was built (so
        # forensic history files line up with the trace timeline).
        session_id = ""
        if self._tracer is not None and hasattr(self._tracer, "session_id"):
            session_id = self._tracer.session_id
        if not session_id:
            import uuid

            session_id = uuid.uuid4().hex[:12]
        try:
            return compaction_mod.Compactor(
                session_id=session_id,
                threshold=threshold,
                keep_recent=keep_recent,
                history_dir=history_dir,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] compactor init failed: {exc}")
            return None

    def _build_tracer(self) -> Any:
        """Construct the per-runtime Tracer. Returns ``None`` when the
        tracing module is unavailable (older .tox builds) or when
        ``config["trace_logging"]`` is False — both paths degrade
        gracefully so trace consumers never crash a session.
        """
        enabled = bool(self._config.get("trace_logging", True))
        try:
            import tdpilot_api_tracing as tracing_mod  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            return tracing_mod.Tracer(
                traces_dir=self._trace_dir_override,
                enabled=enabled,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] tracer init failed: {exc}")
            return None

    def _trace_tool_started(self, name: str, args: dict) -> None:
        """Mark a tool-call start so the matching on_tool_result can
        compute latency. Keyed by name; consecutive calls of the same
        tool in one turn (rare — the tool-use loop is sequential)
        overwrite the prior start time. Latency is best-effort.
        """
        if self._tracer is None:
            return
        # Store (start_monotonic, args) so the matching record can
        # call ``record_tool`` with the original arg hash.
        self._tool_call_starts[name] = (time.monotonic(), args)  # type: ignore[assignment]

    def _push_tool_call_event(self, name: str, args: dict) -> None:
        """Emit ``EV_TOOL_CALL`` and start the always-on latency clock
        so the matching ``EV_TOOL_RESULT`` can carry an elapsed time
        for the chat UI's "(123ms)" badge. The clock is independent
        of the tracer's own timing — Phase 2 (1.8.0)."""
        self._tool_started_monotonic[name] = time.monotonic()
        self._push(EV_TOOL_CALL, {"name": name, "args": args})

    def _push_tool_result_event(self, name: str, result: Any, is_error: bool) -> None:
        """Emit ``EV_TOOL_RESULT`` with elapsed latency since the
        matching ``EV_TOOL_CALL``. Best-effort: if the start clock was
        never set (e.g. textDAT reload mid-turn), latency_ms is
        omitted from the payload — Phase 2 (1.8.0)."""
        started = self._tool_started_monotonic.pop(name, None)
        payload: dict[str, Any] = {"name": name, "result": result, "is_error": is_error}
        if started is not None:
            payload["latency_ms"] = int((time.monotonic() - started) * 1000)
        self._push(EV_TOOL_RESULT, payload)

    def _trace_start_turn(self, user_text: str) -> None:
        """Open a tracer turn record. No-op if tracing disabled."""
        if self._tracer is None:
            return
        try:
            tier = self._config.get("model_tier", "auto") or "auto"
            self._tracer.start_turn(user_text or "", model_tier=str(tier))
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] tracer.start_turn failed: {exc}")

    def _trace_end_turn(self, outcome: str) -> None:
        """Close the active tracer turn. Called from the worker thread
        via the on_turn_done / on_error callbacks. Re-entrant safe.
        """
        if self._tracer is None:
            return
        try:
            self._tracer.end_turn(outcome)
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] tracer.end_turn failed: {exc}")
        # Wipe any straggling tool-start markers from this turn.
        self._tool_call_starts.clear()

    def _trace_update_model(self, tier: str, picked: str) -> None:
        if self._tracer is None:
            return
        try:
            self._tracer.update_model(model_tier=tier or "", model_used=picked or "")
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] tracer.update_model failed: {exc}")

    def _trace_record_tool(self, name: str, _result: Any, is_error: bool) -> None:
        """Record one tool-call's outcome on the active turn. Called
        from the worker via on_tool_result.
        """
        if self._tracer is None:
            return
        entry = self._tool_call_starts.pop(name, None)
        if entry is None:
            started, args = time.monotonic(), {}
        else:
            started, args = entry  # type: ignore[misc]
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            self._tracer.record_tool(
                name,
                args=args,
                latency_ms=latency_ms,
                ok=not is_error,
                error=None if not is_error else "tool returned error",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] tracer.record_tool failed: {exc}")

    def reload_config(self) -> None:
        """Re-read API key and settings (e.g. after the user pastes a new key)."""
        with self._lock:
            self._config = resolved_config()
            preserved = list(self._agent.messages) if self._agent else []
            self._build_agent()
            if self._agent is not None:
                self._agent.messages = preserved

    # ------------------------------------------------------------------
    # Phase 0.1 — dynamic context refresh (cook-thread only)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase 1.3 — severity-tracked validation hints
    # ------------------------------------------------------------------

    def _record_tool_call(self, name: str, is_error: bool, result: Any = None) -> None:
        """Hook called from the worker thread (on_tool_result) for every
        tool the agent invokes. Failed calls don't count — the model
        already saw the error and hasn't actually mutated state.

        Phase 1.6.13 fix — when the agent calls ``tool_batch``, this
        hook used to see only the literal ``tool_batch`` name, which
        is severity=low. Sub-calls hidden inside the batch (including
        high-severity mutations like ``td_create_node`` or
        ``td_exec_python``) escaped the validation-hint system. We
        now flatten the batch's per-call results into the ledger so
        the severity tally reflects what was ACTUALLY executed.
        """
        if is_error:
            return
        if name == "tool_batch" and isinstance(result, dict):
            for sub in result.get("results", []) or []:
                if not isinstance(sub, dict):
                    continue
                sub_name = sub.get("tool")
                if not isinstance(sub_name, str) or not sub_name:
                    continue
                # Mirror the top-level "errors don't count" rule:
                # a sub-call that returned an error didn't mutate.
                if not sub.get("ok", False):
                    continue
                self._turn_tool_calls.append(sub_name)
            return
        self._turn_tool_calls.append(name)

    def _maybe_emit_validation_hint(self) -> None:
        """Inspect the just-finished turn's tool-call list. If any
        high-severity mutation went out without a follow-up validator
        call, emit ``EV_HINT`` so the chat UI can render a soft nudge.
        Never blocks the conversation; never fires on low/medium-only
        turns.
        """
        calls = list(self._turn_tool_calls)
        high_severity = [name for name in calls if _tool_severity(name) == "high"]
        if not high_severity:
            return
        if any(name in _VALIDATOR_TOOLS for name in calls):
            return
        unique = sorted({name for name in high_severity})
        self._push(
            EV_HINT,
            {
                "kind": "missing_validation",
                "tools": unique,
                "message": (
                    "You modified the network ("
                    + ", ".join(unique)
                    + ") without validating. Consider calling td_get_errors "
                    "or td_audit_project to confirm the result is healthy."
                ),
            },
        )

    def _refresh_dynamic_context(self, user_text: str | None = None) -> None:
        """Snapshot the per-turn volatile context on the cook thread.

        :func:`build_dynamic_context` enumerates the bundled knowledge
        index, which calls ``parent().op('kb').children`` — TD globals
        that are NOT thread-safe. If the worker thread invokes the
        agent's ``dynamic_context_provider`` directly, TD pops a
        THREAD CONFLICT dialog (and the call returns junk under the
        graceful try/except). Pre-computing on the cook thread (in
        ``__init__`` for the first turn, in ``start_turn`` for every
        subsequent turn) lifts the TD-touching work onto the safe
        thread; the worker reads the resulting list.

        Phase 3.1 — also includes the triggered skill bodies so
        every turn after activation continues to see them.

        Phase 2.2 — when ``user_text`` is provided AND pre-retrieval
        is enabled, ALSO runs cheap local retrieval (memory_recall,
        recipe_recall, knowledge_search) and prepends the top hits.
        ``user_text=None`` (init/reset path) skips pre-retrieval.
        """
        extras: list[str] = []
        skills_section = self._build_active_skills_section()
        if skills_section:
            extras.append(skills_section)
        if self._pre_retrieval_enabled and user_text:
            retr = self._run_pre_turn_retrieval(user_text)
            if retr:
                extras.append(retr)

        try:
            self._dynamic_context_snapshot = build_dynamic_context(
                extra_sections=(extras or None),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] dynamic context refresh failed: {exc}")
            self._dynamic_context_snapshot = []

    # ------------------------------------------------------------------
    # Phase 2.2 — pre-turn retrieval
    # ------------------------------------------------------------------

    # Hard cap so the retrieval block never explodes the dynamic
    # context. Roughly 800 tokens at ~3 chars/token.
    _PRE_RETRIEVAL_CHAR_BUDGET = 2400
    _PRE_RETRIEVAL_TOP_K_PER_SOURCE = 3
    _PRE_RETRIEVAL_TOP_K_TOTAL = 4

    def _run_pre_turn_retrieval(self, user_text: str) -> str:
        """Run memory_recall + recipe_recall + knowledge_search against
        ``user_text`` and return a formatted markdown block of the top
        hits across all three. Returns ``""`` when nothing relevant
        comes back.

        Cook-thread only — handlers are pure-Python (BM25 over
        in-memory indexes / SQLite FTS) so they don't touch the
        cook-thread dispatcher's marshalling path. Each handler is
        called directly to avoid the round-trip cost of going
        through the agent's tool-use loop.
        """
        text = (user_text or "").strip()
        if not text:
            return ""

        hits: list[dict] = []

        # The three retrieval handlers are pure-Python and safe to
        # call from any thread. Each guarded so a single failure
        # doesn't kill the others.
        sources: list[tuple[str, str, str]] = [
            ("memory", "tdpilot_api_memory", "handle_memory_recall"),
            ("recipe", "tdpilot_api_recipes", "handle_recipe_recall"),
            ("knowledge", "tdpilot_api_knowledge", "handle_knowledge_search"),
        ]
        body = {"query": text, "top_k": self._PRE_RETRIEVAL_TOP_K_PER_SOURCE}
        for label, mod_name, fn_name in sources:
            try:
                mod = __import__(mod_name)
                fn = getattr(mod, fn_name, None)
                if fn is None:
                    continue
                out = fn(dict(body))
            except Exception as exc:  # noqa: BLE001
                print(f"[tdpilot_API/runtime] pre-retrieval {label} failed: {exc}")
                continue
            if not isinstance(out, dict):
                continue
            for m in out.get("matches", []) or []:
                if not isinstance(m, dict):
                    continue
                # Tag the source so the caller-formatted line can
                # show provenance to the model.
                hits.append({"_source": label, **m})

        if not hits:
            return ""

        # Sort by score descending; threshold below which a hit is
        # too weak to be useful. BM25 over short queries can return
        # near-zero scores — those would only confuse the model.
        hits.sort(key=lambda h: float(h.get("score", 0.0) or 0.0), reverse=True)
        top = [h for h in hits if float(h.get("score", 0.0) or 0.0) >= 0.05]
        top = top[: self._PRE_RETRIEVAL_TOP_K_TOTAL]
        if not top:
            return ""

        lines = [
            (
                "## Pre-turn retrieval (top local hits — automatic, model "
                "decides whether to load full content via memory_get / "
                "recipe_get / knowledge_get)"
            )
        ]
        used = 0
        for h in top:
            src = h.get("_source", "?")
            name = h.get("name") or h.get("filename") or "?"
            score = float(h.get("score", 0.0) or 0.0)
            snippet = (h.get("snippet") or h.get("description") or "").replace("\n", " ").strip()
            line = f"- [{src}] {name} (score {score:.2f}) — {snippet}"
            if len(line) > 280:
                line = line[:277] + "..."
            if used + len(line) + 1 > self._PRE_RETRIEVAL_CHAR_BUDGET:
                break
            lines.append(line)
            used += len(line) + 1

        if len(lines) == 1:
            return ""  # only the header survived the budget — drop the block
        return "\n".join(lines)

    def _build_active_skills_section(self) -> str:
        """Render the activated-skills block for the dynamic context.

        Returns ``""`` when no skills have triggered yet so the dynamic
        context stays tight on early turns. Sorted alphabetically by
        name so the section is byte-deterministic across turns when
        the activation set is unchanged — that gives DeepSeek's
        request-level cache a chance to hit on stretches where no
        new skill activates.
        """
        if not self._session_skills_activated:
            return ""
        ordered = sorted(self._session_skills_activated.items())
        bodies = "\n\n".join(f"### Skill: {name}\n\n{body}" for name, body in ordered)
        return "## Active Skills (auto-loaded by trigger this session)\n\n" + bodies

    # ------------------------------------------------------------------
    # Phase 3.1 — trigger-based skill loading
    # ------------------------------------------------------------------

    def _check_skill_triggers(self, user_text: str) -> None:
        """Scan ``user_text`` for skill triggers; activate matches that
        haven't fired this session yet. Idempotent — re-triggering an
        already-active skill is a no-op.

        Cook-thread only. Called from ``start_turn`` before the worker
        spawns so the activated-skill bodies are visible in the next
        ``_refresh_dynamic_context`` snapshot.
        """
        if not user_text:
            return
        try:
            from tdpilot_api_skills import find_triggered_skills  # type: ignore[import-not-found]
        except ImportError:
            return
        try:
            matched = find_triggered_skills(user_text)
        except Exception as exc:  # noqa: BLE001
            print(f"[tdpilot_API/runtime] skill-trigger scan failed: {exc}")
            return
        for entry in matched:
            name = entry.get("name")
            if not name or name in self._session_skills_activated:
                continue
            body = (entry.get("text") or "").strip()
            if not body:
                continue
            self._session_skills_activated[name] = body
            self._push(
                EV_HINT,
                {
                    "kind": "skill_activated",
                    "name": name,
                    "message": (f"Auto-loaded skill '{name}' (matched a trigger keyword in your message)."),
                },
            )

    # ------------------------------------------------------------------
    # v2.1.1 — paused-TD probe.
    #
    # When TD playback is paused (me.time.play = False), TD's
    # ``onFrameStart`` callback does NOT fire. The CookThreadDispatcher
    # pump runs from ``onFrameStart``, so every tool call submitted by
    # the worker thread blocks until the 60s timeout and returns
    # ``{"error": "Tool ... timed out after 60.0s"}``. The agent sees
    # the wall of timeouts and falsely concludes "TD is unresponsive"
    # and tells the user to restart TouchDesigner — when the actual
    # fix is one keypress.
    #
    # Option A in v2.1.1: detect paused state at start_turn and emit
    # a soft EV_HINT explaining the symptom. The pump architecture is
    # untouched; moving pump off ``onFrameStart`` (Option B) is filed
    # as separate tech debt.
    # ------------------------------------------------------------------

    def _is_td_paused(self) -> bool:
        """Return True if TouchDesigner playback is paused.

        Returns False when the play state can't be determined (running
        outside TD, ``parent()`` raises THREAD CONFLICT, etc.) — never
        claim paused without proof, or unit-test environments would
        emit phantom warnings on every turn.
        """
        try:
            return not bool(parent().time.play)  # type: ignore[name-defined]
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Public API used by the COMP extension
    # ------------------------------------------------------------------

    def start_turn(self, user_text: str) -> bool:
        """Queue a user message and start the worker. Returns False if busy or no key."""
        if self._agent is None:
            self._push(EV_ERROR, "No API key set. Paste your DeepSeek key into the COMP parameter.")
            return False
        if self._worker is not None and self._worker.is_alive():
            return False
        # v2.1.1 — paused-TD UX trap. See _is_td_paused docstring above.
        # Emit BEFORE any other turn-prep work so the warning lands
        # in the chat even if a downstream step raises.
        if self._is_td_paused():
            self._push(
                EV_HINT,
                {
                    "kind": "paused_td",
                    "message": (
                        "TouchDesigner playback is paused — tool calls will time out after 60s "
                        "because onFrameStart isn't firing. Press the spacebar in TD (or the "
                        "play button in the timeline) to resume cook-thread tool dispatch."
                    ),
                },
            )
        # v2.1.0 — live-refresh model_tier from the COMP param so users
        # can switch flash ↔ pro ↔ auto mid-session without pulsing
        # Reload Config (which would rebuild the Agent and re-trigger
        # config file reads). Pre-2.1.0 the tier was captured at agent
        # construction and stayed there until reload — so changing the
        # Modeltier dropdown in the parameter panel had no effect on
        # subsequent turns until the user manually pulsed Reload Config.
        # Other config (max_tokens, temperature, model strings) still
        # requires the full rebuild because they're held inside the
        # Agent instance via __init__.
        try:
            live_tier = (parent().par.Modeltier.eval() or "").strip().lower()  # type: ignore[name-defined]
            if live_tier in ("auto", "flash", "pro") and self._agent.model_tier != live_tier:
                self._agent.model_tier = live_tier
                self._config["model_tier"] = live_tier
        except Exception:
            # Outside TD (parent() unavailable) — keep the agent's existing tier.
            pass
        # Phase 3.1 — scan user_text for skill-trigger keywords BEFORE
        # the dynamic-context refresh so the activated-skill bodies
        # show up in this turn's snapshot.
        self._check_skill_triggers(user_text)
        # Refresh the dynamic-context snapshot on the cook thread BEFORE
        # the worker thread starts. The worker reads the snapshot inside
        # _call_api; if we let the worker build it instead, every API
        # call would re-trigger TD's THREAD CONFLICT detector when the
        # bundled-knowledge enumerator hits parent().op('kb').
        # Phase 2.2 — pass user_text so pre-turn retrieval can fire.
        self._refresh_dynamic_context(user_text=user_text)
        # Phase 1.3 — clear the per-turn tool-call ledger so the
        # validation-hint check at turn end only considers THIS turn.
        self._turn_tool_calls = []
        # Phase 4.1 — open a tracer turn record. Must come AFTER
        # _check_skill_triggers / _refresh_dynamic_context so the
        # turn-record's session_id is current; must come BEFORE
        # add_user_message so the timing ts isn't skewed by the
        # message-append cost.
        self._trace_start_turn(user_text)
        self._agent.add_user_message(user_text)
        self._push(EV_STATE, "thinking")
        self._worker = threading.Thread(target=self._run_safe, name="tdpilot_api_agent", daemon=True)
        self._worker.start()
        return True

    # Cook-thread-side worker join timeout. Long enough that a real
    # in-flight DeepSeek call has a chance to wrap, short enough that
    # the user pulsing Stop doesn't think the COMP is hung.
    _STOP_JOIN_TIMEOUT = 2.0

    def stop(self) -> None:
        """Cooperative cancellation. Sets the agent stop flag, cancels
        any pending cook-thread tool calls so the worker isn't stuck
        on a CookThreadDispatcher.__call__ wait, and only reports
        idle once the worker has actually exited (with a 2s grace).

        Phase 1.6.13 fix — pre-fix the audit caught that stop() set
        the flag and pushed idle, but if the worker was blocked
        inside CookThreadDispatcher waiting for a pump, it would not
        see the flag until the next API call between tool calls.
        Cancelling pending cook calls wakes the worker immediately;
        the join lets the UI accurately reflect "actually stopped"
        instead of "told to stop".
        """
        if self._agent is not None:
            self._agent.stop()
        # Wake any worker blocked inside CookThreadDispatcher.__call__.
        self._cook_dispatcher.cancel_pending()
        # Wait for the worker to actually exit. Best effort — if it
        # doesn't exit within the grace window we still report idle
        # (the daemon flag means the process can shut down regardless).
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._STOP_JOIN_TIMEOUT)
        self._push(EV_STATE, "idle")

    def reset(self) -> None:
        """Clear the conversation and prep for a fresh session.

        Order matters:
          1. Set agent stop flag and cancel pending cook-thread
             dispatch so the worker exits.
          2. Wait (best-effort) for the worker to finish. After this
             window, the old worker's events still in the queue can
             arrive late but won't drive new API calls — the worker
             checks the stop flag on every loop iteration.
          3. THEN clear messages and reset per-session ledgers. Pre-
             fix the audit caught that we'd reset history while a
             worker was still alive — the worker could keep going,
             append stale tool_results, and even start a new API
             call against the now-empty history.
        """
        # 1. Signal cancellation + wake any blocked worker.
        if self._agent is not None:
            self._agent.stop()
        self._cook_dispatcher.cancel_pending()

        # 2. Wait for the worker to exit before mutating state.
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._STOP_JOIN_TIMEOUT)

        # 3. Now safe to clear state. Agent.reset() clears messages but
        # NOT the stop flag — clear_stop() lifts the cancellation only
        # AFTER the old worker has been joined (or its grace expired).
        if self._agent is not None:
            self._agent.reset()
            self._agent.clear_stop()
        # Phase 4.1 — close any open tracer turn record so an
        # interrupted turn (reset() called mid-flight) still gets
        # written to disk with outcome="interrupted" instead of
        # vanishing into the next start_turn's overwrite.
        self._trace_end_turn("interrupted")
        # Phase 3.1 — auto-loaded skills are session-scoped by design.
        # A reset starts a fresh session, so previously-triggered
        # skills should NOT carry over into the new conversation.
        self._session_skills_activated.clear()
        # Phase 1.3 — same logic for the per-turn validation ledger.
        self._turn_tool_calls = []
        # Phase 2 (1.8.0) — drop any straggling latency-clock entries.
        # An interrupted turn can leave on_tool_call entries without
        # matching on_tool_result; the next session would otherwise
        # see ghost timing for the same tool name.
        self._tool_started_monotonic.clear()
        # And the dynamic-context snapshot — rebuild from scratch.
        self._refresh_dynamic_context()
        self._push(EV_STATE, "idle")

    def pump_dispatcher(self, max_per_pump: int = 8) -> int:
        """Run pending tool calls on the cook thread. Called once per frame."""
        return self._cook_dispatcher.pump(max_per_pump)

    @property
    def raw_dispatcher(self):
        """Public accessor for the raw (non cook-thread-wrapped) tool
        dispatcher. Used by handlers that already run on the cook
        thread and would deadlock waiting on the cook-thread wrapper —
        currently ``handle_recipe_replay`` and ``handle_tool_batch``.

        Phase 3 (F-10) hardening: previously these handlers reached in
        via ``ext._runtime._raw_dispatcher`` — a private-attribute
        access that broke any time the field was renamed. Calling
        through the property keeps the internal name free to evolve
        while the API stays stable.
        """
        return self._raw_dispatcher

    def messages_snapshot(self) -> list[dict]:
        if self._agent is None:
            return []
        return list(self._agent.messages)

    # ------------------------------------------------------------------
    # Event drain — called once per cook from a Timer/Execute DAT
    # ------------------------------------------------------------------

    def drain_events(self, max_per_call: int = 64) -> list[tuple[str, Any]]:
        """Pop up to max_per_call events. Caller dispatches them to UI sinks."""
        out: list[tuple[str, Any]] = []
        for _ in range(max_per_call):
            try:
                out.append(self._events.get_nowait())
            except Empty:
                break
        return out

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _push(self, kind: str, payload: Any) -> None:
        self._events.put((kind, payload))

    def _run_safe(self) -> None:
        try:
            assert self._agent is not None
            self._agent.run_turn()
        except AgentError as exc:
            # Already surfaced via on_error (Agent emits before raising).
            # Defensive idle marker plus a debug log so silent agent
            # crashes leave a breadcrumb in the textport.
            self._push(EV_STATE, "idle")
            print(f"[tdpilot_API/runtime] AgentError surfaced: {exc}")
        except Exception as exc:  # noqa: BLE001
            self._push(EV_ERROR, redact(f"Worker crash: {type(exc).__name__}: {exc}"))
            self._push(EV_STATE, "idle")
        finally:
            # Brief grace period so a rapid second start_turn doesn't race
            # with the still-finishing thread.
            time.sleep(0.01)
