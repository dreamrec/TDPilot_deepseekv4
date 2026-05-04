"""Unit tests for tdpilot_api_official_docs (Tier 1 + 2 lookup tools).

Stubs `tdpilot_api_knowledge.handle_knowledge_search`,
`handle_knowledge_get`, and `_external_corpora_entries` so we can verify
the routing layer (filters, fallbacks, hint surfaces, soft re-rank)
without depending on a real corpus on disk.

Two correctness properties under test:

  1. Distinguish "no matches" from "no corpus" — the no-corpus hint
     fires only when ``_external_corpora_entries`` doesn't include the
     target corpus, not when count==0.

  2. Soft category filtering — ``doc_type`` re-ranks results but never
     excludes them. A noiseTOP page filed under ``python_api`` still
     surfaces when the agent asks for ``doc_type=operator``.
"""

from __future__ import annotations

from unittest.mock import patch

import tdpilot_api_official_docs as od


def _fake_search(matches=None, count=None):
    """Return a callable suitable for monkeypatching handle_knowledge_search."""
    matches = matches or []

    def _impl(body):
        return {"ok": True, "count": count if count is not None else len(matches), "matches": matches}

    return _impl


def _fake_get(found=False):
    def _impl(body):
        if found:
            return {"ok": True, "filename": "noiseTOP.md", "content": "operator docs..."}
        return {"error": f"not found: {body.get('name')}"}

    return _impl


def _fake_corpora(corpus_names):
    """Return a callable that mocks _external_corpora_entries with the
    given corpus list."""

    def _impl():
        return [{"corpus": name} for name in corpus_names]

    return _impl


# ----------------------------------------------------------------------
# search_official_docs
# ----------------------------------------------------------------------


def test_search_official_docs_routes_to_derivative_corpus():
    captured: dict = {}

    def stub(body):
        captured.update(body)
        return {
            "ok": True,
            "count": 1,
            "matches": [{"name": "noiseTOP", "category": "python_api", "score": 0.9}],
        }

    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
            out = od.handle_search_official_docs({"query": "noiseTOP", "doc_type": "operator"})

    # NOTE: with the soft-rerank fix, doc_type is NOT passed to
    # handle_knowledge_search as a category filter. It's applied locally.
    assert captured["corpus"] == "derivative"
    assert "category" not in captured
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["doc_type_hint_applied"] is True


def test_search_official_docs_returns_hint_when_corpus_missing():
    """When auto-discovery shows no derivative corpus, return the
    no-corpus hint regardless of the query."""
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora([])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search([])):
            out = od.handle_search_official_docs({"query": "anything"})
    assert out["available"] is False
    assert "hint" in out
    assert "derivative" in out["hint"].lower()


def test_search_official_docs_no_matches_does_not_claim_corpus_missing():
    """Bug fix: when corpus IS installed but the query has zero hits,
    do NOT claim 'corpus not installed'. Just return count=0."""
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search([], count=0)):
            out = od.handle_search_official_docs({"query": "totally_unmatched"})
    assert out["ok"] is True
    assert out["available"] is True  # NOT False — corpus IS installed
    assert out["count"] == 0
    # The no-corpus hint must NOT be in the response
    assert "hint" not in out or "isn't installed" not in out.get("hint", "")


def test_search_official_docs_soft_rerank_pushes_doc_type_to_top():
    """The doc_type parameter re-ranks but never excludes. A page
    matching the doc_type bubbles to the top; others stay in BM25 order."""
    bm25_matches = [
        {"name": "Class A", "category": "python_api", "score": 0.9},
        {"name": "Operator B", "category": "operator", "score": 0.7},
        {"name": "Snippet C", "category": "snippet", "score": 0.6},
    ]

    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search(bm25_matches)):
            out = od.handle_search_official_docs({"query": "x", "doc_type": "operator"})

    names = [m["name"] for m in out["matches"]]
    assert names[0] == "Operator B"  # operator-typed bubbles to top
    assert "Class A" in names  # python_api still present (not excluded)
    assert "Snippet C" in names


