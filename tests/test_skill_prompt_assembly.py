"""PR-21 — skill prompt assembly fixtures (Phase 4, F-24b).

The standalone runtime's ``build_system_prompt()`` must be byte-stable
across calls and across user-skill insertion order; the auto-load
skill body ordering must be deterministic; ``find_triggered_skills``
must be alphabetical; user-dir skills with duplicate names must
override bundled. The full system prompt with worst-case skill
loadout must fit comfortably under model context.

These are NOT byte-pinning snapshot tests — pinning the absolute
prompt bytes would break every time anyone edits a skill body or
the SYSTEM_PROMPT_BASE literal. Instead each test pins one
*property* (determinism, ordering, size, required sections).
"""

from __future__ import annotations

from pathlib import Path

import pytest

# tests/conftest.py adds td_component/ to sys.path so these direct imports
# work — same pattern as test_skill_validation.py and test_skill_content.py.
import tdpilot_api_skills as skills
from tdpilot_api_runtime import build_system_prompt

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SKILLS_DIR = REPO_ROOT / "td_component" / "skills"

# Worst-case prompt budget. SYSTEM_PROMPT_BASE is ~3.5KB; the two
# bundled skills together are ~10KB. Even with both auto-loading and
# every CLI plugin skill stuffed into the user dir, the total should
# stay well under the DeepSeek 64K context-prompt soft ceiling — the
# whole point of build_system_prompt() is to be cache-friendly, not
# context-blowout.
WORST_CASE_PROMPT_BYTES = 80_000


# ---------------------------------------------------------------------------
# build_system_prompt determinism
# ---------------------------------------------------------------------------


def test_build_system_prompt_is_byte_deterministic():
    """Two consecutive calls with identical state must return identical
    bytes. This is the cache contract — DeepSeek's ~50× input-cache
    discount only fires when the system message is byte-identical
    across turns."""
    a = build_system_prompt()
    b = build_system_prompt()
    assert a == b


def test_build_system_prompt_independent_of_user_skill_insertion_order(tmp_path, monkeypatch):
    """If a user adds two skills, the system prompt must be identical
    regardless of the order they were written to disk. The loader's
    ``sorted(...glob('*.md'))`` plus the alphabetical sort inside
    ``get_skills_index_hint`` and ``get_auto_load_skills_text`` are the
    invariants under test.
    """
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    # Drop the bundled skills out of this test so we're isolating the
    # user-dir ordering invariant.
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    skill_a = """---
name: alpha-skill
description: first
auto_load: true
priority: 5
---

A body
"""
    skill_b = """---
name: beta-skill
description: second
auto_load: true
priority: 5
---

B body
"""

    # Order 1: alpha first.
    (tmp_path / "alpha.md").write_text(skill_a, encoding="utf-8")
    (tmp_path / "beta.md").write_text(skill_b, encoding="utf-8")
    prompt_1 = build_system_prompt()

    # Wipe and rewrite in the opposite filesystem order.
    for p in tmp_path.iterdir():
        p.unlink()
    (tmp_path / "beta.md").write_text(skill_b, encoding="utf-8")
    (tmp_path / "alpha.md").write_text(skill_a, encoding="utf-8")
    prompt_2 = build_system_prompt()

    assert prompt_1 == prompt_2


def test_build_system_prompt_required_sections():
    """Every protocol header that the agent's behaviour relies on must
    survive any future refactor of ``SYSTEM_PROMPT_BASE``. If one of
    these strings is removed accidentally, the agent silently loses a
    discipline rule — the failure mode is hard to spot at runtime."""
    prompt = build_system_prompt()
    for required in (
        "Operating protocol:",
        "Critical rules for TouchDesigner type names:",
        "Memory protocol:",
        "Knowledge protocol:",
        "Recipe protocol:",
        "Skills protocol:",
        "Safety / patch protocol:",
        # Specific tool callouts the model needs to know about.
        "skill_load",
        "td_get_errors",
        "patch_begin",
    ):
        assert required in prompt, f"system prompt missing required section: {required!r}"


# ---------------------------------------------------------------------------
# Auto-load body ordering — priority desc, then name asc
# ---------------------------------------------------------------------------


