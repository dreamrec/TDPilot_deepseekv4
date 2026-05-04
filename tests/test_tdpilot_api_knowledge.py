"""Phase 1.1 acceptance tests for SQLite/FTS corpus support
in tdpilot_api_knowledge.py.

Covers:
 - SQLite corpus discovery (_sqlite_corpus_descriptors)
 - JSONL corpus discovery (_external_corpora_entries)
 - Prefer-DB rule (brain.db wins over pages.jsonl in same dir)
 - FTS5 query results shape (_query_sqlite_fts)
 - Missing DB graceful degradation
 - Legacy v0 schema fallback
 - FTS injection safety (_fts_quote)
 - BM25 path still works with no SQLite corpora
 - Bonus: merged BM25 + FTS results
 - Bonus: _corpora_summary kind ordering
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "td_component"))

import tdpilot_api_knowledge as kb  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_v1m():
    """Import _chunk_schema_v1 from scripts/ without installing it."""
    spec = importlib.util.spec_from_file_location("v1m", REPO_ROOT / "scripts" / "_chunk_schema_v1.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_v1_brain(db_path: Path, corpus: str, chunks: list[dict], *, trust_tier: str = "official") -> None:
    """Seed a v1 brain.db using the production helper."""
    v1m = _load_v1m()
    v1m.build_v1_fts_index(
        chunks,
        db_path,
        brain_id=corpus,
        trust_tier=trust_tier,
        extra_meta={"display_name": corpus.capitalize()},
    )


def _make_chunks(n: int = 3) -> list[dict]:
    return [
        {
            "chunk_id": f"chunk_{i:04d}",
            "page_id": "noiseTOP_page",
            "title": f"noiseTOP Section {i}",
            "section_title": f"noiseTOP Section {i}",
            "content": f"The noiseTOP generates procedural noise textures. Section {i}.",
            "url": f"https://docs.derivative.ca/noiseTOP#{i}",
            "doc_type": "reference",
            "operator_name": "noiseTOP",
        }
        for i in range(n)
    ]


def _clear_module_caches(monkeypatch) -> None:
    """Reset all module-level caches so tests don't bleed into each other."""
    monkeypatch.setattr(kb, "_corpus_cache", {})
    monkeypatch.setattr(kb, "_corpus_mtime", {})
    monkeypatch.setattr(kb, "_brain_meta_cache", {})
    monkeypatch.setattr(kb, "_brain_meta_mtime", {})


def _make_jsonl_corpus(corpus_dir: Path, n_pages: int = 2) -> None:
    """Write a minimal pages.jsonl into corpus_dir."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    pages = [
        {
            "page_id": f"page_{i}",
            "title": f"Page {i}",
            "text": f"Some content about TouchDesigner operators page {i}.",
            "url": f"https://example.com/page{i}",
            "doc_type": "reference",
            "headings": [f"Heading {i}"],
        }
        for i in range(n_pages)
    ]
    (corpus_dir / "pages.jsonl").write_text("\n".join(json.dumps(p) for p in pages), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1 — SQLite corpus discovery
# ---------------------------------------------------------------------------


def test_discovers_sqlite_corpus(tmp_path, monkeypatch):
    """_sqlite_corpus_descriptors returns one descriptor for a brain.db."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"
    corpus_dir = root / "derivative"
    corpus_dir.mkdir(parents=True)

    db_path = corpus_dir / "docsbrain.db"
    _build_v1_brain(db_path, "derivative", _make_chunks(3), trust_tier="official")

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    descs = kb._sqlite_corpus_descriptors()
    assert len(descs) == 1
    desc = descs[0]
    assert desc["corpus"] == "derivative"
    assert desc["db_path"] == db_path
    assert isinstance(desc["meta"], dict)
    assert desc["meta"].get("brain_id") == "derivative"
    assert desc["trust_tier"] == "official"
    assert desc["display_name"] == "Derivative"


# ---------------------------------------------------------------------------
# Test 2 — JSONL corpus discovery
# ---------------------------------------------------------------------------


