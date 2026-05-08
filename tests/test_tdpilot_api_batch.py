"""Tests for ``tdpilot_api_batch.handle_tool_batch`` (Phase 2.1).

The handler resolves its dispatcher by walking
COMP → extension → runtime via ``parent()`` — that's TD-specific.
For unit tests we monkeypatch ``_resolve_raw_dispatcher`` to return
a controllable callable, which lets us exercise every branch of the
batch logic without spinning up TD.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_batch as tb  # noqa: E402


def _stub_dispatcher(table=None, *, exc=None):
    """Build a fake dispatcher.

    - ``table`` keyed by tool name: returned dict for that call.
      Default returns ``{"ok": True}``.
    - ``exc``: raise this exception instead. Used for the "handler
      raised" branch.
    """
    table = table or {}

    def dispatch(name, args):
        if exc is not None:
            raise exc
        return dict(table.get(name, {"ok": True}))

    return dispatch


def _patch_dispatcher(monkeypatch, dispatcher):
    monkeypatch.setattr(tb, "_resolve_raw_dispatcher", lambda: dispatcher)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_batch_dispatches_all_calls(monkeypatch):
    seen: list[tuple[str, dict]] = []

    def dispatch(name, args):
        seen.append((name, args))
        return {"echo": name}

    _patch_dispatcher(monkeypatch, dispatch)
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_get_info", "args": {}},
                {"tool": "td_get_errors", "args": {"path": "/project1"}},
                {"tool": "td_get_capabilities", "args": {}},
            ]
        }
    )

    assert out["ok"] is True
    assert out["count"] == 3
    assert [r["tool"] for r in out["results"]] == ["td_get_info", "td_get_errors", "td_get_capabilities"]
    assert all(r["ok"] for r in out["results"])
    assert seen == [
        ("td_get_info", {}),
        ("td_get_errors", {"path": "/project1"}),
        ("td_get_capabilities", {}),
    ]


def test_batch_records_elapsed_time(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch({"calls": [{"tool": "td_get_info"}]})
    assert "elapsed_ms" in out["results"][0]
    assert out["results"][0]["elapsed_ms"] >= 0


# ---------------------------------------------------------------------------
# Failure modes — each row reports its own success/error; batch keeps going
# ---------------------------------------------------------------------------


def test_batch_per_call_error_does_not_abort_batch(monkeypatch):
    """A sub-call returning `_tool_error: True` flags ok=False and the
    rest still run. Mirrors recipe_replay's "soft" failure mode."""

    def dispatch(name, args):
        if name == "broken":
            # v1.10.0+: emit the sentinel; legacy `{"error": ...}`
            # alone is deprecated and removed in v2.0.
            return {"_tool_error": True, "error": "something broke"}
        return {"ok": True, "tool": name}

    _patch_dispatcher(monkeypatch, dispatch)
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_get_info"},
                {"tool": "broken"},
                {"tool": "td_get_capabilities"},
            ]
        }
    )

    assert out["ok"] is True  # batch envelope succeeded
    assert [r["ok"] for r in out["results"]] == [True, False, True]
    assert out["results"][1]["error"] == "something broke"
    assert out["results"][1]["result"] is None
    assert out["results"][0]["result"] == {"ok": True, "tool": "td_get_info"}


def test_batch_handler_raising_exception_becomes_error_row(monkeypatch):
    """If the dispatcher raises (rather than returning an error dict),
    the row captures it as a string error and the batch continues."""
    counter = {"n": 0}

    def dispatch(name, args):
        counter["n"] += 1
        if counter["n"] == 2:
            raise RuntimeError("simulated TD crash")
        return {"ok": True}

    _patch_dispatcher(monkeypatch, dispatch)
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_get_info"},
                {"tool": "td_get_errors"},
                {"tool": "td_get_capabilities"},
            ]
        }
    )

    assert out["ok"] is True
    assert [r["ok"] for r in out["results"]] == [True, False, True]
    assert "RuntimeError" in (out["results"][1]["error"] or "")
    assert "simulated TD crash" in (out["results"][1]["error"] or "")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_batch_rejects_empty_call_list(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch({"calls": []})
    assert "error" in out
    assert "non-empty" in out["error"]


def test_batch_rejects_missing_calls_key(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch({})
    assert "error" in out


def test_batch_rejects_oversize_batch(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch({"calls": [{"tool": "td_get_info"} for _ in range(tb.MAX_BATCH_SIZE + 1)]})
    assert "error" in out
    assert str(tb.MAX_BATCH_SIZE) in out["error"]


def test_batch_rejects_nested_tool_batch_per_call(monkeypatch):
    """A sub-call to ``tool_batch`` must be rejected — recursion guard."""
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_get_info"},
                {"tool": "tool_batch", "args": {"calls": [{"tool": "td_get_info"}]}},
            ]
        }
    )
    # Batch envelope still succeeds — we just flag the offender.
    assert out["ok"] is True
    assert out["results"][0]["ok"] is True
    assert out["results"][1]["ok"] is False
    assert "Nested" in out["results"][1]["error"]