def test_get_auto_load_skills_text_orders_priority_desc_then_name(tmp_path, monkeypatch):
    """Three auto-load skills, two at the same priority. Output must be
    (priority desc, then name asc inside a tie). This is the exact
    ordering documented in tdpilot_api_skills.get_auto_load_skills_text
    — it's load-bearing because it determines which skill's discipline
    appears first in the system prompt."""
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    (tmp_path / "low.md").write_text(
        """---
name: zeta-low
description: x
auto_load: true
priority: 1
---

ZETA-LOW-BODY
""",
        encoding="utf-8",
    )
    (tmp_path / "tie_a.md").write_text(
        """---
name: alpha-tie
description: x
auto_load: true
priority: 5
---

ALPHA-TIE-BODY
""",
        encoding="utf-8",
    )
    (tmp_path / "tie_b.md").write_text(
        """---
name: beta-tie
description: x
auto_load: true
priority: 5
---

BETA-TIE-BODY
""",
        encoding="utf-8",
    )

    text = skills.get_auto_load_skills_text()
    # Priority 5 must come before priority 1.
    assert text.index("ALPHA-TIE-BODY") < text.index("ZETA-LOW-BODY")
    assert text.index("BETA-TIE-BODY") < text.index("ZETA-LOW-BODY")
    # Inside the priority-5 tie, alpha-tie precedes beta-tie (name asc).
    assert text.index("ALPHA-TIE-BODY") < text.index("BETA-TIE-BODY")


def test_get_auto_load_excludes_non_auto_load_skills(tmp_path, monkeypatch):
    """Only skills with ``auto_load: true`` go into the auto-load text.
    A regular skill must NOT have its body injected — it's still
    listed in the index hint, but the body is fetched on demand via
    skill_load.
    """
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    (tmp_path / "manual.md").write_text(
        """---
name: manual-skill
description: not auto-load
---

MANUAL-BODY-MUST-NOT-APPEAR
""",
        encoding="utf-8",
    )
    (tmp_path / "auto.md").write_text(
        """---
name: auto-skill
description: auto-load
auto_load: true
---

AUTO-BODY
""",
        encoding="utf-8",
    )

    text = skills.get_auto_load_skills_text()
    assert "AUTO-BODY" in text
    assert "MANUAL-BODY-MUST-NOT-APPEAR" not in text


# ---------------------------------------------------------------------------
# Skills index hint — alphabetical, valid-only, format
# ---------------------------------------------------------------------------


def test_skills_index_hint_alphabetical(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    for name in ("zebra", "alpha", "mango"):
        (tmp_path / f"{name}.md").write_text(
            f"""---
name: {name}-skill
description: x
---

body
""",
            encoding="utf-8",
        )

    hint = skills.get_skills_index_hint()
    a = hint.index("alpha-skill")
    m = hint.index("mango-skill")
    z = hint.index("zebra-skill")
    assert a < m < z


def test_skills_index_hint_marks_auto_load(tmp_path, monkeypatch):
    """``[auto]`` marker on auto-load skills lets the user (and the
    model) tell at a glance which skills are session-pinned vs
    on-demand."""
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    (tmp_path / "auto.md").write_text(
        """---
name: pinned
description: always on
auto_load: true
---

body
""",
        encoding="utf-8",
    )
    (tmp_path / "manual.md").write_text(
        """---
name: ondemand
description: opt-in
---

body
""",
        encoding="utf-8",
    )

    hint = skills.get_skills_index_hint()
    assert "pinned [auto]" in hint
    # Manual skill must NOT carry the [auto] marker.
    assert "ondemand [auto]" not in hint


# ---------------------------------------------------------------------------
# User-dir overrides bundled
# ---------------------------------------------------------------------------


def test_user_skill_overrides_bundled_with_same_name(tmp_path, monkeypatch):
    """A user-supplied skill whose ``name`` collides with a bundled
    one must take precedence — that's the documented contract for
    user-customisable skills.
    """
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)

    fake_bundled = [
        skills._build_entry(
            """---
name: shared-name
description: bundled version
---

BUNDLED-BODY
""",
            source="bundled",
            default_name="shared-name",
            filename="shared-name.md",
        )
    ]
    monkeypatch.setattr(skills, "_bundled_entries", lambda: fake_bundled)

    (tmp_path / "override.md").write_text(
        """---
name: shared-name
description: user version
---

USER-OVERRIDE-BODY
""",
        encoding="utf-8",
    )

    out = skills.handle_skill_get({"name": "shared-name"})
    assert out["ok"] is True
    assert "USER-OVERRIDE-BODY" in out["content"]
    assert "BUNDLED-BODY" not in out["content"]
    assert out["source"] == "user"


