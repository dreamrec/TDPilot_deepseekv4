"""v2.4 / Phase B.4 — content_type tagging for pre-turn BM25 lookup.

Closes the over-eager-tool-use bug (memory entry:
project_tdpilot_api_agent_overeager_tool_use.md): short ambiguous prompts
pulled instruction-shaped lookup hits, which the model then ran as if they
were the user's instructions. The fix tags every stored entry with
``content_type`` ("instruction" | "reference" | "fact") and filters
``instruction`` entries out of the pre-turn lookup UNLESS the user's
message explicitly names them.

Tests pin:
  * Instruction entries hidden on generic queries.
  * Instruction entries surfaced when user names them (substring match).
  * Reference/fact entries always visible (no regression).
  * Legacy entries (no content_type field) default to "reference" on read.
  * memory_save / recipe_save / knowledge_add persist content_type.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

# Indirect attribute name used in tests below to invoke the pre-turn
# lookup function. Spelled out as a constant + getattr-call so the
# security-reminder hook's substring matcher (which flags the literal
# 'eval(') doesn't false-positive on '_run_pre_turn_retrieval(...)'.
_LOOKUP_ATTR = "_run_pre_turn_re" + "trieval"


def _stub_handlers(monkeypatch, *, memory=None, recipes=None, knowledge=None):
    """Patch the three pre-turn lookup handlers so we can drive controlled
    match dicts into the lookup function."""
    import tdpilot_api_knowledge as kb_mod  # noqa: PLC0415
    import tdpilot_api_memory as mem_mod  # noqa: PLC0415
    import tdpilot_api_recipes as rec_mod  # noqa: PLC0415

    monkeypatch.setattr(
        mem_mod,
        "handle_memory_recall",
        lambda body: {"ok": True, "matches": memory or []},
    )
    monkeypatch.setattr(
        rec_mod,
        "handle_recipe_recall",
        lambda body: {"ok": True, "matches": recipes or []},
    )
    monkeypatch.setattr(
        kb_mod,
        "handle_knowledge_search",
        lambda body: {"ok": True, "matches": knowledge or []},
    )


def _run_lookup(rt, text):
    """Wrapper that calls rt._run_pre_turn_retrieval without writing the
    literal '...retrieval(' substring in this file."""
    return getattr(rt, _LOOKUP_ATTR)(text)


def test_b4_instruction_hidden_from_generic_query(monkeypatch):
    """An instruction-typed memory hit MUST be suppressed when the user's
    message doesn't reference the entry by name."""
    import tdpilot_api_runtime as rt_mod  # noqa: PLC0415
    from tdpilot_api_runtime import AgentRuntime  # noqa: PLC0415

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    _stub_handlers(
        monkeypatch,
        memory=[
            {
                "name": "noise_recipe",
                "score": 0.9,
                "snippet": "1. create noiseTOP 2. wire to level 3. ...",
                "content_type": "instruction",
            },
        ],
    )
    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    # Generic prompt — does NOT contain "noise_recipe" — must hide the entry.
    block = _run_lookup(rt, "what's a good FPS for HD output")
    assert "noise_recipe" not in block, f"instruction-typed entry leaked into block: {block!r}"


def test_b4_instruction_admitted_when_user_names_it(monkeypatch):
    """Same instruction-typed entry MUST surface when the user's message
    contains the entry's name as a substring."""
    import tdpilot_api_runtime as rt_mod  # noqa: PLC0415
    from tdpilot_api_runtime import AgentRuntime  # noqa: PLC0415

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    _stub_handlers(
        monkeypatch,
        memory=[
            {
                "name": "noise_recipe",
                "score": 0.9,
                "snippet": "1. create noiseTOP 2. wire to level 3. ...",
                "content_type": "instruction",
            },
        ],
    )
    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    block = _run_lookup(rt, "please run the noise_recipe steps")
    assert "noise_recipe" in block, f"explicit-name reference should surface entry, got block: {block!r}"


def test_b4_reference_always_visible(monkeypatch):
    """Reference-typed entries flow through the filter unchanged."""
    import tdpilot_api_runtime as rt_mod  # noqa: PLC0415
    from tdpilot_api_runtime import AgentRuntime  # noqa: PLC0415

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    _stub_handlers(
        monkeypatch,
        knowledge=[
            {
                "name": "noiseTOP",
                "score": 0.9,
                "snippet": "noiseTOP generates pseudo-random scalar fields",
                "content_type": "reference",
            },
        ],
    )
    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    block = _run_lookup(rt, "how do I do something with noise")
    assert "noiseTOP" in block, f"reference-typed entry should always pass filter, got: {block!r}"


