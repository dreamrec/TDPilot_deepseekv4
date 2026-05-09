"""v2.1.2 regression — `_disable_auth()` becomes opt-out.

Pre-2.1.2 the dpsk4 COMP's ``autostart.onStart()`` unconditionally popped
``TD_MCP_SHARED_SECRET`` and forced ``TD_MCP_REQUIRE_AUTH=0``. That made
the chat (per-launch token) work zero-config but it also meant any
persistent secret a user wrote to
``~/.tdpilot-dpsk4/.tdpilot-dpsk4.env`` got wiped before the
webserverDAT could see it — so persistent MCP auth was effectively
impossible without editing the .tox source.

v2.1.2 gates the bypass on ``TDPILOT_DISABLE_AUTH_BYPASS=1`` (env var
or env-file entry). When set truthy, ``_disable_auth()`` is a no-op
and any secret the env file installed survives. Default behavior is
unchanged — a fresh drag-in still gets unauthenticated MCP access
for the zero-config local-dev flow.
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
    for var in ("TD_MCP_SHARED_SECRET", "TD_MCP_REQUIRE_AUTH", "TDPILOT_DISABLE_AUTH_BYPASS"):
        monkeypatch.delenv(var, raising=False)
    yield mod
    sys.modules.pop("autostart_v212", None)


# --------------------------------------------------------------------------
# Default (opt-out flag NOT set) — pre-2.1.2 behavior preserved.
# --------------------------------------------------------------------------


def test_disable_auth_default_pops_secret_and_forces_zero(autostart, monkeypatch):
    """No flag → auth bypassed. Same as v2.1.1 and earlier."""
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "preexisting-secret-from-env-file")
    monkeypatch.delenv("TD_MCP_REQUIRE_AUTH", raising=False)
    autostart._disable_auth()
    assert "TD_MCP_SHARED_SECRET" not in os.environ
    assert os.environ["TD_MCP_REQUIRE_AUTH"] == "0"


def test_disable_auth_default_overrides_existing_require_auth(autostart, monkeypatch):
    """No flag → ``TD_MCP_REQUIRE_AUTH=1`` from env file gets clobbered to ``0``."""
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ["TD_MCP_REQUIRE_AUTH"] == "0"


# --------------------------------------------------------------------------
# Opt-out flag set — secret survives, REQUIRE_AUTH preserved.
# --------------------------------------------------------------------------


def test_disable_auth_opt_out_preserves_secret(autostart, monkeypatch):
    """``TDPILOT_DISABLE_AUTH_BYPASS=1`` → no-op."""
    monkeypatch.setenv("TDPILOT_DISABLE_AUTH_BYPASS", "1")
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "user-set-secret-eaa6f39f")
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ["TD_MCP_SHARED_SECRET"] == "user-set-secret-eaa6f39f"
    assert os.environ["TD_MCP_REQUIRE_AUTH"] == "1"


@pytest.mark.parametrize("flag_value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
def test_disable_auth_truthy_variants_all_opt_out(autostart, monkeypatch, flag_value):
    """The opt-out check accepts the same truthy strings as ``TDPILOT_API_INSECURE``
    so users can mirror existing conventions without surprises."""
    monkeypatch.setenv("TDPILOT_DISABLE_AUTH_BYPASS", flag_value)
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "kept")
    monkeypatch.setenv("TD_MCP_REQUIRE_AUTH", "1")
    autostart._disable_auth()
    assert os.environ.get("TD_MCP_SHARED_SECRET") == "kept", (
        f"value {flag_value!r} should opt out — secret was wiped"
    )
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "1", (
        f"value {flag_value!r} should opt out — REQUIRE_AUTH was forced to 0"
    )


@pytest.mark.parametrize("flag_value", ["", "0", "false", "FALSE", "no", "off", "  ", "anything-else"])
def test_disable_auth_falsy_variants_still_bypass(autostart, monkeypatch, flag_value):
    """Anything that isn't a recognised truthy value falls through to the
    legacy bypass — keeps users from accidentally enabling auth via a
    typo'd / commented-out env file line.
    """
    monkeypatch.setenv("TDPILOT_DISABLE_AUTH_BYPASS", flag_value)
    monkeypatch.setenv("TD_MCP_SHARED_SECRET", "wiped")
    monkeypatch.delenv("TD_MCP_REQUIRE_AUTH", raising=False)
    autostart._disable_auth()
    assert "TD_MCP_SHARED_SECRET" not in os.environ, (
        f"value {flag_value!r} should NOT opt out — secret survived unexpectedly"
    )
    assert os.environ.get("TD_MCP_REQUIRE_AUTH") == "0"


# --------------------------------------------------------------------------
# Opt-out helper itself — leak-tested independently.
# --------------------------------------------------------------------------


def test_is_truthy_env_unset_returns_false(autostart, monkeypatch):
    monkeypatch.delenv("SOME_FLAG", raising=False)
    assert autostart._is_truthy_env("SOME_FLAG") is False


def test_is_truthy_env_handles_whitespace(autostart, monkeypatch):
    monkeypatch.setenv("SOME_FLAG", "  yes  ")
    assert autostart._is_truthy_env("SOME_FLAG") is True


def test_disable_auth_opt_out_constant_is_documented(autostart):
    """The constant name appears in the function docstring so anyone
    grepping for the env var lands on the explanation."""
    assert "_AUTH_BYPASS_OPT_OUT_VAR" in dir(autostart)
    assert autostart._AUTH_BYPASS_OPT_OUT_VAR == "TDPILOT_DISABLE_AUTH_BYPASS"
    assert "TDPILOT_DISABLE_AUTH_BYPASS" in (autostart._disable_auth.__doc__ or "")
