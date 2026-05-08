"""PR-9 (Phase 2 / 1.8.0) — collapsible tool calls + result truncation.

The standalone runtime now broadcasts EV_TOOL_CALL / EV_TOOL_RESULT as
two structured WS message types (`tool_call`, `tool_result`) carrying
name + args, name + result + is_error + latency_ms. The browser opens
a placeholder ``<details>`` element on tool_call and fills in the
status/latency/result on the matching tool_result.

These tests pin:
  * the runtime helpers populate latency_ms in EV_TOOL_RESULT;
  * the extension translates EV_TOOL_CALL/EV_TOOL_RESULT into the new
    structured WS payloads (and still appends to the transcript);
  * the chat HTML's applyMessage routes the new types;
  * a stray tool_result without a matching tool_call still renders;
  * the truncation threshold + expand UI exists.

We don't run the JS — structural assertions on the IIFE prove the
behaviour-shaped code paths are present.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TD_COMP = ROOT / "td_component"
CHAT_HTML_PATH = TD_COMP / "tdpilot_api_chat.html"


@pytest.fixture(scope="module")
def html() -> str:
    return CHAT_HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def iife(html: str) -> str:
    m = re.search(r"<script>\s*\(\(\)\s*=>\s*\{(.+?)\}\)\(\);\s*</script>", html, re.S)
    assert m, "could not locate the IIFE in chat HTML"
    return m.group(1)


@pytest.fixture(scope="module")
def runtime_src() -> str:
    return (TD_COMP / "tdpilot_api_runtime.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def extension_src() -> str:
    return (TD_COMP / "tdpilot_api_extension.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Runtime: structured EV_TOOL_RESULT payload + latency clock
# ---------------------------------------------------------------------------


def test_runtime_has_always_on_latency_clock(runtime_src: str):
    """A separate `_tool_started_monotonic` map exists alongside the
    tracer-gated `_tool_call_starts`. Without this, latency would be
    None whenever trace_logging is disabled (default for some users)."""
    assert "self._tool_started_monotonic: dict[str, float] = {}" in runtime_src


def test_runtime_push_tool_call_event_helper_exists(runtime_src: str):
    """`_push_tool_call_event` records the start time AND emits
    EV_TOOL_CALL — both must happen so the matching result can compute
    latency."""
    m = re.search(
        r"def _push_tool_call_event\(self, name: str, args: dict\) -> None:(.+?)\n    def ",
        runtime_src,
        re.S,
    )
    assert m, "_push_tool_call_event not found"
    body = m.group(1)
    assert "self._tool_started_monotonic[name] = time.monotonic()" in body
    assert "self._push(EV_TOOL_CALL" in body


def test_runtime_push_tool_result_event_includes_latency(runtime_src: str):
    """`_push_tool_result_event` must compute latency_ms and include
    it in the EV_TOOL_RESULT payload when the start clock was set."""
    m = re.search(
        r"def _push_tool_result_event\(self, name: str, result: Any, is_error: bool\) -> None:(.+?)\n    def ",
        runtime_src,
        re.S,
    )
    assert m, "_push_tool_result_event not found"
    body = m.group(1)
    assert "self._tool_started_monotonic.pop(name, None)" in body
    assert 'payload["latency_ms"] = int' in body
    assert "self._push(EV_TOOL_RESULT, payload)" in body


def test_runtime_lambda_uses_new_helpers(runtime_src: str):
    """`_build_agent` must wire the on_tool_call/on_tool_result
    callbacks through the new helpers, not the old inline _push."""
    # Each lambda body spans a few lines; anchor on the wiring block.
    m = re.search(
        r"on_tool_call=lambda n, a: \((.+?)on_tool_result=",
        runtime_src,
        re.S,
    )
    assert m, "on_tool_call lambda not found"
    assert "self._push_tool_call_event(n, a)" in m.group(1)
    m2 = re.search(
        r"on_tool_result=lambda n, r, e: \((.+?)on_turn_done=",
        runtime_src,
        re.S,
    )
    assert m2, "on_tool_result lambda not found"
    assert "self._push_tool_result_event(n, r, e)" in m2.group(1)


def test_runtime_reset_clears_latency_clock(runtime_src: str):
    """A reset mid-turn must drop straggling latency-clock entries
    so the next session can't see ghost timing."""
    m = re.search(r"def reset\(self\) -> None:(.+?)\n    def ", runtime_src, re.S)
    assert m, "reset() not found"
    body = m.group(1)
    assert "self._tool_started_monotonic.clear()" in body


# ---------------------------------------------------------------------------
# Extension: structured WS payload + JSON coercion
# ---------------------------------------------------------------------------


