"""Chunk Schema v1 — shared helpers for every TDPilot brain builder.

See ``docs/CHUNK_SCHEMA.md`` for the canonical contract.

This module exposes:
  - ``CHUNK_SCHEMA_VERSION`` — integer ``1``.
  - ``enrich_to_v1(chunk, *, trust_tier, source, brain_id)`` — fill in
    required v1 fields when a builder emitted only a v0-shaped chunk.
  - ``build_v1_fts_index(chunks_iter, db_path, brain_id, trust_tier)``
    — write a brain.db with the v1 ``chunks`` + ``chunks_fts`` + ``meta``
    schema and populate it from any iterator of chunk dicts.

The v1 schema is **strictly additive** over v0 at the SQL level: every
v0 column is preserved, with new columns appended. Existing v0 readers
see exactly the columns they expect.

Trust tiers (lowest-priv to highest-trust):
    experimental < transcript < community < personal < bundled < official

Builders pass their per-brain default into ``trust_tier``; the runtime
surfaces it on every search hit (Phase 3.2).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

CHUNK_SCHEMA_VERSION = 1

# Builder version. Bumped only when the shared indexer's behaviour
# changes in a way that producers/consumers must coordinate around.
# Different from CHUNK_SCHEMA_VERSION: the schema can stay at v1 while
# the builder fixes a chunking bug, in which case BUILDER_VERSION
# bumps but old brains still read fine.
BUILDER_VERSION = "1.0"

VALID_TRUST_TIERS = (
    "official",
    "bundled",
    "personal",
    "community",
    "transcript",
    "experimental",
)

DEFAULT_TRUST_TIER = "bundled"

# Recognised values for the meta.source_type field. Free-form is
# permitted; this list documents the canonical set so runtime UIs can
# special-case display ("video" vs "html docs" labelling).
KNOWN_SOURCE_TYPES = ("html", "youtube", "transcript", "docs", "markdown", "mixed")


# ---------------------------------------------------------------------------
# Field enrichment
# ---------------------------------------------------------------------------


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _operator_family_from_name(operator_name: str | None) -> str | None:
    """Infer family suffix from the camelCase operator name.

    Returns one of TOP / CHOP / SOP / DAT / COMP / MAT / POP, or None.
    """
    if not operator_name:
        return None
    for fam in ("TOP", "CHOP", "SOP", "DAT", "COMP", "MAT", "POP"):
        if operator_name.endswith(fam):
            return fam
    return None


def enrich_to_v1(
    chunk: dict[str, Any],
    *,
    trust_tier: str,
    source: str,
    brain_id: str,
) -> dict[str, Any]:
    """Return ``chunk`` augmented with every v1 required field.

    Only fills fields that are missing or empty — explicit values from
    the builder are preserved. Idempotent: enriching an already-v1
    chunk yields the same chunk.

    Args:
        chunk: The builder's emitted chunk dict. May have only v0 fields.
        trust_tier: Per-brain trust label. Validated against
            ``VALID_TRUST_TIERS``; falls back to ``DEFAULT_TRUST_TIER``
            if unknown.
        source: Builder-side source label (e.g. ``"html"``,
            ``"transcript"``, ``"toeexpand"``).
        brain_id: The brain identifier (e.g. ``"derivative"``,
            ``"popx"``). Used as a fallback for the ``page_id`` if
            missing — should never happen but keeps the function
            total.

    Returns:
        A new dict; the input is not mutated.
    """
    out = dict(chunk)

    # Trust tier — validate, default if bogus.
    tier = trust_tier if trust_tier in VALID_TRUST_TIERS else DEFAULT_TRUST_TIER
    out.setdefault("trust_tier", tier)
    if out.get("trust_tier") not in VALID_TRUST_TIERS:
        out["trust_tier"] = tier

    # Schema version stamp.
    out["schema_version"] = CHUNK_SCHEMA_VERSION

    # Source provenance.
    if not out.get("source"):
        out["source"] = source

    # URL — empty string is a valid sentinel for "no public URL".
    if "url" not in out:
        out["url"] = ""

    # Title canonicalisation. v0 used `section_title`; v1 promotes
    # `title` to the canonical name and mirrors it back into
    # `section_title` for SQL compatibility.
    if not out.get("title"):
        out["title"] = out.get("section_title") or ""
    if not out.get("section_title"):
        out["section_title"] = out["title"]

    # Doc type fallback.
    if not out.get("doc_type"):
        out["doc_type"] = "general"

    # IDs.
    if not out.get("page_id"):
        out["page_id"] = brain_id
    if not out.get("chunk_id"):
        offset = out.get("chunk_offset", 0)
        out["chunk_id"] = f"{out['page_id']}__{int(offset):04d}"

    # Content + hash. The hash is over the post-enrichment content so
    # rebuilds with stable text yield stable hashes.
    body = out.get("content", "")
    if not isinstance(body, str):
        body = str(body)
        out["content"] = body
    if not out.get("text_hash"):
        out["text_hash"] = _sha256_text(body)

    # Operator family inference (cheap, idempotent).
    if "operator_family" not in out or out["operator_family"] is None:
        out["operator_family"] = _operator_family_from_name(out.get("operator_name"))

    # List defaults — use [] not None so JSON serialisation is stable.
    for list_field in ("mentioned_operators", "parameter_names", "python_symbols", "headings"):
        if out.get(list_field) is None:
            out[list_field] = []

    # code_blocks default.
    if out.get("code_blocks") is None:
        out["code_blocks"] = []

    return out


# ---------------------------------------------------------------------------
# SQLite indexer
# ---------------------------------------------------------------------------


_CREATE_CHUNKS_V1 = """
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    page_id TEXT,
    title TEXT,
    section_title TEXT,
    url TEXT,
    source TEXT,
    doc_type TEXT,
    trust_tier TEXT,
    text_hash TEXT,
    schema_version INTEGER,
    chunk_offset INTEGER,
    chunk_total INTEGER,
    headings TEXT DEFAULT '[]',
    code_blocks TEXT DEFAULT '[]',
    timestamp_url TEXT,
    timestamp_seconds INTEGER,
    operator_family TEXT,
    operator_name TEXT,
    mentioned_operators TEXT DEFAULT '[]',
    parameter_names TEXT DEFAULT '[]',
    python_symbols TEXT DEFAULT '[]',
    build_number TEXT,
    build_date TEXT,
    change_category TEXT,
    token_estimate INTEGER,
    content TEXT
)
"""

_CREATE_FTS_V1 = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_id UNINDEXED,
    title,
    operator_name,
    parameter_names,
    python_symbols,
    content,
    tokenize='porter unicode61'
)
"""
# NOTE: deliberately NOT using ``content=''`` (contentless mode). With
# ``content=''``, SELECT from the FTS table returns NULLs for stored
# columns — only the MATCH operator works. Keeping a stored copy costs
# ~2× disk on the FTS index but lets the runtime JOIN against the
# chunks table by chunk_id without rowid choreography. Brain DBs cap
# at ~200MB; the trade-off is fine.

