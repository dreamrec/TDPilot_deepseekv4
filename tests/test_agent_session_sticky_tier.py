"""Session-sticky model-tier overrides (v2.3.1).

Pre-fix, the per-turn override at ``_PRO_OVERRIDE_RE`` / ``_FLASH_OVERRIDE_RE``
flipped the tier for ONE turn only — the next turn fell back to the COMP's
configured ``model_tier`` (often ``auto``). Users explicitly asking for
session-wide pro ("use only pro mode this session", "from now on use pro")
saw their preference silently forgotten on the next short prompt that
auto-routed to flash.

Fix: when an override regex matches in proximity to a session-scope cue
("this session", "from now on", "always", "only pro", "stay on pro", ...),
mutate ``self.model_tier`` so the override persists across turns. Plus an
explicit reset path: "back to auto" / "auto routing" returns to auto.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Path setup mirroring tests/conftest.py for tdpilot_api_* modules.
_TD_COMPONENT = Path(__file__).resolve().parents[1] / "td_component"
if str(_TD_COMPONENT) not in sys.path:
    sys.path.insert(0, str(_TD_COMPONENT))


def _make_agent(tier="auto", on_tier_change=None):
    from tdpilot_api_agent import Agent

    kwargs = dict(
        api_key="test-key",
        dispatcher=lambda n, a: {},
        tools=[],
        model="deepseek-v4-pro",
        flash_model="deepseek-v4-flash",
        model_tier=tier,
    )
    if on_tier_change is not None:
        kwargs["on_tier_change"] = on_tier_change
    return Agent(**kwargs)


# ---------------------------------------------------------------------------
# Sticky promotion (Pro)
# ---------------------------------------------------------------------------


class TestSessionStickyPro:
    def test_this_session_phrase_mutates_tier_to_pro(self):
        a = _make_agent("auto")
        a._maybe_promote_tier("use only pro mode this session")
        assert a.model_tier == "pro"

    def test_from_now_on_use_pro_mutates_tier_to_pro(self):
        a = _make_agent("auto")
        a._maybe_promote_tier("from now on use pro")
        assert a.model_tier == "pro"

    def test_always_pro_mutates_tier_to_pro(self):
        a = _make_agent("auto")
        a._maybe_promote_tier("always use pro for this work")
        assert a.model_tier == "pro"

    def test_stay_on_pro_mutates_tier_to_pro(self):
        a = _make_agent("auto")
        a._maybe_promote_tier("stay on pro for the rest of this work")
        assert a.model_tier == "pro"

    def test_after_promotion_subsequent_short_turn_still_routes_to_pro(self):
        """The bug we're fixing — without the session-sticky path, a short
        follow-up after 'use only pro this session' falls back to auto and
        picks flash."""
        a = _make_agent("auto")
        a._maybe_promote_tier("use only pro mode this session")
        # Now simulate a short follow-up turn. Pre-fix this would route to
        # flash (auto heuristic, score 0). Post-fix the tier is pinned.
        assert a._resolve_model("Reply: KICK1") == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# Sticky promotion (Flash)
# ---------------------------------------------------------------------------


class TestSessionStickyFlash:
    def test_this_session_flash_mutates_tier_to_flash(self):
        a = _make_agent("auto")
        a._maybe_promote_tier("use flash for this session")
        assert a.model_tier == "flash"

    def test_from_now_on_flash_mutates_tier_to_flash(self):
        a = _make_agent("pro")
        a._maybe_promote_tier("from now on use flash")
        assert a.model_tier == "flash"


# ---------------------------------------------------------------------------
# Reset path
# ---------------------------------------------------------------------------


class TestTierResetPhrase:
    def test_back_to_auto_resets_tier(self):
        a = _make_agent("pro")
        a._maybe_promote_tier("back to auto")
        assert a.model_tier == "auto"

    def test_auto_routing_resets_tier(self):
        a = _make_agent("flash")
        a._maybe_promote_tier("switch to auto routing please")
        assert a.model_tier == "auto"


# ---------------------------------------------------------------------------
# Per-turn override behaviour is preserved
# ---------------------------------------------------------------------------


class TestPerTurnOverrideUnchanged:
    def test_use_pro_alone_does_not_mutate_tier(self):
        """Plain 'use pro' (no session-scope cue) still flips only the
        current turn — the next turn falls back to the configured tier."""
        a = _make_agent("auto")
        a._maybe_promote_tier("use pro for this one")
        assert a.model_tier == "auto"

    def test_use_pro_alone_still_routes_current_turn_to_pro(self):
        """Verify the per-turn override (v2.1.3) still works after the
        session-sticky addition."""
        a = _make_agent("auto")
        # Without calling _maybe_promote_tier — _resolve_model alone.
        assert a._resolve_model("use pro for this one") == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# on_tier_change callback
# ---------------------------------------------------------------------------


class TestOnTierChangeCallback:
    def test_callback_fires_when_tier_mutates(self):
        seen: list[str] = []
        a = _make_agent("auto", on_tier_change=lambda new: seen.append(new))
        a._maybe_promote_tier("use only pro mode this session")
        assert seen == ["pro"]

    def test_callback_skipped_when_tier_unchanged(self):
        """Already on pro + 'always pro' → no tier change → no callback."""
        seen: list[str] = []
        a = _make_agent("pro", on_tier_change=lambda new: seen.append(new))
        a._maybe_promote_tier("always pro please")
        assert seen == []