def test_search_official_docs_requires_query():
    out = od.handle_search_official_docs({})
    assert "error" in out


# ----------------------------------------------------------------------
# get_operator_doc — exact match + BM25 fallback
# ----------------------------------------------------------------------


def test_get_operator_doc_exact_match_wins():
    """When handle_knowledge_get finds an exact name, fallback isn't called."""
    fallback_called = {"flag": False}

    def fallback_stub(body):
        fallback_called["flag"] = True
        return {"ok": True, "count": 0, "matches": []}

    with patch("tdpilot_api_knowledge.handle_knowledge_get", _fake_get(found=True)):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", fallback_stub):
            out = od.handle_get_operator_doc({"op_type": "noiseTOP"})

    assert out["ok"] is True
    assert fallback_called["flag"] is False


def test_get_operator_doc_falls_back_to_bm25_with_python_api_pages():
    """noiseTOP doc page is filed under 'python_api', not 'operator'.
    The fallback BM25 (no strict category filter) should still find it."""
    fallback_matches = [
        {"name": "noiseTOP Class", "category": "python_api", "score": 0.9},
    ]
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_get", _fake_get(found=False)):
            with patch(
                "tdpilot_api_knowledge.handle_knowledge_search",
                _fake_search(fallback_matches),
            ):
                out = od.handle_get_operator_doc({"op_type": "noisetop"})

    assert out["ok"] is True
    assert out["approximate"] is True
    assert out["count"] == 1


def test_get_operator_doc_missing_op_type():
    out = od.handle_get_operator_doc({})
    assert "error" in out


def test_get_operator_doc_returns_no_corpus_hint_when_derivative_missing():
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora([])):
        with patch("tdpilot_api_knowledge.handle_knowledge_get", _fake_get(found=False)):
            out = od.handle_get_operator_doc({"op_type": "noiseTOP"})
    assert out["ok"] is False
    assert out["available"] is False  # corpus missing
    assert out["not_found"] == "noiseTOP"


# ----------------------------------------------------------------------
# get_param_help
# ----------------------------------------------------------------------


def test_get_param_help_constructs_targeted_query():
    captured: dict = {}

    def stub(body):
        captured.update(body)
        return {"ok": True, "count": 1, "matches": [{"name": "x"}]}

    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
            od.handle_get_param_help({"op_type": "noiseTOP", "param": "type"})

    assert "noiseTOP" in captured["query"]
    assert "type" in captured["query"]
    assert "parameter" in captured["query"].lower()
    assert captured["corpus"] == "derivative"


def test_get_param_help_requires_both_fields():
    assert "error" in od.handle_get_param_help({"op_type": "noiseTOP"})
    assert "error" in od.handle_get_param_help({"param": "type"})


def test_get_param_help_returns_no_corpus_hint_when_derivative_missing():
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora([])):
        out = od.handle_get_param_help({"op_type": "noiseTOP", "param": "type"})
    assert out["ok"] is False
    assert out["available"] is False


# ----------------------------------------------------------------------
# lookup_snippets / lookup_palette_component
# ----------------------------------------------------------------------


def test_lookup_snippets_soft_ranks_snippet_category():
    """Snippets are soft-ranked, not strict-filtered. Non-snippet matches
    still appear in the result."""
    bm25_matches = [
        {"name": "Operator A", "category": "operator", "score": 0.9},
        {"name": "Snippet B", "category": "snippet", "score": 0.6},
    ]
    with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search(bm25_matches)):
        out = od.handle_lookup_snippets({"topic": "audio reactive"})

    names = [m["name"] for m in out["matches"]]
    assert names[0] == "Snippet B"  # snippet bubbles up
    assert "Operator A" in names  # but operator still present


def test_lookup_palette_component_soft_ranks_palette_category():
    bm25_matches = [
        {"name": "Operator A", "category": "operator", "score": 0.9},
        {"name": "Palette B", "category": "palette", "score": 0.5},
    ]
    with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search(bm25_matches)):
        out = od.handle_lookup_palette_component({"name": "audioVU"})

    names = [m["name"] for m in out["matches"]]
    assert names[0] == "Palette B"