_CREATE_META = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
)
"""

# Column order for INSERT must match this list exactly.
_CHUNKS_COLUMNS = (
    "chunk_id",
    "page_id",
    "title",
    "section_title",
    "url",
    "source",
    "doc_type",
    "trust_tier",
    "text_hash",
    "schema_version",
    "chunk_offset",
    "chunk_total",
    "headings",
    "code_blocks",
    "timestamp_url",
    "timestamp_seconds",
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
)


def _row_for_chunks(chunk: dict[str, Any]) -> tuple[Any, ...]:
    return (
        chunk["chunk_id"],
        chunk.get("page_id"),
        chunk.get("title"),
        chunk.get("section_title") or chunk.get("title"),
        chunk.get("url", ""),
        chunk.get("source", ""),
        chunk.get("doc_type", "general"),
        chunk.get("trust_tier", DEFAULT_TRUST_TIER),
        chunk.get("text_hash"),
        chunk.get("schema_version", CHUNK_SCHEMA_VERSION),
        chunk.get("chunk_offset"),
        chunk.get("chunk_total"),
        json.dumps(chunk.get("headings", [])),
        json.dumps(chunk.get("code_blocks", [])),
        chunk.get("timestamp_url"),
        chunk.get("timestamp_seconds"),
        chunk.get("operator_family"),
        chunk.get("operator_name"),
        json.dumps(chunk.get("mentioned_operators", [])),
        json.dumps(chunk.get("parameter_names", [])),
        json.dumps(chunk.get("python_symbols", [])),
        chunk.get("build_number"),
        chunk.get("build_date"),
        chunk.get("change_category"),
        chunk.get("token_estimate", 0),
        chunk.get("content", ""),
    )


def _iter_chunks(source: Iterable[dict[str, Any]] | Path) -> Iterator[dict[str, Any]]:
    """Accept either an in-memory iterable or a chunks.jsonl path."""
    if isinstance(source, Path):
        with source.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                yield json.loads(line)
    else:
        yield from source


def build_v1_fts_index(
    chunks_source: Iterable[dict[str, Any]] | Path,
    db_path: Path,
    *,
    brain_id: str,
    trust_tier: str = DEFAULT_TRUST_TIER,
    extra_meta: dict[str, str] | None = None,
) -> int:
    """Build a brain.db with the v1 chunks + chunks_fts + meta schema.

    Args:
        chunks_source: Either an iterable of chunk dicts, or a Path to
            a chunks.jsonl file.
        db_path: Output SQLite path. If exists, removed first for a
            clean rebuild.
        brain_id: Identifier stored in ``meta.brain_id``.
        trust_tier: Per-brain trust label. Inserted into every chunk
            row that doesn't already carry one (idempotent if chunks
            went through ``enrich_to_v1`` first).
        extra_meta: Optional additional rows for the ``meta`` table —
            Phase 1.6 will populate display_name, source_url, etc.

    Returns:
        Number of chunks indexed.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(_CREATE_CHUNKS_V1)
        conn.execute(_CREATE_FTS_V1)
        conn.execute(_CREATE_META)

        # Phase 1.6 self-description meta. ``schema_version`` /
        # ``brain_id`` / ``trust_tier`` are required and always written
        # here; ``build_date`` and ``builder_version`` are auto-stamped
        # but overridable via ``extra_meta``; ``chunk_count`` is written
        # AFTER the indexing loop because the count is only known then.
        meta_rows = {
            "schema_version": str(CHUNK_SCHEMA_VERSION),
            "brain_id": brain_id,
            # Legacy alias — older readers (the CLI's DocsBrain) may
            # look up "brain_name". Keep the alias until those readers
            # learn to read brain_id.
            "brain_name": brain_id,
            "trust_tier": trust_tier if trust_tier in VALID_TRUST_TIERS else DEFAULT_TRUST_TIER,
            "build_date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "builder_version": BUILDER_VERSION,
        }
        if extra_meta:
            # Caller-provided values override the auto-defaults.
            meta_rows.update(extra_meta)
        for k, v in meta_rows.items():
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (k, v),
            )

        insert_sql = (
            "INSERT OR REPLACE INTO chunks ("
            + ", ".join(_CHUNKS_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _CHUNKS_COLUMNS)
            + ")"
        )

        count = 0
        for raw in _iter_chunks(chunks_source):
            chunk = enrich_to_v1(
                raw,
                trust_tier=trust_tier,
                source=raw.get("source", "html"),
                brain_id=brain_id,
            )
            conn.execute(insert_sql, _row_for_chunks(chunk))
            conn.execute(
                """INSERT INTO chunks_fts
                   (chunk_id, title, operator_name, parameter_names,
                    python_symbols, content)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    chunk["chunk_id"],
                    chunk.get("title", ""),
                    " ".join(
                        filter(
                            None,
                            [
                                chunk.get("operator_name", ""),
                                *chunk.get("mentioned_operators", []),
                            ],
                        )
                    ),
                    " ".join(chunk.get("parameter_names", [])),
                    " ".join(chunk.get("python_symbols", [])),
                    chunk.get("content", ""),
                ),
            )
            count += 1

        # Phase 1.6 — chunk_count is only known after the loop; stamp
        # it now unless caller already provided one (some pipelines
        # know it ahead of time).
        if not extra_meta or "chunk_count" not in extra_meta:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                ("chunk_count", str(count)),
            )

        conn.commit()
        return count
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Read-side helper — used by the standalone runtime to surface a
# brain's identity + trust tier without filename heuristics.
# ---------------------------------------------------------------------------


def read_brain_meta(db_path: Path) -> dict[str, str]:
    """Return the `meta` table contents from a brain.db.

    Phase 1.6 contract: returns ``{}`` (not error) if the DB is missing,
    if the meta table doesn't exist (legacy v0 brain), or if the file
    can't be opened. Callers branch on key presence — they should never
    crash because a brain wasn't fully self-describing.

    Each call opens its own connection; the function is safe to invoke
    from any thread (sqlite3.connect is thread-affine, so a
    long-lived connection couldn't be shared anyway).
    """
    if not db_path.is_file():
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError:
        return {}
    try:
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        except sqlite3.DatabaseError:
            # No meta table — legacy / pre-v1 brain. Synthesise nothing;
            # the caller falls back to filename heuristics.
            return {}
        return {str(k): str(v) for k, v in rows if k}
    finally:
        conn.close()