def test_extension_broadcasts_structured_tool_call(extension_src: str):
    """The EV_TOOL_CALL handler emits a `{type: "tool_call"}` WS
    message with the structured payload (name, args, summary)."""
    m = re.search(r"elif kind == EV_TOOL_CALL:(.+?)elif kind == EV_TOOL_RESULT:", extension_src, re.S)
    assert m, "EV_TOOL_CALL handler not found"
    body = m.group(1)
    assert '"type": "tool_call"' in body
    assert '"name":' in body
    assert '"args":' in body
    # Transcript append must still happen for /history rehydration.
    assert 'self._append_transcript("tool_call"' in body


def test_extension_broadcasts_structured_tool_result(extension_src: str):
    """The EV_TOOL_RESULT handler emits a `{type: "tool_result"}` WS
    message including is_error + latency_ms + the coerced result."""
    m = re.search(r"elif kind == EV_TOOL_RESULT:(.+?)elif kind == EV_DONE:", extension_src, re.S)
    assert m, "EV_TOOL_RESULT handler not found"
    body = m.group(1)
    assert '"type": "tool_result"' in body
    assert '"is_error":' in body
    assert '"latency_ms":' in body
    assert "_coerce_jsonable(result)" in body


def test_coerce_jsonable_handles_primitives_and_collections():
    """Tool results need to round-trip through json.dumps. The helper
    must keep primitives, recurse into dict/list, and repr-fallback
    for exotic types."""
    sys.path.insert(0, str(TD_COMP))
    # Stub td-globals the extension imports lazily; module import
    # happens at the top so we need at least `op`, `parent`, `me`.
    fake_td = types.ModuleType("td")
    sys.modules.setdefault("td", fake_td)
    try:
        import importlib

        mod = importlib.import_module("tdpilot_api_extension")
    finally:
        sys.path.remove(str(TD_COMP))
    coerce = mod._coerce_jsonable
    assert coerce(None) is None
    assert coerce(True) is True
    assert coerce(1.5) == 1.5
    assert coerce("hi") == "hi"
    assert coerce({1: "a"}) == {"1": "a"}  # keys stringified
    assert coerce([1, 2, {"k": "v"}]) == [1, 2, {"k": "v"}]
    assert coerce({"x": (1, 2)}) == {"x": [1, 2]}
    assert coerce({1, 2}) in ([1, 2], [2, 1])

    class Custom:
        def __repr__(self):
            return "<custom>"

    assert coerce(Custom()) == "<custom>"


def test_coerce_jsonable_caps_depth():
    """A deeply nested object must not recurse forever — at the cap
    we repr() the value."""
    sys.path.insert(0, str(TD_COMP))
    fake_td = types.ModuleType("td")
    sys.modules.setdefault("td", fake_td)
    try:
        import importlib

        mod = importlib.import_module("tdpilot_api_extension")
    finally:
        sys.path.remove(str(TD_COMP))
    coerce = mod._coerce_jsonable
    nested = {"k": "v"}
    for _ in range(10):
        nested = {"k": nested}
    out = coerce(nested)
    # Walk inwards, eventually we should hit a stringified value.
    cur = out
    for _ in range(20):
        if isinstance(cur, dict) and "k" in cur:
            cur = cur["k"]
        else:
            break
    assert isinstance(cur, str), "depth cap should produce a stringified leaf"


# ---------------------------------------------------------------------------
# Chat HTML: applyMessage routes new types
# ---------------------------------------------------------------------------


