"""Unit tests for tdpilot_api_introspect (server introspection tools)."""

from __future__ import annotations

import tdpilot_api_introspect as ins


def test_get_server_metrics_returns_uptime_and_pid():
    out = ins.handle_get_server_metrics({})
    assert out["ok"] is True
    assert "uptime_seconds" in out
    assert "pid" in out
    assert isinstance(out["uptime_seconds"], (int, float))
    assert out["uptime_seconds"] >= 0


def test_describe_surface_lists_categories_and_counts():
    out = ins.handle_describe_surface({})
    assert out["ok"] is True
    assert out["builtin_count"] > 0
    assert out["total_count"] >= out["builtin_count"]
    assert isinstance(out["categories"], dict)
    # Should categorize td_get_info under 'td_get'
    all_listed = [n for names in out["categories"].values() for n in names]
    assert "td_get_info" in all_listed
    # Schema/handler tables should be in sync
    assert out["handler_table_consistent"] is True


def test_describe_surface_builtin_count_matches_schemas():
    """The builtin_count should match TOOL_SCHEMAS length exactly."""
    from tdpilot_api_schema_defs import TOOL_SCHEMAS

    out = ins.handle_describe_surface({})
    assert out["builtin_count"] == len(TOOL_SCHEMAS)


def test_get_capabilities_reports_features():
    out = ins.handle_get_capabilities({})
    assert out["ok"] is True
    assert out["variant"] == "standalone"
    assert "features" in out
    # Features that should always be present
    for f in ("memory", "knowledge", "recipes", "skills", "patches", "bm25"):
        assert out["features"][f] is True
    # New Tier 1+2 modules
    assert out["features"]["official_docs"] is True
    assert out["features"]["td2025_native"] is True
    assert out["features"]["introspect"] is True


def test_get_capabilities_includes_exec_mode():
    out = ins.handle_get_capabilities({})
    assert "exec_mode" in out


def test_get_capabilities_outside_td_skips_runtime_state():
    """Without parent() / TD COMP, runtime-level fields just aren't added,
    rather than erroring out."""
    out = ins.handle_get_capabilities({})
    # Should still return ok=True even outside TD
    assert out["ok"] is True
