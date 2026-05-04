"""Unit tests for make_dispatcher in tdpilot_api_dispatcher.

The dispatcher is the hub every tool call routes through, so its error
paths (unknown tool, missing handler on every module, tuple result
normalisation, extras-shadow-builtin precedence) need first-class
coverage. These cases are pure-Python — no TD/MCP plumbing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tdpilot_api_dispatcher import DispatchError, make_dispatcher  # noqa: E402


def test_unknown_tool_returns_error_dict_with_supported_list():
    """Unknown name → {error, supported}; the agent surfaces this back to
    the model so it can pick a real tool on the next turn."""
    mod = SimpleNamespace()  # no handlers at all
    dispatch = make_dispatcher(mod)

    out = dispatch("td_does_not_exist", {})
    assert isinstance(out, dict)
    assert "error" in out
    assert "Unknown tool" in out["error"]
    assert "supported" in out
    assert isinstance(out["supported"], list)
    # Every real schema entry should appear in the supported list.
    assert "td_get_info" in out["supported"]


def test_extras_shadow_builtin_handler():
    """When a tool name appears in both extras AND TOOL_TO_HANDLER, extras
    win. This is the user-pluggable-tools precedence rule (Sprint 4.2)."""
    builtin_called = {"flag": False}
    user_called = {"flag": False}

    def builtin(_body):
        builtin_called["flag"] = True
        return {"who": "builtin"}

    def user(_body):
        user_called["flag"] = True
        return {"who": "user"}

    mod = SimpleNamespace(
        handle_get_info=builtin,
        handle_user=user,
    )
    extras = {"td_get_info": ("handle_user", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("td_get_info", {})
    assert out == {"who": "user"}
    assert user_called["flag"] is True
    assert builtin_called["flag"] is False


def test_tuple_result_is_normalised_into_dict():
    """Some legacy handlers return ``(status_code, payload_dict)``. The
    dispatcher must fold the status into the payload so the model sees
    uniform JSON."""

    def handler(_body):
        return (418, {"ok": True, "value": 42})

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"echo_tuple": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("echo_tuple", {})
    assert isinstance(out, dict)
    assert out["ok"] is True
    assert out["value"] == 42
    assert out["_status"] == 418


def test_handler_missing_from_all_modules_returns_error():
    """Mapping points to a handler name that doesn't exist on any of the
    registered handler modules — must return an explicit error rather
    than crashing."""
    mod_a = SimpleNamespace()  # no handle_* defined
    mod_b = SimpleNamespace(handle_other=lambda _: {"ok": True})
    extras = {"echo_missing": ("handle_does_not_exist", lambda d: d or {})}
    dispatch = make_dispatcher((mod_a, mod_b), extra_mappings=extras)

    out = dispatch("echo_missing", {})
    assert "error" in out
    assert "handle_does_not_exist" in out["error"]


def test_handler_exception_becomes_error_dict_with_traceback():
    """A handler that raises must surface as ``{error, traceback}`` so
    the agent can show it to the model without crashing the loop."""

    def handler(_body):
        raise RuntimeError("kaboom")

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"explode": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("explode", {})
    assert "error" in out
    assert "RuntimeError" in out["error"]
    assert "kaboom" in out["error"]
    assert "traceback" in out


def test_none_handlers_modules_raises_dispatcherror():
    """Programmer error — the dispatcher refuses to be constructed with
    no handler module, since every tool call would fail."""
    with pytest.raises(DispatchError):
        make_dispatcher(None)


def test_empty_handlers_modules_raises_dispatcherror():
    with pytest.raises(DispatchError):
        make_dispatcher(())
