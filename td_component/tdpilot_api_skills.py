"""TDPilot API — skills system (behaviour modulators).

A "skill" is a markdown document with frontmatter that the agent can
load on-demand to modulate its behaviour for a specific workflow:
POPx particles, performance optimization, glsl shaders, audio-reactive
patterns, etc.

Two pools (matches knowledge):
  1. Bundled — markdown files in td_component/skills/, baked into the
     .tox as textDATs inside a `skills` baseCOMP child at build time.
  2. User — ~/.tdpilot-api/skills/*.md. User entries override bundled
     ones with the same `name` field.

Frontmatter:
    ---
    name: <skill name, kebab-case>
    description: <one-line hook>
    auto_load: true | false      (false default — manual-load via skill_load)
    priority: <int>              (higher loads first when many auto-load)
    triggers: [keyword, keyword] (hint for the agent on when to load)
    ---

    <markdown body — discipline, rules, protocols>

Tools:
  skill_list   enumerate available skills with metadata
  skill_get    load full content of a skill
  skill_load   alias for skill_get with stronger semantic ("activate this")

The model treats skill_load's returned content as authoritative
behaviour guidance for the rest of the turn — no special "pin"
mechanism needed; the model naturally incorporates tool results into
its reasoning.

System prompt integration: ``get_skills_index_hint()`` returns a short
list of available skills with their triggers. Auto-load skills (rare,
opt-in) get appended to the system prompt at build time so they're
always present without a tool call.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

USER_SKILLS_DIR = Path.home() / ".tdpilot-api" / "skills"
SKILLS_CONTAINER_NAME = "skills"
SKILL_DAT_PREFIX = "skill_"


def _ensure_user_dir() -> None:
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "skill"


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
        if v.lower() in ("true", "false"):
            meta[k] = v.lower() == "true"
        elif v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            meta[k] = [t.strip() for t in inner.split(",") if t.strip()] if inner else []
        else:
            try:
                meta[k] = int(v)
            except ValueError:
                meta[k] = v
    return meta, body


# ---------------------------------------------------------------------------
# Corpus discovery
# ---------------------------------------------------------------------------


def _bundled_entries() -> list[dict]:
    try:
        comp = parent()  # type: ignore[name-defined]
    except NameError:
        return []
    if comp is None:
        return []
    container = comp.op(SKILLS_CONTAINER_NAME)
    if container is None:
        return []
    entries: list[dict] = []
    for child in container.children:
        try:
            name = child.name
            if not name.startswith(SKILL_DAT_PREFIX):
                continue
            text = child.text or ""
            meta, body = _parse_frontmatter(text)
            entries.append(
                {
                    "source": "bundled",
                    "filename": name[len(SKILL_DAT_PREFIX) :] + ".md",
                    "name": meta.get("name") or name,
                    "description": meta.get("description") or "",
                    "auto_load": bool(meta.get("auto_load", False)),
                    "priority": int(meta.get("priority", 0) or 0),
                    "triggers": meta.get("triggers") or [],
                    "text": body,
                }
            )
        except Exception:
            continue
    return entries


def _user_entries() -> list[dict]:
    if not USER_SKILLS_DIR.is_dir():
        return []
    entries: list[dict] = []
    for p in sorted(USER_SKILLS_DIR.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            entries.append(
                {
                    "source": "user",
                    "filename": p.name,
                    "name": meta.get("name") or p.stem,
                    "description": meta.get("description") or "",
                    "auto_load": bool(meta.get("auto_load", False)),
                    "priority": int(meta.get("priority", 0) or 0),
                    "triggers": meta.get("triggers") or [],
                    "text": body,
                }
            )
        except Exception:
            continue
    return entries


def _all_entries() -> list[dict]:
    bundled = _bundled_entries()
    user = _user_entries()
    user_names = {e["name"] for e in user}
    merged = [e for e in bundled if e["name"] not in user_names] + user
    return merged


# ---------------------------------------------------------------------------
# Public — system-prompt injection
# ---------------------------------------------------------------------------


def find_triggered_skills(user_text: str) -> list[dict]:
    """Phase 3.1 — return skill entries whose ``triggers`` match in ``user_text``.

    Matching rules (deliberately conservative):
      - Case-insensitive.
      - Triggers shorter than 5 chars use word-boundary regex
        (``\\bpop\\b``) to avoid matching ``popular`` / ``population``.
      - Longer triggers use substring match (they're specific enough
        that incidental overlap is rare).
      - A skill matches if AT LEAST ONE of its trigger keywords fires.

    Returns the matched skill entries (as produced by ``_all_entries``).
    Order is alphabetical by skill name so the caller gets a stable,
    deterministic list.
    """
    text = (user_text or "").lower().strip()
    if not text:
        return []

    matched: list[dict] = []
    for entry in _all_entries():
        triggers = entry.get("triggers") or []
        if not isinstance(triggers, list):
            continue
        for trig in triggers:
            if not isinstance(trig, str):
                continue
            t = trig.lower().strip()
            if not t:
                continue
            try:
                if len(t) < 5:
                    pat = r"\b" + re.escape(t) + r"\b"
                    if re.search(pat, text):
                        matched.append(entry)
                        break
                else:
                    if t in text:
                        matched.append(entry)
                        break
            except re.error:
                continue
    matched.sort(key=lambda e: e.get("name", ""))
    return matched


def get_skills_index_hint() -> str:
    """Short hint listing available skills + their triggers."""
    entries = _all_entries()
    if not entries:
        return ""
    lines = []
    for e in sorted(entries, key=lambda x: x["name"]):
        triggers = ", ".join(e["triggers"]) if e["triggers"] else ""
        marker = " [auto]" if e["auto_load"] else ""
        if triggers:
            lines.append(f"- {e['name']}{marker} — {e['description']} (triggers: {triggers})")
        elif e["description"]:
            lines.append(f"- {e['name']}{marker} — {e['description']}")
        else:
            lines.append(f"- {e['name']}{marker}")
    return "\n".join(lines) + "\n"


def get_auto_load_skills_text() -> str:
    """Full content of all auto-load skills, concatenated. Goes into the
    system prompt at runtime build time. Sorted by (priority desc, name)
    so byte-stability is preserved across turns (cache-friendly)."""
    entries = [e for e in _all_entries() if e["auto_load"]]
    if not entries:
        return ""
    entries.sort(key=lambda x: (-x["priority"], x["name"]))
    parts = []
    for e in entries:
        parts.append(f"### Skill: {e['name']}\n\n{e['text'].strip()}\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def handle_skill_list(body: dict) -> dict:
    entries = _all_entries()
    return {
        "ok": True,
        "count": len(entries),
        "skills": [
            {
                "name": e["name"],
                "description": e["description"],
                "auto_load": e["auto_load"],
                "priority": e["priority"],
                "triggers": e["triggers"],
                "source": e["source"],
                "filename": e["filename"],
                "size_bytes": len(e["text"].encode("utf-8")),
            }
            for e in sorted(entries, key=lambda x: x["name"])
        ],
    }


def handle_skill_get(body: dict) -> dict:
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "Missing required field: name"}
    for entry in _all_entries():
        if entry["name"] == name or entry["filename"] == name or entry["filename"] == name + ".md":
            return {
                "ok": True,
                "name": entry["name"],
                "filename": entry["filename"],
                "description": entry["description"],
                "auto_load": entry["auto_load"],
                "priority": entry["priority"],
                "triggers": entry["triggers"],
                "source": entry["source"],
                "content": entry["text"],
            }
    return {"error": f"Skill not found: {name}"}


def handle_skill_load(body: dict) -> dict:
    """Same data as skill_get but the function name signals stronger
    intent ('activate this skill') so the model treats the returned
    content as authoritative for the rest of the turn."""
    result = handle_skill_get(body)
    if result.get("ok"):
        result["activated"] = True
    return result
