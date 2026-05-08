"""PR-21 — bundled CLI plugin skill metadata fixtures (Phase 4, F-24b).

The CLI plugin (``skills/tdpilot-dpsk4-core``, ``tdpilot-dpsk4-production``,
``popx-touchdesigner``) ships SKILL.md files that Claude Code's plugin
host loads at runtime — we don't render them ourselves. So the test
surface is "what we ship": each SKILL.md must parse, declare a name +
description, stay under per-skill size budget, and the worst-case
combined loadout must fit a reasonable token budget.

These are NOT byte-pinning snapshot tests — pinning the absolute bytes
would break every time anyone edits a skill body. Instead each test
pins one *property* (parseability, required fields, byte budget).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Reuse the standalone loader's frontmatter parser — it's the same
# YAML-frontmatter format and the audit tightened the validator
# behaviour. tests/conftest.py adds td_component/ to sys.path.
import tdpilot_api_skills as skills

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO_ROOT / "skills"

# Per-skill body budget. Skills that grow past this should split into
# a smaller core SKILL.md + offloaded ``references/*.md``. The CLI
# plugin host inlines SKILL.md verbatim into the model's system context
# so unbounded growth burns shared cache budget.
PER_SKILL_BYTES_BUDGET = 80_000
# Worst-case combined budget (every plugin skill loaded simultaneously).
COMBINED_BYTES_BUDGET = 200_000

PLUGIN_SKILL_DIRS = sorted(p for p in SKILLS_DIR.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


# ---------------------------------------------------------------------------
# Existence + structural checks
# ---------------------------------------------------------------------------


def test_at_least_three_plugin_skills_present():
    """Sanity: the plugin ships ``tdpilot-dpsk4-core``,
    ``tdpilot-dpsk4-production`` and ``popx-touchdesigner``. If any
    disappears, the plugin's documented value prop is broken."""
    names = {p.name for p in PLUGIN_SKILL_DIRS}
    assert "tdpilot-dpsk4-core" in names
    assert "tdpilot-dpsk4-production" in names
    assert "popx-touchdesigner" in names


@pytest.mark.parametrize("skill_dir", PLUGIN_SKILL_DIRS, ids=lambda p: p.name)
def test_plugin_skill_frontmatter_parses(skill_dir: Path):
    """Every plugin SKILL.md must parse cleanly through the same
    frontmatter validator the standalone loader uses. Catches the same
    class of typos audit P3 #4 exposed in the standalone skills."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    meta, body, errors = skills._parse_frontmatter(text)
    assert errors == [], f"{skill_dir.name}/SKILL.md frontmatter errors: {errors}"
    assert meta.get("name"), f"{skill_dir.name}/SKILL.md missing 'name' field"
    assert meta.get("description"), f"{skill_dir.name}/SKILL.md missing 'description' field"
    assert body.strip(), f"{skill_dir.name}/SKILL.md has empty body"


@pytest.mark.parametrize("skill_dir", PLUGIN_SKILL_DIRS, ids=lambda p: p.name)
def test_plugin_skill_name_matches_directory(skill_dir: Path):
    """The frontmatter ``name`` must match the directory name. Claude
    Code's plugin host resolves skills by directory name, but the
    inlined ``# Skill: <name>`` header uses the frontmatter — drift
    between the two confuses users debugging which skill is active."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    meta, _body, _errors = skills._parse_frontmatter(text)
    assert meta["name"] == skill_dir.name, (
        f"{skill_dir.name}: frontmatter name {meta.get('name')!r} mismatches directory"
    )


# ---------------------------------------------------------------------------
# Size budget — per-skill and worst-case combined
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_dir", PLUGIN_SKILL_DIRS, ids=lambda p: p.name)
def test_plugin_skill_under_per_skill_budget(skill_dir: Path):
    size = (skill_dir / "SKILL.md").stat().st_size
    assert size < PER_SKILL_BYTES_BUDGET, (
        f"{skill_dir.name}/SKILL.md is {size}B, over {PER_SKILL_BYTES_BUDGET}B per-skill budget. "
        "Consider offloading to references/ or splitting."
    )


def test_plugin_skill_combined_under_worst_case_budget():
    """If a user has all three plugin skills active simultaneously
    (Claude Code's plugin host loads SKILL.md by activation), the
    combined SKILL.md bytes form the worst-case prompt-side cost.
    Stay under the budget so cache stays effective.
    """
    total = sum((p / "SKILL.md").stat().st_size for p in PLUGIN_SKILL_DIRS)
    assert total < COMBINED_BYTES_BUDGET, (
        f"combined plugin SKILL.md totals {total}B, over {COMBINED_BYTES_BUDGET}B budget"
    )


# ---------------------------------------------------------------------------
# Description quality — must mention "use this skill" or equivalent trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_dir", PLUGIN_SKILL_DIRS, ids=lambda p: p.name)
def test_plugin_skill_description_has_trigger_guidance(skill_dir: Path):
    """Claude Code's plugin host shows the description to the model
    when picking which skill to activate. A description without
    trigger guidance ("Use when X", "Use this skill for Y") is
    underspecified — the model has nothing to match user intent
    against. This regresses against audit P3 #4's recommendations.
    """
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    meta, _body, _errors = skills._parse_frontmatter(text)
    desc = (meta.get("description") or "").lower()
    has_trigger_phrase = any(
        phrase in desc
        for phrase in (
            "use this skill",
            "use when",
            "use it when",
            "trigger when",
            "applies when",
            "for ",
        )
    )
    assert has_trigger_phrase, (
        f"{skill_dir.name}: description lacks trigger guidance "
        f'(no "use when"/"use this skill"/"for X" phrasing). desc={desc!r}'
    )


# ---------------------------------------------------------------------------
# Body shape — H1 header present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_dir", PLUGIN_SKILL_DIRS, ids=lambda p: p.name)
def test_plugin_skill_body_starts_with_h1(skill_dir: Path):
    """The body convention is "frontmatter + blank line + ``# Title``
    H1 + content". An H1 grounds the model in what skill it's
    consulting. Subtler than it sounds — without an H1, the body
    starts with arbitrary prose and the model can mistake the first
    paragraph for a continuation of the system prompt."""
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    _meta, body, _errors = skills._parse_frontmatter(text)
    body_stripped = body.lstrip()
    assert body_stripped.startswith("# "), (
        f"{skill_dir.name}/SKILL.md body must start with an H1 header. First 80 chars: {body_stripped[:80]!r}"
    )
