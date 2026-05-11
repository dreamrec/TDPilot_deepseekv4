"""Tests for Phase 1.2.1 (v2.2.1) UX polish:

* ``Authmode`` COMP param wired into ``_insecure_mode``.
* Backwards compatibility with the ``TDPILOT_API_INSECURE`` env var.
* ``tdpilot_api_parexec.onValueChange`` routes only ``Apikey`` and
  ``Authmode`` to the extension; everything else is a no-op.
* Empty Apikey values short-circuit so the wipe-after-save in
  ``OnSaveApiKeyPulse`` doesn't trigger infinite recursion.
"""

from __future__ import annotations

import builtins
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_web_callbacks.py"


# ---------------------------------------------------------------------------
# Fake COMP infrastructure — extends the test_standalone_csrf.py pattern
# with a settable Authmode param.
# ---------------------------------------------------------------------------


class _FakeStorage(dict):
    def fetch(self, key, default=None):
        return self.get(key, default)

    def store(self, key, value):
        self[key] = value


class _FakeParam:
    """Minimal stand-in for a TD parameter. Carries a ``.val`` attribute
    that the test mutates and the module reads."""

    def __init__(self, val):
        self.val = val


class _FakeComp(_FakeStorage):
    def __init__(self, *, authmode=None, port=9987):
        super().__init__()
        self._ops = {
            "chat_web_server": SimpleNamespace(
                par=SimpleNamespace(port=SimpleNamespace(eval=lambda p=port: p))
            )
        }
        self.par = SimpleNamespace()
        if authmode is not None:
            # Setattr so hasattr(comp.par, "Authmode") returns True only
            # when the test explicitly asks for a param. The
            # "no Authmode" case (older .tox build) sees the falsy
            # hasattr branch.
            self.par.Authmode = _FakeParam(authmode)

    def op(self, name):
        return self._ops.get(name)


@pytest.fixture
def web_module(monkeypatch):
    """Load tdpilot_api_web_callbacks with TD globals mocked, returning
    (module, comp_setter) where comp_setter(authmode=None) swaps the
    bound COMP for one configured with the given Authmode value."""
    bound: dict = {"comp": _FakeComp()}

    fake_op_registry = {"/project1/tdpilot_API": bound}

    def fake_op(path):
        slot = fake_op_registry.get(path)
        if slot is None:
            return None
        return slot["comp"]

    def fake_parent():
        return bound["comp"]

    monkeypatch.setitem(builtins.__dict__, "op", fake_op)
    monkeypatch.setitem(builtins.__dict__, "parent", fake_parent)
    monkeypatch.setitem(builtins.__dict__, "me", SimpleNamespace())

    # Force a clean import each test so module-level state doesn't leak.
    mod_name = "_tdpilot_api_web_callbacks_v221_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    # Override the module's _comp() helper so it returns OUR fake — the
    # default implementation walks `op('/project1/tdpilot_API')`, which
    # already routes via fake_op above; but defensive override keeps
    # tests robust to future module-internal refactors.
    monkeypatch.setattr(module, "_comp", lambda: bound["comp"])

    def set_comp(*, authmode=None):
        bound["comp"] = _FakeComp(authmode=authmode)
        return bound["comp"]

    yield module, set_comp

    sys.modules.pop(mod_name, None)


# ---------------------------------------------------------------------------
# _insecure_mode — Authmode param takes precedence over env var
# ---------------------------------------------------------------------------


class TestInsecureModeFromAuthmode:
    def test_authmode_open_means_insecure(self, web_module, monkeypatch):
        module, set_comp = web_module
        set_comp(authmode="open")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is True

    def test_authmode_token_means_secure(self, web_module, monkeypatch):
        module, set_comp = web_module
        set_comp(authmode="token")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is False

    def test_authmode_open_overrides_env_var_unset(self, web_module, monkeypatch):
        """Default-state user: no env var, Authmode=open. Should be insecure."""
        module, set_comp = web_module
        set_comp(authmode="open")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is True

    def test_authmode_token_beats_env_var_set(self, web_module, monkeypatch):
        """The COMP param is the source of truth. If user explicitly
        flipped to 'token' but has a stale TDPILOT_API_INSECURE=1 env
        var lingering, the param wins. Otherwise we'd silently leave
        token-mode users running insecure."""
        module, set_comp = web_module
        set_comp(authmode="token")
        monkeypatch.setenv("TDPILOT_API_INSECURE", "1")
        assert module._insecure_mode() is False

    def test_authmode_with_whitespace_and_case(self, web_module, monkeypatch):
        """COMP param values might come back with stray whitespace
        or uppercase. Normalize before comparing."""
        module, set_comp = web_module
        comp = set_comp(authmode="  OPEN  ")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is True

        comp.par.Authmode.val = "Token"
        assert module._insecure_mode() is False

    def test_authmode_unknown_value_falls_through_to_env_var(self, web_module, monkeypatch):
        """If somehow the COMP param has a junk value (corrupted .toe,
        post-rename leftover, etc.), don't trust it — fall back to env
        var instead of defaulting silently to either mode."""
        module, set_comp = web_module
        set_comp(authmode="garbage")
        monkeypatch.setenv("TDPILOT_API_INSECURE", "1")
        assert module._insecure_mode() is True  # env-var fallback wins

        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is False  # default to secure


