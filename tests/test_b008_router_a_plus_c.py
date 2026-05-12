"""v2.4 / B-008 — model-router fixes A (smarter heuristic) + C (reactive
escalation after CycleDetected) + B-007 (prompt fragment forbids
identical-arg probe loops).

Live-debug 2026-05-13: the user observed "Build a kaleidoscope feedback
loop" auto-routing to flash and failing every attempt (cycle-detect at
~50 tools, black-frame output, etc.). The score-based heuristic gave
1 point for the "build" verb but the prompt was too short to score on
length, code fences, or multi-tool keywords. Threshold ≥2 → flash.

Fix A — add two new signals:
  * structural_nouns ("feedback", "loop", "chain", "network", "system",
    "pipeline", ...) — any one of these adds +1 to score.
  * imperative_starters ("build ", "create ", ...) — message MUST start
    with one of these to score +1. Tighter than the prev verb-anywhere
    check, captures task-intent vs lookup-intent cleanly.

Fix C — reactive escalation: when ``run_turn`` catches CycleDetected,
flip ``_cycle_escalate_next_turn = True``. The next auto-tier turn
forces pro for one shot, then resets to auto. Breaks the typical
"flash stuck in probe loop" → "flash stuck again next turn" pattern
with one pro turn at ~3× cost.

Fix B-007 — system prompt protocol point 6 forbids re-calling
diagnostic tools with identical args after a no-change result.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _make_agent(monkeypatch, model_tier: str = "auto"):
    """Build a minimal Agent in auto-tier mode for routing tests."""
    from tdpilot_api_agent import Agent

    return Agent(
        api_key="sk-fake",
        dispatcher=lambda *a, **k: {"ok": True},
        tools=[],
        model_tier=model_tier,
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        system_prompt="test",
    )


# =====================================================================
# Fix A — smarter heuristic
# =====================================================================


def test_b008a_build_kaleidoscope_feedback_loop_routes_to_pro(monkeypatch):
    """The exact prompt from the 2026-05-13 live debug must route to pro.
    Score breakdown post-fix: 'build' pro_keyword (+1) + 'feedback'
    structural-noun (+1) = 2 ≥ 2. ('loop' and 'kaleidoscope' are also
    in structural_nouns but the check is presence-based, +1 once.)"""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("Build a kaleidoscope feedback loop")
    assert picked == "deepseek-v4-pro", (
        f"the canonical B-008 failing prompt must route to pro, got {picked}"
    )


def test_b008a_short_lookup_still_routes_to_flash(monkeypatch):
    """Counterexample: 'what is the framerate?' is a short lookup with
    zero pro signals. Must stay on flash to keep the cost-tier intent."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("what is the framerate?")
    assert picked == "deepseek-v4-flash", (
        f"short lookup must stay on flash, got {picked}"
    )


def test_b008a_structural_noun_alone_is_not_enough(monkeypatch):
    """Just mentioning 'feedback' without a build verb or other pro
    signal must NOT promote to pro — score 1 < 2."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("explain the feedback parameter")
    assert picked == "deepseek-v4-flash", (
        f"structural noun alone should stay on flash, got {picked}"
    )


def test_b008a_short_imperative_without_structural_stays_flash(monkeypatch):
    """Single-target imperative work ('fix the parameter on this op')
    is exactly the cost-savings case for flash. Verb +1, no structural
    noun, no fence, no tool-keyword pair → score 1 < 2 → flash. This
    pins the design choice to NOT double-count the verb via a separate
    imperative-starter bonus."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("fix the parameter on this op")
    assert picked == "deepseek-v4-flash", (
        f"single-target mutation must stay on flash, got {picked}"
    )


def test_b008a_create_plus_pipeline_routes_pro(monkeypatch):
    """create + pipeline → score 2 (create kw, pipeline structural).
    A genuine system-shaped task → pro."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("create a rendering pipeline")
    assert picked == "deepseek-v4-pro", (
        f"imperative verb + structural noun must route to pro, got {picked}"
    )


def test_b008a_verb_anywhere_plus_structural_routes_pro(monkeypatch):
    """The verb-anywhere check (preserved from pre-fix) + structural
    noun is enough for pro, even if the verb isn't at the start."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("how would you build feedback safely?")
    assert picked == "deepseek-v4-pro"


def test_b008a_audit_chip_design_tradeoff(monkeypatch):
    """Design pin: 'Audit this project for problems' (a C.6 chip) scores
    1 (verb 'audit' alone, no structural noun) → flash.

    Deliberate trade-off: 'audit' is genuinely ambiguous — could mean
    quick-check OR deep-analysis. Inflating it to pro by adding 'project'
    or 'problems' as structural-noun signals would over-rotate too many
    neutral lookups to pro. The reactive escalation (B-008-C) safety net
    handles the case where flash thrashes on a real audit: a CycleDetected
    bumps the retry to pro automatically.

    This test exists to document the choice — if someone later changes
    the heuristic so this prompt routes to pro, they should update this
    test consciously, not silently."""
    a = _make_agent(monkeypatch)
    picked = a._resolve_model("Audit this project for problems")
    assert picked == "deepseek-v4-flash", (
        f"audit chip is a design trade-off, expected flash; if you "
        f"intentionally changed this, update the test docstring. Got: {picked}"
    )


