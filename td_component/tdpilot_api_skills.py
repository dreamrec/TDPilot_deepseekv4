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
    surface: standalone|cli|both (informational — indicates which TDPilot
                                   runtime the skill targets; default 'both')
    ---

    <markdown body — discipline, rules, protocols>

Validation
----------

Pre-1.7.2 the frontmatter parser was a one-off custom thing that
silently swallowed bad input — a typo in a user skill made the entry
vanish from skill_list with no error surfaced. 1.7.2 switches to
``yaml.safe_load`` and validates required fields. Invalid skills are
still kept in ``skill_list`` (with ``valid=False`` + the list of
errors) so the user can see what's broken, but they're filtered out
of trigger matching, auto-load, and the system-prompt index. Use
``td_skill_validate`` (or look for ``valid: false`` in
``td_skill_list``) to surface problems.

Trigger semantics
-----------------

Pre-1.7.2 trigger matching was substring-based for any trigger >= 5
chars — meaning ``"don't optimize this"`` would activate the
performance skill because ``optimize`` substring-matched. 1.7.2 uses
word-boundary regex for ALL trigger lengths regardless of size.

Tools:
  skill_list      enumerate available skills with metadata
  skill_get       load full content of a skill
  skill_load      alias for skill_get with stronger semantic
  skill_validate  re-run validation, return per-skill error lists

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

# yaml is a hard dep (pyproject.toml). Soft-import keeps this module
# importable in stripped-down test embeds even if PyYAML isn't installed,
# in which case validation degrades to a parse error per skill.
try:
    import yaml  # type: ignore[import-not-found]
except ImportError:
    yaml = None  # type: ignore[assignment]

# 2.1.3 — namespaced under ~/.tdpilot-dpsk4/api/skills with legacy fallback.
# Pre-2.1.3 user skills landed at ~/.tdpilot-api/skills/ — the audit
# found that directory never existed on the dpsk4 fork's typical install
# (the variant uses ~/.tdpilot-dpsk4/* for everything else), so user-side
# skill overrides silently failed to load.
try:
    from tdpilot_api_config import resolve_user_dir  # type: ignore[import-not-found]

    USER_SKILLS_DIR = resolve_user_dir("skills")
except ImportError:
    USER_SKILLS_DIR = Path.home() / ".tdpilot-api" / "skills"
SKILLS_CONTAINER_NAME = "skills"
SKILL_DAT_PREFIX = "skill_"


def _ensure_user_dir() -> None:
    USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "_", s)
    return s[:60] or "skill"


_VALID_SURFACES = ("standalone", "cli", "both")


def _validate_skill_meta(meta: dict) -> list[str]:
    """Return a list of human-readable errors found in the skill's
    frontmatter. Empty list = valid. Used by ``_parse_frontmatter`` and
    surfaced via ``td_skill_list`` and ``td_skill_validate`` so the
    user can see exactly why a skill was rejected.
    """
    errs: list[str] = []
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        errs.append("missing or non-string 'name'")
    if "description" in meta and not isinstance(meta["description"], str):
        errs.append("'description' must be a string")
    if "auto_load" in meta and not isinstance(meta["auto_load"], bool):
        errs.append("'auto_load' must be a boolean (true/false)")
    if "priority" in meta:
        try:
            int(meta["priority"])
        except (TypeError, ValueError):
            errs.append("'priority' must be an integer")
    if "triggers" in meta:
        triggers = meta["triggers"]
        if not isinstance(triggers, list):
            errs.append("'triggers' must be a list of strings")
        else:
            for i, t in enumerate(triggers):
                if not isinstance(t, str):
                    errs.append(f"'triggers[{i}]' must be a string")
    if "surface" in meta and meta["surface"] not in _VALID_SURFACES:
        errs.append(f"'surface' must be one of {_VALID_SURFACES}, got {meta['surface']!r}")
    return errs


