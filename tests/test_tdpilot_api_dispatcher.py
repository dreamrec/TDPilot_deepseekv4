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


# ---------------------------------------------------------------------------
# Phase 2.3 — failure recovery hints.
#
# The dispatcher annotates error results with a ``recovery_hint`` field
# when the error message matches one of the patterns in
# ``tdpilot_api_recovery._RECOVERY_HINTS``. The agent sees both the
# error and the hint, so it can route differently on the next turn
# instead of retrying the same failed call.
# ---------------------------------------------------------------------------


def test_unknown_operator_error_attaches_hint():
    """The 'Unknown operator type' error path is the canonical
    learn-from-failure case — the hint must point at td_list_families.
    """

    def handler(_body):
        return {"error": "Unknown operator type: noiseTopBad"}

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert "error" in out
    assert "recovery_hint" in out
    hint = out["recovery_hint"]
    assert "td_list_families" in hint or "camelCase" in hint


def test_path_not_found_error_attaches_hint():
    def handler(_body):
        return {"error": "Path not found: /project1/zoinks"}

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert "recovery_hint" in out
    assert "td_get_nodes" in out["recovery_hint"]


def test_thread_conflict_error_attaches_hint():
    """The THREAD CONFLICT pattern — most common when an exec_python
    call returns a raw op() reference."""

    def handler(_body):
        return {"error": "THREAD CONFLICT: TouchDesigner objects cannot be accessed outside main thread"}

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert "recovery_hint" in out
    assert "td_exec_python" in out["recovery_hint"] or "non-cook thread" in out["recovery_hint"]


def test_corpus_not_installed_error_attaches_hint():
    def handler(_body):
        return {"error": "The 'derivative' corpus isn't installed locally."}

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert "recovery_hint" in out
    assert "brains add" in out["recovery_hint"] or "data/normalized" in out["recovery_hint"]


def test_handler_exception_message_matched_against_hints():
    """A handler that RAISES (rather than returns an error dict) gets
    its exception message run through the same matcher.
    """

    def handler(_body):
        raise PermissionError("Permission denied: /etc/restricted")

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert "error" in out
    assert "recovery_hint" in out
    assert "writable" in out["recovery_hint"] or "permission" in out["recovery_hint"].lower()


def test_unknown_tool_error_attaches_no_hint():
    """The "Unknown tool: X" message isn't in the registry — pass
    through as before, no spurious hint."""
    mod = SimpleNamespace()
    dispatch = make_dispatcher(mod)

    out = dispatch("td_does_not_exist", {})
    assert "error" in out
    assert "recovery_hint" not in out


def test_successful_result_passes_through_unchanged():
    """Happy path: no error, no recovery_hint added."""

    def handler(_body):
        return {"ok": True, "fps": 60}

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert out == {"ok": True, "fps": 60}
    assert "recovery_hint" not in out


def test_caller_provided_recovery_hint_is_respected():
    """A handler that already includes its own recovery_hint shouldn't
    have it overwritten by the registry. Lets handlers ship more
    specific suggestions when they have context the registry can't see.
    """

    def handler(_body):
        return {
            "error": "Path not found: /custom",
            "recovery_hint": "specific custom hint",
        }

    mod = SimpleNamespace(handle_get_info=handler)
    extras = {"probe": ("handle_get_info", lambda d: d or {})}
    dispatch = make_dispatcher(mod, extra_mappings=extras)

    out = dispatch("probe", {})
    assert out["recovery_hint"] == "specific custom hint"


def test_hint_for_message_helper():
    """The standalone helper is symmetric with attach_hint and useful
    for UI surfaces (verify panel etc.) that want to render hints
    independent of the dispatch pipeline.
    """
    from tdpilot_api_recovery import hint_for_message

    assert hint_for_message("Unknown operator type: x") is not None
    assert hint_for_message("totally unrelated error message") is None
    assert hint_for_message("") is None
    assert hint_for_message(None) is None  # type: ignore[arg-type]


def test_registered_patterns_introspection():
    """Phase 2.3 ships a known set of patterns; lock in that the
    canonical ones are present so future shrinkage trips a test.
    """
    from tdpilot_api_recovery import registered_patterns

    patterns = registered_patterns()
    assert any("Unknown operator" in p for p in patterns)
    assert any("THREAD CONFLICT" in p or "thread conflict" in p for p in patterns)
    assert any("corpus" in p for p in patterns)
    assert any("recipe" in p for p in patterns)
    assert any("Permission denied" in p for p in patterns)
    # Sanity: at least 8 patterns registered.
    assert len(patterns) >= 8
