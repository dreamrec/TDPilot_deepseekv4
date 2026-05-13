"""TDPilot API — recipe store + best-effort replay.

A "recipe" is a saved sequence of tool calls that reproduce a creative
technique. The user's flow:

    Session 1 (build)
        user: "build me an audio-reactive noise field"
        agent: <12 tool calls> → working network
        agent: recipe_save(name="audio noise field", ...)

    Session 2 (replay)
        user: "do that audio thing again"
        agent: recipe_recall(query="audio") → finds it
        agent: recipe_replay(name="audio noise field") → executes the
               same 12 tool calls in order

Storage layout (matches memory/knowledge):
    ~/.tdpilot-api/recipes/
    ├── INDEX.md                  human-readable inventory (auto-updated)
    ├── audio_noise_field.md      individual recipe
    └── ...

Recipe format (one .md per recipe, fenced JSON for replay):
    ---
    name: Audio reactive noise field
    description: noiseTOP -> levelTOP audio-driven via audioin envelope
    tags: [audio, top, reactive]
    created: 2026-05-04
    tool_calls: 8
    ---

    ## Goal
    Animated noise that pulses with audio amplitude.

    ## Steps
    1. Create noiseTOP at /project1/noise_audio
    2. Create audioinCHOP, analyse band 0
    3. Bind level1.par.opacity to band0
    4. ...

    ## Replay
    ```json
    [
      {"tool": "td_create_node", "args": {"parent_path": "/project1",
        "op_type": "noiseTOP", "name": "noise_audio"}},
      {"tool": "td_create_node", "args": {"parent_path": "/project1",
        "op_type": "audiodeviceinCHOP", "name": "audioin1"}},
      ...
    ]
    ```

The replay JSON is the source of truth for execution; the prose is for
the human reader. handle_recipe_replay parses the fenced JSON block and
walks it via the AgentRuntime's RAW dispatcher (bypassing the cook-
thread queue — the recipe handler already runs on cook thread, so going
through CookThreadDispatcher would deadlock).

Replay is best-effort: a failed step records the error and aborts the
sequence. Future Sprint 3 will integrate snapshot/rollback so a half-
replayed recipe rolls back to the snapshot.

Exposed handlers:
    handle_recipe_save     write a recipe + refresh INDEX.md
    handle_recipe_get      load full recipe text
    handle_recipe_list     enumerate recipes (filter by tag)
    handle_recipe_recall   BM25 search across all recipes
    handle_recipe_replay   execute the recipe's replay JSON sequence
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Storage layout
# ---------------------------------------------------------------------------

# 2.1.3 — namespaced under ~/.tdpilot-dpsk4/api/recipes with legacy fallback.
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    RECIPES_DIR = resolve_user_dir("recipes")
except ImportError:
    RECIPES_DIR = Path.home() / ".tdpilot-api" / "recipes"
RECIPES_INDEX = RECIPES_DIR / "INDEX.md"
# v2.4 / Phase B.4 — content_type controls pre-turn BM25 retrieval visibility.
# Recipes are inherently step lists (the whole point is to replay them), so
# the safe default is "instruction" — the pre-turn filter then surfaces a
# recipe only when the user explicitly names it. A user who wants a recipe
# to behave like reference material (e.g. "this recipe just documents the
# approach for context, don't auto-replay it") can pass content_type="reference".
VALID_CONTENT_TYPES = ("instruction", "reference", "fact")
DEFAULT_CONTENT_TYPE = "instruction"
MAX_INDEX_LINES = 200


def _ensure_dir() -> None:
    RECIPES_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "recipe"


def _resolve_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    if name.endswith(".md"):
        return name
    return _slugify(name) + ".md"


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fm = text[3:end].strip("\n").strip()
    body = text[end + 4 :].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        v = v.strip()
        # Crude list parsing for `tags: [a, b, c]`.
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [t.strip() for t in inner.split(",") if t.strip()] if inner else []
        else:
            meta[k] = v
    return meta, body


def _extract_replay_json(body: str) -> list[dict]:
    """Find the first ```json ... ``` fenced block under a `## Replay`
    heading and return its parsed contents. Tolerant of small whitespace
    variations — the model authoring recipes won't always match a strict
    schema. Returns [] if no replay block found."""
    # Look for a fenced JSON block — prefer one near `## Replay` heading
    # but accept any JSON code block as a fallback.
    replay_match = re.search(
        r"##\s+Replay\s*\n.*?```(?:json)?\s*\n(.*?)\n```",
        body,
        re.DOTALL | re.IGNORECASE,
    )
    if not replay_match:
        replay_match = re.search(r"```json\s*\n(.*?)\n```", body, re.DOTALL)
    if not replay_match:
        return []
    raw = replay_match.group(1).strip()
    try:
        parsed = _json.loads(raw)
    except _json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [step for step in parsed if isinstance(step, dict)]


def _build_index_text() -> str:
    _ensure_dir()
    entries: list[str] = []
    for p in sorted(RECIPES_DIR.glob("*.md")):
        if p.name == "INDEX.md":
            continue
        try:
            meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        title = meta.get("name") or p.stem
        desc = meta.get("description") or ""
        tags = meta.get("tags") or []
        tag_str = " ".join(f"#{t}" for t in tags) if isinstance(tags, list) else ""
        if desc:
            entries.append(f"- [{title}]({p.name}) — {desc} {tag_str}".rstrip())
        else:
            entries.append(f"- [{title}]({p.name}) {tag_str}".rstrip())
    if not entries:
        return ""
    return "\n".join(entries[:MAX_INDEX_LINES]) + "\n"


def _write_index() -> None:
    text = _build_index_text()
    if text:
        RECIPES_INDEX.write_text(text, encoding="utf-8")
    elif RECIPES_INDEX.is_file():
        try:
            RECIPES_INDEX.unlink()
        except Exception:
            pass


def get_recipes_index_hint() -> str:
    """Short hint for system-prompt injection — titles + descriptions
    only. Mirrors the pattern from knowledge."""
    if RECIPES_INDEX.is_file():
        try:
            return RECIPES_INDEX.read_text(encoding="utf-8")
        except Exception:
            return ""
    return _build_index_text()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_recipe_save(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    description = (body.get("description") or "").strip()
    tags = body.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    goal = (body.get("goal") or "").strip()
    steps = body.get("steps") or []  # list of human-readable strings
    replay = body.get("replay") or []  # list of {tool, args} dicts
    # v2.4 / Phase B.4 — content_type. Default "instruction" because the
    # whole point of a recipe is the replay step list. A recipe author who
    # explicitly wants generic BM25 visibility (e.g. for searchable
    # documentation) can pass content_type="reference".
    content_type = (body.get("content_type") or DEFAULT_CONTENT_TYPE).strip().lower()

    if not name:
        return {"error": "Missing required field: name"}
    if not replay:
        return {"error": "Missing required field: replay (list of {tool, args} dicts)"}
    if not isinstance(replay, list) or not all(isinstance(s, dict) for s in replay):
        return {"error": "replay must be a list of {tool, args} dicts"}
    if content_type not in VALID_CONTENT_TYPES:
        return {
            "error": (f"Invalid content_type: {content_type!r}. Must be one of {list(VALID_CONTENT_TYPES)}."),
        }

    _ensure_dir()
    filename = _resolve_filename(name)
    filepath = RECIPES_DIR / filename

    # Format human-readable step list.
    if isinstance(steps, list) and steps:
        steps_md = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(steps))
    else:
        steps_md = "_(no human-readable steps provided — see Replay below)_"

    tags_yaml = "[" + ", ".join(tags) + "]" if tags else "[]"
    created = _dt.datetime.utcnow().strftime("%Y-%m-%d")

    full = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"tags: {tags_yaml}\n"
        f"created: {created}\n"
        f"tool_calls: {len(replay)}\n"
        f"content_type: {content_type}\n"
        f"---\n\n"
        f"## Goal\n{goal or description or name}\n\n"
        f"## Steps\n{steps_md}\n\n"
        f"## Replay\n```json\n{_json.dumps(replay, indent=2)}\n```\n"
    )
    filepath.write_text(full, encoding="utf-8")
    _write_index()
    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "content_type": content_type,
        "tool_calls": len(replay),
    }


def handle_recipe_get(body: dict) -> dict:
    filename = _resolve_filename(body.get("name") or body.get("filename") or "")
    if not filename:
        return {"error": "Missing required field: name"}
    filepath = RECIPES_DIR / filename
    if not filepath.is_file():
        return {"error": f"Recipe not found: {filename}"}
    text = filepath.read_text(encoding="utf-8")
    meta, body_text = _parse_frontmatter(text)
    replay = _extract_replay_json(body_text)
    return {
        "ok": True,
        "filename": filename,
        "metadata": meta,
        "content": body_text,
        "replay": replay,
        "tool_calls": len(replay),
    }


def handle_recipe_list(body: dict) -> dict:
    _ensure_dir()
    tag_filter = (body.get("tag") or "").strip().lower() or None
    entries: list[dict] = []
    for p in sorted(RECIPES_DIR.glob("*.md")):
        if p.name == "INDEX.md":
            continue
        try:
            meta, _ = _parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        if tag_filter and tag_filter not in [t.lower() for t in tags]:
            continue
        entries.append(
            {
                "filename": p.name,
                "name": meta.get("name") or p.stem,
                "description": meta.get("description") or "",
                "tags": tags,
                "created": meta.get("created") or "",
                "tool_calls": meta.get("tool_calls") or 0,
            }
        )
    return {"ok": True, "count": len(entries), "recipes": entries}


def handle_recipe_recall(body: dict) -> dict:
    query = (body.get("query") or "").strip()
    try:
        top_k = max(1, min(int(body.get("top_k", 3) or 3), 20))
    except (TypeError, ValueError):
        top_k = 3
    if not query:
        return {"error": "Missing required field: query"}

    _ensure_dir()
    docs: list[dict] = []
    for p in sorted(RECIPES_DIR.glob("*.md")):
        if p.name == "INDEX.md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
            meta, body_text = _parse_frontmatter(text)
        except Exception:
            continue
        tags = meta.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        docs.append(
            {
                "filename": p.name,
                "name": meta.get("name") or p.stem,
                "description": meta.get("description") or "",
                "tags": " ".join(tags) if tags else "",
                # v2.4 / Phase B.4 — propagate content_type with default-on-read.
                # Legacy recipes (no field) default to "instruction" matching
                # the new save default, so pre-existing recipes are hidden
                # from generic queries — the same protection their step-list
                # nature deserves.
                "content_type": (meta.get("content_type") or DEFAULT_CONTENT_TYPE).strip().lower(),
                "text": body_text,
            }
        )
    if not docs:
        return {"ok": True, "count": 0, "matches": []}

    matches = _bm25_search(query, docs, top_k)
    return {"ok": True, "count": len(matches), "matches": matches}


def handle_validate_recipe(body: dict) -> dict:
    """Pre-save sanity check for a recipe (Tier 1 add).

    Validates:
      - replay is a list of dicts
      - each step has a 'tool' field referencing a known TOOL_TO_HANDLER
        entry (built-in OR user-pluggable via the registered extras)
      - each step has an 'args' field that's a dict (or omitted)

    Use BEFORE recipe_save to catch authoring errors early — saving a
    recipe with an unknown tool name lets the agent author garbage that
    only fails at replay time.

    Body:
      name    — optional, for error messages
      replay  — list of {tool, args} dicts (required)
    """
    replay = body.get("replay")
    if not isinstance(replay, list):
        return {
            "ok": False,
            "valid": False,
            "error": "replay must be a list of {tool, args} dicts",
        }

    # Pull the live handler table. user-pluggable tools register through
    # extras at runtime — we surface those too if the registry is loaded.
    from tdpilot_api_schema_map import TOOL_TO_HANDLER  # type: ignore[import-not-found]

    known_tools: set[str] = set(TOOL_TO_HANDLER.keys())
    try:
        from tdpilot_api_user_tools import _LOADED  # type: ignore[import-not-found]

        for entry in _LOADED:
            if entry.get("ok") and entry.get("name"):
                known_tools.add(entry["name"])
    except Exception:
        pass

    issues: list[dict] = []
    for i, step in enumerate(replay):
        if not isinstance(step, dict):
            issues.append({"step": i, "error": "step is not a dict"})
            continue
        tool = step.get("tool")
        if not isinstance(tool, str) or not tool:
            issues.append({"step": i, "error": "missing or non-string 'tool' field"})
            continue
        if tool not in known_tools:
            issues.append(
                {
                    "step": i,
                    "tool": tool,
                    "error": "unknown tool — not registered in TOOL_TO_HANDLER nor user_tools",
                }
            )
            continue
        args = step.get("args", {})
        if args is not None and not isinstance(args, dict):
            issues.append({"step": i, "tool": tool, "error": "'args' must be a dict (or omitted)"})

    valid = len(issues) == 0
    return {
        "ok": True,
        "valid": valid,
        "name": (body.get("name") or "").strip() or None,
        "step_count": len(replay),
        "issue_count": len(issues),
        "issues": issues,
        "known_tool_count": len(known_tools),
    }


def handle_recipe_replay(body: dict) -> dict:
    """Execute the saved replay JSON in order.

    Two modes:
      * ``transactional=False`` (default) — best-effort. If step N fails,
        the call returns ok=False with completed=N-1 and the error from
        step N. Earlier steps stay in the network.
      * ``transactional=True`` — wraps the replay in a TD undo block
        (ui.undo.startBlock/endBlock). On any step failure, the entire
        sequence is rolled back atomically via project.undo(). On
        success, the block becomes one step in TD's undo stack.

    Calls the agent's RAW dispatcher (bypassing the cook-thread queue
    wrapper) — we're already on cook thread inside this handler, so
    going through CookThreadDispatcher.__call__ would deadlock waiting
    for the cook thread to drain its own queue.
    """
    filename = _resolve_filename(body.get("name") or body.get("filename") or "")
    if not filename:
        return {"error": "Missing required field: name"}
    filepath = RECIPES_DIR / filename
    if not filepath.is_file():
        return {"error": f"Recipe not found: {filename}"}

    dry_run = bool(body.get("dry_run", False))
    transactional = bool(body.get("transactional", False))

    text = filepath.read_text(encoding="utf-8")
    _meta, body_text = _parse_frontmatter(text)
    replay = _extract_replay_json(body_text)
    if not replay:
        return {"error": f"No replay JSON found in recipe: {filename}"}

    # Dry-run returns the plan without ever needing the dispatcher —
    # useful for previewing OR for tests that run outside TD.
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "filename": filename,
            "planned_steps": len(replay),
            "tools": [s.get("tool") for s in replay],
        }

    # Real replay: find the dispatcher. The recipe handler runs inside
    # TD on the cook thread; the lookup helper walks COMP → extension
    # → runtime and returns the raw (non cook-thread-wrapped)
    # dispatcher. PR-19 (F-18) — single-source helper replaces the
    # bespoke walk.
    try:
        from tdpilot_api_lookup import get_raw_dispatcher  # type: ignore[import-not-found]

        raw_dispatcher = get_raw_dispatcher()
    except ImportError as exc:
        return {"error": f"Could not access dispatcher: {exc}"}

    if raw_dispatcher is None:
        return {"error": "Raw dispatcher not available"}

    # Transactional replay: open a TD undo block so the whole
    # sequence is atomic. On failure we close + undo, reverting
    # every step in one atomic operation.
    if transactional:
        try:
            ui.undo.startBlock(f"recipe_replay:{filename}")  # type: ignore[name-defined]
        except Exception as exc:
            return {"error": f"Could not start undo block: {exc}"}

    def _close_and_rollback() -> None:
        try:
            ui.undo.endBlock()  # type: ignore[name-defined]
        except Exception:
            pass
        try:
            project.undo()  # type: ignore[name-defined]
        except Exception:
            pass

    def _close_commit() -> None:
        try:
            ui.undo.endBlock()  # type: ignore[name-defined]
        except Exception:
            pass

    results: list[dict] = []
    for i, step in enumerate(replay):
        tool_name = step.get("tool")
        tool_args = step.get("args") or {}
        if not tool_name:
            results.append({"step": i, "error": "Step missing tool name"})
            if transactional:
                _close_and_rollback()
            return {
                "ok": False,
                "completed": i,
                "total": len(replay),
                "error": "Step missing tool name",
                "rolled_back": transactional,
                "results": results,
            }
        try:
            result = raw_dispatcher(tool_name, tool_args)
            is_error = isinstance(result, dict) and "error" in result
            results.append(
                {
                    "step": i,
                    "tool": tool_name,
                    "is_error": is_error,
                    "result": result,
                }
            )
            if is_error:
                if transactional:
                    _close_and_rollback()
                return {
                    "ok": False,
                    "completed": i,
                    "total": len(replay),
                    "error": f"Step {i} ({tool_name}) failed: {result.get('error')}",
                    "rolled_back": transactional,
                    "results": results,
                }
        except Exception as exc:
            results.append(
                {
                    "step": i,
                    "tool": tool_name,
                    "is_error": True,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            if transactional:
                _close_and_rollback()
            return {
                "ok": False,
                "completed": i,
                "total": len(replay),
                "error": f"Step {i} ({tool_name}) raised: {exc}",
                "rolled_back": transactional,
                "results": results,
            }

    # All steps succeeded. Close the undo block (if any) and report.
    if transactional:
        _close_commit()

    return {
        "ok": True,
        "filename": filename,
        "completed": len(replay),
        "total": len(replay),
        "transactional": transactional,
        "results": results,
    }


# ---------------------------------------------------------------------------
# BM25 wrapper — scoring lives in tdpilot_api_bm25 (extracted 2026-05-04).
# Recipes pass an extra `tags` field into the index so tag terms also
# influence ranking; output dict keeps tags for the agent to surface.
# ---------------------------------------------------------------------------


def _bm25_search(query: str, docs: list[dict], top_k: int) -> list[dict]:
    """Score docs (indexing tags as well as name/description/text) and
    return recipe-shaped match dicts."""
    from tdpilot_api_bm25 import bm25_score  # type: ignore[import-not-found]

    matches: list[dict] = []
    for doc, score in bm25_score(
        query,
        docs,
        index_fields=("name", "description", "tags", "text"),
        top_k=top_k,
    ):
        text = doc.get("text") or ""
        snippet = text.strip().replace("\n", " ")[:280]
        matches.append(
            {
                "filename": doc.get("filename"),
                "name": doc.get("name"),
                "description": doc.get("description"),
                "tags": doc.get("tags"),
                # v2.4 / Phase B.4 — surface content_type for the pre-turn
                # retrieval filter in tdpilot_api_runtime._run_pre_turn_retrieval.
                "content_type": doc.get("content_type") or DEFAULT_CONTENT_TYPE,
                "score": round(score, 3),
                "snippet": snippet,
            }
        )
    return matches
