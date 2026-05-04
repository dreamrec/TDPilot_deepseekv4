"""TDPilot API — subagent fan-out (Sprint 4.1).

Lets the parent agent fork off child agents for parallel/independent
work. Use cases that pay off:

  - "Inspect all 5 children of /project1 and report any issues"
  - "Try 3 noise variations and pick the best"
  - "Search across recipes, knowledge, and memory for this concept"

Use cases that DON'T pay off:
  - Sequential dependency chains (read A → write B → verify)
  - Single-node edits (overhead exceeds savings)
  - Anything under ~5 tool calls total

## Architecture

**Concurrency model: ThreadPoolExecutor(max_workers=3).** Researched
against LangGraph (10 default), CrewAI (sequential), AutoGen (round-
robin), OpenAI Swarm (sequential). Three is the right cap for our
single-process inside-TD scenario — DeepSeek API roundtrips dominate
latency so we get nearly 3× wall-clock speedup on parallel work
without saturating the cook-thread queue.

**Cook-thread dispatcher: shared with parent.** Each subagent is a
worker thread that submits tool calls to the SAME
`CookThreadDispatcher` as the parent. The cook-thread pump drains
requests in FIFO order. Head-of-line blocking risk is real but
acceptable for v1 — DeepSeek roundtrips are 1000× the cook latency,
so a slow tool call holding a frame slot is barely noticeable.

**Recursion limit: depth=2** via a thread-local `is_subagent` flag.
spawn_subagent detects when called from inside a subagent worker and
refuses. Eliminates fork-bomb risk without complex depth tracking.
LangGraph defaults to 25; we go tighter because the tool surface is
destructive.

**Cancellation: per-subagent `threading.Event`.** When the parent
agent's stop() fires, the parent runtime sets a top-level
`cancel_all` flag that all subagents poll. Individual subagents can
also be cancelled via `subagent_cancel(id)`. Standard cooperative
cancellation — no `Thread.kill()`.

**Result aggregation: polling, not streaming.** Anthropic's tool-use
spec doesn't have a long-running async pattern; their batch-jobs docs
use polling: tool returns `job_id`, parent polls. We mirror that.
``subagent_status(id)`` returns `{alive, partial_text, result, error,
tool_calls, duration_ms}`. Parent's loop sees these as ordinary tool
results and continues.

**Event flow: tagged events.** Subagent text/tool_calls feed back
into the parent's event queue with `[sub:<id>]` tags so the chat UI
can display them inline (or collapsed) under the parent's response.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_CONCURRENT = 3  # max simultaneous active subagents
MAX_DEPTH = 2  # parent (depth=0) → child (depth=1); no grandchildren
CHILD_TURN_BUDGET = 20  # smaller than parent's 100 — children handle focused tasks


# Thread-local — set on entry to a subagent worker, checked by
# spawn_subagent to enforce the depth limit. Avoids needing to thread
# a `depth` argument through every dispatcher call.
_thread_state = threading.local()


def _is_inside_subagent() -> bool:
    return bool(getattr(_thread_state, "is_subagent", False))


_SUBAGENT_SYSTEM_PROMPT = (
    "You are a subagent dispatched by a parent TDPilot API agent for a "
    "focused sub-task. Be terse. Complete your assigned task in as few "
    "tool calls as possible. Return a 1-3 sentence summary at the end "
    "so the parent can aggregate. Do NOT spawn further subagents — "
    "that's blocked at depth=2 to prevent fork-bombs.\n\n"
    "Inherit the parent's tool surface. Same TD discipline applies — "
    "inspect before mutating, verify after multi-step changes, prefer "
    "dedicated tools over td_exec_python."
)


# ---------------------------------------------------------------------------
# Per-subagent state
# ---------------------------------------------------------------------------


class SubagentInfo:
    __slots__ = (
        "id",
        "task",
        "started_at",
        "ended_at",
        "status",
        "result",
        "error",
        "partial_text",
        "tool_calls",
        "cancel_event",
        "future",
        "depth",
    )

    def __init__(self, sub_id: str, task: str, depth: int) -> None:
        self.id = sub_id
        self.task = task
        self.started_at = time.time()
        self.ended_at: float | None = None
        # Status: 'queued' / 'running' / 'done' / 'error' / 'cancelled'
        self.status = "queued"
        self.result: str | None = None
        self.error: str | None = None
        self.partial_text = ""
        self.tool_calls = 0
        self.cancel_event = threading.Event()
        self.future = None
        self.depth = depth

    def is_alive(self) -> bool:
        return self.status in ("queued", "running")

    def duration_ms(self) -> int:
        end = self.ended_at if self.ended_at is not None else time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self) -> dict:
        return {
            "subagent_id": self.id,
            "status": self.status,
            "alive": self.is_alive(),
            "task": self.task[:200],
            "started_at": self.started_at,
            "duration_ms": self.duration_ms(),
            "tool_calls": self.tool_calls,
            "result": self.result,
            "error": self.error,
            "partial_text": self.partial_text[-1000:] if self.partial_text else "",
            "depth": self.depth,
        }


# ---------------------------------------------------------------------------
# Manager singleton (per-COMP via parent runtime reference)
# ---------------------------------------------------------------------------


class SubagentManager:
    """Owns the thread pool + the registry. One per AgentRuntime."""

    def __init__(self, parent_runtime: Any) -> None:
        self.parent_runtime = parent_runtime
        self.subagents: dict[str, SubagentInfo] = {}
        self.executor = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT,
            thread_name_prefix="tdpilot_subagent",
        )
        self.lock = threading.Lock()

    def shutdown(self) -> None:
        # Cancel everything outstanding.
        with self.lock:
            for sa in self.subagents.values():
                sa.cancel_event.set()
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass

    def _build_child_agent(
        self, task: str, system_prompt: str | None, cancel_event: threading.Event, info: SubagentInfo
    ):
        """Construct a child Agent that shares the parent's dispatcher
        but tracks its own state via the SubagentInfo."""
        from tdpilot_api_agent import Agent  # type: ignore[import-not-found]

        parent_agent = self.parent_runtime._agent
        if parent_agent is None:
            raise RuntimeError("Parent agent not initialised")

        def on_text(s: str) -> None:
            info.partial_text += s
            # Forward to parent's event queue with tagged envelope so
            # the chat UI can display under [sub:<id>].
            self.parent_runtime._push("subagent_text", {"id": info.id, "text": s})

        def on_tool_call(name: str, args: dict) -> None:
            info.tool_calls += 1
            self.parent_runtime._push(
                "subagent_tool",
                {"id": info.id, "name": name, "args": args},
            )

        def on_done(text: str) -> None:
            info.result = text

        def on_error(exc: BaseException) -> None:
            info.error = f"{type(exc).__name__}: {exc}"

        agent = Agent(
            api_key=parent_agent.api_key,
            dispatcher=self.parent_runtime._dispatcher,  # CookThreadDispatcher
            tools=self.parent_runtime._tools,
            system_prompt=system_prompt or _SUBAGENT_SYSTEM_PROMPT,
            model=parent_agent.model,
            base_url=parent_agent.base_url,
            max_tokens=parent_agent.max_tokens,
            temperature=parent_agent.temperature,
            turn_budget=CHILD_TURN_BUDGET,
            model_tier="flash",  # children default to flash for speed
            flash_model=parent_agent.flash_model,
            on_text=on_text,
            on_tool_call=on_tool_call,
            on_turn_done=on_done,
            on_error=on_error,
        )
        # Share the cancel event by routing it into the agent's stop flag.
        agent._stop_flag = cancel_event  # type: ignore[assignment]
        agent.add_user_message(task)
        return agent

    def _run_subagent(self, info: SubagentInfo, task: str, system_prompt: str | None) -> None:
        """Worker-thread entry point. Sets the thread-local flag,
        runs the agent loop, records the result. Always clears the
        flag and updates status on exit."""
        _thread_state.is_subagent = True
        info.status = "running"
        try:
            agent = self._build_child_agent(
                task,
                system_prompt,
                info.cancel_event,
                info,
            )
            agent.run_turn()
            if info.cancel_event.is_set():
                info.status = "cancelled"
            elif info.error is not None:
                info.status = "error"
            else:
                info.status = "done"
        except BaseException as exc:  # noqa: BLE001 — never let workers crash silently
            info.error = info.error or f"{type(exc).__name__}: {exc}"
            info.status = "error"
        finally:
            info.ended_at = time.time()
            _thread_state.is_subagent = False
            self.parent_runtime._push(
                "subagent_done",
                {
                    "id": info.id,
                    "status": info.status,
                    "result": info.result,
                    "error": info.error,
                    "tool_calls": info.tool_calls,
                    "duration_ms": info.duration_ms(),
                },
            )

    # --- Public API exposed via the tool handlers below ---

    def spawn(self, task: str, system_prompt: str | None = None) -> dict:
        # Depth check: blocked from inside a running subagent.
        if _is_inside_subagent():
            return {
                "error": (
                    "Cannot spawn a subagent from inside another subagent — "
                    "recursion blocked at depth=2 (fork-bomb prevention)."
                ),
            }
        if not task or not task.strip():
            return {"error": "Missing required field: task"}

        with self.lock:
            active = sum(1 for sa in self.subagents.values() if sa.is_alive())
            if active >= MAX_CONCURRENT:
                return {
                    "error": (
                        f"Max concurrent subagents ({MAX_CONCURRENT}) reached. "
                        f"Wait for one to complete or call subagent_cancel."
                    ),
                    "active": active,
                }
            sub_id = "sa_" + uuid.uuid4().hex[:6]
            depth = 1  # parent is depth 0; spawned children are depth 1
            info = SubagentInfo(sub_id, task, depth)
            self.subagents[sub_id] = info

        info.future = self.executor.submit(
            self._run_subagent,
            info,
            task,
            system_prompt,
        )
        return {
            "ok": True,
            "subagent_id": sub_id,
            "queued_at": info.started_at,
            "concurrent_now": sum(1 for sa in self.subagents.values() if sa.is_alive()),
            "max_concurrent": MAX_CONCURRENT,
        }

    def status(self, sub_id: str) -> dict:
        info = self.subagents.get(sub_id)
        if info is None:
            return {"error": f"Unknown subagent_id: {sub_id}"}
        return {"ok": True, **info.to_dict()}

    def wait(self, sub_id: str, timeout_seconds: float = 30.0) -> dict:
        info = self.subagents.get(sub_id)
        if info is None:
            return {"error": f"Unknown subagent_id: {sub_id}"}
        if info.future is None:
            return {"error": "Subagent has no future — not started?"}
        try:
            info.future.result(timeout=max(0.1, min(timeout_seconds, 600.0)))
        except TimeoutError:
            pass
        except Exception:
            # Already recorded into info.error.
            pass
        return {"ok": True, **info.to_dict()}

    def cancel(self, sub_id: str) -> dict:
        info = self.subagents.get(sub_id)
        if info is None:
            return {"error": f"Unknown subagent_id: {sub_id}"}
        info.cancel_event.set()
        return {"ok": True, "cancelled": sub_id, "was_alive": info.is_alive()}

    def list_all(self) -> dict:
        return {
            "ok": True,
            "count": len(self.subagents),
            "active": sum(1 for sa in self.subagents.values() if sa.is_alive()),
            "max_concurrent": MAX_CONCURRENT,
            "subagents": [sa.to_dict() for sa in self.subagents.values()],
        }


# ---------------------------------------------------------------------------
# Manager fetch — locates the parent AgentRuntime + caches the manager
# on it.
# ---------------------------------------------------------------------------


def _get_manager() -> SubagentManager | None:
    """Return the SubagentManager attached to the parent runtime.

    Lazy-creates on first access. Stored as
    ``runtime._subagent_manager`` so it persists across tool calls but
    gets garbage-collected with the runtime on Reload Config.
    """
    try:
        comp = parent()  # type: ignore[name-defined]
    except NameError:
        return None
    if comp is None:
        return None
    ext_dat = comp.op("tdpilot_api_extension")
    if ext_dat is None:
        return None
    ext = ext_dat.module.get_extension(comp)
    if ext is None or ext._runtime is None:
        return None
    rt = ext._runtime
    mgr = getattr(rt, "_subagent_manager", None)
    if mgr is None:
        mgr = SubagentManager(rt)
        rt._subagent_manager = mgr
    return mgr


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_spawn_subagent(body: dict) -> dict:
    mgr = _get_manager()
    if mgr is None:
        return {"error": "Subagent manager not available — running outside TouchDesigner?"}
    return mgr.spawn(
        task=body.get("task") or "",
        system_prompt=body.get("system_prompt"),
    )


def handle_subagent_status(body: dict) -> dict:
    mgr = _get_manager()
    if mgr is None:
        return {"error": "Subagent manager not available"}
    sub_id = (body.get("subagent_id") or body.get("id") or "").strip()
    if not sub_id:
        return {"error": "Missing required field: subagent_id"}
    return mgr.status(sub_id)


def handle_subagent_wait(body: dict) -> dict:
    mgr = _get_manager()
    if mgr is None:
        return {"error": "Subagent manager not available"}
    sub_id = (body.get("subagent_id") or body.get("id") or "").strip()
    if not sub_id:
        return {"error": "Missing required field: subagent_id"}
    try:
        timeout = float(body.get("timeout_seconds", 30.0))
    except (TypeError, ValueError):
        timeout = 30.0
    return mgr.wait(sub_id, timeout)


def handle_subagent_cancel(body: dict) -> dict:
    mgr = _get_manager()
    if mgr is None:
        return {"error": "Subagent manager not available"}
    sub_id = (body.get("subagent_id") or body.get("id") or "").strip()
    if not sub_id:
        return {"error": "Missing required field: subagent_id"}
    return mgr.cancel(sub_id)


def handle_subagent_list(body: dict) -> dict:
    mgr = _get_manager()
    if mgr is None:
        return {"error": "Subagent manager not available"}
    return mgr.list_all()
