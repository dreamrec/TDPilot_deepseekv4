"""TDPilot API — failure recovery hints (Phase 2.3).

When a tool returns ``{"error": ...}`` whose message matches a known
pattern, this module annotates the result with an actionable
``recovery_hint`` field. The dispatcher calls
:func:`attach_hint` on every error result before handing it back to
the agent loop. The agent sees the hint alongside the error and can
route differently on the next turn instead of retrying the same
failed call three times in a row.

Design rules for adding new patterns:

  - **Narrow matches only.** Each regex must be specific enough that
    a false positive is unlikely. Wide patterns (``r"error"``) are
    rejected — they'd attach hints to unrelated failures.
  - **One hint per pattern.** The first matching pattern wins;
    subsequent matches are skipped. Order patterns from most-specific
    to least-specific.
  - **Hints reference tool names the agent can actually call.** The
    point is to nudge the agent to a concrete next step, not to
    explain in prose why the call failed.
  - **Never crash on bad input.** A non-string ``error`` field, a
    None result, an unparseable regex — all degrade to "no hint
    attached" and let the bare error pass through.
"""

from __future__ import annotations

import re
from typing import Any

# Each entry: (compiled regex, hint string).
#
# Patterns match against the error MESSAGE text only. They do NOT
# match against ``traceback`` (stack-trace internals are noisy and
# not what the agent should react to).
#
# Pattern catalog — keep narrow. Add new ones at the END so existing
# narrow matches keep their priority.
_RECOVERY_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"Unknown operator type|Cannot create operator", re.IGNORECASE),
        (
            "Operator type wasn't recognised. Call td_list_families to discover "
            "valid op types in that family, or td_search_official_docs to find "
            "the correct camelCase spelling (e.g. 'noiseTOP' not 'noise')."
        ),
    ),
    (
        re.compile(r"\b401\b|Unauthorized|API key invalid|API key.*missing", re.IGNORECASE),
        (
            "DeepSeek rejected the API key. Paste your key into the COMP's API "
            "Key parameter and pulse Save Key to ~/.tdpilot-api/, then try again."
        ),
    ),
    (
        re.compile(
            r"Path not found|No node at path|No (op|operator) (found )?at|"
            r"path .* does not exist",
            re.IGNORECASE,
        ),
        (
            "The path doesn't resolve. Call td_get_nodes(path='/parent_path') to "
            "list children of the parent COMP — the node may have been renamed "
            "or never created."
        ),
    ),
    (
        re.compile(r"THREAD CONFLICT|thread conflict|outside the main thread", re.IGNORECASE),
        (
            "Tool touched TD globals from a non-cook thread. Don't return raw "
            "op() / parent() references from td_exec_python — stringify them "
            "first (str(op('/path'))) or just return the path."
        ),
    ),
    (
        re.compile(r"corpus.*not installed|isn't installed locally|brain.db.*not found", re.IGNORECASE),
        (
            "The named corpus isn't on disk. Run "
            "`npx tdpilot-dpsk4 brains add <corpus>` to install it under "
            "~/.tdpilot/data/normalized/, OR drop a pages.jsonl into that dir "
            "yourself for an ad-hoc corpus."
        ),
    ),
    (
        re.compile(r"recipe.*invalid|unknown tool.*replay|Step missing tool name", re.IGNORECASE),
        (
            "Recipe step references a tool not in TOOL_TO_HANDLER. Run "
            "td_validate_recipe(replay=...) before saving — it lists every "
            "unknown name. Common cause: a typo in the tool field or a "
            "tool that was renamed between TDPilot versions."
        ),
    ),
    (
        re.compile(r"malformed MATCH expression|fts5: syntax error", re.IGNORECASE),
        (
            "FTS5 query had special characters that broke the parser. The "
            "search layer normally sanitises these — if you're seeing this "
            "directly, retry with simple alphanumeric terms."
        ),
    ),
    (
        re.compile(r"Module .* (not found|missing)|No module named", re.IGNORECASE),
        (
            "An expected textDAT child of the COMP is absent. Check the COMP's "
            "children — common cause is a stale .tox built before a new "
            "module was added. Rebuilding tdpilot_API.tox usually fixes this."
        ),
    ),
    (
        re.compile(r"Permission denied|read-only file system|EACCES", re.IGNORECASE),
        (
            "Filesystem permission error. The default state dir is "
            "~/.tdpilot-dpsk4/api/ (legacy fallback ~/.tdpilot-api/) — verify "
            "it exists and is writable by the TouchDesigner process owner."
        ),
    ),
    (
        re.compile(r"timed out|TimeoutError", re.IGNORECASE),
        (
            "Tool exceeded its time budget. Cook-thread tools have a 60s "
            "default; slow operations like td_screenshot can hit this on a "
            "stalled cook. Stop the agent (pulse Stop), then try a narrower "
            "query or check Cooking Info for stuck operators."
        ),
    ),
    # ------------------------------------------------------------------
    # v2.0.1 — common AttributeError patterns the agent kept hitting in
    # the v2.0 audit. Each one is the wrong-API guess for a real concept;
    # the hint points at the right way to do what the agent actually
    # wanted.
    # ------------------------------------------------------------------
    (
        re.compile(
            r"'td\.\w*[Cc][Hh][Oo][Pp]' object has no attribute 'channels'",
            re.IGNORECASE,
        ),
        (
            "CHOPs don't expose `.channels` directly. Use chop.chans() to get "
            "a list of Channel objects, or chop['channel_name'] / chop[index] "
            "for direct access. Each Channel supports .eval() to read the "
            "current sample value."
        ),
    ),
    (
        re.compile(
            r"'td\.\w*[Cc][Hh][Oo][Pp]' object has no attribute 'text'",
            re.IGNORECASE,
        ),
        (
            "DAT.text exists but CHOP.text does not. For a CHOP's data use "
            "chop.chans() / chop[i] / chop['name']; for its config use "
            "chop.par.<param-name>. If you wanted DAT-style text content, "
            "the operator type may be wrong."
        ),
    ),
    (
        re.compile(r"'td\.Page' object has no attribute 'label'"),
        (
            "There's no .label on a Page. Use page.name (the page's display "
            "name and identifier in the same string). Per-parameter labels "
            "live on Par objects: par.label."
        ),
    ),
    (
        re.compile(r"'td\.Project' object has no attribute 'undo'"),
        (
            "TD's undo machinery is on ui.undo, not project. Use ui.undo.undo() "
            "for one step, ui.undo.redo() to redo, ui.undo.startBlock(name) / "
            "ui.undo.endBlock() to group multiple operations into a single "
            "undo step. The patch_begin/patch_commit/patch_rollback tools "
            "wrap this."
        ),
    ),
    (
        re.compile(r"Invalid target: must be a dotted identifier"),
        (
            "td_python_help expects a CLASS or MODULE name (e.g. 'td.OP', "
            "'td.geometryCOMP', 'tdu', 'tdu.Vector'). For a live operator's "
            "details use td_get_node_detail. For parameter values use "
            "td_get_params. td_python_help is for type-level docs only."
        ),
    ),
    # ------------------------------------------------------------------
    # v2.1.1 — patterns surfaced from a real lighting-redesign turn
    # (184 messages, 11 tool_result errors, all agent-learning errors —
    # zero TD-side bugs). Each one is a wrong-API guess for a real
    # concept; the hint points at the right TD API.
    # ------------------------------------------------------------------
    (
        re.compile(r"'td\.Par' object has no attribute 'rawVal'", re.IGNORECASE),
        (
            "Use par.eval (the live-value method, no args), par.val for the "
            "saved value, or par.expr for the expression text. 'rawVal' was "
            "a deprecated TD-2022 name and was removed in TD 2025."
        ),
    ),
    (
        re.compile(
            r"'td\.renderTOP' object has no attribute '(cooking|numCooks|xres|yres)'",
            re.IGNORECASE,
        ),
        (
            "renderTOP doesn't expose those as direct attributes. Use "
            "top.par.resolutionw / top.par.resolutionh for resolution, "
            "top.cookCount / top.cookTime for cook stats. Call "
            "td_python_help('renderTOP') for the full attribute surface."
        ),
    ),
    (
        re.compile(r"'tdu\.Matrix' object has no attribute 'translation'", re.IGNORECASE),
        (
            "tdu.Matrix uses .tx / .ty / .tz for the translation row, "
            ".decompose() returns (translate, rotate, scale) as three "
            "tuples. There's no '.translation' field."
        ),
    ),
    (
        re.compile(r"'td\.ParCollection' object has no attribute 'children'", re.IGNORECASE),
        (
            "ParCollection is the parameter list, not the operator children. "
            "For child operators use op.children. To iterate parameters use "
            "op.pars() or filter by page with op.pars(page='Page Name')."
        ),
    ),
)