def _parse_frontmatter(text: str) -> tuple[dict, str, list[str]]:
    """Parse YAML frontmatter from a markdown body.

    Returns ``(meta, body, errors)``:
      - ``meta``: dict of parsed frontmatter fields (empty on parse fail)
      - ``body``: the markdown body after the closing ``---``
      - ``errors``: human-readable validation errors. Empty == valid.

    Pre-1.7.2 this was a custom line-by-line parser that silently
    swallowed bad input — a typo in a user skill made the entry vanish
    from skill_list with no error surfaced. Now it uses
    ``yaml.safe_load`` and validates required fields. Invalid skills
    are still surfaced via skill_list (with their error list) so the
    user can see what's wrong; they're filtered out of trigger
    matching, auto-load, and the system-prompt index.
    """
    if not text.startswith("---"):
        return {}, text, ["frontmatter must start with '---'"]
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text, ["unterminated frontmatter (missing closing '---')"]
    fm = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")

    if yaml is None:
        return (
            {},
            body,
            ["pyyaml not installed; cannot parse frontmatter"],
        )
    try:
        meta = yaml.safe_load(fm) or {}
    except yaml.YAMLError as e:
        # YAML errors are usually mid-multi-line; surface a one-line
        # summary rather than the full traceback.
        return {}, body, [f"yaml parse error: {str(e).splitlines()[0]}"]
    if not isinstance(meta, dict):
        return {}, body, [f"frontmatter must be a YAML mapping (got {type(meta).__name__})"]

    errors = _validate_skill_meta(meta)
    return meta, body, errors


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
        except Exception as exc:
            # textDAT-side failures (rare) are unrecoverable per-entry;
            # log + skip but DON'T let one busted entry kill the rest.
            print(f"[tdpilot_API/skills] could not read bundled DAT: {exc}")
            continue
        entries.append(
            _build_entry(
                text, source="bundled", default_name=name, filename=name[len(SKILL_DAT_PREFIX) :] + ".md"
            )
        )
    return entries


