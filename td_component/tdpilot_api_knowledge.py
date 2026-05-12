"""TDPilot API — bundled knowledge corpus + BM25 retrieval.

Knowledge is split into two pools that are searched together:

  1. **Bundled corpus** — markdown files baked into the .tox at build
     time as textDATs inside a child `kb` container. Travels with the
     drag-drop binary; no repo or filesystem dependency. The build
     script copies every `td_component/knowledge/*.md` into a textDAT
     named `kb_<filename_without_ext>`.

  2. **User corpus** — markdown files the user drops into
     ``~/.tdpilot-api/knowledge/``. Same frontmatter schema as bundled
     entries. Override-by-name precedence: a user file with the same
     ``name`` field as a bundled entry replaces it.

Frontmatter schema (matches memory module):
    ---
    name: <human-readable title>
    description: <one-line hook for index>
    category: reference | guide | catalog | tutorial
    ---

    <markdown body>

Exposed handlers:
    handle_knowledge_search   BM25 query across both pools
    handle_knowledge_get      load a specific entry by name
    handle_knowledge_list     enumerate available entries with metadata
    handle_knowledge_add      drop a new entry into the user pool

The runtime injects a SHORT (under 1KB) "knowledge index hint" into
the system prompt — not the full content. The model calls
knowledge_search / knowledge_get when it needs specifics.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

# 2.1.3 — namespaced under ~/.tdpilot-dpsk4/api/knowledge with legacy
# ~/.tdpilot-api/knowledge fallback (see tdpilot_api_config.resolve_user_dir).
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    USER_KNOWLEDGE_DIR = resolve_user_dir("knowledge")
except ImportError:
    USER_KNOWLEDGE_DIR = Path.home() / ".tdpilot-api" / "knowledge"
KB_CONTAINER_NAME = "kb"
KB_DAT_PREFIX = "kb_"
# v2.4 / Phase B.4 — content_type controls pre-turn BM25 retrieval visibility.
# Knowledge entries are descriptive material by definition (operator docs,
# API surfaces, conventions). Default "reference" keeps them freely searchable
# — the same protection memory/recipes give to step lists doesn't apply to
# knowledge content unless the author explicitly opts in.
VALID_CONTENT_TYPES = ("instruction", "reference", "fact")
DEFAULT_CONTENT_TYPE = "reference"

# External "docsbrain" corpora — pre-normalised pages.jsonl files produced
# by the dpsk4 / Claude variants' build pipeline. We auto-discover any
# pages.jsonl under these roots and expose its pages as searchable
# knowledge entries (BM25 over the full text). Each page becomes one
# logical entry with title, description (first heading), category
# (doc_type), and full body text.
#
# These corpora are LARGE (popx: 58 pages, derivative: 2478 pages) so
# they're NOT included in get_knowledge_index_hint() — the system prompt
# stays small. They're searchable on demand via knowledge_search and
# loadable via knowledge_get.
_EXTERNAL_CORPORA_ROOTS = (
    Path.home() / ".tdpilot" / "data" / "normalized",
    Path.home() / ".tdpilot-dpsk4" / "data" / "normalized",
    Path.home() / ".tdpilot-api" / "data" / "normalized",
)

# Phase 1.1 — SQLite/FTS corpora. The dpsk4 / Claude Code variants'
# ``brains add <corpus>`` flow installs ``<corpus>brain.db`` (an FTS5
# SQLite DB) under one of the roots above. Filename is conventionally
# ``docsbrain.db`` for derivative-style HTML corpora and
# ``popxbrain.db`` / ``<id>brain.db`` for everything else; we discover
# any file matching ``*brain.db`` so future builders that pick a new
# convention still work without runtime changes.
_DB_GLOB_PATTERNS: tuple[str, ...] = ("*brain.db",)

# In-memory cache. Keyed by absolute pages.jsonl path. Re-parsed when the
# file mtime changes (cheap stat() check on each search).
_corpus_cache: dict[str, list[dict]] = {}
_corpus_mtime: dict[str, float] = {}

# Per-DB meta-table cache (Phase 1.6). Same mtime-keyed invalidation as
# the JSONL cache. Avoids re-opening + re-querying the meta table on
# every search call.
_brain_meta_cache: dict[str, dict[str, str]] = {}
_brain_meta_mtime: dict[str, float] = {}


def _ensure_user_dir() -> None:
    USER_KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "doc"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = text[3:end].strip("\n").strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, body


# ---------------------------------------------------------------------------
# Corpus discovery — combines bundled (textDATs) + user (filesystem)
# ---------------------------------------------------------------------------


def _bundled_entries() -> list[dict]:
    """Read kb_* textDATs from the COMP's `kb` container child.

    Falls back gracefully when running outside TD (no `parent()` global)
    or when the kb container is missing — both cases just produce zero
    bundled entries, which is fine for tests and older builds.
    """
    try:
        comp = parent()  # type: ignore[name-defined]
    except NameError:
        return []  # not running inside TD
    if comp is None:
        return []
    kb = comp.op(KB_CONTAINER_NAME)
    if kb is None:
        return []
    entries: list[dict] = []
    for child in kb.children:
        try:
            name = child.name
            if not name.startswith(KB_DAT_PREFIX):
                continue
            text = child.text or ""
            meta, body = _parse_frontmatter(text)
            entries.append(
                {
                    "source": "bundled",
                    "filename": name[len(KB_DAT_PREFIX) :] + ".md",  # logical name
                    "name": meta.get("name") or name,
                    "description": meta.get("description") or "",
                    "category": meta.get("category") or "reference",
                    # v2.4 / Phase B.4 — propagate content_type with default-on-read.
                    "content_type": (
                        meta.get("content_type") or DEFAULT_CONTENT_TYPE
                    ).strip().lower(),
                    "text": body,
                }
            )
        except Exception:
            continue
    return entries


def _user_entries() -> list[dict]:
    if not USER_KNOWLEDGE_DIR.is_dir():
        return []
    entries: list[dict] = []
    for p in sorted(USER_KNOWLEDGE_DIR.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            entries.append(
                {
                    "source": "user",
                    "filename": p.name,
                    "name": meta.get("name") or p.stem,
                    "description": meta.get("description") or "",
                    "category": meta.get("category") or "reference",
                    # v2.4 / Phase B.4 — propagate content_type with default-on-read.
                    "content_type": (
                        meta.get("content_type") or DEFAULT_CONTENT_TYPE
                    ).strip().lower(),
                    "text": body,
                }
            )
        except Exception:
            continue
    return entries


def _all_entries() -> list[dict]:
    """Bundled + user. User overrides bundled when names collide.

    Does NOT include external docsbrain corpora — those are too large
    to enumerate in the system prompt index. Use _all_entries_for_search()
    when you want everything.
    """
    bundled = _bundled_entries()
    user = _user_entries()
    user_names = {e["name"] for e in user}
    merged = [e for e in bundled if e["name"] not in user_names] + user
    return merged


# ---------------------------------------------------------------------------
# Phase 1.1 — SQLite/FTS corpus support
# ---------------------------------------------------------------------------


def _fts_quote(query: str) -> str:
    """Make ``query`` safe to drop into an FTS5 MATCH expression.

    Bare user input can break FTS5 — un-paired quotes raise
    ``OperationalError: malformed MATCH expression``, embedded
    parentheses are treated as grouping, ``*`` as a prefix wildcard,
    and so on. Mirrors the CLI's quoting strategy
    (src/td_mcp/knowledge/docsbrain): strip every special char, quote
    each remaining term, OR them.
    """
    cleaned = re.sub(r'["\(\)\*\^\{\}:]', " ", query or "")
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return ""
    return " OR ".join(f'"{t}"' for t in terms)


def _read_brain_meta_with_cache(db_path: Path) -> dict[str, str]:
    """Read the ``meta`` table from a brain.db, cached by mtime.

    Returns ``{}`` for missing files / pre-Phase-1.6 brains without a
    meta table — never raises.
    """
    if not db_path.is_file():
        return {}
    cache_key = str(db_path)
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        return {}
    cached = _brain_meta_cache.get(cache_key)
    if cached is not None and _brain_meta_mtime.get(cache_key) == mtime:
        return cached

    meta: dict[str, str] = {}
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError:
        return {}
    try:
        try:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
            meta = {str(k): str(v) for k, v in rows if k}
        except sqlite3.DatabaseError:
            # Legacy DB without a meta table — empty dict is the documented
            # graceful-degradation signal.
            meta = {}
    finally:
        conn.close()

    _brain_meta_cache[cache_key] = meta
    _brain_meta_mtime[cache_key] = mtime
    return meta


def _sqlite_corpus_descriptors() -> list[dict]:
    """Discover ``*brain.db`` files under the external corpora roots.

    Returns one descriptor per discovered DB:

        {
          "corpus": <dir_name>,           # canonical id (== meta.brain_id when present)
          "db_path": <Path>,
          "meta": dict[str, str],         # parsed meta table (may be empty)
          "trust_tier": str,              # from meta or fallback
          "display_name": str,            # from meta or corpus dir name
          "source_url": str,              # from meta when present
          "chunk_count": int,             # from meta when present, 0 otherwise
        }

    A directory containing BOTH ``pages.jsonl`` and a ``*brain.db``
    yields ONLY a SQLite descriptor here — the JSONL path is preferred
    away in :func:`_external_corpora_entries` so the runtime never
    surfaces the same corpus twice.
    """
    seen_dirs: set[str] = set()
    descriptors: list[dict] = []
    for root in _EXTERNAL_CORPORA_ROOTS:
        if not root.is_dir():
            continue
        try:
            corpus_dirs = sorted(root.iterdir())
        except OSError:
            continue
        for corpus_dir in corpus_dirs:
            if not corpus_dir.is_dir():
                continue
            if corpus_dir.name in seen_dirs:
                continue
            db_path: Path | None = None
            for pat in _DB_GLOB_PATTERNS:
                matches = sorted(corpus_dir.glob(pat))
                if matches:
                    db_path = matches[0]
                    break
            if db_path is None:
                continue
            seen_dirs.add(corpus_dir.name)
            meta = _read_brain_meta_with_cache(db_path)
            try:
                chunk_count = int(meta.get("chunk_count", "0") or "0")
            except (TypeError, ValueError):
                chunk_count = 0
            descriptors.append(
                {
                    "corpus": meta.get("brain_id") or corpus_dir.name,
                    "db_path": db_path,
                    "meta": meta,
                    "trust_tier": meta.get("trust_tier") or "bundled",
                    "display_name": meta.get("display_name") or corpus_dir.name,
                    "source_url": meta.get("source_url") or "",
                    "chunk_count": chunk_count,
                }
            )
    return descriptors


def _query_sqlite_fts(
    db_path: Path,
    query: str,
    limit: int,
    *,
    meta: dict[str, str] | None = None,
) -> list[dict]:
    """Run an FTS5 MATCH query against a brain.db.

    Returns match dicts in the same shape as :func:`_bm25_search`'s
    output (with extra ``url`` / ``trust_tier`` / ``corpus`` fields so
    callers can tier-rank results). Empty list on any failure —
    schema mismatch, malformed DB, FTS syntax errors, or empty query
    after sanitisation. Never raises.

    The JOIN uses ``c.rowid = fts.rowid`` because pre-v1 brains use
    contentless FTS5 (``content=''``) where FTS-side stored columns
    are NULL on SELECT. v1 brains store their own copy and the JOIN
    still works. Either way the chunks-table SELECT returns the real
    column values.
    """
    safe = _fts_quote(query)
    if not safe:
        return []
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError:
        return []
    try:
        # v1 SELECT first — pulls title/url/trust_tier directly. If
        # those columns don't exist (legacy DB), fall back to the
        # narrower v0 SELECT.
        try:
            rows = conn.execute(
                """SELECT c.chunk_id, c.title, c.section_title, c.url,
                          c.doc_type, c.trust_tier, c.operator_name,
                          c.content,
                          bm25(chunks_fts) AS rank
                   FROM chunks_fts
                   JOIN chunks c ON c.rowid = chunks_fts.rowid
                   WHERE chunks_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (safe, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            try:
                rows = conn.execute(
                    """SELECT c.chunk_id, NULL, c.section_title, NULL,
                              c.doc_type, NULL, c.operator_name,
                              c.content,
                              bm25(chunks_fts) AS rank
                       FROM chunks_fts
                       JOIN chunks c ON c.rowid = chunks_fts.rowid
                       WHERE chunks_fts MATCH ?
                       ORDER BY rank
                       LIMIT ?""",
                    (safe, limit),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                print(f"[tdpilot_API/knowledge] FTS query failed for {db_path}: {exc}")
                return []
        except sqlite3.DatabaseError as exc:
            print(f"[tdpilot_API/knowledge] FTS DB error for {db_path}: {exc}")
            return []
    finally:
        conn.close()

    # FTS5 bm25() returns NEGATIVE ranks; lower (more negative) = better
    # match. Convert to a positive [0, 1]-ish "score" the runtime
    # surfaces.
    meta_dict = meta or {}
    fallback_tier = meta_dict.get("trust_tier") or "bundled"
    corpus_id = meta_dict.get("brain_id") or db_path.parent.name
    display_name = meta_dict.get("display_name") or corpus_id

    matches: list[dict] = []
    for row in rows:
        chunk_id, title, section_title, url, doc_type, row_trust, op_name, content, rank = row
        try:
            score = 1.0 / (1.0 + abs(float(rank or 0)))
        except (TypeError, ValueError):
            score = 0.0
        body = content or ""
        snippet = body.strip().replace("\n", " ")[:280]
        name = title or section_title or chunk_id or ""
        description = section_title if title and section_title and title != section_title else ""
        matches.append(
            {
                "name": name,
                "category": doc_type or "reference",
                # v2.4 / Phase B.4 — SQLite FTS rows pre-date the
                # content_type field; the chunks table has no such
                # column. External corpora are reference material by
                # convention (operator docs, official references), so
                # surfacing them as "reference" matches their intent.
                "content_type": DEFAULT_CONTENT_TYPE,
                "description": description,
                "source": f"corpus:{corpus_id}",
                "score": round(score, 3),
                "snippet": snippet,
                "corpus": corpus_id,
                "url": url or "",
                "trust_tier": (row_trust or fallback_tier or "bundled"),
                "operator_name": op_name or "",
                "chunk_id": chunk_id,
                "display_name": display_name,
            }
        )
    return matches


def _external_corpora_entries() -> list[dict]:
    """Discover and load pages.jsonl files from known docsbrain roots.

    Lazy + cached — first call builds the cache; subsequent calls reuse
    it unless the source file's mtime changed. Each corpus's path is
    cached independently so adding a new corpus doesn't blow away
    existing ones.

    Phase 1.1 prefer-DB rule: when a corpus directory contains BOTH a
    ``pages.jsonl`` and a ``*brain.db``, this function skips the JSONL
    so the runtime doesn't surface the same corpus twice. The brain.db
    path is queried lazily via :func:`_query_sqlite_fts` at search time
    instead of being preloaded into memory.
    """
    # Build the set of corpus dir names we already serve from a
    # SQLite descriptor — those win over their pages.jsonl siblings.
    sqlite_dirs: set[str] = set()
    for desc in _sqlite_corpus_descriptors():
        try:
            sqlite_dirs.add(Path(desc["db_path"]).parent.name)
        except Exception:
            continue

    entries: list[dict] = []
    for root in _EXTERNAL_CORPORA_ROOTS:
        if not root.is_dir():
            continue
        try:
            for corpus_dir in sorted(root.iterdir()):
                if not corpus_dir.is_dir():
                    continue
                if corpus_dir.name in sqlite_dirs:
                    # SQLite descriptor handles this corpus.
                    continue
                pages_path = corpus_dir / "pages.jsonl"
                if not pages_path.is_file():
                    continue
                cache_key = str(pages_path)
                try:
                    mtime = pages_path.stat().st_mtime
                except OSError:
                    continue
                if _corpus_mtime.get(cache_key) == mtime and cache_key in _corpus_cache:
                    entries.extend(_corpus_cache[cache_key])
                    continue
                # Re-parse: file is new or has changed.
                corpus_name = corpus_dir.name
                corpus_entries = _parse_pages_jsonl(pages_path, corpus_name)
                _corpus_cache[cache_key] = corpus_entries
                _corpus_mtime[cache_key] = mtime
                entries.extend(corpus_entries)
        except Exception:
            # Per-root failure shouldn't take down the whole search.
            continue
    return entries


def _parse_pages_jsonl(path: Path, corpus_name: str) -> list[dict]:
    """Read a docsbrain pages.jsonl and convert to knowledge-entry dicts.

    Schema observed in production (popx + derivative):
      page_id, url, title, doc_type, headings, text, text_hash,
      operator_category?, operator_name?, operator_family?
    """
    out: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    page = json.loads(line)
                except json.JSONDecodeError:
                    continue
                title = page.get("title") or page.get("page_id") or ""
                text = page.get("text") or ""
                if not title or not text:
                    continue
                headings = page.get("headings") or []
                desc = headings[0] if headings else (page.get("url") or "")
                if isinstance(desc, str) and len(desc) > 200:
                    desc = desc[:197] + "..."
                out.append(
                    {
                        "source": f"corpus:{corpus_name}",
                        "filename": (page.get("page_id") or title) + ".md",
                        "name": title,
                        "description": desc,
                        "category": page.get("doc_type") or "reference",
                        # v2.4 / Phase B.4 — content_type. JSONL pages are
                        # descriptive corpora (operator docs, official
                        # references) — treat as "reference" by default.
                        "content_type": (
                            page.get("content_type") or DEFAULT_CONTENT_TYPE
                        ),
                        "text": text,
                        "url": page.get("url") or "",
                        "corpus": corpus_name,
                        # Phase 1.1 — JSONL corpora pre-date the meta
                        # table so we don't know their trust tier.
                        # Default to "bundled" — Phase 3.2 will branch
                        # on this when ranking results.
                        "trust_tier": "bundled",
                    }
                )
    except OSError:
        pass
    return out


def _corpora_summary() -> list[dict]:
    """Return ``[{corpus, pages, path, kind, trust_tier, ...}]``.

    Used by :func:`get_knowledge_index_hint` and
    :func:`handle_get_capabilities` to surface what the agent has
    available without enumerating thousands of pages. Phase 1.1 makes
    the summary kind-aware: jsonl-backed corpora carry ``"kind":
    "jsonl"``; SQLite-backed corpora carry ``"kind": "sqlite"`` plus
    the meta-table fields (display_name, trust_tier, source_url,
    chunk_count) when available.
    """
    summary: list[dict] = []
    seen: set[str] = set()

    # SQLite corpora first — they win the prefer-DB rule.
    for desc in _sqlite_corpus_descriptors():
        corpus = desc["corpus"]
        if corpus in seen:
            continue
        seen.add(corpus)
        meta = desc["meta"]
        summary.append(
            {
                "corpus": corpus,
                "pages": desc["chunk_count"],
                "path": str(desc["db_path"]),
                "kind": "sqlite",
                "trust_tier": desc["trust_tier"],
                "display_name": desc["display_name"],
                "source_url": desc["source_url"],
                "schema_version": meta.get("schema_version", "0"),
                "build_date": meta.get("build_date", ""),
                "builder_name": meta.get("builder_name", ""),
            }
        )

    # JSONL corpora — only if not already covered by a SQLite descriptor.
    for root in _EXTERNAL_CORPORA_ROOTS:
        if not root.is_dir():
            continue
        try:
            for corpus_dir in sorted(root.iterdir()):
                if not corpus_dir.is_dir():
                    continue
                pages_path = corpus_dir / "pages.jsonl"
                if not pages_path.is_file():
                    continue
                if corpus_dir.name in seen:
                    continue
                seen.add(corpus_dir.name)
                cache_key = str(pages_path)
                count = len(_corpus_cache.get(cache_key, []))
                # If never loaded, count via line scan — fast for jsonl.
                if count == 0:
                    try:
                        with open(pages_path, encoding="utf-8") as f:
                            count = sum(1 for line in f if line.strip())
                    except OSError:
                        count = 0
                summary.append(
                    {
                        "corpus": corpus_dir.name,
                        "pages": count,
                        "path": str(pages_path),
                        "kind": "jsonl",
                        # Legacy JSONL has no meta — best-effort defaults
                        # so callers can rely on these keys existing.
                        "trust_tier": "bundled",
                        "display_name": corpus_dir.name,
                        "source_url": "",
                        "schema_version": "0",
                    }
                )
        except Exception:
            continue
    return summary


def _all_entries_for_search() -> list[dict]:
    """Bundled + user + every external docsbrain corpus."""
    return _all_entries() + _external_corpora_entries()


# ---------------------------------------------------------------------------
# Public — system-prompt injection
# ---------------------------------------------------------------------------


def get_knowledge_index_hint() -> str:
    """Return a SHORT hint listing available knowledge entries — used in
    the system prompt to nudge the model toward calling knowledge_search.
    Deliberately tiny (under 1KB typically) so the prompt cache prefix
    stays stable and small.

    Bundled + user entries are listed by name. External docsbrain
    corpora (popx, derivative, etc.) are NOT enumerated — they're too
    large — but a one-line footer announces their presence + page
    counts so the agent knows knowledge_search will hit them.
    """
    entries = _all_entries()
    lines: list[str] = []
    for e in sorted(entries, key=lambda x: x["name"]):
        desc = e["description"]
        if desc:
            lines.append(f"- {e['name']} — {desc}")
        else:
            lines.append(f"- {e['name']}")

    # Append corpus summary if any external docsbrain corpora are present.
    summary = _corpora_summary()
    if summary:
        lines.append("")
        lines.append("External docsbrain corpora (searchable via knowledge_search, NOT auto-listed):")
        for c in summary:
            lines.append(f"  · {c['corpus']}: {c['pages']} pages")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_knowledge_search(body: dict) -> dict:
    query = (body.get("query") or "").strip()
    try:
        top_k = max(1, min(int(body.get("top_k", 3) or 3), 20))
    except (TypeError, ValueError):
        top_k = 3
    category = (body.get("category") or "").strip().lower() or None
    corpus_filter = (body.get("corpus") or "").strip().lower() or None

    if not query:
        return {"error": "Missing required field: query"}

    # In-memory pool: bundled + user + jsonl-backed external corpora.
    docs = _all_entries_for_search()
    if category:
        docs = [d for d in docs if d.get("category", "").lower() == category]
    if corpus_filter:
        # Filter by `source` ('bundled' / 'user' / 'corpus:<name>')
        # OR by `corpus` field (only set on docsbrain entries)
        docs = [
            d
            for d in docs
            if d.get("corpus", "").lower() == corpus_filter
            or d.get("source", "").lower() == f"corpus:{corpus_filter}"
            or d.get("source", "").lower() == corpus_filter
        ]

    bm25_matches = _bm25_search(query, docs, top_k) if docs else []

    # Phase 1.1 — query each SQLite-backed corpus via FTS5 in addition
    # to the in-memory BM25 above. Each corpus contributes up to
    # ``top_k`` candidates; we merge by score and trim to ``top_k``
    # globally. SQLite corpora carry trust_tier / url / chunk_id on
    # every match so the agent can weight evidence (Phase 3.2).
    sqlite_matches: list[dict] = []
    for desc in _sqlite_corpus_descriptors():
        if corpus_filter and desc["corpus"].lower() != corpus_filter:
            continue
        if category:
            # SQLite corpora carry a per-chunk doc_type column. We
            # filter post-query for simplicity — the result-set is
            # already capped at top_k per corpus.
            partial = _query_sqlite_fts(desc["db_path"], query, top_k, meta=desc["meta"])
            partial = [m for m in partial if (m.get("category") or "").lower() == category]
            sqlite_matches.extend(partial)
        else:
            sqlite_matches.extend(_query_sqlite_fts(desc["db_path"], query, top_k, meta=desc["meta"]))

    all_matches = bm25_matches + sqlite_matches
    if not all_matches:
        return {"ok": True, "count": 0, "matches": []}

    # Sort by score descending; trim to top_k. BM25 and FTS bm25() use
    # different scales but both are normalised into the [0, 1] range
    # before this point so the merge is meaningful.
    all_matches.sort(key=lambda m: m.get("score", 0.0), reverse=True)
    all_matches = all_matches[:top_k]

    return {"ok": True, "count": len(all_matches), "matches": all_matches}


def handle_knowledge_get(body: dict) -> dict:
    """Load full content of a knowledge entry by name. Searches local
    (bundled + user) first, then jsonl-backed external corpora, then
    SQLite-backed corpora. Title match wins over filename match.
    """
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "Missing required field: name"}
    for entry in _all_entries_for_search():
        if entry["name"] == name or entry["filename"] == name or entry["filename"] == name + ".md":
            return {
                "ok": True,
                "name": entry["name"],
                "filename": entry["filename"],
                "category": entry["category"],
                "description": entry["description"],
                "source": entry["source"],
                "url": entry.get("url", ""),
                "content": entry["text"],
                "trust_tier": entry.get("trust_tier", "bundled"),
            }

    # Phase 1.1 — fall back to SQLite-backed corpora. Match by title
    # (chunks_fts only stores indexed text); chunk_id-by-direct-equality
    # is checked too because handle_knowledge_search now surfaces
    # chunk_ids the model can echo back.
    for desc in _sqlite_corpus_descriptors():
        try:
            conn = sqlite3.connect(str(desc["db_path"]))
        except sqlite3.DatabaseError:
            continue
        try:
            try:
                row = conn.execute(
                    """SELECT chunk_id, COALESCE(title, section_title) AS title,
                              COALESCE(url, '') AS url,
                              COALESCE(doc_type, 'reference') AS category,
                              COALESCE(trust_tier, ?) AS trust_tier,
                              content
                       FROM chunks
                       WHERE chunk_id = ? OR title = ? OR section_title = ?
                       LIMIT 1""",
                    (desc["trust_tier"], name, name, name),
                ).fetchone()
            except sqlite3.OperationalError:
                # Legacy DB lacking title column — fall back.
                row = conn.execute(
                    """SELECT chunk_id, section_title AS title, '' AS url,
                              COALESCE(doc_type, 'reference') AS category,
                              ? AS trust_tier, content
                       FROM chunks
                       WHERE chunk_id = ? OR section_title = ?
                       LIMIT 1""",
                    (desc["trust_tier"], name, name),
                ).fetchone()
        finally:
            conn.close()
        if row:
            chunk_id, title, url, category, trust_tier, content = row
            return {
                "ok": True,
                "name": title or chunk_id,
                "filename": (chunk_id or title) + ".md",
                "category": category or "reference",
                # v2.4 / Phase B.4 — external corpora pre-date content_type;
                # default to "reference" (descriptive material).
                "content_type": DEFAULT_CONTENT_TYPE,
                "description": "",
                "source": f"corpus:{desc['corpus']}",
                "url": url or "",
                "content": content or "",
                "trust_tier": trust_tier or desc["trust_tier"],
            }

    return {"error": f"Knowledge entry not found: {name}"}


def handle_knowledge_list(body: dict) -> dict:
    """List local (bundled + user) entries by default. Pass
    include_corpora=true to also list external docsbrain entries — but
    that can be 2500+ entries from derivative + popx, so use sparingly.
    """
    category = (body.get("category") or "").strip().lower() or None
    include_corpora = bool(body.get("include_corpora", False))

    entries = _all_entries_for_search() if include_corpora else _all_entries()
    if category:
        entries = [e for e in entries if e.get("category", "").lower() == category]
    return {
        "ok": True,
        "count": len(entries),
        "entries": [
            {
                "name": e["name"],
                "description": e["description"],
                "category": e["category"],
                "source": e["source"],
                "filename": e["filename"],
                "size_bytes": len(e["text"].encode("utf-8")),
                "url": e.get("url", ""),
            }
            for e in sorted(entries, key=lambda x: x["name"])
        ],
        "corpora_summary": _corpora_summary() if include_corpora else None,
    }


def handle_knowledge_add(body: dict) -> dict:
    """Add a user-knowledge entry to ~/.tdpilot-api/knowledge/. Bundled
    entries are read-only — to override a bundled entry, just save a
    user entry with the same `name` field."""
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    category = (body.get("category") or "reference").strip().lower() or "reference"
    content = (body.get("content") or "").strip()
    # v2.4 / Phase B.4 — content_type opt-in. Default "reference" because
    # knowledge is descriptive material; an author who wants generic-query
    # hiding for an instruction-shaped knowledge entry can pass
    # content_type="instruction".
    content_type = (
        body.get("content_type") or DEFAULT_CONTENT_TYPE
    ).strip().lower()

    if not name:
        return {"error": "Missing required field: name"}
    if not content:
        return {"error": "Missing required field: content"}
    if content_type not in VALID_CONTENT_TYPES:
        return {
            "error": (
                f"Invalid content_type: {content_type!r}. "
                f"Must be one of {list(VALID_CONTENT_TYPES)}."
            ),
        }

    _ensure_user_dir()
    filename = _slugify(name) + ".md"
    filepath = USER_KNOWLEDGE_DIR / filename

    full = (
        f"---\nname: {name}\ndescription: {description}\n"
        f"category: {category}\ncontent_type: {content_type}\n---\n\n{content}\n"
    )
    filepath.write_text(full, encoding="utf-8")
    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "content_type": content_type,
    }


