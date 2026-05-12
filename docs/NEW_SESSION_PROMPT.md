# New-session starter prompt — TDPilot DPSK4 v2.2.0→v3.0 implementation

This file is the copy-pasteable first message for a fresh agent session
that is picking up work on the v2.2.0→v3.0 roadmap.

**To use:** copy everything between the `==== BEGIN PROMPT ====` and
`==== END PROMPT ====` markers below into a new Claude Code (or other
agent) session as the user's first message.

The prompt is self-contained: it tells the agent where it is, what to
read first, what to build, and what footguns to avoid before any code
gets written.

The latest version of this file lives at
`docs/NEW_SESSION_PROMPT.md` on the `main` branch. If you're reading a
copy that disagrees with the repo, the repo wins.

---

## ==== BEGIN PROMPT ====

You're picking up work on **TDPilot DPSK4** — a TouchDesigner AI
assistant. The GitHub repo is `dreamrec/TDPilot_deepseekv4`. `cd`
into your local clone of it before running any commands below — this
prompt uses `<REPO_ROOT>` as a placeholder for whatever path you
cloned to.

### Current state

- Latest shipped release: **v2.3.0** (May 11, 2026 — bilateral-audit
  release closing 9 bugs + adding scoped snapshot tools; tool count
  91 → 93). v2.3.1 fixes have also landed (4-bug audit: ClassVar +
  watchdog + sticky-tier + exec-mode) at commit `8f087cd` on `main`.
- **v2.4 is in planning** — see
  [`docs/plans/v2.4_IMPLEMENTATION_PLAN.md`](./plans/v2.4_IMPLEMENTATION_PLAN.md)
  for the cold-start executable plan covering Phases A–E.
- Most recent merged PR: **#43** (4-bug audit).
- Branch you should start from: `origin/main` (or use the
  worktree referenced in the v2.4 plan if continuing prior work).

### Today's task

Start work on **Phase 0** of the v2.2.0→v3.0 roadmap. Phase 0 is
foundation plumbing — no user-visible UX yet — that subsequent phases
need to ship safely.

The full multi-month plan lives in
[`docs/ROADMAP.md`](./ROADMAP.md). Phase 0 is at
`docs/ROADMAP.md` lines 56–86 (search for `## Phase 0`).

### Before you write any code

Read these three files, in order. They override anything you remember
from training:

1. **[`AGENTS.md`](../AGENTS.md)** at the repo root — the fresh-agent
   kit: critical naming pins, the two .tox files, the canonical
   Textport rebuild recipe, the 7-version-manifest lockstep, the
   12-step release ritual, DeepSeek operating rules, and TD-specific
   gotchas. Don't skim — every section solves a real bug we've
   already hit.
2. **[`docs/ROADMAP.md`](./ROADMAP.md)** — the plan itself. Read
   "How to use this doc", "North-star vision", "Competitive context",
   then drill into Phase 0.
3. **`CHANGELOG.md`** (top entry) — what just shipped in v2.1.5, so
   you understand the "before" state your Phase 0 changes will sit on
   top of.

### The 3 deliverables of Phase 0

(Direct from `docs/ROADMAP.md` "Phase 0 — Foundation".)

1. **`td_component/tdpilot_api_features.py`** — central feature-flag
   registry. Reads COMP params first, then env vars, then
   `config.json`, then declarative `FLAGS` dict defaults. Tests can
   monkey-patch. Skeleton structure:

   ```python
   FLAGS = {
       "AUTO_ROLLBACK":   {"default": True,  "since": "2.2.0"},
       "CYCLE_DETECT":    {"default": True,  "since": "2.2.0"},
       "PLAN_PREVIEW":    {"default": False, "since": "2.5.0"},
       # ...
   }
   def is_enabled(flag_name: str) -> bool: ...
   def get(flag_name: str): ...
   ```

   Add this file to `_API_TOX_SOURCE_FILES` in
   `td_component/build_tdpilot_api_tox.py` so the freshness gate
   picks it up.

2. **`scripts/bench_chat_pipe.py`** — runs N canonical chat turns
   against the mock-DeepSeek fixture machinery already in
   `tests/_mock_deepseek.py` (see memory: PR-20 Mock-DeepSeek
   architecture). Reports per-tool latency, total turn time, token
   usage. We need this baseline before measuring Phase-1 perf
   regressions.

3. **`AGENTS.md` "Phase-PR test conventions" subsection** — codify:
   *every phase PR follows: source diff → unit tests → integration
   tests → request user tox rebuild → live verification.* This is the
   procedural piece that prevents tox-rebuild friction from
   torpedoing iteration speed.

Also add `tests/test_v220_features_module.py` (~6 tests) pinning the
flag-precedence rules: COMP param > env var > config.json > default.

### Hard constraints — do not violate

These are encoded in CI gates and pin tests; violating them = red CI.

- **Package name is `tdpilot-dpsk4`**, NOT `tdpilot`. Repo is
  `dreamrec/TDPilot_deepseekv4`, NOT `dreamrec/TDPilot`. Tests in
  `tests/test_release_critical_names.py` pin this.
