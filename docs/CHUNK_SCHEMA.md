# Chunk Schema v1

**Status:** authoritative · **Implemented in:** Phase 1.5 of the standalone implementation plan · **Used by:** every TDPilot brain builder.

This document is the contract for what a single chunk row looks like, both inside `chunks.jsonl` and inside the `chunks` table of a brain SQLite database. Every brain builder emits v1; the runtime reads v1 without per-source branching.

A "chunk" is a fragment of source material small enough to fit comfortably inside a search-result snippet (target ≤ ~600 tokens). One source page (HTML doc, video transcript, tutorial folder) typically yields multiple chunks.

---

## Required fields

Every chunk MUST carry these. Missing one is a builder bug.

| Field | Type | Description |
|---|---|---|
| `chunk_id` | str | Stable, deterministic identifier. Convention: `<page_id>__<slug>__<seq:04d>` or `<page_id>__<offset:04d>`. Must be globally unique within the brain. |
| `page_id` | str | Stable identifier for the parent page (URL hash, video id, doc path). All chunks from the same page share this. |
| `title` | str | Page or section title. Used in result snippets and ranking. (In v0 this was `section_title`; v1 surfaces `title` as the canonical spelling and mirrors it into `section_title` for backward-compat with v0 SQL.) |
| `url` | str | Canonical source URL. `""` is allowed for local-only sources (transcript files with no public URL). |
| `source` | str | Short builder-side label: `"html"` / `"transcript"` / `"toeexpand"` / `"youtube"` / etc. Lets the runtime explain provenance. |
| `doc_type` | str | Coarse category: `"operator"` / `"guide"` / `"tutorial"` / `"snippet"` / `"release_notes"` / `"general"`. Used for filtering. |
| `trust_tier` | str | One of: `official` / `bundled` / `personal` / `community` / `transcript` / `experimental`. Surfaced per-result so the agent can weight evidence. **NEW in v1.** |
| `text_hash` | str | SHA-256 hex digest of `content`. Used for change detection on rebuild. **NEW in v1 at chunk granularity** (v0 had it at page granularity). |
| `content` | str | The chunk body. Plain text or markdown. **Field name is `content`, NOT `text`** — kept for SQLite column compatibility with v0 brains. |
| `schema_version` | int | Always `1` for v1 chunks. **NEW in v1.** |

### Notes on required fields

- **`chunk_id` stability matters.** Same source → same chunk_id across rebuilds, so caches and cross-references survive.
- **`text_hash` excludes wrapper metadata.** Hash exactly the bytes of `content`, not headings, not titles, not URL.
- **`trust_tier` defaults.** Builders that don't accept a CLI flag still emit a default per-brain trust tier, sourced from the brain's YAML config (`trust_tier:`) or the script's hard-coded default. The runtime never sees `null`.

---

## Optional but recommended

Fields the runtime can use when present, treats as null/empty otherwise.

| Field | Type | Description |
|---|---|---|
| `chunk_offset` | int | Position within the parent page (0-indexed). |
| `chunk_total` | int | Total chunks for this page. Lets the agent know if there's more context to fetch. |
| `headings` | list[str] | Section heading trail (e.g. `["Operators", "TOPs", "noiseTOP"]`). |
| `code_blocks` | list[dict] | `[{"lang": str, "code": str, "line_offset": int}]` extracted from the chunk. |
| `timestamp_url` | str | Video chunks: URL with `?t=` fragment. Text chunks: equals `url`. |
| `timestamp_seconds` | int | Video chunks: start time in seconds. Text chunks: omitted. |
| `section_title` | str | Mirror of `title`. Kept for v0 SQL compatibility — runtime reads `title` first, falls back to `section_title`. |

---

## TD-specific metadata

Fields the runtime uses for TD-aware ranking and disambiguation. Builders extract these when possible; missing values default to `null`/`[]`.

| Field | Type | Description |
|---|---|---|
| `operator_name` | str \| null | e.g. `"noiseTOP"` if the chunk is operator-specific. |
| `operator_family` | str \| null | e.g. `"TOP"`. Inferred from `operator_name` suffix. |
| `parameter_names` | list[str] | Parameters mentioned in the chunk body. |
| `python_symbols` | list[str] | TD Python class/method names mentioned (e.g. `"Td.Project"`). |
| `mentioned_operators` | list[str] | All operator names appearing in the text (super-set of `operator_name`). |
| `token_estimate` | int | Rough token count for the chunk's `content`. Used for ranking + budget enforcement. |

