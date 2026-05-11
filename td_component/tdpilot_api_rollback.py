"""Auto-rollback on error regression — Phase 1.1 of the v2.2.0→v3.0 roadmap.

Wraps each LLM tool batch with:

  1. A baseline ``td_get_errors`` snapshot taken via the dispatcher.
  2. A TD ``ui.undo.startBlock`` opened so the whole batch becomes one
     undo entry.
  3. A post-batch ``td_get_errors`` recheck.
  4. If new *critical* errors (compile-style only) appeared, the undo
     block is rolled back via ``ui.undo.undo()`` and a hint is appended
     to the last ``tool_result`` so the LLM sees the rollback on its
     next API call.

Critical-error predicate is deliberately conservative — Python syntax
errors, expression-parse errors, GLSL compile errors, and Script DAT
load errors. Runtime errors, missing-file references, and warnings
are NOT critical: those are frequently noisy / transient during a
build, and false-positive rollbacks erode trust faster than missing
some genuine regressions.

Coexistence:

  * Batches that contain only read tools skip the wrap entirely (no
    point taking a baseline if nothing can change).
  * Batches containing ``td_exec_python`` (or any other tool whose
    side-effects can't be reverted by TD's undo system) skip the
    wrap with a clear hint — half-rolling-back is worse than not
    rolling back at all.
  * Env var ``TDPILOT_DISABLE_AUTO_ROLLBACK=1`` disables the feature
    entirely for users who want to debug what the agent actually did.

The two ``handle_auto_rollback_*`` handlers are registered in
``TOOL_TO_HANDLER`` (so dispatch can find them) but NOT in
``TOOL_SCHEMAS`` (so the LLM never sees them as callable tools).
That makes them effectively internal-only.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Critical-error predicate
# ---------------------------------------------------------------------------

# Substring signatures we treat as a "critical" error inside a node's
# ``errors`` string. Lower-case match. Curated to avoid false positives
# on transient runtime issues. We can broaden this later if real usage
# shows it's too narrow.
#
# These were chosen by reading TD 2025's error-emit conventions across
# the operator families that the agent most often touches:
#   * Python script DAT errors:  "SyntaxError", "IndentationError",
#     "NameError" (the last one specifically when produced by a
#     module-level binding failure — runtime NameErrors elsewhere are
#     a different beast and we don't catch those).
#   * Expression / parameter parse errors: TD prints "Expression Error"
#     in the node's error string when a custom-parameter expression
#     fails to parse.
#   * GLSL compile errors: glslTOP / glslMAT emit "Compile Error" or
#     "Failed to compile" in their error string when a shader stage
#     refuses to compile.
#   * Script DAT load errors: "Script Error" when a Script DAT's
#     callback module raises during load.
_CRITICAL_PATTERNS: tuple[str, ...] = (
    "syntaxerror",
    "indentationerror",
    "expression error",
    "invalid expression",
    "compile error",
    "failed to compile",
    "shader error",
    "script error",
    "parse error",
)


def is_critical_error(error_msg: str | None) -> bool:
    """True if ``error_msg`` looks like a compile / parse-level breakage.

    Conservative on purpose — a narrow set of substring matches against
    the raw error text returned by ``node.errors()``. See module
    docstring for the rationale.

    Empty / falsy input is non-critical by definition.
    """
    if not error_msg:
        return False
    lower = str(error_msg).lower()
    return any(pat in lower for pat in _CRITICAL_PATTERNS)


def _preview(error_msg: str | None, max_chars: int = 120) -> str:
    """Return the first non-blank line of ``error_msg`` truncated to
    ``max_chars`` characters. Used for the rollback-hint payload."""
    if not error_msg:
        return ""
    for line in str(error_msg).splitlines():
        line = line.strip()
        if line:
            return line[:max_chars]
    return ""


def count_critical_in_issues(issues: list[dict] | None) -> dict:
    """Return ``{"count": N, "by_node": [...]}`` for the subset of
    ``issues`` whose ``errors`` field is critical.

    ``issues`` is the ``issues`` array returned by ``td_get_errors``.
    """
    out: list[dict] = []
    for issue in issues or []:
        errs = issue.get("errors") if isinstance(issue, dict) else None
        if is_critical_error(errs):
            out.append(
                {
                    "path": issue.get("path"),
                    "name": issue.get("name"),
                    "error_preview": _preview(errs),
                }
            )
    return {"count": len(out), "by_node": out}


def diff_errors(baseline: dict | None, current: dict | None) -> dict:
    """Return ``{"count": N, "new_criticals": [...]}`` — new critical
    errors in ``current`` that weren't in ``baseline``.

    Identity is ``(path, error_preview)`` — a node that already had the
    same critical error in baseline doesn't count as "new"; we only
    care about *regressions*, not pre-existing breakage.

    Either input may be ``None`` (e.g. if baseline capture failed) —
    treated as an empty set. In that degenerate case, every critical
    in ``current`` looks "new".
    """
    base_set: set[tuple[Any, str]] = set()
    for issue in (baseline or {}).get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        errs = issue.get("errors")
        if is_critical_error(errs):
            base_set.add((issue.get("path"), _preview(errs)))

    new_criticals: list[dict] = []
    for issue in (current or {}).get("issues", []) or []:
        if not isinstance(issue, dict):
            continue
        errs = issue.get("errors")
        if not is_critical_error(errs):
            continue
        preview = _preview(errs)
        key = (issue.get("path"), preview)
        if key not in base_set:
            new_criticals.append(
                {
                    "path": issue.get("path"),
                    "name": issue.get("name"),
                    "error_preview": preview,
                }
            )
    return {"count": len(new_criticals), "new_criticals": new_criticals}


# ---------------------------------------------------------------------------
# Env-var gate
# ---------------------------------------------------------------------------

ENV_DISABLE = "TDPILOT_DISABLE_AUTO_ROLLBACK"


def is_disabled_via_env(env: dict[str, str] | None = None) -> bool:
    """True if the env var is set to a recognised truthy value.

    Pass ``env`` as a dict in tests; ``None`` reads ``os.environ``.
    """
    src = env if env is not None else os.environ
    val = (src.get(ENV_DISABLE) or "").strip().lower()
    return val in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Mutation classifier (which tool names can introduce errors)
# ---------------------------------------------------------------------------

# Pure-read batches skip the wrap entirely — saves two ``td_get_errors``
# calls per batch. Curated against the chat-pipe's ``TOOL_TO_HANDLER`` —
# names not in this set don't mutate the network in a way that could
# introduce a critical error.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "td_create_node",
        "td_delete_node",
        "td_set_params",
        "td_set_content",
        "td_connect_nodes",
        "td_disconnect",
        "td_rename_node",
        "td_copy_node",
        "td_set_param_bounds",
        "td_clear_param_bounds",
        "td_create_macro",
        "td_pulse_param",
        "td_color_pipeline",
        "td_component_standardize",
        "td_component_notes",
        "td_optimize_visual",
        "td_emergency_stabilize",
        "td_restore_snapshot",
        "td_project_lifecycle",
        "td_patch_apply",
    }
)

# Tools whose side effects ``ui.undo`` can't capture. If a batch
# contains any of these, we stand down entirely — half-rolling-back
# is worse than not rolling back at all.
NON_UNDOABLE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "td_exec_python",  # arbitrary Python; can mutate files, globals, the agent COMP itself
        "td_emergency_stabilize",  # hardware-level state changes (CHOP audio reset etc.)
        "td_patch_apply",  # composite with its own atomic-apply semantics
    }
)


def batch_should_be_guarded(tool_names: list[str]) -> tuple[bool, str | None]:
    """Return ``(guard, reason)``.

    Skip the auto-rollback wrap if:
      * The batch is all reads (no mutation-class tool).
      * The batch contains any non-undoable tool (``td_exec_python`` etc.).

    The patch-session standdown (yield to explicit ``patch_begin`` blocks)
    is intentionally NOT checked in this v1 — the env-var escape hatch
    is enough for users doing intricate patch work. Adding the runtime
    cross-check requires a cook-thread read of ``comp.storage`` from a
    worker-thread context, which adds threading risk for a niche case.
    """
    names = list(tool_names or [])
    if any(n in NON_UNDOABLE_TOOL_NAMES for n in names):
        return False, "non-undoable tool in batch"
    if not any(n in MUTATION_TOOL_NAMES for n in names):
        return False, "pure-read batch"
    return True, None


# ---------------------------------------------------------------------------
# The guard — a context manager wrapped around the tool-batch loop
# ---------------------------------------------------------------------------


class AutoRollbackGuard:
    """Wraps a tool batch with snapshot + diff + conditional rollback.

    Usage from the Agent's ``_loop``::

        guard = AutoRollbackGuard(self.dispatcher, [tu["name"] for tu in tool_uses])
        with guard:
            for tu in tool_uses:
                ...  # dispatch as normal
        if guard.rollback_fired:
            # append guard.hint_text to the last tool_result's content

    On non-guarded batches (pure-read, non-undoable tools, baseline
    capture failure) the context manager is a no-op: ``__enter__``
    returns ``self`` with ``rollback_fired = False`` and ``__exit__``
    does nothing. This keeps the agent-loop call-site clean.

    All TD-side calls go through the dispatcher — this object lives
    in the worker thread, but the dispatcher routes to cook thread,
    so ``ui.undo.*`` and ``td_get_errors`` invocations happen on the
    right thread.
    """

    def __init__(
        self,
        dispatcher: Callable[[str, dict], Any],
        tool_names: list[str],
        get_errors_path: str = "/",
        get_errors_recursive: bool = True,
    ) -> None:
        self._dispatcher = dispatcher
        self._tool_names = list(tool_names or [])
        self._get_errors_path = get_errors_path
        self._get_errors_recursive = bool(get_errors_recursive)
        # Output state — read by the Agent after __exit__ returns.
        self.guarded: bool = False
        self.skip_reason: str | None = None
        self.rollback_fired: bool = False
        self.hint_text: str = ""
        self.new_critical_count: int = 0
        # Internal state
        self._baseline: dict | None = None
        self._undo_block_opened: bool = False

    def __enter__(self) -> AutoRollbackGuard:
        guarded, reason = batch_should_be_guarded(self._tool_names)
        if not guarded:
            self.guarded = False
            self.skip_reason = reason
            return self
        self.guarded = True

        # Capture baseline via the dispatcher (routes to cook thread).
        try:
            self._baseline = self._dispatcher(
                "td_get_errors",
                {
                    "path": self._get_errors_path,
                    "recurse": self._get_errors_recursive,
                },
            )
        except Exception as exc:  # noqa: BLE001 — degrade-gracefully on dispatcher failure
            self.guarded = False
            self.skip_reason = f"baseline capture failed: {type(exc).__name__}: {exc}"
            return self

        # Open the undo block via the dispatcher. If this fails (e.g.
        # running outside TD) we keep ``guarded = True`` but record
        # that the block isn't open; the post-batch path will then
        # skip the ``undo()`` call but STILL emit the hint, so the LLM
        # at least sees the regression flag.
        try:
            res = self._dispatcher("auto_rollback_begin", {"name": "tdpilot_auto_rollback"})
            self._undo_block_opened = isinstance(res, dict) and bool(res.get("ok"))
        except Exception:  # noqa: BLE001
            self._undo_block_opened = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if not self.guarded:
            return False  # don't swallow

        # Even on exception inside the with-block, close the undo block
        # and decide whether to roll back.
        try:
            current = self._dispatcher(
                "td_get_errors",
                {
                    "path": self._get_errors_path,
                    "recurse": self._get_errors_recursive,
                },
            )
        except Exception:  # noqa: BLE001 — degrade-gracefully on dispatcher failure
            current = None

        diff = (
            diff_errors(self._baseline, current)
            if current is not None
            else {
                "count": 0,
                "new_criticals": [],
            }
        )
        self.new_critical_count = int(diff.get("count", 0))

        if self.new_critical_count > 0 and self._undo_block_opened:
            # Attempt the rollback and **inspect the dispatcher's return
            # value** before claiming success. Codex P1 review on PR #34
            # (2026-05-11): the prior version set rollback_fired = True
            # unconditionally when undo was attempted, so if ui.undo.undo()
            # raised or returned an error payload the user + LLM were
            # both told "reverted" while the network stayed broken — a
            # silent-correctness bug, not a crash.
            #
            # ``auto_rollback_end`` returns one of:
            #   * {"ok": True, "rolled_back": True}   -> revert succeeded
            #   * {"ok": True, "rolled_back": False}  -> we asked not to undo
            #   * {"ok": False, ...}                  -> undo() raised
            #   * {"error": "..."}                    -> endBlock() raised
            # …or raises (network drop, dispatcher bug). Only the first
            # shape counts as a successful rollback.
            end_result: Any = None
            try:
                end_result = self._dispatcher("auto_rollback_end", {"undo": True})
            except Exception as exc:  # noqa: BLE001
                end_result = {"error": f"{type(exc).__name__}: {exc}"}
            if (
                isinstance(end_result, dict)
                and end_result.get("ok") is True
                and end_result.get("rolled_back") is True
            ):
                self.rollback_fired = True
                self.hint_text = format_hint(diff, rolled_back=True)
            else:
                # Rollback was attempted but didn't succeed (handler returned
                # error/non-success, or raised). Network is still broken;
                # tell the truth in both signals.
                self.rollback_fired = False
                self.hint_text = format_hint(
                    diff,
                    rolled_back=False,
                    end_failure=_format_end_failure(end_result),
                )
        elif self.new_critical_count > 0:
            # Detected regression but couldn't open the undo block —
            # surface a hint without claiming rollback happened.
            self.rollback_fired = False
            self.hint_text = format_hint(diff, rolled_back=False)
        else:
            # Clean batch — close the undo block as a normal undo entry.
            if self._undo_block_opened:
                try:
                    self._dispatcher("auto_rollback_end", {"undo": False})
                except Exception:  # noqa: BLE001
                    pass
        return False  # never swallow exceptions from the with-block


def _format_end_failure(end_result: Any) -> str:
    """Render a short ``(reason: ...)`` clause for the
    rollback-could-not-apply branch of ``format_hint``. Used by
    ``AutoRollbackGuard.__exit__`` when ``auto_rollback_end`` came
    back without an ``ok: True, rolled_back: True`` payload (Codex
    P1 finding on PR #34: previously we lied about success in this
    case)."""
    if not isinstance(end_result, dict):
        return ""
    if end_result.get("undo_error"):
        return f" (undo raised: {end_result['undo_error']})"
    if end_result.get("error"):
        return f" (endBlock raised: {end_result['error']})"
    return ""


def format_hint(
    diff: dict,
    rolled_back: bool = True,
    end_failure: str = "",
) -> str:
    """Render the hint message appended to the last tool_result on a
    rollback fire. Intentionally short — costs DeepSeek output tokens
    on the next turn (the model has to read + reason about it).

    ``end_failure`` is an optional short clause appended to the
    "could not be applied" message describing what specifically
    failed (Codex P1 followup — surface enough detail that the LLM
    can decide whether to retry, abort, or escalate).
    """
    n = int(diff.get("count", 0))
    items = list(diff.get("new_criticals", []))
    if not items:
        verb = "reverted" if rolled_back else "detected"
        return f"[tdpilot_auto_rollback] {n} new critical errors {verb}."
    names = ", ".join(f"{it.get('path')} ({it.get('error_preview')})" for it in items[:3])
    more = "" if len(items) <= 3 else f", +{len(items) - 3} more"
    if rolled_back:
        action = "the changes were automatically reverted via TD's undo"
    else:
        # The "(reason: ...)" detail is what distinguishes "we never
        # opened the block" from "we opened it but undo() failed".
        # The LLM can use this to pick a recovery strategy.
        action = "the rollback could not be applied so the errors remain" + end_failure
    return (
        f"[tdpilot_auto_rollback] This batch introduced {n} new critical "
        f"error(s) so {action}. Errors: {names}{more}. "
        "Try a different approach."
    )


# ---------------------------------------------------------------------------
# Internal cook-thread handlers — registered in TOOL_TO_HANDLER but not
# exposed in TOOL_SCHEMAS, so the LLM never sees them.
# ---------------------------------------------------------------------------

UNDO_BLOCK_NAME = "tdpilot_auto_rollback"


def handle_auto_rollback_begin(body: dict) -> dict:
    """Open a TD undo block to group the imminent tool batch.

    Returns ``{"ok": True}`` on success, or ``{"error": ...}`` if
    ``ui.undo`` is unavailable (running outside TouchDesigner).
    """
    name = UNDO_BLOCK_NAME
    if isinstance(body, dict) and body.get("name"):
        name = str(body.get("name"))
    try:
        ui.undo.startBlock(name)  # type: ignore[name-defined]
    except NameError:
        return {"error": "ui not available — running outside TouchDesigner?"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ui.undo.startBlock failed: {type(exc).__name__}: {exc}"}
    return {"ok": True, "name": name}


def handle_auto_rollback_end(body: dict) -> dict:
    """Close the auto-rollback undo block.

    If ``body["undo"]`` is truthy, also call ``ui.undo.undo()`` to
    revert the block atomically. Otherwise the block stays in TD's
    undo stack as a normal entry the user could undo manually.
    """
    do_undo = bool(body.get("undo")) if isinstance(body, dict) else False
    try:
        ui.undo.endBlock()  # type: ignore[name-defined]
    except NameError:
        return {"error": "ui not available — running outside TouchDesigner?"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"ui.undo.endBlock failed: {type(exc).__name__}: {exc}"}
    if do_undo:
        try:
            ui.undo.undo()  # type: ignore[name-defined]
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "endblock_ok": True, "undo_error": str(exc)}
        return {"ok": True, "rolled_back": True}
    return {"ok": True, "rolled_back": False}
