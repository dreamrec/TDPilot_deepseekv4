"""Unit tests for the pure parts of tdpilot_api_memory.

Two functions deserve targeted coverage:

  * ``_safe_filename`` — the prefix-dedup logic the docstring calls out
    as a known production bug fix. Without coverage, a regression here
    silently doubles the type prefix again.

  * ``_find_memory_path`` — the 4-candidate fuzzy lookup every read
    operation routes through. A miss returns None and the agent sees
    "memory not found" with no diagnostic, so the candidate ordering
    matters.
"""

from __future__ import annotations

from pathlib import Path

import tdpilot_api_memory as mem  # noqa: E402

# ----------------------------------------------------------------------
# _safe_filename — prefix dedup
# ----------------------------------------------------------------------


def test_safe_filename_adds_type_prefix():
    assert mem._safe_filename("my_thing", "project") == "project_my_thing.md"


def test_safe_filename_does_not_double_prefix():
    """Agent often passes name='project_X' AND type='project'. Without
    dedup we'd build 'project_project_X.md' — the bug this function was
    written to prevent. Regression test for the production fix."""
    assert mem._safe_filename("project_my_thing", "project") == "project_my_thing.md"


def test_safe_filename_falls_back_to_note_for_unknown_type():
    """An unknown type ('something') falls back to the 'note' bucket."""
    assert mem._safe_filename("idea", "something") == "note_idea.md"


def test_safe_filename_slugifies_special_chars():
    """Punctuation is stripped; spaces and dashes become underscores."""
    out = mem._safe_filename("My Cool Idea!", "user")
    assert out == "user_my_cool_idea.md"


# ----------------------------------------------------------------------
# _find_memory_path — fuzzy lookup
# ----------------------------------------------------------------------


def test_find_memory_path_exact_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    target = tmp_path / "user_role.md"
    target.write_text("---\nname: role\n---\nbody", encoding="utf-8")

    found = mem._find_memory_path("user_role.md")
    assert found == target


def test_find_memory_path_bare_name_appends_md(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    target = tmp_path / "user_role.md"
    target.write_text("body", encoding="utf-8")

    # Caller passes 'user_role' without .md — should resolve to the file.
    assert mem._find_memory_path("user_role") == target


def test_find_memory_path_type_prefix_search(tmp_path, monkeypatch):
    """Caller passes a bare slug ('role') — function should locate the
    type-prefixed file 'user_role.md' by trying each valid type."""
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    target = tmp_path / "feedback_role.md"
    target.write_text("body", encoding="utf-8")

    assert mem._find_memory_path("role") == target


def test_find_memory_path_strip_existing_prefix(tmp_path, monkeypatch):
    """Caller passes 'project_X' but the file on disk is named 'X.md'
    (no prefix). The strip-prefix candidate should hit it."""
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    target = tmp_path / "X.md"
    target.write_text("body", encoding="utf-8")

    assert mem._find_memory_path("project_X") == target


def test_find_memory_path_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    assert mem._find_memory_path("ghost") is None
    assert mem._find_memory_path("") is None
    assert mem._find_memory_path(None) is None  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Tier 1 additions: export / import / favorite
# ----------------------------------------------------------------------


def _seed(tmp_path, filename, name, body, mtype="user", description=""):
    text = f"---\nname: {name}\ndescription: {description}\ntype: {mtype}\n---\n\n{body}\n"
    (tmp_path / filename).write_text(text, encoding="utf-8")


def test_memory_export_dumps_all_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    _seed(tmp_path, "user_role.md", "role", "I'm a TD artist.", mtype="user")
    _seed(tmp_path, "feedback_no_lambdas.md", "no lambdas", "Use def.", mtype="feedback")

    out = mem.handle_memory_export({})
    assert out["ok"] is True
    assert out["count"] == 2
    assert "user_role.md" in out["memories"]
    assert out["memories"]["user_role.md"]["meta"]["name"] == "role"
    assert out["memories"]["user_role.md"]["body"].startswith("I'm a TD artist.")


def test_memory_import_skips_existing_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    _seed(tmp_path, "user_role.md", "old", "old body", mtype="user")

    payload = {
        "memories": {
            "user_role.md": {
                "meta": {"name": "new", "type": "user", "description": ""},
                "body": "new body",
            },
            "user_other.md": {
                "meta": {"name": "other", "type": "user", "description": ""},
                "body": "other body",
            },
        }
    }
    out = mem.handle_memory_import(payload)
    assert out["written_count"] == 1
    assert out["skipped_count"] == 1
    assert "user_role.md" in out["skipped"]
    assert "user_other.md" in out["written"]
    # Original file untouched
    assert "old body" in (tmp_path / "user_role.md").read_text(encoding="utf-8")


def test_memory_import_overwrite_replaces_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(mem, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    _seed(tmp_path, "user_role.md", "old", "old body", mtype="user")

    payload = {
        "memories": {
            "user_role.md": {
                "meta": {"name": "new", "type": "user", "description": ""},
                "body": "new body",
            },
        },
        "overwrite": True,
    }
    out = mem.handle_memory_import(payload)
    assert out["written_count"] == 1
    assert "new body" in (tmp_path / "user_role.md").read_text(encoding="utf-8")


def test_memory_import_bad_payload_returns_error():
    out = mem.handle_memory_import({})
    assert "error" in out


def test_memory_favorite_sets_flag_and_rating(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    _seed(tmp_path, "user_role.md", "role", "body", mtype="user")

    out = mem.handle_memory_favorite({"name": "role", "favorite": True, "rating": 5})
    assert out["ok"] is True
    assert out["favorite"] == "true"
    assert out["rating"] == "5"

    text = (tmp_path / "user_role.md").read_text(encoding="utf-8")
    assert "favorite: true" in text
    assert "rating: 5" in text


def test_memory_favorite_requires_at_least_one_field(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    _seed(tmp_path, "user_role.md", "role", "body", mtype="user")

    out = mem.handle_memory_favorite({"name": "role"})
    assert "error" in out


def test_memory_favorite_rejects_out_of_range_rating(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    _seed(tmp_path, "user_role.md", "role", "body", mtype="user")

    out = mem.handle_memory_favorite({"name": "role", "rating": 99})
    assert "error" in out
    assert "out of range" in out["error"].lower() or "0-5" in out["error"]


def test_memory_favorite_missing_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(mem, "MEMORY_DIR", tmp_path)
    out = mem.handle_memory_favorite({"name": "ghost", "favorite": True})
    assert "error" in out
