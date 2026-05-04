"""Tests for the shared Chunk Schema v1 helpers in
``scripts/_chunk_schema_v1.py``.

Phase 1.5 — pin the canonical contract every brain builder follows.
See ``docs/CHUNK_SCHEMA.md`` for the full spec.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_v1_helpers():
    spec = importlib.util.spec_from_file_location(
        "chunk_schema_v1_module",
        REPO_ROOT / "scripts" / "_chunk_schema_v1.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["chunk_schema_v1_module"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def v1():
    return _load_v1_helpers()


# ---------------------------------------------------------------------------
# enrich_to_v1
# ---------------------------------------------------------------------------


def test_enrich_fills_required_fields(v1):
    """Bare-minimum chunk gains every required v1 field."""
    bare = {"page_id": "p1", "content": "noiseTOP usage"}
    out = v1.enrich_to_v1(bare, trust_tier="official", source="html", brain_id="derivative")
    assert out["schema_version"] == 1
    assert out["trust_tier"] == "official"
    assert out["source"] == "html"
    assert out["url"] == ""
    assert out["title"] == ""  # no source title
    assert out["section_title"] == ""
    assert out["doc_type"] == "general"
    assert out["chunk_id"] == "p1__0000"  # derived from page_id + offset 0
    assert out["text_hash"] == v1._sha256_text("noiseTOP usage")
    assert out["mentioned_operators"] == []
    assert out["parameter_names"] == []
    assert out["python_symbols"] == []
    assert out["headings"] == []
    assert out["code_blocks"] == []


def test_enrich_preserves_existing_explicit_values(v1):
    """Builder-set values aren't overwritten."""
    rich = {
        "chunk_id": "explicit_id",
        "page_id": "p7",
        "title": "Custom Title",
        "section_title": "Custom Title",
        "url": "https://example.com/x",
        "source": "transcript",
        "doc_type": "tutorial",
        "trust_tier": "transcript",
        "text_hash": "deadbeef",
        "content": "ignored for hash test",
        "operator_name": "noiseTOP",
        "operator_family": "TOP",
    }
    out = v1.enrich_to_v1(rich, trust_tier="official", source="html", brain_id="d")
    assert out["chunk_id"] == "explicit_id"
    assert out["title"] == "Custom Title"
    assert out["url"] == "https://example.com/x"
    assert out["source"] == "transcript"
    assert out["trust_tier"] == "transcript"
    # Hash NOT recomputed when explicit.
    assert out["text_hash"] == "deadbeef"


def test_enrich_validates_trust_tier_falls_back_on_unknown(v1):
    """Unknown trust_tier falls back to DEFAULT_TRUST_TIER."""
    out = v1.enrich_to_v1(
        {"page_id": "p", "content": "x"},
        trust_tier="not-a-real-tier",
        source="html",
        brain_id="b",
    )
    assert out["trust_tier"] == v1.DEFAULT_TRUST_TIER
    assert out["trust_tier"] == "bundled"


def test_enrich_is_idempotent(v1):
    """Enriching a v1 chunk yields the same chunk."""
    base = v1.enrich_to_v1(
        {"page_id": "p", "content": "abc"},
        trust_tier="official",
        source="html",
        brain_id="d",
    )
    again = v1.enrich_to_v1(base, trust_tier="official", source="html", brain_id="d")
    assert base == again


def test_enrich_promotes_section_title_to_title(v1):
    """v0 chunks have section_title; v1 mirrors it as title."""
    out = v1.enrich_to_v1(
        {"page_id": "p", "section_title": "noiseTOP", "content": "x"},
        trust_tier="official",
        source="html",
        brain_id="d",
    )
    assert out["title"] == "noiseTOP"
    assert out["section_title"] == "noiseTOP"


def test_enrich_infers_operator_family(v1):
    out = v1.enrich_to_v1(
        {"page_id": "p", "content": "x", "operator_name": "noiseTOP"},
        trust_tier="official",
        source="html",
        brain_id="d",
    )
    assert out["operator_family"] == "TOP"


