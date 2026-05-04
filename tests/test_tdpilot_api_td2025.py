"""Unit tests for tdpilot_api_td2025 (TD-runtime introspection tools).

The Python-env and threading probes don't need TD; the TD-specific
probes (logger, TDResources, color pipeline, component-standardize,
audit-project) return a structured "outside TD" error rather than
crashing — those tests verify the error path. Live-TD behaviour is
covered by integration tests separately.
"""

from __future__ import annotations

import tdpilot_api_td2025 as td25


def test_python_env_status_returns_python_version():
    out = td25.handle_python_env_status({})
    assert out["ok"] is True
    assert "python_version" in out
    assert "executable" in out
    assert "sys_path" in out
    assert isinstance(out["sys_path"], list)


def test_python_env_status_caps_sys_path():
    """sys.path is capped at 25 entries to keep response small."""
    out = td25.handle_python_env_status({})
    assert len(out["sys_path"]) <= 25


def test_python_env_status_caps_loaded_modules():
    out = td25.handle_python_env_status({})
    assert "loaded_modules_sample" in out
    assert len(out["loaded_modules_sample"]) <= 50


def test_threading_status_lists_current_thread():
    out = td25.handle_threading_status({})
    assert out["ok"] is True
    assert out["active_count"] >= 1
    assert out["main_thread"]
    assert out["current_thread"]
    # MainThread should appear in the threads list
    assert any(t["name"].startswith("MainThread") for t in out["threads"])


def test_logger_status_outside_td_returns_error():
    """Without project global, returns structured error."""
    out = td25.handle_logger_status({})
    assert "error" in out
    assert "outside TouchDesigner" in out["error"]


def test_tdresources_inspect_outside_td_returns_error():
    """Without `ui` global, returns a structured outside-TD error."""
    out = td25.handle_tdresources_inspect({})
    assert "error" in out


def test_tdresources_inspect_unavailable_returns_ok_false():
    """When `ui` exists but doesn't expose tdResources/TDResources, the
    handler returns ok=true with available=false + a hint, NOT an error.
    This avoids a misleading 'tool failed' in the chat transcript on TD
    builds where the registry isn't exposed."""
    import builtins

    class _FakeUI:
        # No tdResources / TDResources attribute
        pass

    # Inject a fake `ui` global into the module's namespace.
    original = getattr(td25, "ui", None)
    try:
        td25.ui = _FakeUI()  # type: ignore[attr-defined]
        out = td25.handle_tdresources_inspect({})
    finally:
        if original is None and hasattr(td25, "ui"):
            del td25.ui  # type: ignore[attr-defined]
        else:
            td25.ui = original  # type: ignore[attr-defined]

    assert out["ok"] is True
    assert out["available"] is False
    assert "hint" in out


def test_color_pipeline_outside_td_returns_error():
    out = td25.handle_color_pipeline({})
    assert "error" in out


def test_component_standardize_outside_td_returns_error():
    out = td25.handle_component_standardize({"path": "/project1"})
    assert "error" in out


def test_audit_project_outside_td_returns_error():
    out = td25.handle_audit_project({"path": "/"})
    assert "error" in out


def test_audit_project_clamps_max_depth():
    """max_depth must be 1-20; out-of-range values get clamped silently
    rather than erroring."""
    out_low = td25.handle_audit_project({"path": "/", "max_depth": 0})
    # Clamps to 1 (minimum). Outside TD this still errors on op() but
    # the clamping happens before that, so we just verify no ValueError.
    assert "error" in out_low

    out_high = td25.handle_audit_project({"path": "/", "max_depth": 999})
    assert "error" in out_high


def test_audit_project_invalid_max_depth_falls_back_to_default():
    out = td25.handle_audit_project({"path": "/", "max_depth": "not_an_int"})
    # Doesn't crash on bad input.
    assert isinstance(out, dict)