# ---------------------------------------------------------------------------
# BM25 wrapper — scoring lives in tdpilot_api_bm25 (extracted 2026-05-04).
# ---------------------------------------------------------------------------


def _bm25_search(query: str, docs: list[dict], top_k: int) -> list[dict]:
    """Score docs and return knowledge-shaped match dicts (category +
    source instead of memory's filename + type).

    Phase 1.1 — propagate ``trust_tier`` and ``url`` so jsonl-backed
    corpora's hits look uniform with SQLite hits at the agent layer.
    Bundled / user entries default to ``trust_tier="bundled"``;
    explicit values from the doc dict win.
    """
    from tdpilot_api_bm25 import bm25_score  # type: ignore[import-not-found]

    matches: list[dict] = []
    for doc, score in bm25_score(query, docs, top_k=top_k):
        text = doc.get("text") or ""
        snippet = text.strip().replace("\n", " ")[:280]
        matches.append(
            {
                "name": doc.get("name"),
                "category": doc.get("category"),
                # v2.4 / Phase B.4 — surface content_type for the pre-turn
                # retrieval filter in tdpilot_api_runtime._run_pre_turn_retrieval.
                "content_type": doc.get("content_type") or DEFAULT_CONTENT_TYPE,
                "description": doc.get("description"),
                "source": doc.get("source"),
                "score": round(score, 3),
                "snippet": snippet,
                "url": doc.get("url", ""),
                "trust_tier": doc.get("trust_tier", "bundled"),
                "corpus": doc.get("corpus", ""),
            }
        )
    return matches