def test_batch_rejects_call_with_missing_tool_name(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch(
        {"calls": [{"args": {"path": "/p"}}]}  # no "tool"
    )
    assert out["results"][0]["ok"] is False
    assert "missing" in (out["results"][0]["error"] or "").lower()


def test_batch_rejects_non_dict_call_entry(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch({"calls": ["not-a-dict", {"tool": "td_get_info"}]})
    assert out["results"][0]["ok"] is False
    assert "not an object" in (out["results"][0]["error"] or "")
    # The valid second entry still ran.
    assert out["results"][1]["ok"] is True


def test_batch_rejects_non_dict_args(monkeypatch):
    _patch_dispatcher(monkeypatch, _stub_dispatcher())
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_get_info", "args": "should be dict"},
                {"tool": "td_get_info"},
            ]
        }
    )
    assert out["results"][0]["ok"] is False
    assert "must be an object" in (out["results"][0]["error"] or "")
    assert out["results"][1]["ok"] is True


def test_batch_no_dispatcher_means_clean_error(monkeypatch):
    """Outside TD ``_resolve_raw_dispatcher`` returns None — surface
    a clear error, don't crash."""
    monkeypatch.setattr(tb, "_resolve_raw_dispatcher", lambda: None)
    out = tb.handle_tool_batch({"calls": [{"tool": "td_get_info"}]})
    assert "error" in out
    assert "dispatcher" in out["error"].lower()


# ---------------------------------------------------------------------------
# Schema parity — every entry in TOOL_SCHEMAS must have a TOOL_TO_HANDLER
# entry. tool_batch added in Phase 2.1; this guards future regressions.
# ---------------------------------------------------------------------------


def test_tool_batch_present_in_schemas_and_handlers():
    from tdpilot_api_schema_defs import TOOL_SCHEMAS  # type: ignore[import-not-found]
    from tdpilot_api_schema_map import TOOL_TO_HANDLER  # type: ignore[import-not-found]

    schema_names = {s["name"] for s in TOOL_SCHEMAS}
    assert "tool_batch" in schema_names
    assert "tool_batch" in TOOL_TO_HANDLER
    handler_fn_name, _adapter = TOOL_TO_HANDLER["tool_batch"]
    assert handler_fn_name == "handle_tool_batch"
    # And the parity invariant the rest of the project relies on.
    assert schema_names == set(TOOL_TO_HANDLER.keys())


# ---------------------------------------------------------------------------
# F-10 — public dispatcher accessor (PR-14)
# ---------------------------------------------------------------------------
#
# Pre-PR-14 the resolver reached in via
# ``ext._runtime._raw_dispatcher`` — two private attrs in a row.
# Renaming either field broke this caller silently. PR-14 introduces
# ``ext.runtime.raw_dispatcher`` as the supported access path.


def _strip_python_comments_and_docstrings(src: str) -> str:
    """Drop ``#`` line comments and triple-quoted blocks. The static
    F-10 checks must not false-positive on docstrings or comments
    that legitimately mention the old private-attr access pattern."""
    import re

    src = re.sub(r'"""[\s\S]*?"""', "", src)
    src = re.sub(r"'''[\s\S]*?'''", "", src)
    src = re.sub(r"#[^\n]*", "", src)
    return src


def test_resolve_raw_dispatcher_uses_public_accessors():
    """Source-level pin: the resolver in tdpilot_api_batch must NOT
    reach in via private attrs. Either direct access via the public
    ``ext.runtime.raw_dispatcher`` chain (PR-14) OR delegation to
    ``tdpilot_api_lookup.get_raw_dispatcher`` (PR-19) is fine — both
    bypass the legacy private form."""
    src = (REPO_ROOT / "td_component" / "tdpilot_api_batch.py").read_text()
    code = _strip_python_comments_and_docstrings(src)
    public_chain = "ext.runtime.raw_dispatcher" in code
    via_helper = "get_raw_dispatcher" in code
    assert public_chain or via_helper, (
        "tool_batch resolver should use the public accessor chain "
        "or delegate to tdpilot_api_lookup.get_raw_dispatcher"
    )
    assert "ext._runtime._raw_dispatcher" not in code, (
        "tool_batch resolver still hits the private dispatcher attr"
    )