def test_apply_message_handles_tool_call_type(iife: str):
    m = re.search(r"function applyMessage\(msg\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "applyMessage not found"
    body = m.group(1)
    assert "case 'tool_call':" in body
    assert "appendToolCall(msg)" in body


def test_apply_message_handles_tool_result_type(iife: str):
    m = re.search(r"function applyMessage\(msg\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "case 'tool_result':" in body
    assert "appendToolResult(msg)" in body


# ---------------------------------------------------------------------------
# Chat HTML: tool-call rendering structure
# ---------------------------------------------------------------------------


def test_append_tool_call_creates_details_element(iife: str):
    m = re.search(r"function appendToolCall\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "appendToolCall not found"
    body = m.group(1)
    assert "createElement('details')" in body
    assert "createElement('summary')" in body
    assert "tool-pair-summary" in body
    assert "running…" in body or "running…" in body
    # Args JSON block lives inside the body for click-to-expand.
    assert "tool-args-block" in body


def test_pending_tool_call_tracks_open_placeholder(iife: str):
    """The IIFE keeps a `pendingToolCall` reference so the matching
    tool_result event can fill it in. Must be cleared after pairing
    or on fullSync (DOM wipe)."""
    assert "let pendingToolCall = null;" in iife
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "appendToolResult not found"
    body = m.group(1)
    assert "pendingToolCall = null" in body


def test_full_sync_clears_pending_tool_call(iife: str):
    """fullSync wipes the DOM. The pendingToolCall reference would
    otherwise point to a detached <details>; subsequent tool_results
    would silently drop into nothing."""
    m = re.search(r"function fullSync\(rows\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "pendingToolCall = null" in body


def test_append_tool_result_pairs_with_open_call(iife: str):
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "pendingToolCall.name === payload.name" in body
    assert "pendingToolCall.statusEl" in body
    assert "pendingToolCall.body.appendChild" in body


def test_append_tool_result_handles_stray_event(iife: str):
    """A tool_result that doesn't match a pending call still renders
    — important when textDAT reloads mid-turn drop the pending pointer."""
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # The fallback path creates its own details element.
    fallback = re.search(r"// Stray tool_result(.+?)$", body, re.S)
    assert fallback, "fallback (stray tool_result) branch not found"
    fallback_body = fallback.group(1)
    assert "createElement('details')" in fallback_body
    assert "createElement('summary')" in fallback_body


def test_append_tool_result_auto_opens_errors(iife: str):
    """Errors deserve attention — the <details> opens automatically
    so the user doesn't have to click to see what went wrong.
    PR-12 added screenshot auto-open with the same plumbing — both
    paths must check is_error."""
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # Either the original isError-only check or the PR-12 (isError ||
    # screenshot) variant satisfies "errors auto-open".
    assert (
        "if (isError) pendingToolCall.details.open = true" in body
        or "if (isError || screenshot) pendingToolCall.details.open = true" in body
    )
    assert (
        "if (isError) details.open = true" in body or "if (isError || screenshot) details.open = true" in body
    )


def test_status_pill_records_latency_when_present(iife: str):
    """Latency badge is "(123ms)" appended to the status pill. When
    payload.latency_ms is null, latency stays out of the summary."""
    m = re.search(r"function appendToolResult\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "(typeof payload.latency_ms === 'number')" in body
    assert "'ms)'" in body or "'ms)'" in body


# ---------------------------------------------------------------------------
# Result truncation
# ---------------------------------------------------------------------------


def test_result_truncation_threshold_constant(iife: str):
    """A named constant means the truncation length can be tweaked
    without hunting through render code."""
    assert "TOOL_RESULT_TRUNC = 400" in iife


def test_build_result_block_truncates_long_results(iife: str):
    m = re.search(
        r"function buildResultBlock\(text, isError(?:, options)?\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M
    )
    assert m, "buildResultBlock not found"
    body = m.group(1)
    # PR-9 expressed truncation as `length <= TRUNC`; PR-12 flipped to
    # `length > TRUNC` and stored the boolean in `truncated`. Either
    # comparison form is fine — what matters is the threshold is used.
    assert "text.length <= TOOL_RESULT_TRUNC" in body or "text.length > TOOL_RESULT_TRUNC" in body
    assert "text.slice(0, TOOL_RESULT_TRUNC)" in body
    # Expand button toggles to full text.
    assert "createElement('button')" in body
    assert "tool-expand" in body
    assert "expand.addEventListener('click'" in body
    # PR-9 set `pre.textContent = text` directly; PR-12 swaps to
    # `appendWithNodePathChips(pre, text)` so paths render as chips
    # in the expanded view too.
    assert "pre.textContent = text" in body or "appendWithNodePathChips(pre, text)" in body


def test_build_result_block_marks_errors(iife: str):
    """isError adds an "err" class so the result body is themed red."""
    m = re.search(
        r"function buildResultBlock\(text, isError(?:, options)?\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M
    )
    assert m
    body = m.group(1)
    assert "isError ? ' err'" in body


# ---------------------------------------------------------------------------
# Inline-arg signature formatting
# ---------------------------------------------------------------------------


def test_format_arg_sig_handles_primitives(iife: str):
    m = re.search(r"function formatArgSig\(args\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "formatArgSig not found"
    body = m.group(1)
    # String values >40 chars must be elided.
    assert "v.length > 40" in body
    # Arrays/objects render as marker tokens, not stringified.
    assert "'[…]'" in body
    assert "'{…}'" in body


def test_format_arg_sig_caps_at_two_keys(iife: str):
    m = re.search(r"function formatArgSig\(args\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    # The cap is enforced via Math.min(keys.length, 2).
    assert "Math.min(keys.length, 2)" in body


# ---------------------------------------------------------------------------
# CSS sanity
# ---------------------------------------------------------------------------


def test_css_styles_for_tool_pair_present(html: str):
    """The CSS for the new collapsible UI must be in place — without
    these rules the <details>/summary defaults look broken in dark mode."""
    assert "details.tool-pair" in html
    assert ".tool-pair-summary" in html
    assert ".tool-pair-body" in html
    assert ".tool-result-body" in html
    assert ".tool-result-truncated" in html
    assert ".tool-expand" in html