def test_discovers_jsonl_corpus(tmp_path, monkeypatch):
    """_external_corpora_entries parses a pages.jsonl corpus."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"
    corpus_dir = root / "popx"
    _make_jsonl_corpus(corpus_dir, n_pages=2)

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    entries = kb._external_corpora_entries()
    assert len(entries) == 2
    assert all(e["corpus"] == "popx" for e in entries)
    assert all("name" in e for e in entries)
    assert all("text" in e for e in entries)


# ---------------------------------------------------------------------------
# Test 3 — Prefer-DB rule
# ---------------------------------------------------------------------------


def test_prefer_sqlite_over_jsonl_when_both_present(tmp_path, monkeypatch):
    """When brain.db AND pages.jsonl coexist, only the SQLite descriptor is
    surfaced; _external_corpora_entries skips the jsonl."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"
    corpus_dir = root / "mixed"
    corpus_dir.mkdir(parents=True)

    # Both present
    db_path = corpus_dir / "docsbrain.db"
    _build_v1_brain(db_path, "mixed", _make_chunks(2))
    _make_jsonl_corpus(corpus_dir)

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    # SQLite descriptor must be found
    descs = kb._sqlite_corpus_descriptors()
    assert any(d["corpus"] == "mixed" for d in descs)

    # JSONL entries must NOT be loaded (prefer-DB rule)
    entries = kb._external_corpora_entries()
    jsonl_entries = [e for e in entries if e.get("corpus") == "mixed"]
    assert jsonl_entries == []


# ---------------------------------------------------------------------------
# Test 4 — FTS query returns expected match shape
# ---------------------------------------------------------------------------


def test_query_sqlite_fts_returns_match_shape(tmp_path, monkeypatch):
    """_query_sqlite_fts returns at least one match with required keys."""
    _clear_module_caches(monkeypatch)
    db_path = tmp_path / "docsbrain.db"
    _build_v1_brain(db_path, "derivative", _make_chunks(3), trust_tier="official")

    meta = kb._read_brain_meta_with_cache(db_path)
    matches = kb._query_sqlite_fts(db_path, "noiseTOP", 2, meta=meta)

    assert len(matches) >= 1
    required_keys = {"name", "score", "snippet", "trust_tier", "corpus", "url", "chunk_id"}
    for m in matches:
        assert required_keys.issubset(m.keys()), f"Missing keys: {required_keys - m.keys()}"
    assert all(m["trust_tier"] == "official" for m in matches)
    assert all(m["corpus"] == "derivative" for m in matches)


# ---------------------------------------------------------------------------
# Test 5 — Missing DB returns empty list
# ---------------------------------------------------------------------------


def test_missing_db_does_not_error(tmp_path):
    """Querying a nonexistent brain.db returns [] without raising."""
    result = kb._query_sqlite_fts(tmp_path / "missing.db", "anything", 5)
    assert result == []


# ---------------------------------------------------------------------------
# Test 6 — Legacy v0 schema falls back gracefully
# ---------------------------------------------------------------------------


def test_schema_mismatch_degrades_gracefully(tmp_path):
    """v0-schema brain.db (no title/trust_tier columns) still returns matches."""
    db_path = tmp_path / "legacybrain.db"
    conn = sqlite3.connect(str(db_path))
    try:
        # Minimal v0 chunks table (no title, no trust_tier, no url)
        conn.execute(
            """CREATE TABLE chunks (
                chunk_id TEXT PRIMARY KEY,
                section_title TEXT,
                doc_type TEXT,
                operator_name TEXT,
                content TEXT
            )"""
        )
        # Contentless FTS5 — mirrors legacy CLI brains
        conn.execute(
            """CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_id UNINDEXED,
                section_title,
                operator_name,
                content,
                content=''
            )"""
        )
        # Insert rows
        conn.execute(
            "INSERT INTO chunks VALUES (?, ?, ?, ?, ?)",
            ("c0", "Legacy Section", "reference", "noiseTOP", "noiseTOP content"),
        )
        conn.execute(
            "INSERT INTO chunks_fts (rowid, chunk_id, section_title, operator_name, content) "
            "VALUES (1, 'c0', 'Legacy Section', 'noiseTOP', 'noiseTOP content')"
        )
        conn.commit()
    finally:
        conn.close()

    matches = kb._query_sqlite_fts(db_path, "noiseTOP", 5)
    # Should return at least one match via the v0 fallback query
    assert isinstance(matches, list)
    assert len(matches) >= 1


# ---------------------------------------------------------------------------
# Test 7 — FTS injection safety
# ---------------------------------------------------------------------------