def test_runtime_exposes_raw_dispatcher_property():
    """The AgentRuntime should expose ``raw_dispatcher`` as a property
    so callers in tool_batch / recipes / patches / macros can avoid
    private-attribute access. Source-level check; the property is
    instantiated against TD's parent() so we can't construct it
    standalone."""
    src = (REPO_ROOT / "td_component" / "tdpilot_api_runtime.py").read_text()
    assert "@property\n    def raw_dispatcher" in src, "AgentRuntime.raw_dispatcher property missing"
    # The body returns the underlying _raw_dispatcher field.
    assert "return self._raw_dispatcher" in src


def test_extension_exposes_runtime_property():
    """Same hardening for ``ext.runtime`` — handlers can no longer
    rely on the leading-underscore private form."""
    src = (REPO_ROOT / "td_component" / "tdpilot_api_extension.py").read_text()
    assert "@property\n    def runtime" in src, "TDPilotAPIExt.runtime property missing"
    assert "return self._runtime" in src


def _accessor_or_helper(code: str) -> bool:
    """Return True if the code reaches the dispatcher via either the
    public accessor chain (PR-14) or the lookup helper (PR-19) — both
    are acceptable. Private-attribute access is not."""
    return "ext.runtime.raw_dispatcher" in code or "get_raw_dispatcher" in code


def test_recipes_uses_public_accessor():
    code = _strip_python_comments_and_docstrings(
        (REPO_ROOT / "td_component" / "tdpilot_api_recipes.py").read_text()
    )
    assert _accessor_or_helper(code)
    assert "ext._runtime._raw_dispatcher" not in code


def test_patches_uses_public_accessor():
    code = _strip_python_comments_and_docstrings(
        (REPO_ROOT / "td_component" / "tdpilot_api_patches.py").read_text()
    )
    assert _accessor_or_helper(code)
    assert "ext._runtime._raw_dispatcher" not in code


def test_macros_uses_public_accessor():
    code = _strip_python_comments_and_docstrings(
        (REPO_ROOT / "td_component" / "tdpilot_api_macros.py").read_text()
    )
    assert _accessor_or_helper(code)
    assert "ext._runtime._raw_dispatcher" not in code


def test_no_more_private_runtime_dispatcher_access_anywhere():
    """Strict gate against future regressions — no `_runtime._raw_dispatcher`
    anywhere in td_component/. The runtime module itself uses
    ``self._raw_dispatcher`` internally, which is fine; this test
    looks for the EXTERNAL access pattern. Comments and docstrings
    that mention the old pattern are stripped first so historical
    notes don't trigger the gate."""
    import re

    td_dir = REPO_ROOT / "td_component"
    pat = re.compile(r"\b\w+\._runtime\._raw_dispatcher\b")
    for py in td_dir.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        code = _strip_python_comments_and_docstrings(text)
        match = pat.search(code)
        assert match is None, (
            f"{py.name} still reaches in via private _runtime._raw_dispatcher: {match.group(0)}"
        )


# ---------------------------------------------------------------------------
# F-10 — additional batch coverage to cement Phase 1.6.13 fix paths
# ---------------------------------------------------------------------------


def test_batch_rejects_when_runtime_missing(monkeypatch):
    """When the resolver returns None (no COMP / no extension /
    runtime not built), the handler returns a clean error string
    rather than raising. Pre-PR-14 this branch was uncovered."""
    monkeypatch.setattr(tb, "_resolve_raw_dispatcher", lambda: None)
    out = tb.handle_tool_batch({"calls": [{"tool": "td_get_info", "args": {}}]})
    assert isinstance(out, dict)
    assert "error" in out
    assert "dispatcher" in out["error"].lower()


def test_batch_severity_flattening_visible_per_call(monkeypatch):
    """The Phase 1.6.13 severity-flattening fix lives in the runtime,
    but tool_batch's per-call result entries are what the runtime
    inspects to flatten. Pin the result shape so a future refactor
    of handle_tool_batch can't accidentally drop the per-call
    `tool` / `ok` / `result` / `error` keys the runtime relies on."""
    dispatcher = _stub_dispatcher(
        {
            "td_create_node": {"path": "/project1/n1"},
            "td_get_errors": {"errors": []},
        }
    )
    _patch_dispatcher(monkeypatch, dispatcher)
    out = tb.handle_tool_batch(
        {
            "calls": [
                {"tool": "td_create_node", "args": {"type": "noiseTOP"}},
                {"tool": "td_get_errors", "args": {}},
            ]
        }
    )
    assert out["ok"] is True
    assert out["count"] == 2
    for entry in out["results"]:
        assert "tool" in entry
        assert "ok" in entry
        assert "result" in entry
        assert "error" in entry
        assert "elapsed_ms" in entry
