"""v2.1.2 → C-1 (audit 2026-05-19) — `_disable_auth()` evolution.

History:

* Pre-2.1.2: ``autostart.onStart()`` unconditionally popped
  ``TD_MCP_SHARED_SECRET`` and forced ``TD_MCP_REQUIRE_AUTH=0`` on COMP
  load. Persistent MCP auth was effectively impossible — any secret in
  ``~/.tdpilot-dpsk4/.tdpilot-dpsk4.env`` got wiped before the
  webserverDAT could see it.
* v2.1.2: gated the bypass on ``TDPILOT_DISABLE_AUTH_BYPASS=1`` (opt-out).
  Default behavior unchanged — fresh drag-in still bypassed auth.
* v2.6 / C-1 audit fix (2026-05-19): inverted the default. The wipe now
  ONLY fires when ``TDPILOT_ENABLE_AUTH_BYPASS=1`` is set (opt-in). The
  legacy opt-out var still parses but is a no-op (with a deprecation
  print). Default = SECURE: the env file's secret survives so the
  webserverDAT enforces auth on every request.

These tests pin the v2.6 contract. The v2.1.2-era assertions ("no flag
→ auth bypassed") are now flipped to ("no flag → auth NOT bypassed");
the old behavior is exercised via the new opt-in env var.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUTOSTART_PATH = REPO_ROOT / "td_component" / "autostart.py"


@pytest.fixture
def autostart(monkeypatch):
    """Load ``td_component/autostart.py`` as a standalone module.

    Module-level imports (``os``) are pure; the TD-callback functions
    (``onStart``/``onFrameStart``) reference TD globals like
    ``parent()`` but the helpers we test (``_disable_auth``,
    ``_is_truthy_env``) don't, so importing as a free module is fine.
    The fixture also clears the auth-related env vars so prior test
    state never leaks into the assertions below.
    """
    spec = importlib.util.spec_from_file_location("autostart_v212", AUTOSTART_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["autostart_v212"] = mod
    spec.loader.exec_module(mod)
    for var in (
        "TD_MCP_SHARED_SECRET",
        "TD_MCP_REQUIRE_AUTH",
        "TDPILOT_DISABLE_AUTH_BYPASS",
        "TDPILOT_ENABLE_AUTH_BYPASS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield mod
    sys.modules.pop("autostart_v212", None)


# --------------------------------------------------------------------------
# C-1 (v2.6): default-secure. NO flag set → env file's secret survives.
# --------------------------------------------------------------------------


def test_disable_auth_default_preserves_secret(autostart, monkeypatch):
    """C-1 fix: no flag → secret PRESERVED, REQUIRE_AUTH NOT forced to 0.

    Inverts the v2.1.2 behavior. The pre-v2.6 default was vulnerable:
    any local process could POST /api/exec to td_exec_python because
    auth was forced off on every COMP load.
    """
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "preexisting-secret-from-env-file")
    monkeypatch.delenv("TD_MCP_REQUIRE_AUTH", raising=False)
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_SHARED_SECRET") == "preexisting-secret-from-env-file"
    assert "TD_MCP_REQUIRE_AUTH" not in os.environ


def test_disable_auth_default_does_not_override_existing_require_auth(autostart, monkeypatch):
    """C-1: no flag → ``TD_MCP_REQUIRE_AUTH=1`` from env file SURVIVES."""
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "1"


# --------------------------------------------------------------------------
# C-1: explicit opt-in (TDPILOT_ENABLE_AUTH_BYPASS) → legacy bypass behavior.
# --------------------------------------------------------------------------


def test_disable_auth_opt_in_wipes_secret(autostart, monkeypatch):
    """``TDPILOT_ENABLE_AUTH_BYPASS=1`` reproduces the pre-v2.6 zero-config flow.

    For single-user dev boxes / CI where the MCP port is unreachable
    from outside. Must wipe the secret AND force REQUIRE_AUTH=0.
    """
    monkeypatch.setenv("TDPILOT_ENABLE_AUTH_BYPASS", "1")
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "dev-secret")
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert "TD_MCP_SHARED_SECRET" not in os.environ
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "0"


@pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
def test_disable_auth_opt_in_truthy_variants(autostart, monkeypatch, flag_value):
    """The opt-in check accepts the same canonical truthy strings as the
    rest of the codebase (autostart, runtime, web_callbacks)."""
    monkeypatch.setenv("TDPILOT_ENABLE_AUTH_BYPASS", flag_value)
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "should-wipe")
    autostart._disable_auth()
    assert "TD_MCP_SHARED_SECRET" not in os.environ, (
        f"value {flag_value!r} should opt IN to bypass; secret survived"
    )
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "0"


@pytest.mark.parametrize("flag_value", ["", "0", "false", "FALSE", "no", "off", "  ", "anything-else"])
def test_disable_auth_opt_in_falsy_variants_stay_secure(autostart, monkeypatch, flag_value):
    """Anything that isn't a recognised truthy value falls back to the
    secure default — secret survives, REQUIRE_AUTH untouched.
    """
    monkeypatch.setenv("TDPILOT_ENABLE_AUTH_BYPASS", flag_value)
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "should-survive")
    monkeypatch.delenv("TD_MCP_REQUIRE_AUTH", raising=False)
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_SHARED_SECRET") == "should-survive", (
        f"value {flag_value!r} should NOT opt in to bypass; secret was wiped"
    )
    assert "TD_MCP_REQUIRE_AUTH" not in os.environ


# --------------------------------------------------------------------------
# C-1: legacy opt-out var (TDPILOT_DISABLE_AUTH_BYPASS) — now a no-op,
# parsed only for backwards-compat with v2.1.2..v2.5 env files.
# --------------------------------------------------------------------------


def test_disable_auth_legacy_opt_out_var_still_secure(autostart, monkeypatch):
    """``TDPILOT_DISABLE_AUTH_BYPASS=1`` is honored (no-op since default is
    already secure). Secret + REQUIRE_AUTH survive untouched."""
    monkeypatch.setenv("TDPILOT_DISABLE_AUTH_BYPASS", "1")
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "stays")
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_SHARED_SECRET") == "stays"
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "1"


def test_disable_auth_legacy_opt_out_wins_over_opt_in(autostart, monkeypatch):
    """If BOTH legacy opt-out AND new opt-in are set, the SAFER one
    (opt-out → secure) wins. Defense-in-depth: it's never wrong to be
    more secure.
    """
    monkeypatch.setenv("TDPILOT_DISABLE_AUTH_BYPASS", "1")
    monkeypatch.setenv("TDPILOT_ENABLE_AUTH_BYPASS", "1")
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "stays")
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_SHARED_SECRET") == "stays"
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "1"


# --------------------------------------------------------------------------
# Helper + constants.
# --------------------------------------------------------------------------


def test_is_truthy_env_unset_returns_false(autostart, monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert autostart._is_truthy_env("SOME_FLAG") is False


def test_is_truthy_env_handles_whitespace(autostart, monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "  yes  ")
    assert autostart._is_truthy_env("SOME_FLAG") is True


def test_disable_auth_constants_are_documented(autostart):
    """The constant names appear in the function docstring so anyone
    grepping for either env var lands on the explanation."""
    assert "_AUTH_BYPASS_OPT_OUT_VAR" in dir(autostart)
    assert "_AUTH_BYPASS_OPT_IN_VAR" in dir(autostart)
    assert autostart._AUTH_BYPASS_OPT_OUT_VAR == "TDPILOT_DISABLE_AUTH_BYPASS"
    assert autostart._AUTH_BYPASS_OPT_IN_VAR == "TDPILOT_ENABLE_AUTH_BYPASS"
    doc = autostart._disable_auth.__doc__ or ""
    assert "TDPILOT_DISABLE_AUTH_BYPASS" in doc
    assert "TDPILOT_ENABLE_AUTH_BYPASS" in doc


# --------------------------------------------------------------------------
# v2.5.4 N-1 first-run UX hint: when default-secure mode is active AND
# no secret is installed, print a Textport line telling the user how to
# either install a secret or opt into the legacy zero-config flow.
# --------------------------------------------------------------------------


def test_n1_first_run_hint_printed_when_no_secret(autostart, monkeypatch, capsys):
    """No env vars set + no secret installed → user gets the
    diagnostic line (otherwise their MCP server returns 401 with no
    obvious cause)."""
    # All three env vars deliberately unset by the fixture.
    autostart._disable_auth()
    captured = capsys.readouterr().out
    assert "default-secure mode" in captured
    assert "TDPILOT_ENABLE_AUTH_BYPASS" in captured
    assert "tdpilot_API.tox" in captured or "Authmode wizard" in captured


def test_n1_no_hint_when_secret_installed(autostart, monkeypatch, capsys):
    """If the env file populated TD_MCP_SHARED_SECRET, MCP requests
    will authenticate successfully — no hint needed."""
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "installed-by-env-file")
    autostart._disable_auth()
    captured = capsys.readouterr().out
    assert "default-secure mode" not in captured


def test_n1_no_hint_when_user_opted_into_bypass(autostart, monkeypatch, capsys):
    """If the user explicitly set TDPILOT_ENABLE_AUTH_BYPASS=1, the
    bypass branch runs FIRST and the N-1 hint never fires."""
    monkeypatch.setenv("TDPILOT_ENABLE_AUTH_BYPASS", "1")
    autostart._disable_auth()
    captured = capsys.readouterr().out
    # The bypass branch prints its own message; N-1 must not fire.
    assert "default-secure mode" not in captured
    assert "auth bypass enabled" in captured


def test_n1_no_hint_when_require_auth_already_zero(autostart, monkeypatch, capsys):
    """If the env file set TD_MCP_REQUIRE_AUTH=0 directly (legacy
    workflow), the webserverDAT is already non-secure — N-1 doesn't
    fire because MCP requests will pass anyway."""
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "0")
    autostart._disable_auth()
    captured = capsys.readouterr().out
    assert "default-secure mode" not in captured
