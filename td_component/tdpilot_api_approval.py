"""Tool approval gates — v2.5.3.

Runtime click-through gate for destructive tools. Sits one layer
inside the v2.4 user-intent gate (B-008) and outside the v2.4 auth
layer (Authmode). Composition table::

    incoming agent tool_use
      → User-intent gate     (v2.4 / B-008; system-prompt level)
      → Tool Approval gate   (v2.5.3 — THIS MODULE; runtime level)
      → Auth gate            (Authmode token/open; per request)
      → Origin allowlist     (cross-origin browser CSRF rejection)
      → dispatcher           (the actual tool body)

Why a runtime gate when the system-prompt gate already exists?

The system prompt is just text the LLM might ignore. The runtime gate
is a HARD BLOCK — the dispatcher never even runs unless the user
clicks Approve in the chat panel within the timeout. Defense in
depth; the system prompt + the runtime gate compose, they don't
replace each other.

The three modes::

    off              — no runtime checks (parity with pre-v2.5.3).
    destructive_only — gate the destructive set below (DEFAULT).
    all              — gate every tool. Paranoid; rarely useful.

The destructive set is intentionally conservative — high-blast-radius
tools that touch the user's project. The agent's own COMP path
(``/project1/tdpilot_API`` typically) is excluded from path-based
gates so the agent can freely manage its own machinery.

Threading model
---------------
The approval flow crosses thread boundaries:

* The agent's ``_loop`` runs on a worker thread. It calls
  :meth:`ApprovalRegistry.request_approval` and blocks on a
  :class:`threading.Event` (per ``approval_id``) up to ``timeout_s``.
* The HTTP /approve handler runs on the cook thread when the user
  clicks Approve/Deny in the chat panel. It looks up the
  ``approval_id``, sets the decision, signals the Event.
* On timeout, the worker proceeds with ``"timeout"`` decision and
  the registry cleans up after the worker calls
  :meth:`pop_decision`.

Pending approvals live in :attr:`ApprovalRegistry._pending` for the
runtime instance. (We deliberately do NOT use ``comp.storage`` here
because approvals are inherently per-Agent — they shouldn't persist
across textDAT reloads.)

The registry is thread-safe via an internal lock on the dict
mutations + ``Event`` semantics for the wait/signal handoff.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Mode + decision types
# ---------------------------------------------------------------------------

# COMP-param values for ``Approvalmode``.
MODE_OFF = "off"
MODE_DESTRUCTIVE_ONLY = "destructive_only"
MODE_ALL = "all"
VALID_MODES = (MODE_DESTRUCTIVE_ONLY, MODE_OFF, MODE_ALL)
DEFAULT_MODE = MODE_DESTRUCTIVE_ONLY

# Decision strings returned by ``request_approval``.
DECISION_APPROVE = "approve"
DECISION_DENY = "deny"
DECISION_TIMEOUT = "timeout"
DECISION_NOT_REQUIRED = "not_required"

# Default per-request timeout in seconds. Tunable via the runtime
# constructor; 30s aligns with the UI countdown.
DEFAULT_TIMEOUT_S = 30.0


# Destructive tools that always require approval when
# ``destructive_only`` mode is active. The list is conservative —
# anything that mutates structure / runs code / restores a snapshot.
DESTRUCTIVE_TOOLS_ALWAYS: frozenset[str] = frozenset(
    {
        "td_exec_python",  # arbitrary Python execution
        "td_delete_node",  # destructive node removal
        "td_restore_snapshot",  # restores full project state
        "snapshot_restore_scoped",  # v2.3.0 — scoped restore
        "td_disconnect",  # cuts network wiring
    }
)

# Tools that are destructive ONLY when targeting paths OUTSIDE the
# agent's own COMP. Renaming a node inside the agent COMP (e.g.
# ``/project1/tdpilot_API/cache_dat``) is internal bookkeeping; the
# same call against user nodes mutates their project surface.
DESTRUCTIVE_TOOLS_PATH_AWARE: frozenset[str] = frozenset(
    {
        "td_rename_node",
        "td_set_content",
    }
)


# ---------------------------------------------------------------------------
# Required-or-not logic
# ---------------------------------------------------------------------------


def _extract_path(args: dict | None) -> str | None:
    """Pull the target path from a tool's args dict. Several tools use
    ``path`` as the canonical key. Returns ``None`` if absent / wrong
    shape — caller treats that as "no path info, gate conservatively".
    """
    if not isinstance(args, dict):
        return None
    candidate = args.get("path") or args.get("node_path") or args.get("target")
    return candidate if isinstance(candidate, str) else None


def _is_inside_agent_comp(path: str, agent_comp_path: str) -> bool:
    """True iff ``path`` is the agent COMP itself or a descendant."""
    if not agent_comp_path:
        return False
    p = path.rstrip("/")
    base = agent_comp_path.rstrip("/")
    return p == base or p.startswith(base + "/")


def is_approval_required(
    *,
    tool_name: str,
    args: dict | None,
    mode: str,
    agent_comp_path: str = "",
) -> bool:
    """Top-level gate decision: does this (tool, args) need approval
    under the current mode?
    """
    if mode == MODE_OFF or not mode:
        return False
    if mode == MODE_ALL:
        return True
    if mode != MODE_DESTRUCTIVE_ONLY:
        # Unknown mode → fail safe to destructive_only semantics.
        mode = MODE_DESTRUCTIVE_ONLY

    if tool_name in DESTRUCTIVE_TOOLS_ALWAYS:
        return True

    if tool_name in DESTRUCTIVE_TOOLS_PATH_AWARE:
        path = _extract_path(args)
        if path is None:
            # No path info → gate conservatively.
            return True
        return not _is_inside_agent_comp(path, agent_comp_path)

    return False


# ---------------------------------------------------------------------------
# Denial / timeout result construction
# ---------------------------------------------------------------------------


def build_denied_result(tool_name: str, decision: str, reason: str = "") -> dict:
    """Compose the synthetic ``tool_result`` injected when approval
    is denied or times out. Mirrors the existing ``_tool_error``
    convention so existing recovery + journal-hint paths handle it
    uniformly.
    """
    if decision == DECISION_TIMEOUT:
        msg = (
            f"Tool {tool_name} aborted — user did not approve within "
            f"the timeout window. If you need this tool, ASK the user "
            "explicitly first (one short sentence) and let them respond "
            "before retrying."
        )
    else:  # explicit deny
        msg = (
            f"Tool {tool_name} denied by user"
            + (f" ({reason})" if reason else "")
            + ". Switch strategy or ASK the user what they'd prefer."
        )
    return {
        "_tool_error": True,
        "_tool_denied": True,  # discriminator for tests + future UIs
        "decision": decision,
        "error": msg,
    }


# ---------------------------------------------------------------------------
# ApprovalRegistry — manages pending requests
# ---------------------------------------------------------------------------


@dataclass
class _PendingApproval:
    """One in-flight approval. ``event`` is signalled when the user
    responds (or the runtime times out and synthesises a decision)."""

    approval_id: str
    tool_name: str
    args: dict
    event: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None  # set before event.set()
    reason: str = ""


class ApprovalRegistry:
    """In-memory registry of pending approval requests.

    Thread-safety: a single lock guards dict mutations. The
    blocking wait on the ``Event`` happens outside the lock so the
    worker thread doesn't hold contention while parked.
    """

    def __init__(self):
        self._pending: dict[str, _PendingApproval] = {}
        self._lock = threading.Lock()

    def register(self, tool_name: str, args: dict) -> _PendingApproval:
        """Create + record a new pending approval. Returns the
        record so the caller can wait on its event."""
        approval_id = uuid.uuid4().hex
        pending = _PendingApproval(
            approval_id=approval_id,
            tool_name=tool_name,
            args=dict(args) if isinstance(args, dict) else {},
        )
        with self._lock:
            self._pending[approval_id] = pending
        return pending

    def record_response(self, approval_id: str, decision: str, reason: str = "") -> bool:
        """Mark an approval resolved. Returns True if the id was found
        (caller can return 200 OK), False otherwise (stale id → 404)."""
        with self._lock:
            pending = self._pending.get(approval_id)
        if pending is None:
            return False
        if decision not in (DECISION_APPROVE, DECISION_DENY):
            return False
        pending.decision = decision
        pending.reason = reason
        pending.event.set()
        return True

    def pop(self, approval_id: str) -> _PendingApproval | None:
        """Remove the registry entry. Called by the worker after the
        wait returns (regardless of timeout vs explicit decision)."""
        with self._lock:
            return self._pending.pop(approval_id, None)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._pending)


# ---------------------------------------------------------------------------
# Top-level orchestrator — used by the agent dispatch loop
# ---------------------------------------------------------------------------


def request_approval_or_skip(
    *,
    tool_name: str,
    args: dict | None,
    mode: str,
    agent_comp_path: str,
    registry: ApprovalRegistry,
    on_request: Any,  # Callable[[approval_id, tool_name, args, timeout_s], None]
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, str]:
    """Return ``(decision, reason)`` where decision is one of
    ``DECISION_*``.

    Blocks the calling thread up to ``timeout_s`` while waiting for
    the user click. Safe to call from a worker thread — the cook
    thread signals via :meth:`ApprovalRegistry.record_response`.
    """
    if not is_approval_required(
        tool_name=tool_name,
        args=args,
        mode=mode,
        agent_comp_path=agent_comp_path,
    ):
        return DECISION_NOT_REQUIRED, ""

    pending = registry.register(tool_name, args or {})
    try:
        # Fire the chat-pipe-side notification so the HTML banner appears.
        # ``on_request`` is the runtime's push function; it MUST not
        # raise (we wrap defensively).
        try:
            on_request(pending.approval_id, tool_name, args or {}, int(timeout_s * 1000))
        except Exception as exc:  # noqa: BLE001
            # If we can't notify the UI, deny by default rather than
            # silently granting via fall-through.
            registry.pop(pending.approval_id)
            return (
                DECISION_DENY,
                f"approval notification failed: {type(exc).__name__}: {exc}",
            )

        signaled = pending.event.wait(timeout=timeout_s)
        record = registry.pop(pending.approval_id)
    except BaseException:
        # Best-effort cleanup so a parking worker doesn't leak entries.
        registry.pop(pending.approval_id)
        raise

    if not signaled or record is None or record.decision is None:
        return DECISION_TIMEOUT, ""
    return record.decision, record.reason


__all__ = [
    "DECISION_APPROVE",
    "DECISION_DENY",
    "DECISION_NOT_REQUIRED",
    "DECISION_TIMEOUT",
    "DEFAULT_MODE",
    "DEFAULT_TIMEOUT_S",
    "DESTRUCTIVE_TOOLS_ALWAYS",
    "DESTRUCTIVE_TOOLS_PATH_AWARE",
    "MODE_ALL",
    "MODE_DESTRUCTIVE_ONLY",
    "MODE_OFF",
    "VALID_MODES",
    "ApprovalRegistry",
    "build_denied_result",
    "is_approval_required",
    "request_approval_or_skip",
]