def attach_hint(result: Any) -> Any:
    """If ``result`` is an error dict with a recognised pattern, return
    a new dict that carries an additional ``recovery_hint`` field.

    Non-error results pass through unchanged. So do error results
    whose message doesn't match any pattern. The function never
    raises — bad input degrades silently to the original result.

    v1.10.0: also normalises results to the new ``_tool_error``
    convention. If a result has an ``error`` key but no explicit
    ``_tool_error`` sentinel, this function stamps ``_tool_error: True``
    so the agent loop's classifier never has to fall back to the
    legacy ``"error" in result`` heuristic. An explicit
    ``_tool_error: False`` (handler legitimately returns success WITH
    an error field — e.g. ``td_get_errors``) is respected.
    """
    if not isinstance(result, dict):
        return result
    err = result.get("error")
    if not isinstance(err, str) or not err:
        return result
    enriched: dict | None = None
    if "_tool_error" not in result:
        enriched = dict(result)
        enriched["_tool_error"] = True
    if "recovery_hint" not in result:
        for pattern, hint in _RECOVERY_HINTS:
            try:
                if pattern.search(err):
                    enriched = dict(enriched or result)
                    enriched["recovery_hint"] = hint
                    break
            except Exception:
                # Defensive — a bad regex or re-engine hiccup must not
                # take down a tool result.
                continue
    return enriched if enriched is not None else result


def hint_for_message(message: str) -> str | None:
    """Convenience for callers that want to look up the hint without
    wrapping a full result dict (useful in tests + UX surfaces).
    """
    if not isinstance(message, str) or not message:
        return None
    for pattern, hint in _RECOVERY_HINTS:
        if pattern.search(message):
            return hint
    return None


def registered_patterns() -> tuple[str, ...]:
    """Return the source pattern strings, in registration order.

    Used by docs / introspection / tests to enumerate what's covered.
    """
    return tuple(p.pattern for p, _ in _RECOVERY_HINTS)
