# TDPilot DPSK4 — Implementation Plans

This directory holds cold-start executable plans for each release arc. Each file is self-contained — a fresh agent session can drop into it and execute.

## Active plans (v2.5 → v2.7)

Authored 2026-05-18 after deep audit of:
- Local v2.4.0 ship state (105 MCP tools, 2000 tests passing, kaleidoscope task verified)
- Upstream `dreamrec/TDPilot` v1.6.16 (2026-05-18) — activity log, `td_self_update`, `_read_journal` hints

**User-confirmed scope exclusions:** voice control deferred indefinitely.

| Plan | Theme | Effort | Status |
|---|---|---|---|
| [`v2.5_IMPLEMENTATION_PLAN.md`](./v2.5_IMPLEMENTATION_PLAN.md) | Agent self-awareness + safety + distribution polish | ~3 weeks | not_started |
| [`v2.6_IMPLEMENTATION_PLAN.md`](./v2.6_IMPLEMENTATION_PLAN.md) | Retrieval + knowledge (hybrid retrieval, skill packs, web ingestion) | ~3-4 weeks | not_started |
| [`v2.7_IMPLEMENTATION_PLAN.md`](./v2.7_IMPLEMENTATION_PLAN.md) | Orchestration + distribution maturity (Flow FSM, self-update, MCP Config) | ~6 weeks | not_started |

**Total timeline:** ~12-13 weeks (3 months) for v2.5 → v2.7 with disciplined cadence.

## Historical plans

| Plan | Status |
|---|---|
| [`v2.4_IMPLEMENTATION_PLAN.md`](./v2.4_IMPLEMENTATION_PLAN.md) | SHIPPED 2026-05-13 as v2.4.0 (Phase A/B/C + B-001..B-010 live-debug bugs) |

## How to use these plans

### Resuming work in a fresh session
1. Read [`../ROADMAP.md`](../ROADMAP.md) for high-level context.
2. Open the active plan file (`v2.5_IMPLEMENTATION_PLAN.md` to start).
3. Find the Phase Overview table — pick first row with `Status: not_started` or `in_progress`.
4. Follow that phase's section. Pre-flight checks → Files → Tests → Validation gates → Update status field in plan.
5. After phase merges to `main`, edit the plan file's overview table and set `Status: completed`. Push doc update.

### When you hit a blocker
Mark the phase `Status: blocked` and write a paragraph below the phase explaining what blocks. Move to the next independent phase.

### Cross-references
- [`AGENTS.md`](../../AGENTS.md) — operating rules, release flow, naming pins, .tox rebuild discipline, DeepSeek prefix-cache contract
- [`CHANGELOG.md`](../../CHANGELOG.md) — what shipped when
- [`NEW_SESSION_PROMPT.md`](../NEW_SESSION_PROMPT.md) — copy-pasteable starter prompt for fresh agents
- [`docs/ROADMAP.md`](../ROADMAP.md) — multi-release index + historical v2.2→v3.0 plan

## Plan format conventions

Each plan file follows the same structure:
- **§0 Bootstrap context** — current state, pre-conditions, derived-artifacts checklist
- **§1 Phase overview table** — IDs, effort, tox-rebuild flag, status
- **§N Per-phase detail** — branch name, pre-flight checks, files to create/modify, code shapes, tests, validation gates, risks, resume instructions
- **§Last Release engineering** — version bumps, tool-count update, .tox rebuilds, tag + GH release ritual
- **§Last+1 Risk register** — cross-cutting risks for the release
- **§Last+2 Resume instructions** — how a future session picks up

## Phase ID scheme

Phase IDs are stable slugs like `v2.5.1-activity-log` suitable for branch names, PR titles, commit messages. Format: `v<MAJOR>.<MINOR>.<PATCH>-<kebab-slug>`. Don't renumber; if a phase is dropped, leave the slot empty rather than renumbering downstream phases.
