"""Phase 4.2 — agent eval: knowledge corpus and memory round-trip.

Three evals:
  - corpus_present    — td_search_official_docs returns results; Phase 1.1
                        "corpus not installed" regression guard.
  - search_trust_tier — knowledge_search results carry a trust_tier field;
                        Phase 3.2 validation.
  - memory_save_recall — end-to-end: save a marker then recall it verbatim.
"""

from __future__ import annotations

import pytest

from .conftest import (
    assert_no_error_event,
    assert_reply_contains,
    assert_tool_in_sequence,
    assistant_replies,
    reset_session,
    run_eval_turn,
    send_prompt,
    wait_for_turn_complete,
)

pytestmark = pytest.mark.agent_eval


# ---------------------------------------------------------------------------
# Eval 1 — knowledge corpus present (Phase 1.1 fix verification)
# ---------------------------------------------------------------------------


def test_knowledge_corpus_present(base_url):
    """td_search_official_docs must return real results for 'noiseTOP'.

    Verifies the Phase 1.1 corpus-install fix: the reply must NOT contain
    the error strings emitted when the corpus is missing, and must mention
    at least one noise-related page name.
    """
    rows = run_eval_turn(
        base_url,
        "Search the official TouchDesigner docs for noiseTOP."
        " Use td_search_official_docs and report at least one matching page name.",
    )

    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["td_search_official_docs"])

    # Corpus-missing guard — Phase 1.1 regression check.
    full_reply = "\n".join(assistant_replies(rows)).lower()
    assert "isn't installed" not in full_reply, "Reply contained 'isn't installed' — corpus appears missing"
    assert "corpus not installed" not in full_reply, (
        "Reply contained 'corpus not installed' — corpus appears missing"
    )

    # Must surface a noise-related term.
    assert_reply_contains(rows, "noise", case_insensitive=True)

    # Must surface either the operator family abbreviation or an op-shaped name.
    reply_lower = full_reply
    has_top = "top" in reply_lower
    # Accept any word that looks like an operator name: contains "noise" and
    # at least one uppercase run or ends in common TD suffixes.
    has_op_shape = any(
        token
        for token in full_reply.split()
        if "noise" in token.lower()
        and (
            any(c.isupper() for c in token)
            or token.lower().endswith(("top", "chop", "sop", "dat", "mat", "comp"))
        )
    )
    assert has_top or has_op_shape, (
        f"Reply did not mention 'TOP' or an operator-shaped name; reply tail:\n{full_reply[-400:]}"
    )


# ---------------------------------------------------------------------------
# Eval 2 — knowledge_search returns trust_tier (Phase 3.2)
# ---------------------------------------------------------------------------

_CANONICAL_TIERS = {"official", "bundled", "personal", "community", "transcript", "experimental"}
_KNOWN_CORPUS_NAMES = {"derivative", "popx", "paketa12"}


def test_knowledge_search_returns_trust_tier(base_url):
    """knowledge_search results must expose a trust_tier field.

    The agent should surface either the literal string 'trust_tier' or one
    of the canonical tier values, AND mention the corpus the result came from.
    """
    rows = run_eval_turn(
        base_url,
        "Use knowledge_search with query='noise' and top_k=2."
        " Then for the first match, tell me which corpus it came from"
        " and what its trust_tier is.",
    )

    assert_no_error_event(rows)
    assert_tool_in_sequence(rows, ["knowledge_search"])

    reply = "\n".join(assistant_replies(rows)).lower()

    # Must mention trust_tier literally OR one of the canonical tier values.
    has_trust_tier_field = "trust_tier" in reply
    has_tier_value = any(tier in reply for tier in _CANONICAL_TIERS)
    assert has_trust_tier_field or has_tier_value, (
        "Reply did not mention 'trust_tier' or any canonical tier value"
        f" ({_CANONICAL_TIERS}); reply tail:\n{reply[-400:]}"
    )

    # Must mention the corpus the result came from.
    has_corpus_word = "corpus" in reply
    has_corpus_name = any(name in reply for name in _KNOWN_CORPUS_NAMES)
    assert has_corpus_word or has_corpus_name, (
        "Reply did not mention 'corpus' or a known corpus name"
        f" ({_KNOWN_CORPUS_NAMES}); reply tail:\n{reply[-400:]}"
    )


# ---------------------------------------------------------------------------
# Eval 3 — memory save and recall (end-to-end)
# ---------------------------------------------------------------------------


def test_memory_save_and_recall(base_url):
    """memory_save followed by memory_get/memory_recall must echo the content.

    Two-turn eval on a single session (no reset between turns):
      Turn 1: save a marker via memory_save.
      Turn 2: recall the marker via memory_get or memory_recall; the agent
              must reproduce the exact content string.
    """
    prompt1 = (
        "Use memory_save to save a memory."
        " name=eval_phase42_marker, type=feedback,"
        " description='Phase 4.2 eval marker',"
        " content='hello phase 4.2'."
        " Confirm when saved."
    )
    prompt2 = (
        "Recall the memory called eval_phase42_marker"
        " (use memory_get) and quote a few words from its content."
    )

    # ---------- Turn 1 ----------
    reset_session(base_url)
    send_prompt(base_url, prompt1)
    rows1 = wait_for_turn_complete(base_url)

    assert_no_error_event(rows1)
    assert_tool_in_sequence(rows1, ["memory_save"])
    assert_reply_contains(rows1, "eval_phase42_marker", case_insensitive=True)

    # ---------- Turn 2 (same session — no reset) ----------
    send_prompt(base_url, prompt2)
    rows_full = wait_for_turn_complete(base_url)

    assert_no_error_event(rows_full)

    # Both memory_save AND one of the recall tools must appear across the
    # full transcript (rows_full includes the history of turn 1 as well).
    tools_seen = set()
    for row in rows_full:
        if row.get("role") != "tool_call":
            continue
        name = row.get("message", "").split("(", 1)[0].strip()
        tools_seen.add(name)

    assert "memory_save" in tools_seen, f"memory_save missing from full transcript; tools seen: {tools_seen}"
    recall_tools = {"memory_get", "memory_recall"}
    assert recall_tools & tools_seen, (
        f"Neither memory_get nor memory_recall found in full transcript; tools seen: {tools_seen}"
    )

    # The agent must quote back the saved content verbatim.
    final_reply = "\n".join(assistant_replies(rows_full))
    assert "hello phase 4.2" in final_reply, (
        f"Final reply did not contain 'hello phase 4.2';\n{final_reply[-400:]}"
    )