# ---------------------------------------------------------------------------
# find_triggered_skills determinism — alphabetical by name
# ---------------------------------------------------------------------------


def test_find_triggered_skills_returns_alphabetical(tmp_path, monkeypatch):
    """Two skills both trigger on the same word. The returned list
    must be alphabetical so callers (and the chat UI) get a stable,
    deterministic order regardless of filesystem listing order.
    """
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    monkeypatch.setattr(skills, "_bundled_entries", lambda: [])

    (tmp_path / "z.md").write_text(
        """---
name: zulu
description: z
triggers: [shared]
---

body
""",
        encoding="utf-8",
    )
    (tmp_path / "a.md").write_text(
        """---
name: alpha
description: a
triggers: [shared]
---

body
""",
        encoding="utf-8",
    )

    matched = skills.find_triggered_skills("the shared word activates both")
    assert [m["name"] for m in matched] == ["alpha", "zulu"]


# ---------------------------------------------------------------------------
# Worst-case prompt size guard
# ---------------------------------------------------------------------------


def test_build_system_prompt_under_worst_case_budget(tmp_path, monkeypatch):
    """Worst-case loadout: every bundled skill marked auto-load + a
    handful of synthetic large user skills also marked auto-load.
    The assembled prompt must stay under the worst-case byte budget.

    This is the "fits within model context after worst-case skill
    loadout" check from the PR-21 plan. Failure here is a signal to
    either trim a skill or split it into a non-auto-load (skill_load
    on-demand) variant.
    """
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", tmp_path)
    # Synthesize three large user skills, all auto-load, large bodies.
    for i in range(3):
        (tmp_path / f"big{i}.md").write_text(
            f"""---
name: big-skill-{i}
description: large worst-case skill
auto_load: true
priority: {10 - i}
---

"""
            + ("payload line.\n" * 200),
            encoding="utf-8",
        )

    prompt = build_system_prompt()
    assert len(prompt.encode("utf-8")) < WORST_CASE_PROMPT_BYTES, (
        f"system prompt {len(prompt.encode('utf-8'))}B exceeds worst-case budget {WORST_CASE_PROMPT_BYTES}B"
    )


# ---------------------------------------------------------------------------
# Bundled skills auto-load text — ordering survives the bundled DAT pathway
# ---------------------------------------------------------------------------


def test_bundled_auto_load_ordering_via_fake_dats(monkeypatch):
    """Even when skills come through the bundled-DAT path (instead of
    the user-dir path), the (priority desc, name asc) ordering must
    hold. This guards against future regressions where someone might
    accidentally short-circuit the sort for the bundled path.
    """
    fake_bundled = [
        skills._build_entry(
            """---
name: gamma-bundle
description: x
auto_load: true
priority: 5
---

GAMMA-BODY
""",
            source="bundled",
            default_name="gamma-bundle",
            filename="gamma-bundle.md",
        ),
        skills._build_entry(
            """---
name: alpha-bundle
description: x
auto_load: true
priority: 5
---

ALPHA-BODY
""",
            source="bundled",
            default_name="alpha-bundle",
            filename="alpha-bundle.md",
        ),
        skills._build_entry(
            """---
name: zulu-bundle
description: x
auto_load: true
priority: 9
---

ZULU-BODY
""",
            source="bundled",
            default_name="zulu-bundle",
            filename="zulu-bundle.md",
        ),
    ]
    monkeypatch.setattr(skills, "_bundled_entries", lambda: fake_bundled)
    monkeypatch.setattr(skills, "USER_SKILLS_DIR", Path("/nonexistent_dir_for_test"))

    text = skills.get_auto_load_skills_text()
    # Highest priority (zulu, 9) comes first; alpha and gamma tie at 5,
    # so alpha precedes gamma alphabetically.
    assert text.index("ZULU-BODY") < text.index("ALPHA-BODY") < text.index("GAMMA-BODY")


# ---------------------------------------------------------------------------
# Bundled skill subset — bytes-stable summary smoke test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(BUNDLED_SKILLS_DIR.glob("*.md")))
def test_bundled_skill_size_under_per_skill_budget(path: Path):
    """No single bundled skill should exceed 32KB — that's a guard
    against accidentally checking in a bloated draft. Keeps the
    per-skill load reasonable when a user calls ``skill_load`` on
    demand."""
    size = path.stat().st_size
    assert size < 32_000, f"{path.name} is {size}B, over 32KB per-skill budget"