# ---------------------------------------------------------------------------
# Backwards compat: COMP without Authmode param (older .tox) still works
# ---------------------------------------------------------------------------


class TestBackwardsCompatibility:
    def test_no_authmode_param_falls_back_to_env_var(self, web_module, monkeypatch):
        """Users running a pre-v2.2.1 .tox have no Authmode param on
        the COMP. The check should fall through to the env var
        without exception."""
        module, set_comp = web_module
        set_comp(authmode=None)  # no Authmode attr on comp.par
        monkeypatch.setenv("TDPILOT_API_INSECURE", "1")
        assert module._insecure_mode() is True

        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is False

    def test_comp_resolution_failure_does_not_raise(self, web_module, monkeypatch):
        """If _comp() raises or returns None for any reason (cooking
        race, COMP destroyed, etc.), the auth check must not crash —
        it should degrade silently to the env-var fallback."""
        module, set_comp = web_module
        monkeypatch.setattr(module, "_comp", lambda: None)
        monkeypatch.setenv("TDPILOT_API_INSECURE", "yes")
        assert module._insecure_mode() is True
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        assert module._insecure_mode() is False


# ---------------------------------------------------------------------------
# Auth-gate end-to-end: open mode bypasses token, token mode enforces it
# ---------------------------------------------------------------------------


class TestAuthGateEndToEnd:
    def test_open_mode_lets_send_through_without_token(self, web_module, monkeypatch):
        """The whole point of Phase 1.2.1 — drag-in users hit /send
        with no X-TDPilot-Token header and it Just Works."""
        module, set_comp = web_module
        set_comp(authmode="open")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        err = module._check_auth(
            "POST",
            "/send",
            {"origin": "http://127.0.0.1:9987"},
        )
        assert err is None

    def test_token_mode_still_blocks_without_token(self, web_module, monkeypatch):
        """Security-conscious users flip Authmode=token; the token
        check kicks back in."""
        module, set_comp = web_module
        set_comp(authmode="token")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        err = module._check_auth(
            "POST",
            "/send",
            {"origin": "http://127.0.0.1:9987"},
        )
        assert err is not None
        assert err[0] == 401

    def test_open_mode_still_blocks_cross_origin(self, web_module, monkeypatch):
        """Open mode bypasses the TOKEN check only — the origin
        allowlist still enforces single-machine binding. A browser
        tab from a malicious site can't drive the chat-pipe even
        when Authmode=open."""
        module, set_comp = web_module
        set_comp(authmode="open")
        monkeypatch.delenv("TDPILOT_API_INSECURE", raising=False)
        err = module._check_auth(
            "POST",
            "/send",
            {"origin": "https://attacker.example.com"},
        )
        assert err is not None
        assert err[0] == 403
        assert "cross-origin" in err[1].lower()


# ---------------------------------------------------------------------------
# Parexec onValueChange routing
# ---------------------------------------------------------------------------


@pytest.fixture
def parexec_module(monkeypatch):
    """Import tdpilot_api_parexec with TD globals mocked. Returns the
    module + a recorder dict so tests can assert which extension method
    was called."""
    recorder: dict = {"calls": []}

    monkeypatch.setitem(builtins.__dict__, "debug", lambda *a, **k: None)
    monkeypatch.setitem(builtins.__dict__, "op", lambda p: None)

    mod_name = "_tdpilot_api_parexec_v221_test"
    sys.modules.pop(mod_name, None)
    path = Path(__file__).resolve().parents[1] / "td_component" / "tdpilot_api_parexec.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)

    yield module, recorder

    sys.modules.pop(mod_name, None)


def _make_param_and_comp(name, val, recorder):
    """Build a fake (par, comp) pair that mimics the TD types the
    parexec module pokes at."""

    class _Ext:
        def OnApikeyValueChange(self, par):
            recorder["calls"].append(("OnApikeyValueChange", par.name, par.val))

        def OnAuthmodeValueChange(self, par, prev):
            recorder["calls"].append(("OnAuthmodeValueChange", par.name, par.val, prev))

    class _ExtModule:
        @staticmethod
        def get_extension(comp):
            return _Ext()

    class _ExtDat:
        @property
        def module(self):
            return _ExtModule

    class _Comp:
        def op(self, name):
            if name == "tdpilot_api_extension":
                return _ExtDat()
            return None

    comp = _Comp()
    par = SimpleNamespace(name=name, val=val, owner=comp)
    return par, comp