- **Don't touch `_TOX_SOURCE_FILES` or `_API_TOX_SOURCE_FILES` lists
  with comment-only edits.** Even a `# noqa` comment bumps the source
  hash and fails `check_tox_freshness.py` / `check_tox_api_freshness.py`
  until the .tox is rebuilt in a live TD session. See memory:
  `feedback_noqa_on_tox_source_breaks_ci.md`.
- **DeepSeek thinking blocks must be echoed back to the API
  verbatim.** Only strip `reasoning_content` sub-keys. Stripping
  `type: thinking` content blocks causes HTTP 400 on the next turn.
  See memory: `feedback_deepseek_thinking_blocks_must_echo.md`.
- **TD Textport runs only single-line statements.** Multi-line `with`
  / `def` / `class` after a continuation prompt eats the next statement
  as SyntaxError. Always use the one-line `exec(compile(...))` form
  in the canonical rebuild recipe (AGENTS.md → ".tox rebuild").
- **No `time.sleep()` on TD's main thread.** It blocks cooks and
  invalidates diagnostics.
- **Use `comp.storage`, not module globals**, for state that has to
  survive textDAT reloads (WS clients, registries, etc.).
- **7 version manifests in lockstep.** Don't bump for this PR (Phase 0
  is docs+tests+plumbing only — no version bump until end of Phase 1
  when v2.2.0 ships). When you DO bump, all 7 files plus the
  `API_VERSION` constant move together. CI gate
  `scripts/check_versions.py` enforces.
- **Use `gh` CLI for git ops.** Not the GitHub web UI.
- **Squash-merge PRs.** Don't merge-commit. AGENTS.md has the full
  12-step release ritual; key non-obvious step: `gh release create`
  is mandatory after `git push origin vX.Y.Z` — pushing the tag
  alone does NOT fire `release-assets.yml`.
- **After every `git push`, check CI**: `gh run list --branch
  <branch>` or `gh pr checks`. Don't claim "done" until CI is green.

### First action you should take

Before writing any code:

```bash
cd <REPO_ROOT>                     # your local clone of dreamrec/TDPilot_deepseekv4
git fetch origin && git checkout -b claude/v2.2.0-phase-0-foundation origin/main
git log --oneline -5   # confirm 93c9a30 AGENTS.md is the tip
uv sync --extra dev --frozen
uv run pytest tests/ -q   # baseline: 1688/1688 should pass
uv run ruff check src tests scripts td_component
uv run python scripts/check_versions.py
uv run python scripts/check_tox_freshness.py
uv run python scripts/check_tox_api_freshness.py
```

If any of those fail on a clean `main`, STOP and investigate before
adding Phase 0 work on top.

Once the baseline is green, work through Phase 0's three deliverables
in the order listed above. After source changes land, ask me (the
user) to rebuild the API .tox using the canonical recipe in
`AGENTS.md` (search for "tdpilot-api-tox-rebuild") — do not attempt to
rebuild it yourself; the rebuild has to happen inside a running
TouchDesigner session, and there are 4 specific footguns documented
in that recipe.

### Release flow when Phase 0 is ready

Phase 0 does NOT bump version. It ships as a docs+plumbing PR like
PR #31 (the post-v2.1.5 hygiene fix) or PR #32 (AGENTS.md). The flow:

```
1. Commit on branch       claude/v2.2.0-phase-0-foundation
2. Push                   git push -u origin HEAD
3. Open PR                gh pr create --title "..." --body "..."
4. Watch CI               gh pr checks
5. Squash-merge           gh pr merge --squash
6. Pull main + cleanup    git checkout main && git pull && git branch -D claude/...
```

When Phase 1 ships at v2.2.0, the full 12-step release ritual kicks
in (see AGENTS.md). That's a separate PR.

### When in doubt

- Conflicting instructions? `docs/ROADMAP.md` is source-of-truth for
  the plan; `AGENTS.md` is source-of-truth for the workflow; this
  file is just the bootstrap. Defer to those.
- Surprising CI failure? Check `feedback_*.md` memory files first —
  most production footguns are already documented there.
- Unsure whether a change is "Phase 0 scope" or "Phase 1 scope"? Ask
  before coding. Phase boundaries matter for release-cadence
  discipline.

Acknowledge that you've read AGENTS.md, ROADMAP.md, and the latest
CHANGELOG entry. Then propose your Phase 0 implementation order before
writing any code.

## ==== END PROMPT ====

---

## Maintenance notes (not part of the prompt)

- This file is pinned by `tests/test_release_critical_names.py`. If
  you rename or move it, the pin test fails until the test is updated
  too. Same for `docs/ROADMAP.md`.
- Both docs are explicitly listed in `.gitignore` exceptions (search
  for `!/docs/ROADMAP.md` and `!/docs/NEW_SESSION_PROMPT.md`). The
  default policy under `docs/` is `/docs/*.md` deny-list — these two
  files plus `docs/MANUAL.md` and `docs/CHUNK_SCHEMA.md` are the only
  tracked free-form docs.
- When v2.2.0 ships, update this prompt's "Current state" section to
  reflect v2.2.0 as latest and shift "Today's task" to Phase 1 or
  Phase 2 depending on what's next. Same drift discipline as
  AGENTS.md.
- The prompt deliberately inlines the 7-or-so worst footguns rather
  than punting everything to AGENTS.md. If a future agent skips
  AGENTS.md (they will), the inlined constraints still protect the
  repo from the most expensive mistakes.