def test_lookup_palette_component_falls_back_when_first_pass_empty():
    """First pass empty → fallback augments query with 'palette component'."""
    call_log: list[dict] = []

    def stub(body):
        call_log.append(dict(body))
        if "palette component" in body["query"]:
            return {"ok": True, "count": 1, "matches": [{"name": "fallback hit"}]}
        return {"ok": True, "count": 0, "matches": []}

    with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
        out = od.handle_lookup_palette_component({"name": "audioVU"})

    assert len(call_log) == 2
    assert out["count"] == 1


# ----------------------------------------------------------------------
# Tier 2 — recommendation tools
# ----------------------------------------------------------------------


def test_recommend_official_component_returns_three_buckets():
    """The tool runs ONE search and soft-reranks for three category
    buckets — palette, operator, snippet."""
    bm25_pool = [
        {"name": "Pal", "category": "palette", "score": 0.9},
        {"name": "Op", "category": "operator", "score": 0.8},
        {"name": "Snip", "category": "snippet", "score": 0.7},
        {"name": "Other", "category": "general", "score": 0.6},
    ]

    with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search(bm25_pool)):
        out = od.handle_recommend_official_component({"goal": "audio-reactive feedback loop"})

    assert "palette_components" in out
    assert "operators" in out
    assert "snippets" in out
    # Each bucket bubbles its category-matching item to the top
    assert out["palette_components"][0]["name"] == "Pal"
    assert out["operators"][0]["name"] == "Op"
    assert out["snippets"][0]["name"] == "Snip"


def test_recommend_requires_goal():
    out = od.handle_recommend_official_component({})
    assert "error" in out


def test_find_official_example_prefers_examples_corpus():
    call_log: list[dict] = []

    def stub(body):
        call_log.append(dict(body))
        if body.get("corpus") == "examples":
            return {"ok": True, "count": 1, "matches": [{"name": "particle_demo"}]}
        return {"ok": True, "count": 0, "matches": []}

    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["examples", "derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
            out = od.handle_find_official_example({"topic": "particle"})

    assert out["source"] == "examples"


def test_find_official_example_falls_back_to_derivative_soft_rerank():
    """No examples corpus → soft-rerank derivative pool by snippet+operator."""

    def stub(body):
        if body.get("corpus") == "examples":
            return {"ok": True, "count": 0, "matches": []}
        return {
            "ok": True,
            "count": 2,
            "matches": [
                {"name": "Op page", "category": "operator", "score": 0.8},
                {"name": "Snippet page", "category": "snippet", "score": 0.7},
            ],
        }

    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora(["derivative"])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
            out = od.handle_find_official_example({"topic": "anything"})

    assert out["source"] == "derivative:soft-rerank"
    assert out["count"] >= 1


def test_find_official_example_returns_hint_when_no_corpora():
    with patch("tdpilot_api_knowledge._external_corpora_entries", _fake_corpora([])):
        with patch("tdpilot_api_knowledge.handle_knowledge_search", _fake_search([], count=0)):
            out = od.handle_find_official_example({"topic": "ghosts"})
    assert "hint" in out


def test_explain_better_way_dedupes_across_two_queries():
    """The tool runs two queries and merges by name. A name appearing in
    both should count once."""

    def stub(body):
        return {
            "ok": True,
            "count": 2,
            "matches": [
                {"name": "shared", "score": 0.8},
                {"name": f"unique_{body['query'][:5]}", "score": 0.7},
            ],
        }

    with patch("tdpilot_api_knowledge.handle_knowledge_search", stub):
        out = od.handle_explain_better_way({"current_approach": "X", "goal": "Y"})

    names = [m["name"] for m in out["matches"]]
    assert names.count("shared") == 1
    assert len(names) == 3


def test_explain_better_way_requires_goal():
    out = od.handle_explain_better_way({"current_approach": "X"})
    assert "error" in out
