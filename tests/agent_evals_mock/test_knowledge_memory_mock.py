"""Mock-driven mirror of ``tests/agent_evals/test_knowledge_memory.py``."""

from __future__ import annotations

from agent_evals_mock._eval_harness import run_mock_eval

_CANONICAL_TIERS = {
    "official",
    "bundled",
    "personal",
    "community",
    "transcript",
    "experimental",
}
_KNOWN_CORPUS_NAMES = {"derivative", "popx", "paketa12"}


def test_knowledge_corpus_present_mock(mock_deepseek):
    """td_search_official_docs must return real results for 'noiseTOP'.

    Verifies the corpus is reachable (no "isn't installed" / "corpus
    not installed" markers) and that the agent surfaces a noise-related
    operator name.
    """
    server = mock_deepseek("knowledge_corpus_present")
    result = run_mock_eval(
        server,
        prompt=(
            "Search the official TouchDesigner docs for noiseTOP."
            " Use td_search_official_docs and report at least one matching page name."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("td_search_official_docs")
    reply = result.final_text.lower()
    assert "isn't installed" not in reply, "reply contained corpus-missing marker"
    assert "corpus not installed" not in reply
    result.assert_text_contains("noise", case_insensitive=True)
    has_top = "top" in reply
    has_op_shape = any(
        token
        for token in result.final_text.split()
        if "noise" in token.lower()
        and (
            any(c.isupper() for c in token)
            or token.lower().endswith(("top", "chop", "sop", "dat", "mat", "comp"))
        )
    )
    assert has_top or has_op_shape, f"reply did not name a TOP-shaped op; tail:\n{result.final_text[-400:]}"
    assert server.thinking_violations() == []


def test_knowledge_search_returns_trust_tier_mock(mock_deepseek):
    """knowledge_search results must expose a trust_tier field."""
    server = mock_deepseek("knowledge_search_trust_tier")
    result = run_mock_eval(
        server,
        prompt=(
            "Use knowledge_search with query='noise' and top_k=2."
            " Then for the first match, tell me which corpus it came from"
            " and what its trust_tier is."
        ),
    )
    result.assert_no_error()
    result.assert_tool_called("knowledge_search")

    reply = result.final_text.lower()
    has_trust_tier_field = "trust_tier" in reply
    has_tier_value = any(tier in reply for tier in _CANONICAL_TIERS)
    assert has_trust_tier_field or has_tier_value, (
        f"reply did not mention 'trust_tier' or any canonical tier value "
        f"({_CANONICAL_TIERS}); tail:\n{reply[-400:]}"
    )

    has_corpus_word = "corpus" in reply
    has_corpus_name = any(name in reply for name in _KNOWN_CORPUS_NAMES)
    assert has_corpus_word or has_corpus_name, (
        f"reply did not mention 'corpus' or a known corpus name "
        f"({_KNOWN_CORPUS_NAMES}); tail:\n{reply[-400:]}"
    )
    assert server.thinking_violations() == []


def test_memory_save_and_recall_mock(mock_deepseek):
    """memory_save followed by memory_get must round-trip the content."""
    server = mock_deepseek("memory_save_and_recall")
    result = run_mock_eval(
        server,
        prompt=[
            (
                "Use memory_save to save a memory."
                " name=eval_phase42_marker, type=feedback,"
                " description='Phase 4.2 eval marker',"
                " content='hello phase 4.2'."
                " Confirm when saved."
            ),
            (
                "Recall the memory called eval_phase42_marker"
                " (use memory_get) and quote a few words from its content."
            ),
        ],
    )
    result.assert_no_error()
    names = result.tool_call_names()
    assert "memory_save" in names, f"memory_save missing; tools called: {names}"
    recall_tools = {"memory_get", "memory_recall"}
    assert recall_tools & set(names), f"neither memory_get nor memory_recall called; tools: {names}"
    assert "hello phase 4.2" in result.final_text, (
        f"final reply did not contain saved content;\ntail:\n{result.final_text[-400:]}"
    )
    assert server.thinking_violations() == []
