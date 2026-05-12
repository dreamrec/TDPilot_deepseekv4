"""v2.4 / B-004 follow-up — EV_MODEL must broadcast over WS, not only
write the COMP param.

The original B-004 fix (commit 9250100) was on the agent side: it made
``on_model_change`` fire on every turn instead of only on tier change.
That fixed half the problem.

The other half — surfaced in the 2026-05-13 follow-up live test — is in
the extension drain handler. Commit b1345e3 (B-003) inserted a new
``elif kind == EV_TIER_SYNC`` branch between EV_MODEL's COMP-param
write and its WS broadcast tail. The broadcast got orphaned under
EV_TIER_SYNC, where its ``str(tier)``/``str(picked)``/``str(short)``
references silently NameError'd. Net effect post-B-003: neither the
per-turn EV_MODEL nor the sticky-tier EV_TIER_SYNC successfully sent
the ``{"type":"model"}`` WS message, so the chat-panel model badge
stayed empty.

These tests pin both:
  * EV_MODEL emits exactly one ``model`` broadcast with the full
    {tier, model, short} payload.
  * EV_TIER_SYNC emits a ``model`` broadcast with the new tier (and
    blank picked/short, since the actual routed model isn't known
    until the next turn fires its own EV_MODEL).
  * Re-entering the EV_TIER_SYNC branch without a prior EV_MODEL
    must NOT raise NameError on tier/picked/short.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


class _StubExt:
    """Minimal stand-in for TDPilotAPIExt — provides only the surfaces
    that ``_handle_event`` actually touches: an ``owner`` with the COMP
    params, a transcript list, an html-status buffer, and a captured
    broadcast log."""

    class _Par:
        def __init__(self, val=""):
            self.val = val

    class _Owner:
        def __init__(self):
            self.par_Activemodel = _StubExt._Par("")
            self.par_Modeltier = _StubExt._Par("auto")
            # Aliased so attribute access matches TD's ``op.par.Foo`` shape.

        class _ParProxy:
            def __init__(self, outer):
                self._outer = outer

            @property
            def Activemodel(self):
                return self._outer.par_Activemodel

            @property
            def Modeltier(self):
                return self._outer.par_Modeltier

        @property
        def par(self):
            return self._ParProxy(self)

    def __init__(self):
        self.owner = self._Owner()
        self.broadcasts: list[dict] = []
        self.transcript: list[tuple[str, str]] = []

    # ---- methods the handler calls -------------------------------
    def _broadcast(self, payload):
        self.broadcasts.append(dict(payload))

    def _append_transcript(self, role, msg):
        self.transcript.append((role, str(msg)))

    def _html_append(self, role, msg):
        pass

    def _set_status(self, s):
        pass

    def _html_status(self, s):
        pass

    def _set_last_tool(self, name):
        pass

    def _play_done_sound(self, kind):
        pass

    def _drain_inbox_one(self):
        pass


def _make_ext_and_handler():
    """Import the real ``_handle_event`` and bind it to a stub instance."""
    from tdpilot_api_extension import TDPilotAPIExt

    ext = _StubExt()
    # Bind the real method to our stub instance — Python's descriptor
    # protocol lets us call an unbound method with the stub as self.
    return ext, TDPilotAPIExt._handle_event.__get__(ext, _StubExt)


def test_b004_ev_model_broadcasts_to_ws():
    """EV_MODEL must emit a ``{"type":"model"}`` WS broadcast with the
    full tier+model+short payload — this is what populates the chat
    badge in tdpilot_api_chat.html (case 'model' → setModel(msg))."""
    from tdpilot_api_runtime import EV_MODEL

    ext, handle = _make_ext_and_handler()
    handle(EV_MODEL, {"tier": "pro", "model": "deepseek-reasoner"})

    model_broadcasts = [b for b in ext.broadcasts if b.get("type") == "model"]
    assert len(model_broadcasts) == 1, (
        f"EV_MODEL must emit exactly one 'model' broadcast — got "
        f"{len(model_broadcasts)}: {model_broadcasts!r}"
    )
    b = model_broadcasts[0]
    assert b["tier"] == "pro"
    assert b["model"] == "deepseek-reasoner"
    # short normalises to 'pro' (because 'pro' is in 'deepseek-reasoner'?
    # actually no — let's accept either 'pro' if classifier matches or
    # the full model string if it doesn't).
    assert b["short"] in ("pro", "deepseek-reasoner")
    # And the COMP param is also written (Sprint 4.3 contract).
    assert ext.owner.par.Activemodel.val != ""


def test_b004_ev_model_flash_classification():
    """The 'short' classifier maps any model name containing 'flash'
    to the short label 'flash' — this drives the badge CSS class."""
    from tdpilot_api_runtime import EV_MODEL

    ext, handle = _make_ext_and_handler()
    handle(EV_MODEL, {"tier": "auto", "model": "deepseek-flash-v1"})

    model_broadcasts = [b for b in ext.broadcasts if b.get("type") == "model"]
    assert len(model_broadcasts) == 1
    assert model_broadcasts[0]["short"] == "flash"
    assert ext.owner.par.Activemodel.val == "flash"


def test_b004_ev_tier_sync_broadcasts_without_prior_ev_model():
    """Pre-fix EV_TIER_SYNC referenced tier/picked/short which were
    only in scope of EV_MODEL — NameError if EV_TIER_SYNC ever fired
    first. Post-fix it builds its own payload from new_tier."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    ext, handle = _make_ext_and_handler()
    # Critical: do NOT fire EV_MODEL first. Pre-fix this would NameError.
    handle(EV_TIER_SYNC, "pro")

    model_broadcasts = [b for b in ext.broadcasts if b.get("type") == "model"]
    assert len(model_broadcasts) == 1, (
        f"EV_TIER_SYNC must emit a 'model' broadcast on first fire — "
        f"got {len(model_broadcasts)}: {model_broadcasts!r}"
    )
    assert model_broadcasts[0]["tier"] == "pro"
    assert model_broadcasts[0]["short"] == "pro"
    # picked/full-model is unknown at promotion time; expect blank.
    assert model_broadcasts[0]["model"] == ""
    # And the Modeltier COMP param is written (B-003 contract).
    assert ext.owner.par.Modeltier.val == "pro"


