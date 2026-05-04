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
# Sprint 4.1 — subagent events. Forwarded by SubagentManager into the
# parent runtime's event queue so the chat UI can display sub-task
# progress under collapsible [worker:<id>] sections.
EV_SUB_TEXT = "subagent_text"  # payload: {"id": str, "text": str}
EV_SUB_TOOL = "subagent_tool"  # payload: {"id": str, "name": str, "args": dict}
EV_SUB_DONE = (
    "subagent_done"  # payload: {"id": str, "status": str, "result", "error", "tool_calls", "duration_ms"}
)


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
    "(persists in ~/.tdpilot-api/knowledge/, NOT in memory).\n\n"
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
    """Construct the system prompt for one ``start_turn``.

    Always begins with SYSTEM_PROMPT_BASE (byte-stable). Memory and
    knowledge indexes follow in alphabetical order so the prefix stays
    cache-friendly for DeepSeek's auto-cache (~50× discount on hits).
    """
    parts = [SYSTEM_PROMPT_BASE]

    # Memory index
    try:
        from tdpilot_api_memory import get_memory_index_content  # type: ignore[import-not-found]

        mem_index = get_memory_index_content()
        if mem_index and mem_index.strip():
            parts.append(
                "\n\n## Memory Index — files you can recall via memory_get / memory_recall\n\n" + mem_index
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] memory index injection failed: {exc}")

    # Knowledge index hint (just titles + descriptions, NOT full content —
    # the model calls knowledge_get when it needs specifics)
    try:
        from tdpilot_api_knowledge import get_knowledge_index_hint  # type: ignore[import-not-found]

        kb_hint = get_knowledge_index_hint()
        if kb_hint and kb_hint.strip():
            parts.append(
                "\n\n## Knowledge Index — call knowledge_search(query) or knowledge_get(name)\n\n" + kb_hint
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] knowledge index injection failed: {exc}")

    # Recipes index — saved replayable sequences. Hint-only (titles +
    # descriptions); full content fetched via recipe_get / recipe_replay.
    try:
        from tdpilot_api_recipes import get_recipes_index_hint  # type: ignore[import-not-found]

        recipes_hint = get_recipes_index_hint()
        if recipes_hint and recipes_hint.strip():
            parts.append(
                "\n\n## Recipes — call recipe_recall(query) / recipe_replay(name)\n\n" + recipes_hint
            )
    except Exception as exc:
        print(f"[tdpilot_API/runtime] recipes index injection failed: {exc}")

    # Skills: index hint (always) + auto-load skill bodies (when a skill
    # has auto_load=true in frontmatter). Auto-load skills are rare but
    # let the user/maintainer pin always-on disciplines.
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


# Backwards-compat alias — older code referenced SYSTEM_PROMPT directly.
# New code should call build_system_prompt() so the memory index is
# regenerated each turn (memories saved this session show up next).
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
        # invoke other tools synchronously (e.g. handle_recipe_replay).
        # The recipe handler runs on the cook thread already; if it
        # called self._dispatcher (the cook-thread wrapper) it would
        # deadlock waiting for the cook thread to drain its own queue.
        self._raw_dispatcher = dispatcher
        self._tools = tools
        self._system_prompt = system_prompt
        self._config = config or resolved_config()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

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
            on_tool_call=lambda n, a: self._push(EV_TOOL_CALL, {"name": n, "args": a}),
            on_tool_result=lambda n, r, e: self._push(
                EV_TOOL_RESULT, {"name": n, "result": r, "is_error": e}
            ),
            on_turn_done=lambda s: (
                self._push(EV_DONE, s),
                self._push(EV_STATE, "idle"),
            ),
            on_error=lambda exc: (
                self._push(EV_ERROR, redact(f"{type(exc).__name__}: {exc}")),
                self._push(EV_STATE, "idle"),
            ),
            # Surface routed model so the COMP Status param can show
            # `model: flash · thinking…` etc. Goes through the same event
            # queue + drain → extension wires this to a Status line.
            on_model_change=lambda tier, picked: self._push(EV_MODEL, {"tier": tier, "model": picked}),
        )

    def reload_config(self) -> None:
        """Re-read API key and settings (e.g. after the user pastes a new key)."""
        with self._lock:
            self._config = resolved_config()
            preserved = list(self._agent.messages) if self._agent else []
            self._build_agent()
            if self._agent is not None:
                self._agent.messages = preserved

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
        self._agent.add_user_message(user_text)
        self._push(EV_STATE, "thinking")
        self._worker = threading.Thread(target=self._run_safe, name="tdpilot_api_agent", daemon=True)
        self._worker.start()
        return True

    def stop(self) -> None:
        if self._agent is not None:
            self._agent.stop()
        self._push(EV_STATE, "idle")

    def reset(self) -> None:
        if self._agent is not None:
            self._agent.reset()
        # Make sure no in-flight tool call is left blocking the worker.
        self._cook_dispatcher.cancel_pending()
        self._push(EV_STATE, "idle")

    def pump_dispatcher(self, max_per_pump: int = 8) -> int:
        """Run pending tool calls on the cook thread. Called once per frame."""
        return self._cook_dispatcher.pump(max_per_pump)

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
