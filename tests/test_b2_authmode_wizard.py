"""v2.4 / Phase B.2 — Authmode=open → token migration wizard.

Tests pin the introspect-side surface (firstrun_status) so the chat HTML
has a reliable signal to drive the wizard. The HTTP route handler
(POST /set-authmode in tdpilot_api_web_callbacks.py) is integration-level —
it needs a TD-side request/response pair to test end-to-end; that's covered
by manual smoke test in a live TD session, per the plan.

These tests pin:
  * Legacy COMPs (Authmode=open) surface `authmode_is_open: True` + a
    `switch_to_token_auth` step PREPENDED to next_steps (so the user
    sees the migration prompt first).
  * Token-mode COMPs do NOT surface the wizard step (no spurious nudges
    for users who are already on the safe default).
  * No-COMP (test context where `parent()` raises NameError) defaults to
    closed-loop behavior — empty authmode, no wizard step.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))


def _inject_parent(monkeypatch, authmode_value: str | None):
    """Inject a fake `parent()` into the introspect module's globals.

    ``authmode_value=None`` simulates a COMP without an Authmode param
    (truly legacy build, unusual). ``""`` simulates the COMP present
    but Authmode unset. ``"open"`` / ``"token"`` simulate the two
    real Authmode states.
    """
    import tdpilot_api_introspect

    if authmode_value is None:
        # COMP exists but has no Authmode attr
        fake_par = SimpleNamespace()
        fake_comp = SimpleNamespace(par=fake_par)
    else:
        fake_par = SimpleNamespace(Authmode=SimpleNamespace(val=authmode_value))
        fake_comp = SimpleNamespace(par=fake_par)
    monkeypatch.setitem(tdpilot_api_introspect.__dict__, "parent", lambda: fake_comp)


def test_b2_firstrun_surfaces_authmode_open_with_wizard_step(monkeypatch):
    """Legacy Authmode=open → firstrun reports authmode_is_open=True and
    PREPENDS a switch_to_token_auth step to next_steps."""
    _inject_parent(monkeypatch, "open")
    from tdpilot_api_introspect import firstrun_status

    result = firstrun_status()

    assert result["authmode"] == "open"
    assert result["authmode_is_open"] is True
    step_names = [s["name"] for s in result["next_steps"]]
    assert "switch_to_token_auth" in step_names, (
        f"open-mode COMP must surface the migration step, got: {step_names}"
    )
    # The migration step must be FIRST so the user sees it before any
    # other quickstart items.
    assert step_names[0] == "switch_to_token_auth", (
        f"migration step must be prepended, got order: {step_names}"
    )


def test_b2_firstrun_no_wizard_step_when_authmode_token(monkeypatch):
    """Token-mode COMP must NOT surface the migration prompt — no
    nudge for users already on the safe default."""
    _inject_parent(monkeypatch, "token")
    from tdpilot_api_introspect import firstrun_status

    result = firstrun_status()

    assert result["authmode"] == "token"
    assert result["authmode_is_open"] is False
    step_names = [s["name"] for s in result["next_steps"]]
    assert "switch_to_token_auth" not in step_names, (
        f"token-mode COMP must not surface migration prompt, got: {step_names}"
    )


def test_b2_firstrun_no_wizard_step_when_no_parent(monkeypatch):
    """Test context (no `parent()` builtin) → empty authmode, no step.
    This guards against false-positive wizard prompts in non-TD harnesses."""
    # Don't inject parent — the function will hit NameError internally.
    import tdpilot_api_introspect

    # Make sure no leftover injection from a prior test.
    tdpilot_api_introspect.__dict__.pop("parent", None)

    from tdpilot_api_introspect import firstrun_status

    result = firstrun_status()

    assert result["authmode"] == ""
    assert result["authmode_is_open"] is False
    step_names = [s["name"] for s in result["next_steps"]]
    assert "switch_to_token_auth" not in step_names


def test_b2_firstrun_step_has_recommended_action(monkeypatch):
    """The migration step carries a `recommended_action` field so the
    chat HTML can branch on it (POST mode=token to /set-authmode)."""
    _inject_parent(monkeypatch, "open")
    from tdpilot_api_introspect import firstrun_status

    result = firstrun_status()
    migration_step = next(s for s in result["next_steps"] if s["name"] == "switch_to_token_auth")
    assert migration_step.get("recommended_action") == "switch_to_token"


def test_b2_firstrun_empty_authmode_treated_as_not_open(monkeypatch):
    """Authmode value of empty string → not 'open' → no wizard prompt.
    Guards against false positives when the COMP exists but Authmode
    is unset (shouldn't happen with the schema default, but defensive)."""
    _inject_parent(monkeypatch, "")
    from tdpilot_api_introspect import firstrun_status

    result = firstrun_status()

    assert result["authmode"] == ""
    assert result["authmode_is_open"] is False
    step_names = [s["name"] for s in result["next_steps"]]
    assert "switch_to_token_auth" not in step_names
