"""v2.4 / B-008-T — task-sticky pro routing.

Follow-up to B-008 A+C (commit eaaaa24). User feedback after seeing
the A+C land:

    "we should have maybe like a rule once he detected he needs to be on
    pro mode, till the user confirms the task is done he should stay on
    pro. the costs are small and very transparent (UI)"

Design:
  * Entering: heuristic-induced pro routing OR cycle-escalation latches
    ``Agent._task_sticky_pro = True``.
  * Sticky: while True, ``_resolve_model`` returns ``self.model`` for any
    auto-tier turn, bypassing the heuristic.
  * Exiting: a user message matching ``_TASK_DONE_RE`` (thanks/perfect/
    done/that works/...) clears the flag in ``_maybe_clear_task_sticky``,
    called from ``_loop`` BEFORE ``_resolve_model``.
  * Precedence preserved: user pins (model_tier=flash) and per-turn
    overrides (_FLASH_OVERRIDE_RE) still win — task-sticky only governs
    the auto-tier path.
  * Guard: "thanks, now also fix Y" mixed messages do NOT clear sticky
    (negative lookahead in the regex).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _make_agent(model_tier: str = "auto"):
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
# Entering task-sticky
# =====================================================================


def test_b008t_heuristic_pro_enters_task_sticky():
    """A heuristic-induced pro routing must latch _task_sticky_pro=True
    so subsequent turns of the same multi-turn task stay on pro."""
    a = _make_agent()
    assert a._task_sticky_pro is False, "pre-condition"
    a._resolve_model("Build a kaleidoscope feedback loop")
    assert a._task_sticky_pro is True, "heuristic-induced pro routing must enter task-sticky"


def test_b008t_cycle_escalation_enters_task_sticky():
    """The cycle-escalation path also latches task-sticky — so the
    multi-turn recovery effort stays on pro until the user signals done.
    Pre-B-008-T this was a one-shot (pro for next turn, then back to
    auto), which lost context if the recovery itself took >1 turn."""
    a = _make_agent()
    a._cycle_escalate_next_turn = True
    a._resolve_model("anything")
    assert a._task_sticky_pro is True, "cycle-escalation must enter task-sticky, not just one-shot"
    assert a._cycle_escalate_next_turn is False, "the one-shot flag must still be consumed"


def test_b008t_heuristic_flash_does_not_enter_sticky():
    """A flash-routed turn (short lookup) must NOT latch sticky-pro.
    The sticky-pro feature is for task continuity, not for casual lookups
    that happen to ride the agent's coattails."""
    a = _make_agent()
    a._resolve_model("what is the framerate?")
    assert a._task_sticky_pro is False, "flash routing must NOT latch sticky"


# =====================================================================
# Sticky persists across turns
# =====================================================================


def test_b008t_sticky_persists_on_short_lookup():
    """The whole point: once sticky is set, a follow-up short lookup
    that would normally route to flash STAYS on pro. This preserves the
    working-memory and DeepSeek auto-cache for the in-progress task."""
    a = _make_agent()
    # Turn 1: build task → enters sticky.
    a._resolve_model("Build a kaleidoscope feedback loop")
    assert a._task_sticky_pro is True
    # Turn 2: a short lookup mid-task. Without sticky this would flash.
    picked = a._resolve_model("what's the framerate now?")
    assert picked == "deepseek-v4-pro", "short lookup mid-task must stay on pro via sticky"
    # Sticky still set for turn 3.
    assert a._task_sticky_pro is True


def test_b008t_sticky_persists_across_many_turns():
    """Simulate a typical 5-turn build: sticky persists across all."""
    a = _make_agent()
    a._resolve_model("Build a particle system")  # enters sticky
    assert a._task_sticky_pro is True
    for prompt in ["check the errors", "ok now wire it up", "screenshot it", "looks off, adjust"]:
        picked = a._resolve_model(prompt)
        assert picked == "deepseek-v4-pro", f"sticky must keep '{prompt}' on pro"
    assert a._task_sticky_pro is True, "sticky still set at end of build"


# =====================================================================
# Exiting task-sticky via task-done signal
# =====================================================================


def test_b008t_task_done_clears_sticky():
    """A user message matching _TASK_DONE_RE clears sticky via
    _maybe_clear_task_sticky. This is the explicit exit signal."""
    a = _make_agent()
    a._task_sticky_pro = True
    cleared = a._maybe_clear_task_sticky("thanks, that works perfectly!")
    assert cleared is True
    assert a._task_sticky_pro is False


def test_b008t_various_done_signals_clear():
    """Pin the conservative done-signal vocabulary so we catch
    accidental regex narrowing."""
    a = _make_agent()
    for done_signal in [
        "thanks!",
        "thank you",
        "perfect",
        "awesome work",
        "that works",
        "it works now",
        "looks good",
        "looks great",
        "ship it",
        "we're done",
        "all done",
        "that's it",
        "nailed it",
        "done!",
        "finished",
        "completed",
    ]:
        a._task_sticky_pro = True
        a._maybe_clear_task_sticky(done_signal)
        assert a._task_sticky_pro is False, f"'{done_signal}' must clear sticky"