def _user_entries() -> list[dict]:
    if not USER_SKILLS_DIR.is_dir():
        return []
    entries: list[dict] = []
    for p in sorted(USER_SKILLS_DIR.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[tdpilot_API/skills] could not read {p.name}: {exc}")
            continue
        entries.append(_build_entry(text, source="user", default_name=p.stem, filename=p.name))
    return entries


def _build_entry(text: str, *, source: str, default_name: str, filename: str) -> dict:
    """Construct a skill entry from raw markdown text. Validation
    errors land in ``validation_errors`` + ``valid=False`` instead of
    being silently swallowed — so ``td_skill_list`` surfaces them and
    the user can see why a skill is broken.
    """
    meta, body, errors = _parse_frontmatter(text)
    return {
        "source": source,
        "filename": filename,
        "name": (meta.get("name") if isinstance(meta.get("name"), str) else None) or default_name,
        "description": meta.get("description") if isinstance(meta.get("description"), str) else "",
        "auto_load": bool(meta.get("auto_load", False)),
        "priority": _coerce_int(meta.get("priority", 0)),
        "triggers": meta.get("triggers") if isinstance(meta.get("triggers"), list) else [],
        "surface": meta.get("surface") if meta.get("surface") in _VALID_SURFACES else "both",
        "text": body,
        "valid": not errors,
        "validation_errors": errors,
    }


def _coerce_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _all_entries() -> list[dict]:
    """All entries — valid AND invalid — so ``handle_skill_list`` can
    show the user broken skills with their error reasons. For
    behaviour modulation use ``_valid_entries()`` instead."""
    bundled = _bundled_entries()
    user = _user_entries()
    user_names = {e["name"] for e in user}
    merged = [e for e in bundled if e["name"] not in user_names] + user
    return merged


def _valid_entries() -> list[dict]:
    """Only entries that passed frontmatter validation. This is what
    triggers, auto-load, and the system-prompt index iterate over —
    invalid skills must NOT be loaded into the agent's context."""
    return [e for e in _all_entries() if e.get("valid", True)]


# ---------------------------------------------------------------------------
# Public — system-prompt injection
# ---------------------------------------------------------------------------


def find_triggered_skills(user_text: str) -> list[dict]:
    """Phase 3.1 — return skill entries whose ``triggers`` match in ``user_text``.

    Matching rules (1.7.2):
      - Case-insensitive.
      - Word-boundary regex for ALL trigger lengths regardless of size.
        Pre-1.7.2 only triggers shorter than 5 chars used word
        boundaries; longer ones did substring matching, which meant
        ``"don't optimize this"`` activated the performance skill
        because ``optimize`` substring-matched. Word-boundary on every
        length avoids that footgun.
      - A skill matches if AT LEAST ONE of its trigger keywords fires.
      - Invalid skills (frontmatter parse failures) are excluded —
        they're surfaced via ``td_skill_list`` instead.

    Returns the matched skill entries. Order is alphabetical by skill
    name so the caller gets a stable, deterministic list.
    """
    text = (user_text or "").lower().strip()
    if not text:
        return []

    matched: list[dict] = []
    for entry in _valid_entries():
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
                pat = r"\b" + re.escape(t) + r"\b"
                if re.search(pat, text):
                    matched.append(entry)
                    break
            except re.error:
                continue
    matched.sort(key=lambda e: e.get("name", ""))
    return matched


def get_skills_index_hint() -> str:
    """Short hint listing available VALID skills + their triggers.

    Invalid skills are excluded from the system-prompt index — the
    model shouldn't be encouraged to call ``skill_load`` on a broken
    skill. The user can still see them via ``td_skill_list``.
    """
    entries = _valid_entries()
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
    """Full content of all auto-load VALID skills, concatenated. Goes
    into the system prompt at runtime build time. Sorted by
    (priority desc, name) so byte-stability is preserved across turns
    (cache-friendly).

    Invalid skills are filtered out — auto-loading a broken skill
    body would inject malformed guidance into every turn's system
    prompt, which is exactly the opposite of what auto-load is for.
    """
    entries = [e for e in _valid_entries() if e["auto_load"]]
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
    """Enumerate every skill — valid and invalid — with its metadata,
    validity flag, and any frontmatter validation errors. Pre-1.7.2
    invalid skills were silently dropped; now they're surfaced so the
    user can see what's broken."""
    entries = _all_entries()
    invalid_count = sum(1 for e in entries if not e.get("valid", True))
    return {
        "ok": True,
        "count": len(entries),
        "valid_count": len(entries) - invalid_count,
        "invalid_count": invalid_count,
        "skills": [
            {
                "name": e["name"],
                "description": e["description"],
                "auto_load": e["auto_load"],
                "priority": e["priority"],
                "triggers": e["triggers"],
                "surface": e.get("surface", "both"),
                "source": e["source"],
                "filename": e["filename"],
                "size_bytes": len(e["text"].encode("utf-8")),
                "valid": e.get("valid", True),
                "validation_errors": e.get("validation_errors", []),
            }
            for e in sorted(entries, key=lambda x: x["name"])
        ],
    }


def handle_skill_validate(body: dict) -> dict:
    """Re-run frontmatter validation across all skills and return
    only the ones with errors. Useful for the user after editing a
    skill in ``~/.tdpilot-api/skills/`` — calls this and gets a focused
    diff of what's still broken without scrolling through all skills.

    With ``name``: validate only that one skill, return its full
    metadata + errors. Without: return all invalid skills.
    """
    name = (body or {}).get("name")
    if isinstance(name, str) and name.strip():
        target = name.strip()
        for entry in _all_entries():
            if entry["name"] == target or entry["filename"] in (target, target + ".md"):
                return {
                    "ok": True,
                    "name": entry["name"],
                    "valid": entry.get("valid", True),
                    "validation_errors": entry.get("validation_errors", []),
                    "filename": entry["filename"],
                    "source": entry["source"],
                }
        return {"error": f"Skill not found: {target}"}

    invalid = [e for e in _all_entries() if not e.get("valid", True)]
    return {
        "ok": True,
        "invalid_count": len(invalid),
        "invalid_skills": [
            {
                "name": e["name"],
                "filename": e["filename"],
                "source": e["source"],
                "validation_errors": e.get("validation_errors", []),
            }
            for e in invalid
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