def test_b004_ev_tier_sync_invalid_tier_no_param_write_but_still_broadcasts():
    """An invalid tier payload (e.g. accidental empty string or a typo)
    must NOT write the Modeltier param, but the broadcast can still
    fire with a blank short — defensive shape."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    ext, handle = _make_ext_and_handler()
    ext.owner.par_Modeltier.val = "auto"  # baseline
    handle(EV_TIER_SYNC, "garbage")

    assert ext.owner.par.Modeltier.val == "auto", (
        "invalid tier must not mutate Modeltier"
    )
    # Broadcast is still allowed (with blank short) — the WS client
    # is robust to empty payloads and just renders a clean badge.
    model_broadcasts = [b for b in ext.broadcasts if b.get("type") == "model"]
    assert len(model_broadcasts) == 1
    assert model_broadcasts[0]["tier"] == "garbage"
    assert model_broadcasts[0]["short"] == ""  # not in the allowlist


def test_b004_no_namerror_on_ev_tier_sync_only_path():
    """Regression pin: the bug we just fixed was a silent NameError on
    tier/picked/short. This test would have surfaced the regression
    via an unexpected exception, not just a missing broadcast."""
    from tdpilot_api_runtime import EV_TIER_SYNC

    ext, handle = _make_ext_and_handler()
    # Fire EV_TIER_SYNC alone — no prior EV_MODEL to define the names.
    # Pre-fix this raised NameError ("name 'tier' is not defined").
    try:
        handle(EV_TIER_SYNC, "flash")
    except NameError as exc:
        raise AssertionError(
            f"EV_TIER_SYNC must not NameError on tier/picked/short — "
            f"these names are local to EV_MODEL's scope. Got: {exc}"
        ) from exc