def test_enrich_does_not_mutate_input(v1):
    inp = {"page_id": "p", "content": "x"}
    v1.enrich_to_v1(inp, trust_tier="official", source="html", brain_id="d")
    assert inp == {"page_id": "p", "content": "x"}


def test_enrich_text_hash_is_sha256_hex(v1):
    out = v1.enrich_to_v1(
        {"page_id": "p", "content": "hello"},
        trust_tier="official",
        source="html",
        brain_id="d",
    )
    assert out["text_hash"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


# ---------------------------------------------------------------------------
# build_v1_fts_index
# ---------------------------------------------------------------------------


def _seed_chunks():
    return [
        {
            "page_id": "noisetop",
            "title": "noiseTOP",
            "doc_type": "operator",
            "operator_name": "noiseTOP",
            "parameter_names": ["amplitude", "period"],
            "content": "Generates noise textures on the GPU.",
        },
        {
            "page_id": "leveltop",
            "title": "levelTOP",
            "doc_type": "operator",
            "operator_name": "levelTOP",
            "parameter_names": ["brightness", "contrast"],
            "content": "Adjusts brightness, contrast, and levels.",
        },
        {
            "page_id": "guide_audio_reactive",
            "title": "Audio Reactive Workflows",
            "doc_type": "guide",
            "mentioned_operators": ["audiodeviceinCHOP", "noiseTOP"],
            "content": "How to wire CHOPs into TOP parameters for audio-reactive visuals.",
        },
    ]


def test_build_index_emits_v1_columns(v1, tmp_path):
    """The chunks table must have every v1 column."""
    db = tmp_path / "test.db"
    n = v1.build_v1_fts_index(_seed_chunks(), db, brain_id="testbrain", trust_tier="official")
    assert n == 3

    conn = sqlite3.connect(str(db))
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    finally:
        conn.close()

    must_have = {
        # v1 additions
        "trust_tier",
        "text_hash",
        "schema_version",
        "chunk_offset",
        "chunk_total",
        "headings",
        "code_blocks",
        "timestamp_url",
        "timestamp_seconds",
        "title",
        # v0 carryovers
        "chunk_id",
        "page_id",
        "section_title",
        "url",
        "source",
        "doc_type",
        "operator_family",
        "operator_name",
        "mentioned_operators",
        "parameter_names",
        "python_symbols",
        "build_number",
        "build_date",
        "change_category",
        "token_estimate",
        "content",
    }
    missing = must_have - cols
    assert not missing, f"v1 chunks table missing columns: {missing}"


def test_build_index_writes_meta_rows(v1, tmp_path):
    db = tmp_path / "meta.db"
    v1.build_v1_fts_index(_seed_chunks(), db, brain_id="testbrain", trust_tier="official")
    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    assert rows["schema_version"] == "1"
    assert rows["brain_id"] == "testbrain"
    assert rows["brain_name"] == "testbrain"  # backwards-compat alias
    assert rows["trust_tier"] == "official"


def test_build_index_extra_meta_merges(v1, tmp_path):
    """Phase 1.6 will pass extra_meta — confirm it merges cleanly."""
    db = tmp_path / "extra_meta.db"
    v1.build_v1_fts_index(
        _seed_chunks(),
        db,
        brain_id="testbrain",
        trust_tier="official",
        extra_meta={"display_name": "Test Brain", "source_url": "https://x.example"},
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    assert rows["display_name"] == "Test Brain"
    assert rows["source_url"] == "https://x.example"


def test_build_index_fts_search_returns_results(v1, tmp_path):
    """End-to-end: write a brain, run an FTS query, get a hit."""
    db = tmp_path / "search.db"
    v1.build_v1_fts_index(_seed_chunks(), db, brain_id="testbrain", trust_tier="official")

    conn = sqlite3.connect(str(db))
    try:
        rows = list(
            conn.execute(
                """SELECT c.chunk_id, c.title, c.trust_tier
                   FROM chunks_fts f JOIN chunks c ON c.chunk_id = f.chunk_id
                   WHERE f.content MATCH ?""",
                ("noise",),
            )
        )
    finally:
        conn.close()

    chunk_ids = {row[0] for row in rows}
    assert chunk_ids, "no FTS hits for 'noise'"
    assert all(row[2] == "official" for row in rows)


def test_build_index_accepts_jsonl_path(v1, tmp_path):
    """Builders that emit chunks.jsonl can pass the Path directly."""
    chunks_path = tmp_path / "chunks.jsonl"
    chunks_path.write_text(
        "\n".join(json.dumps(c) for c in _seed_chunks()) + "\n",
        encoding="utf-8",
    )
    db = tmp_path / "from_jsonl.db"
    n = v1.build_v1_fts_index(chunks_path, db, brain_id="testbrain", trust_tier="bundled")
    assert n == 3


def test_build_index_replaces_existing_db(v1, tmp_path):
    """Re-running on the same path wipes the old DB."""
    db = tmp_path / "replace.db"
    v1.build_v1_fts_index(_seed_chunks(), db, brain_id="b1", trust_tier="bundled")
    v1.build_v1_fts_index(_seed_chunks(), db, brain_id="b2", trust_tier="official")
    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    assert rows["brain_id"] == "b2"


def test_build_index_chunks_get_text_hash(v1, tmp_path):
    """Schema-v1 contract: every chunk has a SHA-256 text_hash."""
    db = tmp_path / "hashed.db"
    v1.build_v1_fts_index(_seed_chunks(), db, brain_id="b", trust_tier="bundled")
    conn = sqlite3.connect(str(db))
    try:
        hashes = [row[0] for row in conn.execute("SELECT text_hash FROM chunks")]
    finally:
        conn.close()
    assert len(hashes) == 3
    for h in hashes:
        # 64-char hex string
        assert isinstance(h, str)
        assert len(h) == 64
        int(h, 16)  # raises if non-hex


# ---------------------------------------------------------------------------
# Builder→shared-helper coupling. Locks in that every builder routes its
# index step through the shared v1 helpers — catches regressions where
# someone re-introduces a divergent inline indexer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "script_path",
    [
        "scripts/build_brain.py",
        "scripts/build_docs_brain.py",
        "scripts/build_tutorial_brain.py",
    ],
)
def test_builder_imports_shared_v1_helpers(script_path):
    """Every builder must import from ``_chunk_schema_v1`` and expose a
    ``build_fts_index`` that ultimately delegates to ``build_v1_fts_index``.
    """
    text = (REPO_ROOT / script_path).read_text("utf-8")
    assert "from _chunk_schema_v1 import" in text, f"{script_path} missing v1 import"
    assert "build_v1_fts_index" in text, f"{script_path} doesn't reference build_v1_fts_index"


# ---------------------------------------------------------------------------
# Phase 1.6 — meta table for self-description.
# ---------------------------------------------------------------------------


def test_build_index_auto_stamps_build_date_and_chunk_count(v1, tmp_path):
    """The shared indexer must always populate build_date + chunk_count
    in the meta table — Phase 1.6 contract.
    """
    db = tmp_path / "auto_meta.db"
    n = v1.build_v1_fts_index(_seed_chunks(), db, brain_id="b", trust_tier="bundled")
    assert n == 3

    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()

    assert rows["chunk_count"] == "3"
    assert "build_date" in rows
    # Format: ISO 8601 UTC, e.g. "2026-05-05T12:34:56Z"
    bd = rows["build_date"]
    assert len(bd) >= 19
    assert bd.endswith("Z")
    assert "T" in bd
    # builder_version is also stamped automatically.
    assert rows["builder_version"] == v1.BUILDER_VERSION


def test_build_index_extra_meta_overrides_auto_stamp(v1, tmp_path):
    """Caller-provided values in extra_meta beat the auto-stamps."""
    db = tmp_path / "override.db"
    v1.build_v1_fts_index(
        _seed_chunks(),
        db,
        brain_id="b",
        trust_tier="bundled",
        extra_meta={
            "build_date": "2030-01-01T00:00:00Z",
            "builder_version": "test-99.99",
            "chunk_count": "999",  # explicit override even though we'd normally compute
        },
    )
    conn = sqlite3.connect(str(db))
    try:
        rows = dict(conn.execute("SELECT key, value FROM meta"))
    finally:
        conn.close()
    assert rows["build_date"] == "2030-01-01T00:00:00Z"
    assert rows["builder_version"] == "test-99.99"
    assert rows["chunk_count"] == "999"


def test_build_index_writes_phase_16_self_description_keys(v1, tmp_path):
    """Spec: each brain.db meta should accommodate display_name,
    description, source_url, source_type, builder_name.
    """
    db = tmp_path / "selfdesc.db"
    v1.build_v1_fts_index(
        _seed_chunks(),
        db,
        brain_id="testbrain",
        trust_tier="official",
        extra_meta={
            "display_name": "Test Brain",
            "description": "Test brain for unit tests.",
            "source_url": "https://example.com/docs",
            "source_type": "html",
            "builder_name": "build_brain.py",
        },
    )
    rows = v1.read_brain_meta(db)
    # Required v1.6 self-description keys
    assert rows["display_name"] == "Test Brain"
    assert rows["description"] == "Test brain for unit tests."
    assert rows["source_url"] == "https://example.com/docs"
    assert rows["source_type"] == "html"
    assert rows["builder_name"] == "build_brain.py"
    # Required core keys
    assert rows["brain_id"] == "testbrain"
    assert rows["trust_tier"] == "official"
    assert rows["schema_version"] == "1"


def test_read_brain_meta_returns_empty_for_missing_db(v1, tmp_path):
    """Missing file is not an error — returns {} so callers can fall
    back to filename heuristics gracefully.
    """
    assert v1.read_brain_meta(tmp_path / "does_not_exist.db") == {}


def test_read_brain_meta_returns_empty_for_legacy_db_without_meta_table(v1, tmp_path):
    """A pre-v1 brain.db that has chunks but no meta table must not
    crash the reader — it's a legitimate state for older corpora.
    """
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE chunks (chunk_id TEXT, content TEXT)")
        conn.execute("INSERT INTO chunks VALUES ('x', 'hello')")
        conn.commit()
    finally:
        conn.close()
    assert v1.read_brain_meta(db) == {}


def test_read_brain_meta_round_trips(v1, tmp_path):
    """write → read identity check."""
    db = tmp_path / "rt.db"
    v1.build_v1_fts_index(
        _seed_chunks(),
        db,
        brain_id="b",
        trust_tier="official",
        extra_meta={"display_name": "B", "source_url": "https://b.example"},
    )
    meta = v1.read_brain_meta(db)
    assert meta["brain_id"] == "b"
    assert meta["display_name"] == "B"
    assert meta["source_url"] == "https://b.example"
    assert meta["trust_tier"] == "official"
    assert meta["schema_version"] == "1"
    assert meta["chunk_count"] == "3"


def test_build_brain_make_chunk_emits_v1_record():
    """build_brain.py's _make_chunk helper must emit a Chunk Schema v1 record.

    Smoke-loads the script as a module and exercises _make_chunk with
    minimal args; confirms every required v1 field is populated.
    """
    spec = importlib.util.spec_from_file_location(
        "build_brain_for_v1_test",
        REPO_ROOT / "scripts" / "build_brain.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    chunk = module._make_chunk(
        page_id="testpage",
        seq=1,
        section_title="noiseTOP",
        doc_type="operator",
        operator_name="noiseTOP",
        content="Generates noise textures.",
        url="https://docs.derivative.ca/noiseTOP",
        trust_tier="official",
    )

    # Required v1 fields
    for field in (
        "chunk_id",
        "page_id",
        "title",
        "url",
        "source",
        "doc_type",
        "trust_tier",
        "text_hash",
        "schema_version",
        "content",
    ):
        assert field in chunk, f"_make_chunk missing v1 required field {field!r}"

    assert chunk["schema_version"] == 1
    assert chunk["trust_tier"] == "official"
    assert chunk["title"] == "noiseTOP"
    assert chunk["operator_family"] == "TOP"  # inferred from name suffix
    assert len(chunk["text_hash"]) == 64  # SHA-256 hex
