"""PR-17 (F-12) — explicit ``_tool_error`` sentinel for tool failures.

Pre-1.8.1 the agent loop and ``tool_batch`` decided "did this call
fail?" via ``"error" in result`` — a brittle heuristic that
misclassified any successful handler whose result legitimately
contained an ``error`` field (e.g. ``td_get_errors`` returning a list
of compile errors).

PR-17 introduces ``_tool_error: bool`` as the authoritative flag.
``is_tool_error_result(result)`` checks the sentinel first and falls
back to the legacy ``error`` key for one release (deprecated, scheduled
for removal in v2.0). The dispatcher stamps every synthetic error
return with ``_tool_error=True`` so the new convention propagates by
default.

Tests cover:
  * ``is_tool_error_result`` truth table (sentinel-True, sentinel-False,
    legacy fallback, non-dict input).
  * Dispatcher synthetic errors carry the sentinel.
  * The agent loop (``tdpilot_api_agent``) imports + uses the helper.
  * ``tool_batch`` imports + uses the helper.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_dispatcher as disp  # noqa: E402

# ---------------------------------------------------------------------------
# is_tool_error_result truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result,expected",
    [
        # New convention — sentinel authoritative.
        ({"_tool_error": True}, True),
        ({"_tool_error": True, "error": "boom"}, True),
        ({"_tool_error": False}, False),
        # Sentinel says success EVEN IF an `error` field is present —
        # this is the whole point of the migration: handlers that
        # legitimately return an `error` field on success.
        ({"_tool_error": False, "error": "compile error in /project1"}, False),
        ({"_tool_error": False, "errors": ["a", "b"]}, False),
        # Truthy sentinel values.
        ({"_tool_error": 1}, True),
        ({"_tool_error": "yes"}, True),
        # Falsy sentinel values.
        ({"_tool_error": 0}, False),
        ({"_tool_error": ""}, False),
        ({"_tool_error": None}, False),
        # Legacy fallback — no sentinel, `error` present.
        ({"error": "Unknown tool"}, True),
        ({"error": ""}, True),  # empty string still triggers (key-presence semantics)
        # Legacy fallback — no sentinel, no error key.
        ({"ok": True, "path": "/project1"}, False),
        ({}, False),
        # Non-dict inputs.
        (None, False),
        ("error", False),
        (["error"], False),
        (42, False),
    ],
)
def test_is_tool_error_result_truth_table(result, expected):
    assert disp.is_tool_error_result(result) is expected


def test_tool_error_key_constant_is_dunder_underscore():
    """Single source of truth — the key name lives in
    ``TOOL_ERROR_KEY`` so any future rename only happens in one place."""
    assert disp.TOOL_ERROR_KEY == "_tool_error"


# ---------------------------------------------------------------------------
# Dispatcher synthetic errors carry the sentinel
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatcher_with_no_handlers():
    """Build a dispatcher with empty handler module so every routed
    call falls into the synthetic-error paths."""

    class _EmptyHandlersModule:
        pass

    return disp.make_dispatcher(handlers_modules=[_EmptyHandlersModule()])


def test_dispatcher_unknown_tool_carries_sentinel(dispatcher_with_no_handlers):
    """Unknown-tool path returns ``{"error": ..., "_tool_error": True}``."""
    out = dispatcher_with_no_handlers("nonexistent_tool", {})
    assert isinstance(out, dict)
    assert out.get("_tool_error") is True
    # Legacy `error` key remains for the model — it's the human-readable side.
    assert "error" in out
    assert disp.is_tool_error_result(out) is True


def test_dispatcher_handler_not_found_carries_sentinel():
    """When the schema maps to a handler name that doesn't exist on
    any handlers module, the dispatcher synthesises an error AND
    stamps the sentinel."""

    class _StubHandlers:
        pass

    # Inject a fake mapping pointing at a missing handler name.
    extras = {"phantom_tool": ("handle_phantom_function", lambda body: body)}
    dispatcher = disp.make_dispatcher(handlers_modules=[_StubHandlers()], extra_mappings=extras)
    out = dispatcher("phantom_tool", {})
    assert isinstance(out, dict)
    assert out.get("_tool_error") is True
    assert disp.is_tool_error_result(out) is True


def test_dispatcher_handler_exception_carries_sentinel():
    """Handler raises → synthetic error with `_tool_error=True` AND
    redacted traceback."""

    class _RaisingHandlers:
        def handle_explode(self, body):
            raise RuntimeError("kaboom in /Users/secret/path")

    dispatcher = disp.make_dispatcher(
        handlers_modules=[_RaisingHandlers()],
        extra_mappings={"explode": ("handle_explode", lambda body: body)},
    )
    out = dispatcher("explode", {})
    assert isinstance(out, dict)
    assert out.get("_tool_error") is True
    # `error` and `traceback` keys both populated.
    assert "RuntimeError" in out.get("error", "")
    assert "traceback" in out


def test_dispatcher_successful_handler_does_not_stamp_sentinel():
    """Sentinel must NOT appear on successful handler returns.
    Otherwise every call would look like an error to is_tool_error_result."""

    class _OkHandlers:
        def handle_ok(self, body):
            return {"path": "/project1/foo", "ok": True}

    dispatcher = disp.make_dispatcher(
        handlers_modules=[_OkHandlers()],
        extra_mappings={"ok_tool": ("handle_ok", lambda body: body)},
    )
    out = dispatcher("ok_tool", {})
    assert isinstance(out, dict)
    assert "_tool_error" not in out
    assert disp.is_tool_error_result(out) is False


def test_dispatcher_successful_handler_returning_error_field_not_misclassified():
    """The whole motivation for the sentinel: a successful handler
    whose result includes an "error" field (e.g. td_get_errors
    returning a list of project errors). Without the sentinel the
    legacy heuristic misclassifies; WITH the sentinel applied
    explicitly by the handler, the agent loop sees success."""

    class _GetErrorsHandler:
        def handle_get_errors(self, body):
            # Successful inspection: returns errors as DATA, not as
            # a tool failure. Handler explicitly opts into the
            # sentinel-False signal.
            return {
                "errors": [{"node": "/project1/n1", "msg": "bad type"}],
                "_tool_error": False,
                "error": "1 error found",  # human-readable summary, NOT a tool failure
            }

    dispatcher = disp.make_dispatcher(
        handlers_modules=[_GetErrorsHandler()],
        extra_mappings={"td_get_errors": ("handle_get_errors", lambda body: body)},
    )
    out = dispatcher("td_get_errors", {})
    assert disp.is_tool_error_result(out) is False, (
        "handler explicitly opted into _tool_error=False; agent loop "
        "must respect that and NOT classify the call as a failure"
    )


# ---------------------------------------------------------------------------
# agent.py + batch.py wire through the helper
# ---------------------------------------------------------------------------


def test_agent_module_imports_helper():
    """The agent loop must call ``is_tool_error_result`` rather than
    re-implement the heuristic. Source-level pin guards against the
    helper being shadowed by a local re-definition that drifts."""
    src = (REPO_ROOT / "td_component" / "tdpilot_api_agent.py").read_text()
    assert "from tdpilot_api_dispatcher import is_tool_error_result" in src
    assert "is_tool_error_result(result)" in src
    # And the brittle inline heuristic is gone.
    # (Strip docstrings/comments first — the changelog/migration notes
    # may legitimately mention the old form.)
    import re as _re

    code = _re.sub(r'"""[\s\S]*?"""', "", src)
    code = _re.sub(r"#[^\n]*", "", code)
    assert 'isinstance(result, dict) and "error" in result' not in code


def test_batch_module_imports_helper():
    src = (REPO_ROOT / "td_component" / "tdpilot_api_batch.py").read_text()
    assert "from tdpilot_api_dispatcher import is_tool_error_result" in src
    assert "is_tool_error_result(result)" in src

    import re as _re

    code = _re.sub(r'"""[\s\S]*?"""', "", src)
    code = _re.sub(r"#[^\n]*", "", code)
    assert 'isinstance(result, dict) and "error" in result' not in code


def test_agent_exception_path_marks_tool_error_explicitly():
    """When ``self.dispatcher(...)`` raises, the synthesised result
    dict must carry ``_tool_error=True`` so subsequent inspection
    uses the new sentinel rather than relying on the legacy `error`
    fallback."""
    src = (REPO_ROOT / "td_component" / "tdpilot_api_agent.py").read_text()
    # The exception branch synthesizes the error result.
    assert '"_tool_error": True' in src
