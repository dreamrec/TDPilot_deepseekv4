"""TDPilot API — persistent memory store (Claude Code-compatible).

File-based markdown memory with YAML-style frontmatter, indexed via
MEMORY.md. The pattern matches Claude Code's
``~/.claude/projects/<project>/memory/`` tree exactly so memory files
can be copied between Claude and tdpilot_API if desired.

Storage layout:
    ~/.tdpilot-api/memory/
    ├── MEMORY.md                 # auto-regenerated index, max 200 lines
    ├── feedback_<topic>.md       # corrections + validated approaches
    ├── project_<topic>.md        # current work, decisions, deadlines
    ├── user_<topic>.md           # user role, preferences, knowledge
    └── reference_<topic>.md      # pointers to external systems

Memory file format:
    ---
    name: human-readable title
    description: one-line hook used to decide relevance later
    type: feedback | project | user | reference
    ---

    <markdown content — instructions, facts, references>

Exposed handlers (consumed by the agent's dispatcher):
    handle_memory_save   write a new memory + refresh index
    handle_memory_get    load a memory's full text
    handle_memory_list   enumerate memories, optionally filter by type
    handle_memory_recall BM25 search across all memories
    handle_memory_delete remove a memory + refresh index

Plus ``get_memory_index_content()`` which the runtime calls when
building each turn's system prompt — that's what makes memory feel
ambient ("the agent already knows me") rather than something you
have to explicitly trigger.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

# 2.1.3 — `resolve_user_dir` returns the new ~/.tdpilot-dpsk4/api/memory
# location, falling back to the legacy ~/.tdpilot-api/memory if it
# already has content (so existing user memories continue to work).
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    MEMORY_DIR = resolve_user_dir("memory")
except ImportError:
    MEMORY_DIR = Path.home() / ".tdpilot-api" / "memory"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
VALID_TYPES = ("user", "feedback", "project", "reference")
# v2.4 / Phase B.4 — content_type controls pre-turn BM25 retrieval visibility.
# "instruction" entries are step lists / how-to guides — surfaced ONLY when
# the user explicitly names them, never on generic queries (closes the
# drive-by-tool-execution bug where the model treated retrieval hits as
# user-issued commands). "reference" / "fact" entries are descriptive and
# freely searchable. Legacy entries without this field default to
# "reference" on read (safest — they remain visible to BM25).
VALID_CONTENT_TYPES = ("instruction", "reference", "fact")
DEFAULT_CONTENT_TYPE = "reference"
MAX_INDEX_LINES = 200


def _ensure_dir() -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "memory"


def _safe_filename(name: str, mtype: str) -> str:
    """``<type>_<slug>.md`` — but DON'T double the prefix if the caller
    already included the type in the name.

    The agent often passes ``name='project_my_thing'`` (with the type
    prefix baked in) and ``type='project'`` separately. Without the
    dedup, _slugify would produce 'project_my_thing' and we'd build
    'project_project_my_thing.md' — a file the agent then can't
    retrieve via memory_get(name='project_my_thing.md') because the
    real filename is differently named.
    """
    slug = _slugify(name)
    type_clean = mtype if mtype in VALID_TYPES else "note"
    if slug.startswith(type_clean + "_"):
        return f"{slug}.md"
    return f"{type_clean}_{slug}.md"


def _find_memory_path(name: str) -> Path | None:
    """Locate a memory file by any of the names a caller might use.

    Tries in order:
      1. Exact filename (with .md)
      2. Name + .md extension
      3. ``<type>_<name>.md`` for each valid type (handles the "user
         passed a bare slug, file has type prefix" case)
      4. Strip a type prefix from the name and try again (handles
         "user passed 'project_X', file is 'X.md' for some reason")
    Returns the resolved Path or None if nothing matches.
    """
    name = (name or "").strip()
    if not name:
        return None
    candidates: list[str] = []
    if name.endswith(".md"):
        candidates.append(name)
    else:
        candidates.append(name + ".md")
        for mtype in VALID_TYPES:
            candidates.append(f"{mtype}_{name}.md")
            if name.startswith(mtype + "_"):
                candidates.append(name[len(mtype) + 1 :] + ".md")
    seen = set()
    for c in candidates:
        if c in seen:
            continue
        seen.add(c)
        p = MEMORY_DIR / c
        if p.is_file():
            return p
    return None


def _resolve_filename(name: str) -> str:
    """Legacy: returns the canonical filename for a name. Used for save
    paths and delete operations. For READS, prefer _find_memory_path
    which handles fuzzy lookup."""
    name = (name or "").strip()
    if not name:
        return ""
    if name.endswith(".md"):
        return name
    return name + ".md"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse ``---\\nkey: value\\n---\\n`` frontmatter. Returns
    (meta_dict, body_str). If no frontmatter is found, returns ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = text[3:end].strip("\n").strip()
    body = text[end + 4 :].lstrip("\n")  # +4 to skip "\n---"
    meta: dict[str, str] = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    return meta, body


def _build_index_text() -> str:
    """Regenerate MEMORY.md content from all memory files in MEMORY_DIR.

    Sorted alphabetically (deterministic, byte-stable across reloads —
    important for DeepSeek's auto-cache; see Phase 1.1 in the roadmap).
    Truncated to MAX_INDEX_LINES so a runaway memory folder can't
    blow up the system prompt.
    """
    _ensure_dir()
    entries: list[str] = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        title = meta.get("name") or p.stem
        desc = meta.get("description") or ""
        if desc:
            entries.append(f"- [{title}]({p.name}) — {desc}")
        else:
            entries.append(f"- [{title}]({p.name})")
    if not entries:
        return ""
    return "\n".join(entries[:MAX_INDEX_LINES]) + "\n"


def _write_index() -> None:
    text = _build_index_text()
    if text:
        MEMORY_INDEX.write_text(text, encoding="utf-8")
    elif MEMORY_INDEX.is_file():
        # No memories left — wipe the index file too so it doesn't
        # mislead future reads.
        try:
            MEMORY_INDEX.unlink()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public — system-prompt injection
# ---------------------------------------------------------------------------


def get_memory_index_content() -> str:
    """Return MEMORY.md text for inclusion in the agent's system prompt.

    Called once per ``start_turn`` by the AgentRuntime. Returns an empty
    string when there are no memories yet (no system-prompt injection
    in that case so a fresh installation has zero memory overhead).
    """
    if MEMORY_INDEX.is_file():
        try:
            return MEMORY_INDEX.read_text(encoding="utf-8")
        except Exception:
            return ""
    # Index file may be missing if memories were created without using
    # handle_memory_save. Rebuild lazily so the first read still works.
    text = _build_index_text()
    if text:
        try:
            MEMORY_INDEX.write_text(text, encoding="utf-8")
        except Exception:
            pass
    return text


# ---------------------------------------------------------------------------
# Tool handlers (dispatched as tools by the agent)
# ---------------------------------------------------------------------------


def handle_memory_save(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    content = (body.get("content") or "").strip()
    mtype = (body.get("type") or "").strip().lower()
    description = (body.get("description") or "").strip()
    # v2.4 / Phase B.4 — content_type opt-in field. Default "reference" so
    # callers who don't pass it get BM25-visible entries (preserves prior
    # behaviour). Callers explicitly tagging step lists with "instruction"
    # opt INTO the safer hide-from-generic-queries discipline.
    content_type = (
        body.get("content_type") or DEFAULT_CONTENT_TYPE
    ).strip().lower()

    if not name:
        return {"error": "Missing required field: name"}
    if not content:
        return {"error": "Missing required field: content"}
    if mtype not in VALID_TYPES:
        return {
            "error": f"Invalid type: {mtype!r}. Must be one of {list(VALID_TYPES)}.",
        }
    if content_type not in VALID_CONTENT_TYPES:
        return {
            "error": (
                f"Invalid content_type: {content_type!r}. "
                f"Must be one of {list(VALID_CONTENT_TYPES)}."
            ),
        }

    _ensure_dir()
    filename = _safe_filename(name, mtype)
    filepath = MEMORY_DIR / filename

    full = (
        f"---\nname: {name}\ndescription: {description}\n"
        f"type: {mtype}\ncontent_type: {content_type}\n---\n\n{content}\n"
    )
    filepath.write_text(full, encoding="utf-8")
    _write_index()
    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "content_type": content_type,
        "index_lines": len(_build_index_text().splitlines()),
    }


def handle_memory_get(body: dict) -> dict:
    name = (body.get("name") or body.get("filename") or "").strip()
    if not name:
        return {"error": "Missing required field: name (or filename)"}
    filepath = _find_memory_path(name)
    if filepath is None:
        return {
            "error": f"Memory not found: {name}",
            "hint": "Try memory_list to see saved memories, or memory_recall(query) for fuzzy search.",
        }
    try:
        text = filepath.read_text(encoding="utf-8")
    except Exception as exc:
        return {"error": f"Read failed: {type(exc).__name__}: {exc}"}
    meta, body_text = _parse_frontmatter(text)
    return {
        "ok": True,
        "filename": filepath.name,
        "metadata": meta,
        "content": body_text,
    }


def handle_memory_delete(body: dict) -> dict:
    name = (body.get("name") or body.get("filename") or "").strip()
    if not name:
        return {"error": "Missing required field: name (or filename)"}
    filepath = _find_memory_path(name)
    if filepath is None:
        return {"error": f"Memory not found: {name}"}
    try:
        filepath.unlink()
    except Exception as exc:
        return {"error": f"Delete failed: {type(exc).__name__}: {exc}"}
    _write_index()
    return {"ok": True, "filename": filepath.name}


def handle_memory_list(body: dict) -> dict:
    _ensure_dir()
    type_filter = (body.get("type") or "").strip().lower() or None
    entries: list[dict] = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        mtype = meta.get("type") or "unknown"
        if type_filter and mtype != type_filter:
            continue
        entries.append(
            {
                "filename": p.name,
                "name": meta.get("name") or p.stem,
                "description": meta.get("description") or "",
                "type": mtype,
                "size_bytes": p.stat().st_size,
            }
        )
    return {"ok": True, "count": len(entries), "memories": entries}


def handle_memory_recall(body: dict) -> dict:
    """BM25 search across all memory files. Returns ranked matches with
    snippets — the agent can then call handle_memory_get for full text."""
    query = (body.get("query") or "").strip()
    try:
        top_k = max(1, min(int(body.get("top_k", 3) or 3), 20))
    except (TypeError, ValueError):
        top_k = 3
    type_filter = (body.get("type") or "").strip().lower() or None

    if not query:
        return {"error": "Missing required field: query"}

    _ensure_dir()
    docs: list[dict] = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
            meta, body_text = _parse_frontmatter(text)
        except Exception:
            continue
        mtype = meta.get("type") or "unknown"
        if type_filter and mtype != type_filter:
            continue
        # v2.4 / Phase B.4 — propagate content_type with default-on-read
        # so legacy frontmatter without the field still flows through as
        # "reference" (the safe, freely-searchable default).
        docs.append(
            {
                "filename": p.name,
                "name": meta.get("name") or p.stem,
                "description": meta.get("description") or "",
                "type": mtype,
                "content_type": (
                    meta.get("content_type") or DEFAULT_CONTENT_TYPE
                ).strip().lower(),
                "text": body_text,
            }
        )

    if not docs:
        return {"ok": True, "count": 0, "matches": []}

    matches = _bm25_search(query, docs, top_k)
    return {"ok": True, "count": len(matches), "matches": matches}


# ---------------------------------------------------------------------------
# Tier 1 + 2 additions (export / import / favorite)
# ---------------------------------------------------------------------------


def handle_memory_export(body: dict) -> dict:
    """Dump every memory file as a single JSON object. Used for backup,
    cross-machine sharing, or archiving before a destructive change.

    Output shape:
        {
          "ok": True,
          "count": N,
          "memories": {
            "<filename>": {"meta": {...}, "body": "..."},
            ...
          }
        }
    """
    _ensure_dir()
    entries: dict[str, dict] = {}
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
            meta, body_text = _parse_frontmatter(text)
        except Exception:
            continue
        entries[p.name] = {"meta": meta, "body": body_text}
    return {
        "ok": True,
        "count": len(entries),
        "directory": str(MEMORY_DIR),
        "memories": entries,
    }


def handle_memory_import(body: dict) -> dict:
    """Restore memories from an export dump.

    Body fields:
      memories  — dict of {filename: {meta, body}} (required)
      overwrite — bool, default False. When False, existing files
                  are skipped (returned in `skipped`).

    Useful for sharing technique libraries between machines, restoring
    after `~/.tdpilot-api/memory/` was wiped, or syncing a curated set
    of memories from version control.
    """
    memories = body.get("memories")
    if not isinstance(memories, dict):
        return {"error": "Missing or invalid field: memories (dict of {filename: {meta, body}})"}
    overwrite = bool(body.get("overwrite", False))

    _ensure_dir()
    written: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []

    for filename, entry in memories.items():
        if not isinstance(entry, dict):
            errors.append({"filename": filename, "error": "entry not a dict"})
            continue
        meta = entry.get("meta") or {}
        body_text = entry.get("body") or ""
        if not isinstance(meta, dict) or not isinstance(body_text, str):
            errors.append({"filename": filename, "error": "meta must be dict and body must be string"})
            continue

        target = MEMORY_DIR / filename
        if target.exists() and not overwrite:
            skipped.append(filename)
            continue

        # Reconstruct the markdown frontmatter + body. Preserve any
        # custom keys in the meta dict (favorite, rating, etc.).
        fm_lines = "\n".join(f"{k}: {v}" for k, v in meta.items())
        full = f"---\n{fm_lines}\n---\n\n{body_text.lstrip(chr(10))}\n"
        try:
            target.write_text(full, encoding="utf-8")
            written.append(filename)
        except Exception as exc:
            errors.append({"filename": filename, "error": f"{type(exc).__name__}: {exc}"})

    _write_index()
    return {
        "ok": True,
        "written_count": len(written),
        "skipped_count": len(skipped),
        "error_count": len(errors),
        "written": written,
        "skipped": skipped,
        "errors": errors,
    }


def handle_memory_favorite(body: dict) -> dict:
    """Mark a memory as favorite and/or rate it (0-5).

    Stores `favorite: true` and/or `rating: <int>` in the file's
    frontmatter. The agent can later filter `memory_list`/`recall` by
    these fields when prioritising context retrieval.

    Body fields:
      name      — memory name or filename (required)
      favorite  — bool, optional. None leaves current state unchanged.
      rating    — int 0-5, optional. None leaves current state unchanged.
    """
    name = (body.get("name") or body.get("filename") or "").strip()
    if not name:
        return {"error": "Missing required field: name (or filename)"}

    favorite = body.get("favorite")
    rating = body.get("rating")
    if favorite is None and rating is None:
        return {"error": "Provide at least one of: favorite (bool) or rating (0-5)."}

    if rating is not None:
        try:
            rating_int = int(rating)
        except (TypeError, ValueError):
            return {"error": f"rating must be 0-5, got {rating!r}"}
        if rating_int < 0 or rating_int > 5:
            return {"error": f"rating out of range (0-5): {rating_int}"}
    else:
        rating_int = None

    filepath = _find_memory_path(name)
    if filepath is None:
        return {"error": f"Memory not found: {name}"}

    text = filepath.read_text(encoding="utf-8")
    meta, body_text = _parse_frontmatter(text)
    if favorite is not None:
        meta["favorite"] = "true" if bool(favorite) else "false"
    if rating_int is not None:
        meta["rating"] = str(rating_int)

    fm_lines = "\n".join(f"{k}: {v}" for k, v in meta.items())
    full = f"---\n{fm_lines}\n---\n\n{body_text.lstrip(chr(10))}\n"
    filepath.write_text(full, encoding="utf-8")

    return {
        "ok": True,
        "filename": filepath.name,
        "favorite": meta.get("favorite"),
        "rating": meta.get("rating"),
    }


# ---------------------------------------------------------------------------
# BM25 wrapper — scoring lives in tdpilot_api_bm25 (extracted 2026-05-04).
# Each search-using module now formats its own match dict; only the
# scoring loop is shared.
# ---------------------------------------------------------------------------


def _bm25_search(query: str, docs: list[dict], top_k: int) -> list[dict]:
    """Score docs against query and return formatted match dicts. Index
    fields: name + description + text. Match dict carries memory-specific
    metadata (filename, type) plus a snippet of the doc body."""
    from tdpilot_api_bm25 import bm25_score  # type: ignore[import-not-found]

    matches: list[dict] = []
    for doc, score in bm25_score(query, docs, top_k=top_k):
        text = doc.get("text") or ""
        snippet = text.strip().replace("\n", " ")[:280]
        matches.append(
            {
                "filename": doc.get("filename"),
                "name": doc.get("name"),
                "type": doc.get("type"),
                # v2.4 / Phase B.4 — surface content_type so the pre-turn
                # retrieval filter (in tdpilot_api_runtime._run_pre_turn_retrieval)
                # can suppress "instruction" hits on generic prompts.
                "content_type": doc.get("content_type") or DEFAULT_CONTENT_TYPE,
                "description": doc.get("description"),
                "score": round(score, 3),
                "snippet": snippet,
            }
        )
    return matches
