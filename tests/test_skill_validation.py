"""Regression tests for the skill loader's frontmatter validation (1.7.2).

Pre-1.7.2 the loader had a custom YAML-ish parser that silently
swallowed bad input — typo'd user skills vanished from skill_list with
no error surfaced. Now we use ``yaml.safe_load`` and validate required
fields. Invalid skills are kept in skill_list (with their error list)
but filtered out of trigger matching, auto-load, and the system-prompt
index.

Also covers the trigger-semantics fix: pre-1.7.2 triggers >= 5 chars
fell through to substring match, so ``"don't optimize this"`` activated
the performance skill via a substring hit on ``optimize``. 1.7.2 uses
word-boundary regex for ALL trigger lengths.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Standalone module path is added to sys.path by tests/conftest.py.
# Importing tdpilot_api_skills directly works because of that fixture.
import tdpilot_api_skills as skills

# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


def test_valid_skill_passes_validation():
    text = """---
name: my-skill
description: A test skill
auto_load: false
priority: 5
triggers: [foo, bar]
surface: standalone
---

Body content here.
"""
    meta, body, errors = skills._parse_frontmatter(text)
    assert errors == []
    assert meta["name"] == "my-skill"
    assert meta["description"] == "A test skill"
    assert meta["auto_load"] is False
    assert meta["priority"] == 5
    assert meta["triggers"] == ["foo", "bar"]
    assert meta["surface"] == "standalone"
    assert "Body content" in body


def test_missing_name_field_is_flagged():
    text = """---
description: forgot the name
---

body
"""
    meta, _body, errors = skills._parse_frontmatter(text)
    assert any("name" in e for e in errors)


def test_non_list_triggers_is_flagged():
    text = """---
name: bad-triggers
triggers: "should-be-a-list"
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("triggers" in e and "list" in e for e in errors)


def test_bad_yaml_returns_parse_error():
    text = """---
name: my-skill
description: : invalid : : yaml : ::
triggers: [unterminated
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert errors, "expected at least one parse error"
    assert any("yaml" in e.lower() for e in errors)


def test_no_frontmatter_is_flagged():
    text = "Just a markdown body, no frontmatter at all.\n"
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("'---'" in e or "frontmatter" in e.lower() for e in errors)


def test_unterminated_frontmatter_is_flagged():
    text = """---
name: oops
description: never closed
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("unterminated" in e.lower() or "closing" in e.lower() for e in errors)


def test_invalid_surface_value_is_flagged():
    text = """---
name: weird-surface
surface: xbox360
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("surface" in e for e in errors)


def test_non_bool_auto_load_is_flagged():
    text = """---
name: bad-bool
auto_load: "yes please"
---

body
"""
    _meta, _body, errors = skills._parse_frontmatter(text)
    assert any("auto_load" in e for e in errors)


# ---------------------------------------------------------------------------
# Build entry — invalid skills still surface
# ---------------------------------------------------------------------------


def test_build_entry_marks_valid_skill():
    text = """---
name: ok-skill
description: works
---

body
"""
    entry = skills._build_entry(text, source="user", default_name="fallback", filename="ok.md")
    assert entry["valid"] is True
    assert entry["validation_errors"] == []
    assert entry["name"] == "ok-skill"


def test_build_entry_marks_invalid_skill_but_keeps_it():
    """Pre-1.7.2 invalid skills were silently skipped. Now they're
    kept in the entry list with valid=False so the user can see
    them via skill_list. They're filtered out of trigger matching
    via _valid_entries() but still listed."""
    text = """---
description: missing name field
---

body
"""
    entry = skills._build_entry(text, source="user", default_name="my-fallback", filename="broken.md")
    assert entry["valid"] is False
    assert entry["validation_errors"]
    # Falls back to the filename-derived default for display purposes.
    assert entry["name"] == "my-fallback"


def test_default_surface_when_omitted_is_both():
    text = """---
name: surfaceless
---

body
"""
    entry = skills._build_entry(text, source="user", default_name="x", filename="x.md")
    assert entry["surface"] == "both"


# ---------------------------------------------------------------------------
# Trigger matching — word-boundary for ALL lengths
# ---------------------------------------------------------------------------


def _patched_user_dir(tmp_path, monkeypatch):
    """Repoint USER_SKILLS_DIR at a tmp dir + clear bundled entries."""
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])
    return tmp_path


def _write_skill(dirpath, filename, text):
    p = dirpath / filename
    p.write_text(text, encoding="utf-8")


def test_trigger_does_not_substring_match_pre_1_7_2_regression(tmp_path, monkeypatch):
    """Pre-1.7.2 ``optimize`` trigger substring-matched on ``"don't
    optimize"``. 1.7.2 uses word-boundary regex for all lengths so
    only standalone ``optimize`` words activate the skill."""
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "perf.md",
        """---
