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
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

USER_KNOWLEDGE_DIR = Path.home() / ".tdpilot-api" / "knowledge"
KB_CONTAINER_NAME = "kb"
KB_DAT_PREFIX = "kb_"

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

# In-memory cache. Keyed by absolute pages.jsonl path. Re-parsed when the
# file mtime changes (cheap stat() check on each search).
_corpus_cache: dict[str, list[dict]] = {}
_corpus_mtime: dict[str, float] = {}


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


def _external_corpora_entries() -> list[dict]:
    """Discover and load pages.jsonl files from known docsbrain roots.

    Lazy + cached — first call builds the cache; subsequent calls reuse
    it unless the source file's mtime changed. Each corpus's path is
    cached independently so adding a new corpus doesn't blow away
    existing ones.
    """
    entries: list[dict] = []
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
                        "text": text,
                        "url": page.get("url") or "",
                        "corpus": corpus_name,
                    }
                )
    except OSError:
        pass
    return out


def _corpora_summary() -> list[dict]:
    """Return [{corpus, pages, path}] for the get_knowledge_index_hint
    footer. Cheap — counts come from the cache, no re-parse."""
    summary: list[dict] = []
    seen: set[str] = set()
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
    if not docs:
        return {"ok": True, "count": 0, "matches": []}

    matches = _bm25_search(query, docs, top_k)
    return {"ok": True, "count": len(matches), "matches": matches}


def handle_knowledge_get(body: dict) -> dict:
    """Load full content of a knowledge entry by name. Searches local
    (bundled + user) first, then external docsbrain corpora. Title
    match wins over filename match."""
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

    if not name:
        return {"error": "Missing required field: name"}
    if not content:
        return {"error": "Missing required field: content"}

    _ensure_user_dir()
    filename = _slugify(name) + ".md"
    filepath = USER_KNOWLEDGE_DIR / filename

    full = f"---\nname: {name}\ndescription: {description}\ncategory: {category}\n---\n\n{content}\n"
    filepath.write_text(full, encoding="utf-8")
    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
    }


# ---------------------------------------------------------------------------
# BM25 wrapper — scoring lives in tdpilot_api_bm25 (extracted 2026-05-04).
# ---------------------------------------------------------------------------


def _bm25_search(query: str, docs: list[dict], top_k: int) -> list[dict]:
    """Score docs and return knowledge-shaped match dicts (category +
    source instead of memory's filename + type)."""
    from tdpilot_api_bm25 import bm25_score  # type: ignore[import-not-found]

    matches: list[dict] = []
    for doc, score in bm25_score(query, docs, top_k=top_k):
        text = doc.get("text") or ""
        snippet = text.strip().replace("\n", " ")[:280]
        matches.append(
            {
                "name": doc.get("name"),
                "category": doc.get("category"),
                "description": doc.get("description"),
                "source": doc.get("source"),
                "score": round(score, 3),
                "snippet": snippet,
            }
        )
    return matches
