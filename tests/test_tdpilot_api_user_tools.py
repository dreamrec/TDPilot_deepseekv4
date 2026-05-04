"""Unit tests for the user-pluggable tool loader (tdpilot_api_user_tools).

This is the security trust boundary for user-supplied code: schema
validation bugs would let malformed tools register silently. Tests use
``tmp_path`` to write fake user-tool .py files and ``monkeypatch`` to
redirect ``USER_TOOLS_DIR`` so the tests never touch the user's real
``~/.tdpilot-api/tools/``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tdpilot_api_user_tools as ut  # noqa: E402

# ----------------------------------------------------------------------
# _validate_schema — pure dict validation
# ----------------------------------------------------------------------


def _good_schema(name: str = "my_tool") -> dict:
    return {
        "name": name,
        "description": "A test tool.",
        "input_schema": {
            "type": "object",
            "properties": {"x": {"type": "integer"}},
        },
    }


def test_validate_schema_accepts_well_formed_dict():
    assert ut._validate_schema(_good_schema()) is None


def test_validate_schema_rejects_non_dict():
    err = ut._validate_schema("not a dict")
    assert err and "must be a dict" in err


def test_validate_schema_rejects_missing_name():
    s = _good_schema()
    del s["name"]
    err = ut._validate_schema(s)
    assert err and "name" in err


def test_validate_schema_rejects_bad_name_regex():
    err = ut._validate_schema(_good_schema(name="has spaces"))
    assert err and "name" in err


def test_validate_schema_rejects_input_schema_missing_type_object():
    s = _good_schema()
    s["input_schema"]["type"] = "array"
    err = ut._validate_schema(s)
    assert err and "type" in err


def test_validate_schema_rejects_non_string_description():
    s = _good_schema()
    s["description"] = 123
    err = ut._validate_schema(s)
    assert err and "description" in err


# ----------------------------------------------------------------------
# load_user_tools — directory scan + registration
# ----------------------------------------------------------------------


def _write_user_tool(
    path: Path, name: str = "demo_tool", desc: str = "demo desc", handle_body: str = "return {'echo': args}"
) -> Path:
    path.write_text(
        f"SCHEMA = {{\n"
        f"    'name': '{name}',\n"
        f"    'description': '{desc}',\n"
        f"    'input_schema': {{'type': 'object', 'properties': {{}}}},\n"
        f"}}\n"
        f"def handle(args):\n"
        f"    {handle_body}\n",
        encoding="utf-8",
    )
    return path


def test_load_user_tools_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)
    schemas: list = []
    modules: list = []
    extras: dict = {}
    out = ut.load_user_tools(schemas, modules, extras)
    assert out == []
    assert schemas == []


def test_load_user_tools_nonexistent_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path / "does_not_exist")
    out = ut.load_user_tools([], [], {})
    assert out == []


def test_load_user_tools_valid_file_registers_tool(tmp_path, monkeypatch):
    _write_user_tool(tmp_path / "demo.py", name="demo_tool")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    schemas: list = []
    modules: list = []
    extras: dict = {}
    out = ut.load_user_tools(schemas, modules, extras)

    assert len(schemas) == 1
    assert schemas[0]["name"] == "demo_tool"
    assert "demo_tool" in extras
    handler_fn_name, _ = extras["demo_tool"]
    assert handler_fn_name == "handle_demo_tool"
    assert hasattr(modules[-1], "handle_demo_tool")
    assert any(entry["ok"] for entry in out)


def test_load_user_tools_skips_underscore_prefixed_files(tmp_path, monkeypatch):
    _write_user_tool(tmp_path / "_helper.py", name="should_not_register")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    schemas: list = []
    modules: list = []
    extras: dict = {}
    ut.load_user_tools(schemas, modules, extras)

    assert schemas == []
    assert "should_not_register" not in extras


def test_load_user_tools_user_overrides_builtin(tmp_path, monkeypatch):
    """When a user tool's name shadows an entry in the schema list, the
    builtin is removed and the user tool wins (Sprint 4.2 contract)."""
    _write_user_tool(tmp_path / "shadow.py", name="td_get_info")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    builtin_schemas = [
        {"name": "td_get_info", "description": "builtin", "input_schema": {"type": "object"}},
    ]
    schemas = list(builtin_schemas)
    extras: dict = {}
    ut.load_user_tools(schemas, [], extras)

    # Only one schema entry remains, and it's the user's.
    assert len([s for s in schemas if s["name"] == "td_get_info"]) == 1
    assert "td_get_info" in extras


def test_load_user_tools_missing_schema_logged_and_skipped(tmp_path, monkeypatch):
    """Files without a SCHEMA dict are recorded in the registry as failed
    but never appended to the live schemas list."""
    (tmp_path / "broken.py").write_text("def handle(a): return a\n", encoding="utf-8")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    schemas: list = []
    out = ut.load_user_tools(schemas, [], {})

    assert schemas == []
    assert any(not e["ok"] and "SCHEMA" in (e.get("error") or "") for e in out)


def test_load_user_tools_missing_handle_logged_and_skipped(tmp_path, monkeypatch):
    """File has SCHEMA but no callable handle()."""
    (tmp_path / "noimpl.py").write_text(
        "SCHEMA = {'name': 'foo', 'description': '', 'input_schema': {'type': 'object'}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    schemas: list = []
    out = ut.load_user_tools(schemas, [], {})

    assert schemas == []
    assert any(not e["ok"] and "handle" in (e.get("error") or "") for e in out)


# ----------------------------------------------------------------------
# handle_tool_validate — dry-validation entry point
# ----------------------------------------------------------------------


def test_handle_tool_validate_valid_file(tmp_path, monkeypatch):
    p = _write_user_tool(tmp_path / "ok.py", name="dry_test")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    out = ut.handle_tool_validate({"path": str(p)})
    assert out["ok"] is True
    assert out["schema"]["name"] == "dry_test"
    assert out["has_handle"] is True


def test_handle_tool_validate_relative_path_resolves_under_user_dir(tmp_path, monkeypatch):
    _write_user_tool(tmp_path / "relpath.py", name="rel_test")
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)

    out = ut.handle_tool_validate({"path": "relpath.py"})
    assert out["ok"] is True
    assert out["schema"]["name"] == "rel_test"


def test_handle_tool_validate_missing_file_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ut, "USER_TOOLS_DIR", tmp_path)
    out = ut.handle_tool_validate({"path": str(tmp_path / "nope.py")})
    assert "error" in out
    assert "not found" in out["error"].lower()


def test_handle_tool_validate_missing_path_field_returns_error():
    out = ut.handle_tool_validate({})
    assert "error" in out
    assert "path" in out["error"].lower()