name: performance-mode
description: optimize TD projects
triggers: [optimize, slow]
---

body
""",
    )
    # Standalone word — must match.
    matched = skills.find_triggered_skills("can you optimize this network")
    assert any(m["name"] == "performance-mode" for m in matched)

    # Substring inside a longer word — must NOT match.
    matched = skills.find_triggered_skills("the optimization is fine")
    assert not any(m["name"] == "performance-mode" for m in matched)


def test_short_trigger_keeps_word_boundary_behavior(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "popx.md",
        """---
name: popx-mode
description: POP discipline
triggers: [pop, popx]
---

body
""",
    )
    assert any(m["name"] == "popx-mode" for m in skills.find_triggered_skills("make a pop particle"))
    # 'population' must NOT match the 'pop' trigger.
    assert not any(
        m["name"] == "popx-mode" for m in skills.find_triggered_skills("the population is increasing")
    )


def test_invalid_skills_excluded_from_triggers(tmp_path, monkeypatch):
    """Even if a broken skill has triggers in its frontmatter, it
    must NOT activate — invalid frontmatter means we shouldn't load
    its body into the agent's context."""
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "broken.md",
        """---
description: missing name
triggers: [optimize]
---

body
""",
    )
    matched = skills.find_triggered_skills("please optimize this")
    assert not any("optimize" in (m.get("triggers") or []) for m in matched if not m.get("valid", True))
    # Strictly: no entry from the broken skill should appear.
    assert all(m.get("valid", True) for m in matched)


# ---------------------------------------------------------------------------
# skill_list + skill_validate handlers
# ---------------------------------------------------------------------------


def test_skill_list_includes_invalid_with_errors(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "good.md",
        """---
name: good
description: works
---

body
""",
    )
    _write_skill(
        tmp_path,
        "bad.md",
        """---
description: missing name
---

body
""",
    )
    out = skills.handle_skill_list({})
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["valid_count"] == 1
    assert out["invalid_count"] == 1
    bad = next(s for s in out["skills"] if s["filename"] == "bad.md")
    assert bad["valid"] is False
    assert bad["validation_errors"]


def test_skill_validate_no_args_returns_only_invalid(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "good.md",
        """---
name: good
description: works
---

body
""",
    )
    _write_skill(
        tmp_path,
        "bad.md",
        """---
description: missing name
---

body
""",
    )
    out = skills.handle_skill_validate({})
    assert out["ok"] is True
    assert out["invalid_count"] == 1
    assert out["invalid_skills"][0]["filename"] == "bad.md"


def test_skill_validate_by_name_returns_specific_skill(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "alpha.md",
        """---
name: alpha
description: a skill
---

body
""",
    )
    out = skills.handle_skill_validate({"name": "alpha"})
    assert out["ok"] is True
    assert out["valid"] is True
    assert out["validation_errors"] == []


def test_skill_validate_unknown_name_returns_error(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    out = skills.handle_skill_validate({"name": "does-not-exist"})
    assert "error" in out


# ---------------------------------------------------------------------------
# get_skills_index_hint + get_auto_load_skills_text only see valid skills
# ---------------------------------------------------------------------------


def test_index_hint_excludes_invalid(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    _write_skill(
        tmp_path,
        "good.md",
        """---
name: good-skill
description: works fine
triggers: [foo]
---

body
""",
    )
    _write_skill(
        tmp_path,
        "bad.md",
        """---
description: forgot name
triggers: [bar]
---

body
""",
    )
    hint = skills.get_skills_index_hint()
    assert "good-skill" in hint
    # bad.md fell back to its filename stem 'bad'; that name shouldn't
    # appear because the entry is invalid.
    assert "\n- bad " not in hint and "\n- bad\n" not in hint


def test_auto_load_excludes_invalid(tmp_path, monkeypatch):
    _patched_user_dir(tmp_path, monkeypatch)
    # Even with auto_load: true, an invalid skill must NOT be
    # injected into the system prompt — that's exactly what the
    # filter exists to prevent.
    _write_skill(
        tmp_path,
        "auto.md",
        """---
description: missing name but auto_load
auto_load: true
---

THIS BODY MUST NOT APPEAR IN THE SYSTEM PROMPT
""",
    )
    text = skills.get_auto_load_skills_text()
    assert "MUST NOT APPEAR" not in text
