"""Unit tests for the shared BM25 scorer (tdpilot_api_bm25).

Extracted from inline duplicates in memory/knowledge/recipes during the
2026-05-04 audit. The contract: same scoring as the prior implementation
so search ranking doesn't drift after the dedup.
"""

from __future__ import annotations

import math

from tdpilot_api_bm25 import bm25_score, tokenize  # noqa: E402


def test_tokenize_lowercases_and_splits_on_word_chars():
    assert tokenize("Hello World!") == ["hello", "world"]
    assert tokenize("snake_case-and-dash") == ["snake_case", "and", "dash"]
    assert tokenize("") == []
    assert tokenize(None) == []


def test_empty_query_returns_empty_list():
    docs = [{"name": "a", "description": "", "text": "anything"}]
    assert bm25_score("", docs) == []
    assert bm25_score("   ", docs) == []


def test_empty_docs_returns_empty_list():
    assert bm25_score("anything", []) == []


def test_returns_top_k_only():
    docs = [{"name": f"doc{i}", "description": "", "text": "particle particle simulation"} for i in range(8)]
    out = bm25_score("particle", docs, top_k=3)
    assert len(out) == 3
    # All returned scores should be > 0.
    for _, score in out:
        assert score > 0


def test_zero_score_docs_filtered_out():
    docs = [
        {"name": "match", "description": "", "text": "particle simulation"},
        {"name": "miss", "description": "", "text": "shader compilation"},
    ]
    out = bm25_score("particle", docs, top_k=5)
    assert len(out) == 1
    assert out[0][0]["name"] == "match"


def test_scoring_orders_by_relevance():
    docs = [
        {"name": "a", "description": "", "text": "particle"},
        {"name": "b", "description": "", "text": "particle particle"},
        {"name": "c", "description": "", "text": "particle particle particle"},
    ]
    out = bm25_score("particle", docs, top_k=3)
    # Highest TF should rank first.
    assert [d["name"] for d, _ in out] == ["c", "b", "a"]


def test_index_fields_extra_field_indexed():
    """recipes module passes `tags` as an extra index field — terms in
    that field should affect the score."""
    docs = [
        {"name": "no_tag", "description": "", "tags": "", "text": ""},
        {"name": "with_tag", "description": "", "tags": "perf feedback", "text": ""},
    ]
    # Default index_fields wouldn't see `tags`, so this query produces 0
    # matches. Adding `tags` to index_fields surfaces with_tag.
    default_out = bm25_score("feedback", docs, top_k=5)
    assert default_out == []

    tagged_out = bm25_score(
        "feedback",
        docs,
        index_fields=("name", "description", "tags", "text"),
        top_k=5,
    )
    assert len(tagged_out) == 1
    assert tagged_out[0][0]["name"] == "with_tag"


def test_idf_formula_matches_canonical_bm25():
    """Sanity check: a single doc, single term match, score equals the
    closed-form BM25 calculation (k1=1.5, b=0.75)."""
    docs = [{"name": "single", "description": "", "text": "particle"}]
    out = bm25_score("particle", docs, top_k=1)
    assert len(out) == 1
    _, score = out[0]
    # n_docs=1, n_qi=1 → idf = log((1 - 1 + 0.5)/(1 + 0.5) + 1)
    expected_idf = math.log((1 - 1 + 0.5) / (1 + 0.5) + 1)
    # tf=1, avg_dl=dl=1 → bm25 = idf * 1*(k1+1)/(1 + k1*(1-b+b*1/1))
    k1, b = 1.5, 0.75
    expected = expected_idf * 1 * (k1 + 1) / (1 + k1 * (1 - b + b * 1 / 1))
    assert score == expected
