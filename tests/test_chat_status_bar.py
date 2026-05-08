"""PR-10 (Phase 2 / 1.8.0) — status bar with model badge + token meter.

Three layers:
  * Agent: ``on_usage`` callback fires once per API call carrying the
    DeepSeek response's ``usage`` dict.
  * Runtime: emits ``EV_USAGE`` with a sanitised int-only subset; the
    extension forwards as a ``{type: "usage"}`` WS message. ``EV_MODEL``
    is now broadcast as a structured ``{type: "model"}`` WS message
    instead of a string concatenation onto the agent status.
  * Chat HTML: status bar split — LHS holds transport (``ws <state>``),
    RHS holds agent state + model badge + per-turn token meter. The
    meter resets on each new turn (transition out of idle) and on
    ``fullSync`` (history rehydrate / /reset).

Structural assertions on each layer prove the wires connect.
"""

from __future__ import annotations

import re
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
    assert m
    return m.group(1)


@pytest.fixture(scope="module")
def runtime_src() -> str:
    return (TD_COMP / "tdpilot_api_runtime.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def agent_src() -> str:
    return (TD_COMP / "tdpilot_api_agent.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def extension_src() -> str:
    return (TD_COMP / "tdpilot_api_extension.py").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Agent: on_usage callback
# ---------------------------------------------------------------------------


def test_agent_accepts_on_usage_callback(agent_src: str):
    """Agent.__init__ must declare on_usage as an optional callback so
    the runtime can plumb usage into EV_USAGE without a default
    AttributeError when an older runtime build doesn't pass it."""
    assert "on_usage: Callable[[dict], None] = _noop" in agent_src
    assert "self.on_usage = on_usage" in agent_src


def test_agent_loop_invokes_on_usage_per_call(agent_src: str):
    """The per-call usage hook must fire after each ``_call_api``
    return, not just at turn end. Per-call granularity is needed so
    long tool-use chains show progressive token accumulation."""
    # The usage hook is fired right after we have the response; allow
    # either ordering of content / usage extraction.
    full_block = re.search(
        r"response = self\._call_api\(\)(.+?)# Append assistant turn",
        agent_src,
        re.S,
    )
    assert full_block, "_call_api response handling block not found"
    body = full_block.group(1)
    assert 'response.get("usage")' in body
    assert "self.on_usage(usage)" in body


# ---------------------------------------------------------------------------
# Runtime: EV_USAGE + sanitiser
# ---------------------------------------------------------------------------


def test_runtime_defines_ev_usage(runtime_src: str):
    """The constant must exist and be exported (extension imports it
    by name)."""
    assert 'EV_USAGE = "usage"' in runtime_src


def test_runtime_pushes_ev_usage_via_lambda(runtime_src: str):
    """on_usage callback in _build_agent must push EV_USAGE through
    the sanitiser so non-int / hostile fields are dropped before
    they reach the WS payload."""
    assert "on_usage=lambda usage: self._push(EV_USAGE, _sanitise_usage(usage))" in runtime_src


def test_sanitise_usage_helper_drops_non_int_fields(runtime_src: str):
    """The sanitiser is the single point that converts an upstream
    usage dict into the on-the-wire shape. It must accept only the
    documented keys and only int / float values."""
    m = re.search(r"def _sanitise_usage\(usage: Any\) -> dict\[str, int\]:(.+?)\n\ndef ", runtime_src, re.S)
    if not m:
        m = re.search(
            r"def _sanitise_usage\(usage: Any\) -> dict\[str, int\]:(.+?)\n\nSYSTEM_PROMPT_BASE",
            runtime_src,
            re.S,
        )
    assert m, "_sanitise_usage body not found"
    body = m.group(1)
    # Boolean rejection is important — `True == 1` in Python so a
    # boolean field would otherwise survive the int check.
    assert "isinstance(v, bool)" in body
    assert "isinstance(v, int)" in body
    assert "isinstance(v, float)" in body


def test_sanitise_usage_field_allowlist(runtime_src: str):
    """The hardcoded allowlist defines what reaches the frontend.
    A future model adding "experimental_x_tokens" must be added here
    explicitly — silent passthrough is the wrong default."""
    m = re.search(r"_USAGE_FIELDS = \(([^)]+)\)", runtime_src)
    assert m, "_USAGE_FIELDS tuple not found"
    fields = m.group(1)
    assert '"input_tokens"' in fields
    assert '"output_tokens"' in fields
    assert '"cache_read_input_tokens"' in fields


def test_sanitise_usage_runtime_behaviour():
    """Functional test of the sanitiser. Imports the runtime module
    (which has td-globals stubbed so this is safe outside TD)."""
    import sys
    import types

    sys.path.insert(0, str(TD_COMP))
    sys.modules.setdefault("td", types.ModuleType("td"))
    try:
        import importlib

        rt = importlib.import_module("tdpilot_api_runtime")
    finally:
        sys.path.remove(str(TD_COMP))
    s = rt._sanitise_usage
    assert s({"input_tokens": 10, "output_tokens": 20}) == {
        "input_tokens": 10,
        "output_tokens": 20,
    }
    # Booleans dropped (True == 1 trap).
    assert s({"input_tokens": True, "output_tokens": 5}) == {"output_tokens": 5}
    # Non-allowlist fields dropped.
    assert s({"input_tokens": 1, "experimental_x": 999}) == {"input_tokens": 1}
    # Float coerced to int.
    assert s({"output_tokens": 12.7}) == {"output_tokens": 12}
    # Garbage types ignored.
    assert s({"input_tokens": "10"}) == {}
    assert s(None) == {}
    assert s("nope") == {}
    # Missing fields just don't appear.
    assert s({}) == {}


# ---------------------------------------------------------------------------
# Extension: structured model + usage WS payloads
# ---------------------------------------------------------------------------


def test_extension_imports_ev_usage(extension_src: str):
    assert "EV_USAGE," in extension_src


def test_extension_broadcasts_structured_model_event(extension_src: str):
    """EV_MODEL now produces a `{type: "model"}` WS payload (was
    previously folded into a status string)."""
    m = re.search(r"elif kind == EV_MODEL:(.+?)elif kind == EV_USAGE:", extension_src, re.S)
    assert m, "EV_MODEL handler not found"
    body = m.group(1)
    assert '"type": "model"' in body
    assert '"tier":' in body
    assert '"model":' in body
    assert '"short":' in body


def test_extension_broadcasts_usage_event(extension_src: str):
    m = re.search(r"elif kind == EV_USAGE:(.+?)(?:elif kind ==|\n    # ---|\n    def )", extension_src, re.S)
    assert m, "EV_USAGE handler not found"
    body = m.group(1)
    assert '"type": "usage"' in body
    assert '"usage":' in body


# ---------------------------------------------------------------------------
# Chat HTML: status-bar split
# ---------------------------------------------------------------------------


def test_status_bar_has_split_lhs_rhs_markup(html: str):
    """The DOM has the new <span> containers for ws-state, model
    badge, and token meter."""
    assert 'id="ws-label"' in html
    assert 'id="ws-state"' in html
    assert 'id="model-badge"' in html
    assert 'id="token-meter"' in html


def test_iife_grabs_new_status_bar_handles(iife: str):
    assert "const $wsState = document.getElementById('ws-state')" in iife
    assert "const $modelBadge = document.getElementById('model-badge')" in iife
    assert "const $tokenMeter = document.getElementById('token-meter')" in iife


def test_iife_tracks_per_turn_token_state(iife: str):
    assert "let modelShort" in iife
    assert "let modelTier" in iife
    assert "let turnInputTokens" in iife
    assert "let turnOutputTokens" in iife
    assert "let turnCachedTokens" in iife
    assert "let lastWorking" in iife


def test_render_writes_to_split_spans_not_combined_string(iife: str):
    """Pre-1.8.0 render() composed `<state> · ws <ws>` into a single
    string. Now it writes ws to $wsState directly so the split layout
    works."""
    m = re.search(r"function render\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "$wsState.textContent = w" in body
    assert "$status.textContent = a" in body


def test_render_resets_token_meter_on_new_turn(iife: str):
    m = re.search(r"function render\(\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "if (!lastWorking)" in body
    assert "turnInputTokens = 0" in body
    assert "turnOutputTokens = 0" in body
    assert "turnCachedTokens = 0" in body


def test_full_sync_resets_token_meter(iife: str):
    """A history rehydrate / /reset must zero the meter so the next
    turn starts clean — pre-fix, the meter would carry across."""
    m = re.search(r"function fullSync\(rows\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "turnInputTokens = 0" in body
    assert "turnOutputTokens = 0" in body
    assert "turnCachedTokens = 0" in body
    assert "lastWorking = false" in body
    assert "refreshTokenMeter()" in body


def test_set_model_handler_present(iife: str):
    """The new structured `{type: "model"}` WS message routes
    through setModel which updates modelShort/Tier/Full."""
    m = re.search(r"function setModel\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "setModel not found"
    body = m.group(1)
    assert "modelShort = " in body
    assert "modelTier = " in body
    assert "modelFull = " in body
    assert "refreshModelBadge()" in body


def test_record_usage_accumulates_per_call(iife: str):
    m = re.search(r"function recordUsage\(payload\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "recordUsage not found"
    body = m.group(1)
    assert "turnInputTokens += u.input_tokens" in body
    assert "turnOutputTokens += u.output_tokens" in body
    assert "turnCachedTokens += u.cache_read_input_tokens" in body
    assert "refreshTokenMeter()" in body


def test_apply_message_routes_model_and_usage(iife: str):
    m = re.search(r"function applyMessage\(msg\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m
    body = m.group(1)
    assert "case 'model':" in body
    assert "setModel(msg)" in body
    assert "case 'usage':" in body
    assert "recordUsage(msg)" in body


def test_format_token_count_uses_k_suffix(iife: str):
    """Long token counts compress to "12.5k" so the status bar
    doesn't grow unboundedly."""
    m = re.search(r"function formatTokenCount\(n\)\s*\{(.+?)^\s{2}\}", iife, re.S | re.M)
    assert m, "formatTokenCount not found"
    body = m.group(1)
    assert "n >= 100000" in body
    assert "n >= 10000" in body
    assert "n >= 1000" in body
    assert "'k'" in body