# =====================================================================
# Fix C — reactive cycle escalation
# =====================================================================


def test_b008c_cycle_detect_promotes_next_turn(monkeypatch):
    """After run_turn catches CycleDetected, the very next _resolve_model
    call (in auto tier) must return pro regardless of prompt content."""
    a = _make_agent(monkeypatch)
    # Simulate the post-CycleDetected state directly.
    a._cycle_escalate_next_turn = True
    # A prompt that would normally route to flash:
    picked = a._resolve_model("what is the framerate?")
    assert picked == "deepseek-v4-pro", (
        f"post-cycle-detect must force pro for one turn, got {picked}"
    )


def test_b008c_escalation_flag_resets_after_one_use(monkeypatch):
    """The escalation is one-shot: after using it once, the flag flips
    back to False and the NEXT next turn routes by normal heuristic."""
    a = _make_agent(monkeypatch)
    a._cycle_escalate_next_turn = True
    a._resolve_model("anything")  # consumes the flag
    assert a._cycle_escalate_next_turn is False, (
        "escalation must self-reset after one use"
    )
    # Next turn — a short lookup must return to flash routing.
    picked = a._resolve_model("ping")
    assert picked == "deepseek-v4-flash", (
        f"after one-shot escalation, lookups must return to flash, got {picked}"
    )


def test_b008c_run_turn_sets_flag_on_cycle_detected(monkeypatch):
    """The catch site is run_turn's BaseException handler — type-name
    matching on CycleDetected (avoids the module-reload class-identity
    issue from B-005)."""
    a = _make_agent(monkeypatch)

    # Inject a CycleDetected from the real module to verify the name-match
    # branch in run_turn is hit.
    from tdpilot_api_cycle_detector import CycleDetected

    def _loop_raises():
        raise CycleDetected(tool_name="td_get_errors", count=3, args_summary="{}")

    a._loop = _loop_raises  # type: ignore[method-assign]
    a.on_error = lambda exc: None  # swallow for the test

    assert a._cycle_escalate_next_turn is False, "pre-condition"
    with pytest.raises(CycleDetected):
        a.run_turn()
    assert a._cycle_escalate_next_turn is True, (
        "run_turn must set _cycle_escalate_next_turn on CycleDetected"
    )


def test_b008c_run_turn_does_not_set_flag_on_other_exceptions(monkeypatch):
    """Only CycleDetected should arm the escalation. A normal ValueError
    or AgentError leaves the flag at False — the heuristic handles
    routing as usual."""
    a = _make_agent(monkeypatch)

    def _loop_raises():
        raise ValueError("unrelated")

    a._loop = _loop_raises  # type: ignore[method-assign]
    a.on_error = lambda exc: None

    with pytest.raises(ValueError):
        a.run_turn()
    assert a._cycle_escalate_next_turn is False, (
        "non-cycle exceptions must NOT arm escalation"
    )


def test_b008c_run_turn_uses_name_match_not_isinstance(monkeypatch):
    """Same defence as B-005's name-match in _run_safe. Simulate a
    'foreign' CycleDetected (same name, different class identity) and
    verify the flag still flips. Models TD's textDAT module reload
    pattern that motivated B-005."""
    a = _make_agent(monkeypatch)

    class CycleDetected(Exception):  # type: ignore[no-redef] — synthetic foreign class
        pass

    def _loop_raises():
        raise CycleDetected()

    a._loop = _loop_raises  # type: ignore[method-assign]
    a.on_error = lambda exc: None

    with pytest.raises(CycleDetected):
        a.run_turn()
    assert a._cycle_escalate_next_turn is True, (
        "foreign-class CycleDetected (different identity, same name) "
        "must still arm escalation — name-match not isinstance"
    )


# =====================================================================
# Fix B-007 — system prompt forbids identical-arg probe loops
# =====================================================================


def test_b007_system_prompt_contains_identical_probe_rule():
    """The cache-stable SYSTEM_PROMPT_BASE must include the protocol
    point that forbids re-calling diagnostic tools with identical args
    after a 'no-change' result. This is the prompt-side mitigation that
    pairs with the cycle-ledger safety net."""
    from tdpilot_api_runtime import SYSTEM_PROMPT_BASE

    # Must mention the rule clearly enough that the agent learns it.
    assert "identical args" in SYSTEM_PROMPT_BASE, (
        "system prompt must contain 'identical args' rule wording"
    )
    # Must list the tools we've seen this fire on.
    for tool_name in ("td_get_errors", "td_analyze_frame", "td_get_node_detail"):
        assert tool_name in SYSTEM_PROMPT_BASE, (
            f"system prompt's identical-probe rule must mention {tool_name}"
        )
    # Must give SWITCH-STRATEGY guidance (don't just say "stop").
    assert "SWITCH STRATEGY" in SYSTEM_PROMPT_BASE, (
        "system prompt must direct agent to switch strategy, not just stop"
    )


def test_b007_system_prompt_is_byte_stable_across_calls():
    """build_system_prompt() must return bytes-identical output on
    repeat calls — Phase 0.1 cache-stability contract. The B-007
    insertion is in the immutable string literal, so this is implicit
    but worth pinning."""
    from tdpilot_api_runtime import build_system_prompt

    a = build_system_prompt()
    b = build_system_prompt()
    assert a == b, "system prompt must be byte-stable across calls"