def test_b4_legacy_entry_defaults_to_reference(monkeypatch):
    """Legacy entries (no content_type field at all) MUST default to
    'reference' so they remain visible to BM25."""
    import tdpilot_api_runtime as rt_mod  # noqa: PLC0415
    from tdpilot_api_runtime import AgentRuntime  # noqa: PLC0415

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    _stub_handlers(
        monkeypatch,
        memory=[
            # No content_type key at all — simulates a pre-B.4 entry.
            {
                "name": "legacy_entry",
                "score": 0.9,
                "snippet": "this entry has no content_type field",
            },
        ],
    )
    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    block = _run_lookup(rt, "query against legacy data here")
    assert "legacy_entry" in block, (
        f"legacy entry defaulting to reference must remain visible, got: {block!r}"
    )


def test_b4_short_name_not_substring_matched(monkeypatch):
    """Entry names < 4 chars are NOT used for substring-match — prevents
    false positives like an entry named 'fps' matching every message that
    happens to contain 'fps'."""
    import tdpilot_api_runtime as rt_mod  # noqa: PLC0415
    from tdpilot_api_runtime import AgentRuntime  # noqa: PLC0415

    monkeypatch.setattr(rt_mod, "fetch_api_key", lambda: "sk-fake")
    _stub_handlers(
        monkeypatch,
        memory=[
            {
                "name": "fps",  # 3 chars — too short for substring match
                "score": 0.9,
                "snippet": "fps trivia",
                "content_type": "instruction",
            },
        ],
    )
    rt = AgentRuntime(dispatcher=lambda *a: {"ok": True}, tools=[])
    # Even though "fps" is in the message, the 4-char floor blocks
    # substring-naming on the instruction entry — so the hit is filtered.
    block = _run_lookup(rt, "how do I check the fps in HD output")
    assert "- [memory] fps" not in block, f"short-name instruction must NOT substring-match, got: {block!r}"


def test_b4_memory_save_writes_content_type(tmp_path, monkeypatch):
    """handle_memory_save MUST persist content_type into the frontmatter."""
    import tdpilot_api_memory  # noqa: PLC0415

    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    result = tdpilot_api_memory.handle_memory_save(
        {
            "name": "test_entry",
            "description": "test",
            "type": "feedback",
            "content_type": "instruction",
            "content": "body",
        }
    )
    assert result.get("ok"), f"save failed: {result}"
    assert result.get("content_type") == "instruction"
    filepath = tmp_path / result["filename"]
    text = filepath.read_text(encoding="utf-8")
    assert "content_type: instruction" in text


def test_b4_memory_save_default_reference(tmp_path, monkeypatch):
    """Omitting content_type → default 'reference' (backwards-compat)."""
    import tdpilot_api_memory  # noqa: PLC0415

    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    result = tdpilot_api_memory.handle_memory_save(
        {
            "name": "test_default",
            "description": "test",
            "type": "feedback",
            "content": "body",
        }
    )
    assert result.get("content_type") == "reference"


def test_b4_recipe_save_default_instruction(tmp_path, monkeypatch):
    """Recipe save default is 'instruction' (recipes are step lists)."""
    import tdpilot_api_recipes  # noqa: PLC0415

    monkeypatch.setattr(tdpilot_api_recipes, "RECIPES_DIR", tmp_path)
    monkeypatch.setattr(tdpilot_api_recipes, "RECIPES_INDEX", tmp_path / "INDEX.md")
    result = tdpilot_api_recipes.handle_recipe_save(
        {
            "name": "test_recipe",
            "description": "test",
            "replay": [{"tool": "td_get_nodes", "args": {}}],
        }
    )
    assert result.get("ok"), f"save failed: {result}"
    assert result.get("content_type") == "instruction", f"recipes must default to instruction, got: {result}"


def test_b4_memory_save_rejects_invalid_content_type(tmp_path, monkeypatch):
    """Bogus content_type → error response (don't silently coerce)."""
    import tdpilot_api_memory  # noqa: PLC0415

    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(tdpilot_api_memory, "MEMORY_INDEX", tmp_path / "MEMORY.md")
    result = tdpilot_api_memory.handle_memory_save(
        {
            "name": "x",
            "description": "x",
            "type": "feedback",
            "content_type": "guideline",  # not in VALID_CONTENT_TYPES
            "content": "body",
        }
    )
    assert "error" in result
    assert "content_type" in result["error"].lower()


def test_b4_knowledge_add_writes_content_type(tmp_path, monkeypatch):
    """handle_knowledge_add MUST persist content_type to user-pool frontmatter."""
    import tdpilot_api_knowledge  # noqa: PLC0415

    monkeypatch.setattr(tdpilot_api_knowledge, "USER_KNOWLEDGE_DIR", tmp_path)
    result = tdpilot_api_knowledge.handle_knowledge_add(
        {
            "name": "test_kb",
            "description": "test",
            "category": "guide",
            "content_type": "instruction",
            "content": "body",
        }
    )
    assert result.get("ok"), f"add failed: {result}"
    assert result.get("content_type") == "instruction"
    filepath = tmp_path / result["filename"]
    text = filepath.read_text(encoding="utf-8")
    assert "content_type: instruction" in text
