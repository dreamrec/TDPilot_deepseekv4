"""Release-critical name pins for the dpsk4 fork (post-v2.1.5).

The repo was forked from `dreamrec/TDPilot` (parent) and renamed to
`dreamrec/TDPilot_deepseekv4`, with the npm package renamed from
`tdpilot` → `tdpilot-dpsk4` and the Python entrypoint similarly
renamed. The rename was incomplete in several release-critical
places:

  * `npm-publish.yml`'s "skip if already on registry" probe hard-coded
    `tdpilot@${tag}` (would silently probe the wrong package, and would
    falsely skip the dpsk4 publish if the parent shipped a matching
    version).
  * `scripts/runtime_stress_matrix.py` and `scripts/full_td_mcp_e2e.py`
    launched `uv run ... tdpilot`, which fails with ``ModuleNotFoundError``
    because this repo's `[project.scripts]` only registers
    `tdpilot-dpsk4`.
  * `scripts/install_claude_plugin.sh`'s uninstall instructions told
    the user to remove the wrong marketplace name.

All four were caught by the Codex review on PR #30 (post-v2.1.5). This
test pins them so a future rename or copy-paste from the parent fork's
docs doesn't silently regress any of them.

Approach: pure string/yaml inspection — no TD, no DeepSeek. Fast
enough to run on every PR.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_NPM_PACKAGE = "tdpilot-dpsk4"
EXPECTED_PY_SCRIPT = "tdpilot-dpsk4"
EXPECTED_REPO_SLUG = "dreamrec/TDPilot_deepseekv4"
EXPECTED_MARKETPLACE_NAME = "dreamrec-TDPilot_deepseekv4"


# ---------------------------------------------------------------------------
# Source of truth: pyproject + npm/package.json
# ---------------------------------------------------------------------------


def test_pyproject_registers_dpsk4_script():
    """The Python entrypoint must be ``tdpilot-dpsk4`` so the live-TD
    scripts (which launch it with ``uv run ... tdpilot-dpsk4``) work."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^\s*tdpilot-dpsk4\s*=\s*"[^"]+:main"\s*$', text, re.MULTILINE)
    assert m is not None, (
        "pyproject.toml [project.scripts] must declare `tdpilot-dpsk4 = ...`. "
        "Renaming this would break runtime_stress_matrix.py and "
        "full_td_mcp_e2e.py launches."
    )


def test_npm_package_name_is_dpsk4():
    """The npm-published package must be ``tdpilot-dpsk4``. The npm
    publish workflow reads this file at runtime."""
    pkg = json.loads((ROOT / "npm" / "package.json").read_text(encoding="utf-8"))
    assert pkg["name"] == EXPECTED_NPM_PACKAGE, (
        f"npm/package.json name must be `{EXPECTED_NPM_PACKAGE}`, got `{pkg['name']}`. "
        "Renaming would mismatch the npmjs.com Trusted Publisher binding."
    )


# ---------------------------------------------------------------------------
# Codex P1a — npm-publish.yml registry check
# ---------------------------------------------------------------------------


def test_npm_publish_workflow_does_not_hard_code_parent_pkg_name():
    """The "skip if version already on registry" check must NOT hard-code
    ``tdpilot@${tag_version}``. The fix reads the package name from
    ``npm/package.json`` at workflow runtime.

    Codex P1a on PR #30: pre-fix the check probed `tdpilot@X.Y.Z` (the
    parent fork's package). If `tdpilot` and `tdpilot-dpsk4` ever shared
    a version number, the dpsk4 publish would be falsely skipped.
    """
    text = (ROOT / ".github" / "workflows" / "npm-publish.yml").read_text(encoding="utf-8")
    # The bad pattern: hard-coded `"tdpilot@${tag_version}"` (parent fork name)
    # — note `\b` after `tdpilot` so this doesn't false-positive on the
    # correct `tdpilot-dpsk4@...` form.
    bad = re.search(r'"tdpilot@\$\{?tag_version\}?"', text)
    assert bad is None, (
        "npm-publish.yml registry-check step must NOT hard-code "
        '`"tdpilot@${tag_version}"`. Use the package name read from '
        "npm/package.json instead (see the v2.1.6 patch). Codex P1a "
        "on PR #30."
    )
    # And the fix MUST be present: `node -p "require('./npm/package.json').name"`
    assert "require('./npm/package.json').name" in text, (
        "npm-publish.yml must read the package name dynamically from "
        "npm/package.json via `node -p`. See the post-v2.1.5 patch."
    )


# ---------------------------------------------------------------------------
# Codex P1b — live-TD scripts launch the right entrypoint
# ---------------------------------------------------------------------------


def _server_args_literal(path: Path) -> str:
    """Return the chunk of source that constructs the StdioServerParameters
    args list — the line that hands a script name to ``uv run``. Used
    by the two P1b tests below.
    """
    text = path.read_text(encoding="utf-8")
    # Match: ["run", "--directory", <something>, "<script-name>"]
    m = re.search(
        r'\[\s*"run"\s*,\s*"--directory"\s*,\s*[^,\]]+,\s*"([^"]+)"\s*,?\s*\]',
        text,
    )
    assert m is not None, f"Could not find server-args literal in {path.name}"
    return m.group(1)


def test_runtime_stress_matrix_launches_dpsk4_script():
    """``scripts/runtime_stress_matrix.py`` must launch ``tdpilot-dpsk4``,
    not ``tdpilot``. The latter raises ``ModuleNotFoundError`` because
    this repo's pyproject.toml doesn't register that script.

    Codex P1b on PR #30."""
    script = _server_args_literal(ROOT / "scripts" / "runtime_stress_matrix.py")
    assert script == EXPECTED_PY_SCRIPT, (
        f"runtime_stress_matrix.py must launch `{EXPECTED_PY_SCRIPT}`, "
        f"got `{script}`. The parent fork's `tdpilot` entrypoint doesn't "
        "exist in this repo's pyproject.toml."
    )


def test_full_td_mcp_e2e_launches_dpsk4_script():
    """Same invariant as ``test_runtime_stress_matrix_launches_dpsk4_script``
    but for the e2e harness."""
    script = _server_args_literal(ROOT / "scripts" / "full_td_mcp_e2e.py")
    assert script == EXPECTED_PY_SCRIPT, (
        f"full_td_mcp_e2e.py must launch `{EXPECTED_PY_SCRIPT}`, got "
        f"`{script}`. See runtime_stress_matrix sibling test for context."
    )


# ---------------------------------------------------------------------------
# Codex P2a — install_claude_plugin.sh uninstall instructions
# ---------------------------------------------------------------------------


def test_install_script_uninstall_uses_correct_marketplace_name():
    """The uninstall snippet must remove ``dreamrec-TDPilot_deepseekv4``,
    not ``dreamrec-TDPilot`` (the parent fork's marketplace).

    Codex P2a on PR #30: pre-fix the user would follow instructions
    and remove the wrong marketplace, leaving the dpsk4 one installed."""
    text = (ROOT / "scripts" / "install_claude_plugin.sh").read_text(encoding="utf-8")
    # The marketplace-remove line must reference the dpsk4 marketplace.
    bad = re.search(r"claude plugin marketplace remove dreamrec-TDPilot\s*$", text, re.MULTILINE)
    assert bad is None, (
        "install_claude_plugin.sh uninstall instructions must NOT tell the "
        "user to remove `dreamrec-TDPilot` (the parent fork's marketplace). "
        f"Use `{EXPECTED_MARKETPLACE_NAME}` instead. Codex P2a on PR #30."
    )
    good = re.search(
        rf"claude plugin marketplace remove {re.escape(EXPECTED_MARKETPLACE_NAME)}",
        text,
    )
    assert good is not None, (
        f"install_claude_plugin.sh must contain "
        f"`claude plugin marketplace remove {EXPECTED_MARKETPLACE_NAME}` "
        "in the uninstall block."
    )


# ---------------------------------------------------------------------------
# Codex P3 — CI matrix tests every supported Python version
# ---------------------------------------------------------------------------


def test_ci_python_matrix_includes_every_supported_version():
    """If ``pyproject.toml`` says ``requires-python = ">=3.10"``, then CI
    must actually run the test suite on 3.10. Pre-fix CI only tested
    3.11 + 3.12, leaving the 3.10 claim untested.

    Codex P3 on PR #30. The reviewer ran the full suite under 3.10
    locally and it passed 1688/1688 — adding 3.10 to the matrix pins
    that claim against accidental future regressions.
    """
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
    assert m is not None
    constraint = m.group(1).strip()
    # The constraint is `>=X.Y` style; extract the minimum.
    floor_match = re.search(r">=\s*3\.(\d+)", constraint)
    assert floor_match, f"Unexpected requires-python constraint: {constraint!r}"
    floor_minor = int(floor_match.group(1))
    expected_min = f"3.{floor_minor}"

    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # Match the matrix line in the `test` job (the only matrix block).
    m = re.search(r"python-version:\s*\[([^\]]+)\]", ci_text)
    assert m is not None, "ci.yml matrix.python-version not found"
    versions = [v.strip().strip('"').strip("'") for v in m.group(1).split(",")]
    assert expected_min in versions, (
        f"CI matrix must include `{expected_min}` (pyproject.toml declares "
        f'`requires-python = "{constraint}"`). Found versions: {versions}. '
        "Codex P3 on PR #30."
    )


# ---------------------------------------------------------------------------
# AGENTS.md presence + content invariants (post-PR-31 review)
# ---------------------------------------------------------------------------


def test_agents_md_exists_at_repo_root():
    """AGENTS.md must exist at the repo root so coding agents (Claude,
    Codex, Copilot, Cursor) can discover it without searching.

    Reviewer on PR #30 called this out explicitly: "I'd also commit a
    cleaned-up AGENTS.md to main, because the local one contains
    important release and DeepSeek-specific operating rules that future
    agents won't see from the public repo." The AGENTS.md convention
    (https://agents.md/) places the file at repo root by default.
    """
    path = ROOT / "AGENTS.md"
    assert path.is_file(), (
        "AGENTS.md must exist at the repo root. See "
        "https://agents.md/ for the convention. Without it, fresh "
        "agents miss the release flow, .tox rebuild dance, DeepSeek "
        "prefix-cache rules, and TD-specific gotchas."
    )


def test_agents_md_pins_all_critical_names():
    """AGENTS.md must reference every canonical name from the
    naming-pins table in `Critical naming pins`. If a future edit
    accidentally drops one, this test fails so it stays consistent
    with the rest of the repo."""
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    required_names = (
        EXPECTED_NPM_PACKAGE,  # tdpilot-dpsk4
        EXPECTED_REPO_SLUG,  # dreamrec/TDPilot_deepseekv4
        EXPECTED_MARKETPLACE_NAME,  # dreamrec-TDPilot_deepseekv4
    )
    for name in required_names:
        assert name in text, (
            f"AGENTS.md is missing the canonical name `{name}`. The "
            "Critical naming pins table is load-bearing — every release-"
            "critical identifier should be listed there so a future agent "
            "doesn't have to grep for them."
        )


def test_agents_md_covers_release_critical_topics():
    """AGENTS.md must mention every load-bearing topic from the
    project's operational rules. These are the topics that have cost
    real debug time and that the user's reviewer specifically wanted
    documented in the public repo (PR #30 follow-up).

    Checks are deliberately loose (substring match) so the exact
    wording can evolve while still pinning that the topic IS covered.
    """
    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8").lower()
    required_topics = {
        # Release flow
        "gh release create": "GitHub Release creation step (otherwise release-assets.yml never fires)",
        "scripts/check_versions.py": "version-sync gate",
        "check_tox_freshness": ".tox freshness gate",
        # DeepSeek-specific
        "thinking": "the thinking-blocks echo rule",
        "prefix-cache": "DeepSeek auto-cache stability requirement",
        # Security model
        "tdpilot_api_insecure": "the insecure-mode escape hatch",
        "exec_mode": "the td_exec_python sandbox levels",
        # TD-specific gotchas
        "cook thread": "the cook-thread vs worker-thread split",
        "comp.storage": "state-survives-reload pattern",
        "feedbacktop": "the canonical feedbackTOP wiring discipline",
        "webrendertop": "the http:// vs file:// origin trap",
        # Tox dance
        "_tox_source_files": "the source-file list that gets baked into the .tox",
        "td_mcp_repo_root": "the env var required for the rebuild recipe",
    }
    missing = [(k, why) for k, why in required_topics.items() if k.lower() not in text]
    assert not missing, "AGENTS.md is missing these load-bearing topics:\n" + "\n".join(
        f"  - {k!r} — {why}" for k, why in missing
    )


# ---------------------------------------------------------------------------
# docs/ROADMAP.md + docs/NEW_SESSION_PROMPT.md (added post-v2.1.5 alongside
# AGENTS.md): the v2.2.0→v3.0 implementation plan plus a copy-pasteable
# starter prompt for fresh agent sessions. Both are explicitly exempted
# from the `/docs/*.md` deny-list in .gitignore so they ship with the
# public repo. If either file is renamed, moved, or stripped of the
# load-bearing structure that makes it useful, these tests fail.
# ---------------------------------------------------------------------------


def test_roadmap_md_exists_and_covers_all_phases():
    """`docs/ROADMAP.md` must exist and cover every phase (0–6) of the
    v2.2.0→v3.0 plan. Phase numbering is load-bearing: the
    `NEW_SESSION_PROMPT.md` starter and `AGENTS.md` cross-reference both
    point at specific phase headings. If a phase section gets deleted
    accidentally, this test fails so the deletion is forced to be
    deliberate (and the cross-refs get updated in the same PR).

    Also asserts cross-links to the bootstrap prompt and AGENTS.md are
    present, so a fresh agent landing on the ROADMAP has working
    pointers to the other two source-of-truth docs.
    """
    path = ROOT / "docs" / "ROADMAP.md"
    assert path.is_file(), (
        "docs/ROADMAP.md must exist. It's the v2.2.0→v3.0 implementation "
        "plan, source-of-truth for what we're building next. Without it, "
        "a fresh agent session has nowhere to anchor multi-phase work. "
        "If you're intentionally removing it, also remove the "
        "`!/docs/ROADMAP.md` exception in .gitignore and the AGENTS.md "
        "cross-reference."
    )
    text = path.read_text(encoding="utf-8")
    # Every phase must have a top-level heading. Phase numbering is
    # referenced by NEW_SESSION_PROMPT.md and AGENTS.md cross-links.
    for phase in range(7):
        marker = f"## Phase {phase}"
        assert marker in text, (
            f"docs/ROADMAP.md is missing the `{marker}` section. "
            "Phase headings are load-bearing — they're cited by line "
            "number in NEW_SESSION_PROMPT.md and referenced by "
            "phase-PR branch names like `claude/v2.2.0-phase-0-foundation`."
        )
    # Cross-links to the bootstrap and operating-rules docs must work.
    required_links = (
        "NEW_SESSION_PROMPT.md",
        "AGENTS.md",
        "CHANGELOG.md",
    )
    for link in required_links:
        assert link in text, (
            f"docs/ROADMAP.md is missing a reference to `{link}`. "
            "The 'How to use this doc' section links to all three; "
            "removing one strands fresh agents who follow the chain."
        )


def test_new_session_prompt_md_exists_with_required_invariants():
    """`docs/NEW_SESSION_PROMPT.md` must exist and preserve the
    properties that make it useful:

    1. The `==== BEGIN PROMPT ====` / `==== END PROMPT ====` markers
       (the copy-paste UX depends on them being unambiguous).
    2. Inline references to the worst footguns — package name,
       thinking-blocks echo rule, .tox-freshness gates, 7-manifest
       lockstep. These exist inside the prompt body precisely so a
       future agent that skips AGENTS.md (they will) is still
       protected from the most expensive mistakes.
    3. A pointer to AGENTS.md and ROADMAP.md, so the bootstrap chain
       stays intact.

    If a future edit removes any of these, this test fails — forcing
    the maintainer to either restore the invariant or update this pin
    deliberately.
    """
    path = ROOT / "docs" / "NEW_SESSION_PROMPT.md"
    assert path.is_file(), (
        "docs/NEW_SESSION_PROMPT.md must exist. It's the copy-pasteable "
        "starter prompt for a fresh agent picking up roadmap work. "
        "If you're intentionally removing it, also remove the "
        "`!/docs/NEW_SESSION_PROMPT.md` exception in .gitignore and the "
        "AGENTS.md cross-reference."
    )
    text = path.read_text(encoding="utf-8")
    # Copy-paste markers — the whole UX of this file depends on a
    # human (or agent-operator) being able to extract the prompt body
    # by between-marker selection.
    assert "==== BEGIN PROMPT ====" in text, (
        "docs/NEW_SESSION_PROMPT.md is missing the `==== BEGIN PROMPT ====` "
        "marker. Copy-paste extraction breaks without it."
    )
    assert "==== END PROMPT ====" in text, (
        "docs/NEW_SESSION_PROMPT.md is missing the `==== END PROMPT ====` "
        "marker. Copy-paste extraction breaks without it."
    )
    # Inlined footguns — these are deliberately duplicated from
    # AGENTS.md so an agent that skips AGENTS.md still gets the
    # protection.
    text_lc = text.lower()
    required_inlined_constraints = {
        EXPECTED_NPM_PACKAGE.lower(): "package-name pin",
        "thinking": "DeepSeek thinking-blocks echo rule",
        "check_tox_freshness": "tox-freshness gate",
        "check_versions.py": "7-manifest version-sync gate",
        "gh release create": "GitHub Release creation step",
        "comp.storage": "state-survives-reload pattern",
    }
    missing_inlined = [
        (k, why) for k, why in required_inlined_constraints.items() if k.lower() not in text_lc
    ]
    assert not missing_inlined, (
        "docs/NEW_SESSION_PROMPT.md is missing these inlined footgun callouts:\n"
        + "\n".join(f"  - {k!r} — {why}" for k, why in missing_inlined)
    )
    # Bootstrap chain — pointers to the two source-of-truth docs the
    # fresh agent must read.
    for required_link in ("AGENTS.md", "ROADMAP.md"):
        assert required_link in text, (
            f"docs/NEW_SESSION_PROMPT.md is missing a reference to `{required_link}`. "
            "The bootstrap chain breaks if either link drops."
        )