### TD-specific from release-notes pages only

Used by `build_brain.py`'s `release_notes` doc_type:

| Field | Type | Description |
|---|---|---|
| `build_number` | str \| null | e.g. `"2026.10000"`. |
| `build_date` | str \| null | ISO-8601 date. |
| `change_category` | str \| null | `"Added"` / `"Fixed"` / `"Changed"` / etc. |

---

## SQLite layout

Every brain database carries:

### `chunks` table

One row per chunk. Columns mirror the canonical fields above. v1 adds:
- `trust_tier TEXT`
- `text_hash TEXT`
- `schema_version INTEGER`
- `chunk_offset INTEGER`
- `chunk_total INTEGER`
- `headings TEXT` (JSON-encoded list)
- `code_blocks TEXT` (JSON-encoded list)
- `timestamp_url TEXT`
- `timestamp_seconds INTEGER`

The v0 columns (`page_id`, `doc_type`, `section_title`, `operator_*`, `mentioned_operators`, `parameter_names`, `python_symbols`, `build_*`, `change_category`, `token_estimate`, `content`, `source`, `url`) are all preserved. v1 is **strictly additive** at the SQL level; existing v0 readers see exactly the columns they expect.

### `chunks_fts` virtual table

FTS5 contentless index. Indexed columns:

```
chunk_id UNINDEXED, title, operator_name, parameter_names,
python_symbols, content
```

Note `chunk_id` is `UNINDEXED` (we use it for joins, not search).

### `meta` table

`(key TEXT PRIMARY KEY, value TEXT)`. Phase 1.5 minimum:

```
schema_version → "1"
brain_id       → e.g. "derivative"
```

Phase 1.6 (separate item) extends this with `display_name`, `trust_tier`, `source_url`, `source_type`, `build_date`, `chunk_count`, `builder_version`.

---

## Migration: v0 → v1

A v0 brain (built before Phase 1.5 lands) is still readable — its chunks are missing the v1 required fields beyond what v0 already had. Concretely, the runtime sees:
- `trust_tier` missing → fall back to brain-level default (from config or `"bundled"`).
- `text_hash` missing → can't do change detection but search still works.
- `schema_version` missing → assume `0`.

There is no automatic migration in Phase 1.5. To upgrade an existing brain, rebuild it from source — the builders now emit v1.

---

## Why `content` and not `text`

The original sketch in the implementation plan used `text` for the chunk body field. Schema v1 keeps the v0 spelling `content` because:

1. The SQLite column is named `content` in every shipped brain.db. Renaming would require migrating every existing user brain or maintaining dual readers.
2. The CLI's search-side `DocsBrain` class queries `content`. Changing the field name in chunks.jsonl while keeping the column name is more confusing than just keeping both.
3. `content` is more conventional in chunk-store projects (Pinecone, Weaviate, etc. all use `content` or a synonym, not raw `text`).

If a future v2 schema migrates to `text`, it goes through a proper migration path.

---

## Builders that emit v1

- [`scripts/build_brain.py`](../scripts/build_brain.py) — generic, config-driven.
- [`scripts/build_docs_brain.py`](../scripts/build_docs_brain.py) — derivative.ca-specific.
- [`scripts/build_tutorial_brain.py`](../scripts/build_tutorial_brain.py) — video tutorial corpora.

All three rely on the shared helper module [`scripts/_chunk_schema_v1.py`](../scripts/_chunk_schema_v1.py) which exports:
- `enrich_to_v1(chunk, *, trust_tier, source, brain_id) -> dict` — adds the required v1 fields if missing.
- `build_v1_fts_index(chunks_iter, db_path, brain_id, trust_tier) -> int` — writes the v1 SQLite schema and populates from a chunk iterator.
- `CHUNK_SCHEMA_VERSION` — the integer `1`.

Tests pin parity in [`tests/test_chunk_schema_v1.py`](../tests/test_chunk_schema_v1.py).