def test_b008t_negative_signals_keep_sticky():
    """Negative signals ('still broken', 'not working') deliberately
    do NOT match the done regex — the agent must stay on pro while the
    user is still asking for help."""
    a = _make_agent()
    for keep_signal in [
        "still broken",
        "not working",
        "still failing",
        "the output is still black",
        "no, that didn't work",
        "ok let's try again",  # casual ack, not done
        "yes",  # too ambiguous
        "sure",  # too ambiguous
    ]:
        a._task_sticky_pro = True
        a._maybe_clear_task_sticky(keep_signal)
        assert a._task_sticky_pro is True, f"'{keep_signal}' must NOT clear sticky"


def test_b008t_mixed_done_plus_more_work_keeps_sticky():
    """'thanks, now also fix Y' is a mixed message: partial-done +
    more-work-coming. The user is signaling continuation. _TASK_DONE_RE
    has a negative-lookahead guard for this — sticky stays."""
    a = _make_agent()
    for mixed_signal in [
        "thanks, now also fix the noise param",
        "perfect, but next add particles",
        "looks good, and also change the color",
        "that works, then update the levels",
    ]:
        a._task_sticky_pro = True
        a._maybe_clear_task_sticky(mixed_signal)
        assert a._task_sticky_pro is True, (
            f"'{mixed_signal}' (partial-done + more-work) must NOT clear sticky"
        )


def test_b008t_clear_is_noop_when_already_clear():
    """_maybe_clear_task_sticky on a not-yet-set flag returns False
    without side effects."""
    a = _make_agent()
    assert a._task_sticky_pro is False
    result = a._maybe_clear_task_sticky("thanks!")
    assert result is False, "must report no-op when flag was already clear"
    assert a._task_sticky_pro is False


# =====================================================================
# Precedence — user-explicit pins beat sticky
# =====================================================================


def test_b008t_user_flash_pin_beats_sticky():
    """If the user has explicitly pinned ``model_tier='flash'`` (via the
    COMP dropdown or 'use only flash this session'), task-sticky-pro
    must NOT override that. User explicit > system auto."""
    a = _make_agent(model_tier="flash")
    a._task_sticky_pro = True
    picked = a._resolve_model("anything")
    assert picked == "deepseek-v4-flash", "user-pinned flash must win over task-sticky"


def test_b008t_explicit_per_turn_flash_override_beats_sticky():
    """Even in sticky-pro mode, a per-turn 'use flash' override in the
    user_text must win for that turn. (The override is per-turn, so
    sticky-pro reasserts on the next turn — that's the expected
    behavior.)"""
    a = _make_agent()
    a._task_sticky_pro = True
    picked = a._resolve_model("use flash to answer this quickly")
    assert picked == "deepseek-v4-flash", "explicit flash override must beat sticky for this turn"


def test_b008t_pinned_pro_redundant_with_sticky():
    """If model_tier='pro' (user-pinned), the result is pro whether or
    not sticky is set — they're aligned. Just verify no weirdness."""
    a = _make_agent(model_tier="pro")
    a._task_sticky_pro = True
    assert a._resolve_model("anything") == "deepseek-v4-pro"
    a._task_sticky_pro = False
    assert a._resolve_model("anything") == "deepseek-v4-pro"


# =====================================================================
# Integration with prior heuristics
# =====================================================================


def test_b008t_full_task_lifecycle():
    """End-to-end story:
    Turn 1: 'Build a kaleidoscope feedback loop' → pro (heuristic) + sticky
    Turn 2: 'check the errors' → pro (sticky persists)
    Turn 3: 'looks broken' → pro (sticky persists, negative signal)
    Turn 4: 'perfect, that works!' → clear sticky for THIS turn; turn
            resolution itself depends on prompt content (no pro signals
            → flash). Subsequent turns also auto.
    Turn 5: 'what's the framerate?' → flash (no sticky, normal heuristic)
    """
    a = _make_agent()

    # T1
    assert a._resolve_model("Build a kaleidoscope feedback loop") == "deepseek-v4-pro"
    assert a._task_sticky_pro is True

    # T2-3: sticky-pro carries through
    assert a._resolve_model("check the errors") == "deepseek-v4-pro"
    assert a._resolve_model("looks broken") == "deepseek-v4-pro"

    # T4: done signal — but the resolution happens AFTER _maybe_clear,
    # so we simulate that order here:
    a._maybe_clear_task_sticky("perfect, that works!")
    assert a._task_sticky_pro is False
    picked_t4 = a._resolve_model("perfect, that works!")
    assert picked_t4 == "deepseek-v4-flash", (
        "after task-done, the same turn resolves by heuristic — "
        "the done signal itself has no pro-keyword/structural-noun"
    )

    # T5: a fresh lookup, no sticky → flash.
    assert a._resolve_model("what's the framerate?") == "deepseek-v4-flash"
    assert a._task_sticky_pro is False