def test_fts_syntax_injection_safe(tmp_path):
    """_fts_quote sanitises pathological inputs and never crashes FTS5."""
    # These must produce non-empty, double-quoted OR-expressions
    for raw in ['"; DROP TABLE chunks; --', "noiseTOP()", "star*wildcard", "key:value"]:
        result = kb._fts_quote(raw)
        assert result != "", f"Expected non-empty output for: {raw!r}"
        # Every term should be wrapped in double quotes
        terms = result.split(" OR ")
        for term in terms:
            assert term.startswith('"') and term.endswith('"'), f"Term not quoted: {term!r} from {raw!r}"

    # Empty / whitespace-only input must return ""
    assert kb._fts_quote("") == ""
    assert kb._fts_quote("   ") == ""
    assert kb._fts_quote(None) == ""

    # Verify real DB doesn't crash on pathological input
    db_path = tmp_path / "docsbrain.db"
    _build_v1_brain(db_path, "test", _make_chunks(2))

    for malicious in ['"); DROP TABLE chunks; --', "(", "*", ":"]:
        result = kb._query_sqlite_fts(db_path, malicious, 5)
        assert isinstance(result, list), f"Expected list, got {type(result)} for {malicious!r}"


# ---------------------------------------------------------------------------
# Test 8 — BM25 path works when no SQLite corpora present
# ---------------------------------------------------------------------------


def test_bm25_path_still_works_when_no_sqlite_corpora_present(tmp_path, monkeypatch):
    """handle_knowledge_search returns BM25 hits even with no brain.db."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"
    corpus_dir = root / "popx"
    _make_jsonl_corpus(corpus_dir, n_pages=3)

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    resp = kb.handle_knowledge_search({"query": "TouchDesigner operators", "top_k": 5})
    assert resp.get("ok") is True
    assert resp["count"] >= 1
    matches = resp["matches"]
    assert all("name" in m for m in matches)
    assert all("score" in m for m in matches)


# ---------------------------------------------------------------------------
# Bonus Test 9 — Merged BM25 + FTS results
# ---------------------------------------------------------------------------


def test_handle_knowledge_search_merges_bm25_and_fts(tmp_path, monkeypatch):
    """handle_knowledge_search merges BM25 and FTS results from both corpora."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"

    # JSONL-only corpus
    jsonl_dir = root / "popx"
    _make_jsonl_corpus(jsonl_dir, n_pages=2)

    # SQLite corpus
    sqlite_dir = root / "derivative"
    sqlite_dir.mkdir(parents=True)
    _build_v1_brain(sqlite_dir / "docsbrain.db", "derivative", _make_chunks(3), trust_tier="official")

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    # Query that matches both corpora
    resp = kb.handle_knowledge_search({"query": "noiseTOP", "top_k": 10})
    assert resp.get("ok") is True
    sources = {m.get("corpus", m.get("source", "")) for m in resp["matches"]}
    # FTS hits come from derivative
    assert "derivative" in sources


# ---------------------------------------------------------------------------
# Bonus Test 10 — _corpora_summary kind ordering
# ---------------------------------------------------------------------------


def test_corpora_summary_kind_field(tmp_path, monkeypatch):
    """_corpora_summary returns sqlite entries before jsonl entries."""
    _clear_module_caches(monkeypatch)
    root = tmp_path / "normalized"

    # JSONL corpus (alphabetically before derivative)
    jsonl_dir = root / "aaapopx"
    _make_jsonl_corpus(jsonl_dir, n_pages=2)

    # SQLite corpus (alphabetically after aaapopx)
    sqlite_dir = root / "derivative"
    sqlite_dir.mkdir(parents=True)
    _build_v1_brain(sqlite_dir / "docsbrain.db", "derivative", _make_chunks(2))

    monkeypatch.setattr(kb, "_EXTERNAL_CORPORA_ROOTS", (root,))

    summary = kb._corpora_summary()
    kinds = [e["kind"] for e in summary]

    # All sqlite entries must precede all jsonl entries
    last_sqlite = max((i for i, k in enumerate(kinds) if k == "sqlite"), default=-1)
    first_jsonl = min((i for i, k in enumerate(kinds) if k == "jsonl"), default=len(kinds))
    assert last_sqlite < first_jsonl, f"sqlite entries must come before jsonl. kinds={kinds}"
    assert "sqlite" in kinds
    assert "jsonl" in kinds
