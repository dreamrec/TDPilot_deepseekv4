"""Unit tests for the pure-Python parts of tdpilot_api_recipes.

The replay-JSON extractor and frontmatter parser are responsible for
turning the model's saved markdown back into an executable step list. A
regex failure silently returns ``[]``, so explicit coverage of the
preferred + fallback fence patterns matters more than usual.
"""

from __future__ import annotations

from tdpilot_api_recipes import (  # noqa: E402
    _extract_replay_json,
    _parse_frontmatter,
)

# ----------------------------------------------------------------------
# _extract_replay_json — regex-driven JSON extractor
# ----------------------------------------------------------------------


def test_extract_replay_json_under_replay_heading():
    body = """## Replay

```json
[{"tool": "td_get_info", "args": {}}]
```
"""
    out = _extract_replay_json(body)
    assert out == [{"tool": "td_get_info", "args": {}}]


def test_extract_replay_json_falls_back_to_any_json_fence():
    """No ## Replay heading — accept any ```json``` block as fallback."""
    body = """Some prose.

```json
[{"tool": "td_get_nodes", "args": {"path": "/project1"}}]
```

More prose.
"""
    out = _extract_replay_json(body)
    assert out == [{"tool": "td_get_nodes", "args": {"path": "/project1"}}]


def test_extract_replay_json_missing_block_returns_empty():
    out = _extract_replay_json("No fenced JSON anywhere in this body.")
    assert out == []


def test_extract_replay_json_invalid_json_returns_empty():
    body = """## Replay

```json
[not valid json
```
"""
    out = _extract_replay_json(body)
    assert out == []


def test_extract_replay_json_top_level_object_not_list_returns_empty():
    """The contract is a JSON list of step dicts. A bare object should
    not be silently accepted as a single-step replay."""
    body = """## Replay

```json
{"tool": "td_get_info", "args": {}}
```
"""
    out = _extract_replay_json(body)
    assert out == []


def test_extract_replay_json_filters_non_dict_items():
    body = """## Replay

```json
[{"tool": "ok"}, "garbage", 42, {"tool": "second"}]
```
"""
    out = _extract_replay_json(body)
    assert out == [{"tool": "ok"}, {"tool": "second"}]


# ----------------------------------------------------------------------
# _parse_frontmatter — YAML-ish frontmatter parser
# ----------------------------------------------------------------------


def test_parse_frontmatter_with_simple_keys():
    text = """---
name: test_recipe
description: a simple recipe
---
body content here
"""
    meta, body = _parse_frontmatter(text)
    assert meta == {"name": "test_recipe", "description": "a simple recipe"}
    assert body.startswith("body content")


def test_parse_frontmatter_with_tag_list():
    text = """---
name: tagged
tags: [perf, feedback, audio]
---
body
"""
    meta, body = _parse_frontmatter(text)
    assert meta["tags"] == ["perf", "feedback", "audio"]


def test_parse_frontmatter_with_empty_tag_list():
    text = """---
name: empty_tags
tags: []
---
body
"""
    meta, _ = _parse_frontmatter(text)
    assert meta["tags"] == []


def test_parse_frontmatter_no_frontmatter_returns_text_unchanged():
    text = "no frontmatter at all\njust a body."
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text


def test_parse_frontmatter_unterminated_block_returns_unchanged():
    text = "---\nname: oops\nno_closing_marker\n"
    meta, body = _parse_frontmatter(text)
    assert meta == {}
    assert body == text


# ----------------------------------------------------------------------
# Tier 1: handle_validate_recipe
# ----------------------------------------------------------------------


def test_validate_recipe_accepts_well_formed_replay():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe(
        {
            "name": "test",
            "replay": [
                {"tool": "td_get_info", "args": {}},
                {"tool": "td_get_nodes", "args": {"path": "/project1"}},
            ],
        }
    )
    assert out["ok"] is True
    assert out["valid"] is True
    assert out["step_count"] == 2
    assert out["issue_count"] == 0


def test_validate_recipe_flags_unknown_tool():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe(
        {
            "replay": [
                {"tool": "td_get_info", "args": {}},
                {"tool": "td_does_not_exist", "args": {}},
            ],
        }
    )
    assert out["valid"] is False
    assert out["issue_count"] == 1
    assert out["issues"][0]["tool"] == "td_does_not_exist"


def test_validate_recipe_flags_non_dict_step():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe({"replay": ["not a dict"]})
    assert out["valid"] is False
    assert "not a dict" in out["issues"][0]["error"]


def test_validate_recipe_flags_missing_tool_field():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe({"replay": [{"args": {}}]})
    assert out["valid"] is False
    assert "tool" in out["issues"][0]["error"].lower()


def test_validate_recipe_flags_non_dict_args():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe(
        {
            "replay": [{"tool": "td_get_info", "args": "not a dict"}],
        }
    )
    assert out["valid"] is False
    assert "args" in out["issues"][0]["error"].lower()


def test_validate_recipe_rejects_non_list_replay():
    from tdpilot_api_recipes import handle_validate_recipe

    out = handle_validate_recipe({"replay": "not a list"})
    assert out["valid"] is False
    assert "list" in out["error"]