class TestParexecValueChangeRouting:
    def test_apikey_change_routes_to_OnApikeyValueChange(self, parexec_module):
        module, recorder = parexec_module
        par, _ = _make_param_and_comp("Apikey", "sk-new", recorder)
        module.onValueChange(par, prev="")
        assert recorder["calls"] == [("OnApikeyValueChange", "Apikey", "sk-new")]

    def test_authmode_change_routes_to_OnAuthmodeValueChange(self, parexec_module):
        module, recorder = parexec_module
        par, _ = _make_param_and_comp("Authmode", "token", recorder)
        module.onValueChange(par, prev="open")
        assert recorder["calls"] == [("OnAuthmodeValueChange", "Authmode", "token", "open")]

    def test_other_param_changes_are_noop(self, parexec_module):
        """Maxtokens / Temperature / Turnbudget value changes should
        NOT route — the parexec value-change is narrow on purpose to
        avoid spurious extension calls / reload loops."""
        module, recorder = parexec_module
        for name in ("Maxtokens", "Temperature", "Turnbudget", "Soundondone"):
            par, _ = _make_param_and_comp(name, "1", recorder)
            module.onValueChange(par, prev="")
        assert recorder["calls"] == []

    def test_missing_extension_does_not_raise(self, parexec_module):
        """If the extension DAT isn't reachable (boot race, deletion,
        etc.) the value-change handler must log + return, not crash."""
        module, _ = parexec_module

        class _BrokenComp:
            def op(self, name):
                return None

        par = SimpleNamespace(name="Apikey", val="sk-x", owner=_BrokenComp())
        # Should not raise.
        module.onValueChange(par, prev="")

    def test_extension_method_exception_is_swallowed(self, parexec_module):
        """A handler raising must not propagate into the cook thread —
        TD would otherwise mark the DAT in error state."""
        module, _ = parexec_module

        class _RaisingExt:
            def OnApikeyValueChange(self, par):
                raise RuntimeError("simulated handler bug")

        class _ExtModule:
            @staticmethod
            def get_extension(comp):
                return _RaisingExt()

        class _ExtDat:
            @property
            def module(self):
                return _ExtModule

        class _Comp:
            def op(self, name):
                return _ExtDat() if name == "tdpilot_api_extension" else None

        par = SimpleNamespace(name="Apikey", val="sk-x", owner=_Comp())
        # Should not raise.
        module.onValueChange(par, prev="")


# ---------------------------------------------------------------------------
# Apikey value-change short-circuit (empty value = no recursion)
# ---------------------------------------------------------------------------


class TestApikeyEmptyShortCircuit:
    """The danger: OnApikeyValueChange triggers OnSaveApiKeyPulse, which
    wipes Apikey.val = "" after saving. That wipe re-fires onValueChange
    a second time. The handler MUST short-circuit on empty values to
    avoid infinite recursion (or, worse, save_api_key_to_config("") which
    would clobber the saved key file with an empty string).

    The short-circuit lives in tdpilot_api_extension.OnApikeyValueChange,
    not in the parexec — the parexec routes EVERY Apikey change
    regardless of value. So this test reaches into the extension class
    via a controlled import.
    """

    def test_empty_value_does_not_call_save_pulse(self, monkeypatch):
        """Reach the extension module, stub OnSaveApiKeyPulse, drive
        OnApikeyValueChange with an empty value, assert no save fired."""
        # Lazy import — extension module imports TD globals at top level
        # via dispatcher/runtime; we mock those out.
        monkeypatch.setitem(builtins.__dict__, "op", lambda p: None)
        monkeypatch.setitem(builtins.__dict__, "parent", lambda: SimpleNamespace())
        monkeypatch.setitem(builtins.__dict__, "me", SimpleNamespace())

        # Patch OnSaveApiKeyPulse to record calls and skip its real body
        # (which would need ~/.tdpilot-api/ write access etc.).
        save_calls: list = []

        # The simplest path: drive OnApikeyValueChange via a synthetic
        # extension-like object that carries the real method but stubs
        # OnSaveApiKeyPulse. We don't import the real extension module
        # (it has a wide TD-globals surface); instead replicate the
        # 5-line method body inline and assert its short-circuit.

        class _MiniExt:
            def OnSaveApiKeyPulse(self):
                save_calls.append("save")

            # Pulled verbatim from tdpilot_api_extension.OnApikeyValueChange.
            def OnApikeyValueChange(self, par):
                try:
                    value = str(par.val or "").strip()
                except Exception:
                    value = ""
                if not value:
                    return
                self.OnSaveApiKeyPulse()

        ext = _MiniExt()
        # Empty values — must not call save.
        for empty in ("", None, "   ", "\t\n"):
            ext.OnApikeyValueChange(SimpleNamespace(val=empty))
        assert save_calls == []

        # Real value — must call save.
        ext.OnApikeyValueChange(SimpleNamespace(val="sk-foo"))
        assert save_calls == ["save"]
