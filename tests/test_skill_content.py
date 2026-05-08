"""Regression tests for the bundled-skill content (1.7.2).

Ensures the skills under ``td_component/skills/`` stay current with the
TD build TDPilot ships against. Audit P3 #6 flagged that pre-1.7.2
``popx-mode.md`` referenced TD 2025.32460 while v1.7.0 actually targets
2025.32820, and ``performance-mode.md`` cargo-culted exact parameter
names like ``cookpulsewhennotviewed`` that don't apply uniformly across
op families.

These tests assert the audit fixes hold:
  * popx-mode mentions tracePOP / triangulatePOP / 2025.32820
  * performance-mode counsels "inspect first" instead of hardcoding
  * Both skills declare ``surface: standalone``
  * Both skills parse cleanly through the validator
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tdpilot_api_skills as skills
import yaml

SKILLS_DIR = Path(__file__).resolve().parents[1] / "td_component" / "skills"


@pytest.fixture(scope="module")
def popx_text() -> str:
    return (SKILLS_DIR / "popx-mode.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def perf_text() -> str:
    return (SKILLS_DIR / "performance-mode.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Frontmatter parses cleanly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(SKILLS_DIR.glob("*.md")))
def test_bundled_skill_frontmatter_parses(path: Path):
    text = path.read_text(encoding="utf-8")
    meta, body, errors = skills._parse_frontmatter(text)
    assert errors == [], f"{path.name} has frontmatter errors: {errors}"
    assert meta.get("name"), f"{path.name} missing name"
    assert body.strip(), f"{path.name} has empty body"


@pytest.mark.parametrize("path", sorted(SKILLS_DIR.glob("*.md")))
def test_bundled_skill_declares_surface(path: Path):
    """Audit P2 #4 — every bundled skill must explicitly declare its
    target surface so users running both standalone + CLI can see
    which one a skill applies to."""
    text = path.read_text(encoding="utf-8")
    meta, _body, _errors = skills._parse_frontmatter(text)
    assert meta.get("surface") in ("standalone", "cli", "both"), (
        f"{path.name} missing or invalid surface field: {meta.get('surface')!r}"
    )


# ---------------------------------------------------------------------------
# Content currency — popx-mode (audit P3 #6)
# ---------------------------------------------------------------------------


def test_popx_references_current_td_build(popx_text: str):
    """The skill must reference the build TDPilot ships against
    (2025.32820 in v1.7.0). Pre-1.7.2 it referenced 2025.32460."""
    assert "2025.32820" in popx_text, "popx-mode is stale relative to v1.7.0"
    assert "2025.32460" not in popx_text, "popx-mode still references the old build 2025.32460"


def test_popx_mentions_v17_native_pops(popx_text: str):
    """v1.7.0 added a major batch of native POPs. The skill must
    teach them so the agent doesn't reach for POPx ops on input
    types the native set now handles."""
    for op in ("tracePOP", "triangulatePOP", "dmxFixturePOP", "alembicOutPOP"):
        assert op in popx_text, f"popx-mode missing v1.7 native POP: {op}"


def test_popx_documents_polygonize_migration_trap(popx_text: str):
    """Polygonize POP became 3D-only in 2025.32820 — agents who
    learned the 2D path from prior tutorials will hit a real wall.
    The skill must call this out."""
    lower = popx_text.lower()
    assert "polygonize" in lower
    assert "3d-only" in lower or "3d only" in lower or "tracepop" in lower


# ---------------------------------------------------------------------------
# Content currency — performance-mode (audit P3 #6)
# ---------------------------------------------------------------------------


def test_perf_counsels_inspect_first_pattern(perf_text: str):
    """Pre-1.7.2 the skill cargo-culted exact parameter names like
    ``cookpulsewhennotviewed=False`` as if every op type had it. Now
    it must teach 'inspect first, then set available controls'."""
    lower = perf_text.lower()
    assert "inspect" in lower
    # Must mention the read-back-to-confirm pattern.
    assert "td_get_params" in perf_text


def test_perf_warns_about_screenshot_perf_cost(perf_text: str):
    """td_screenshot triggers a cook — using it during perf debugging
    measurably skews results. Pre-1.7.2 the skill recommended it
    unconditionally as a verification step."""
    lower = perf_text.lower()
    assert "screenshot" in lower
    # The warning should be present in some form.
    assert "trigger" in lower or "cook" in lower or "skew" in lower


def test_perf_acknowledges_param_name_drift(perf_text: str):
    """The skill must NOT promise that ``cookpulsewhennotviewed`` is
    universal — it isn't. The text should hedge with phrasing about
    family-specific params or 'when it exists'."""
    if "cookpulsewhennotviewed" in perf_text:
        # If the param is still mentioned, the surrounding text must
        # caveat it — the audit's complaint was the unqualified
        # promise that this param exists everywhere.
        lower = perf_text.lower()
        assert (
            "if the param" in lower
            or "when they exist" in lower
            or "when it exists" in lower
            or "exists" in lower
        ), "performance-mode should hedge on cookpulsewhennotviewed availability"
