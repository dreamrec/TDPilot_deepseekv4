# TDPilot DPSK4 — Implementation Plans

This directory holds cold-start executable plans for each release arc. Each file is self-contained — a fresh agent session can drop into it and execute.

## Active plans (v2.6 → v2.7)

Authored 2026-05-18 after deep audit of:
- Local v2.4.0 ship state (105 MCP tools, 2000 tests passing, kaleidoscope task verified)
- Upstream `dreamrec/TDPilot` v1.6.16 (2026-05-18) — activity log, `td_self_update`, `_read_journal` hints

**User-confirmed scope exclusions:** voice control deferred indefinitely.

| Plan | Theme | Effort | Status |
|---|---|---|---|
| [`v2.5_IMPLEMENTATION_PLAN.md`](./v2.5_IMPLEMENTATION_PLAN.md) | Agent self-awareness + safety + distribution polish | ~3 weeks | **SHIPPED 2026-05-19 as v2.5.0 + v2.5.1** |
| [`v2.6_IMPLEMENTATION_PLAN.md`](./v2.6_IMPLEMENTATION_PLAN.md) | Retrieval + knowledge (hybrid retrieval, skill packs, web ingestion) | ~3-4 weeks | not_started |
| [`v2.7_IMPLEMENTATION_PLAN.md`](./v2.7_IMPLEMENTATION_PLAN.md) | Orchestration + distribution maturity (Flow FSM, self-update, MCP Config) | ~6 weeks | not_started |

**Total timeline:** ~9-10 weeks remaining for v2.6 → v2.7 with disciplined cadence.

## v2.5 retrospective (2026-05-19)

**All 8 phases shipped end-to-end** in a single dev day, exceeding the ~3 week estimate:

| Phase | Slug | Outcome |
|---|---|---|
| v2.5.1 | activity-log | `td_get_activity_log` + chat-pipe ring + `_read_journal` hints in tool results |
| v2.5.2 | ocr-sidecar | `td_ocr_image` via PaddleOCR subprocess (`[ocr]` extras) |
| v2.5.3 | tool-approval-gates | `Approvalmode` COMP Menu param + 30s chat-banner click-through |
| v2.5.4 | auth-env-to-file | `maybe_migrate_env_to_file` closes drag-and-go shell-env hole |
| v2.5.5 | td-2025-32820-card | Already shipped pre-v2.5 |
| v2.5.6 | stdio-discipline | AST-based contract test for rogue `print()` in stdio mode |
| v2.5.7 | check-for-updates | `td_check_for_updates` (GitHub Releases API + .tox hash drift) |
| v2.5.8 | trace-viewer | `td_get_traces` reads chat-pipe JSONL trace files |

**Tool count: 105 → 109.** **Tests: 2000 → 2108 passing.**

Plus: post-ship live audit found 1 real bug (HEAD route 404) + chat-pipe surface gap for `td_get_traces` — both closed in v2.5.1 (chat-pipe alias) + the included HEAD route fix.

### Deferred to a future patch (logged from v2.5.0 live-chat audit)

| ID | Scope | Estimated effort |
|---|---|---|
| v2.5.1.2 | Chat-pipe `td_get_activity_log` — needs `ActivityRing` promoted to module singleton in `tdpilot_api_activity_log.py` | ~30 min |
| v2.5.1.3 | Chat-pipe `td_check_for_updates` — needs ~300 LOC of `src/td_mcp/lifecycle/update_check.py` ported to restricted-mode-safe TD-side code | ~2 hours |

Both are smaller than a full release; fold into v2.6 release engineering or ship as v2.5.2 patch.

## Post-2.5.3 audit retrospective (2026-05-19)

A fresh-eyes audit of the v2.5.3 ship state (run same day as v2.5.0/2.5.1/2.5.2/2.5.3 to validate the release before downstream consumers picked it up) surfaced **1 Critical + 4 High + 2 Medium security findings**, doc drift, and a regression-prevention testing gap. The follow-up shipped as PR [#53](https://github.com/dreamrec/TDPilot_deepseekv4/pull/53) (squash-merged as `6a9aabe`, 4 commits, 2113 → 2141 tests).

| Finding | Severity | Status |
|---|---|---|
| **C-1** MCP webserverDAT default-secure auth (inverts `_disable_auth` in `autostart.py`; breaking change for legacy zero-config users) | Critical | ✅ SHIPPED on main |
| **H-1** `snapshot_restore_scoped` path-traversal sandbox | High | ✅ SHIPPED |
| **H-2** `TDPILOT_DISABLE_TOOL_APPROVAL` truthiness fix | High | ✅ SHIPPED |
| **H-3** `td_ocr_image` extension + root allowlist | High | ✅ SHIPPED |
| **H-4** `Authmode=token` wins over stale `TDPILOT_API_INSECURE=1` | High | ✅ SHIPPED |
| **M-1** Traceback redaction in `callbacks/router.py` | Medium | ⏸ DEFERRED — see [`AUDIT_2026_05_19_FOLLOWUPS.md` § A.2](./AUDIT_2026_05_19_FOLLOWUPS.md#a2--m-1-traceback-redaction) (requires byte-equivalence baseline regen) |
| **M-2** `POST /set-authmode` lockout-direction `confirm: true` | Medium | ✅ SHIPPED |
| **D-1..D-4** Doc drift (v2.5 plan statuses, AGENTS counts, npm-publish OIDC docs) | — | ✅ SHIPPED |
| **Schema↔handler parity test** (closes v2.5.1 regression class) | — | ✅ SHIPPED — `tests/test_chat_pipe_surface_parity.py` |
| **C-1 part B** MCP-side origin allowlist | Critical (defense-in-depth) | ⏸ DEFERRED — see [§ A.1](./AUDIT_2026_05_19_FOLLOWUPS.md#a1--c-1-part-b-mcp-side-origin-allowlist) (requires byte-equivalence baseline regen) |
| **A-1** Extract `td_shared/` package (~4000 LOC silently forked between `src/td_mcp/` and `td_component/`) | Architecture | ⏸ PLANNED — see [§ B.1](./AUDIT_2026_05_19_FOLLOWUPS.md#b1--extract-td_shared-package-1-week--parity-ci) (~1 week, 5 phases) |
| **A-2** Split `tool_registry.py` (2168-line god-import) | Architecture | ⏸ PLANNED — depends on A-1 |
| **A-3** Decompose `_loop` (401-line method, all 3 v2.5.x bug sites lived here) | Architecture | ⏸ PLANNED |
| **Mock-evals scenario coverage** (cycle-detect / rollback / alias scenarios) | Testing | ⏸ PLANNED — [§ C](./AUDIT_2026_05_19_FOLLOWUPS.md#section-c--mock-evals-scenario-coverage-recommended-half-day) (~½ day, no .tox impact, highest regression-prevention ROI) |

**Headline breaking change**: C-1 inverts the MCP auth default from "always insecure, opt-out to secure" to "always secure, opt-in to insecure". Users who relied on the zero-config zero-auth flow need to set `TDPILOT_ENABLE_AUTH_BYPASS=1` in `~/.tdpilot-dpsk4/.tdpilot-dpsk4.env` (or run the `Authmode` wizard to install a secret). See the [CHANGELOG](../../CHANGELOG.md) "Unreleased" entry for the full migration matrix.

## Historical plans

| Plan | Status |
|---|---|
| [`v2.4_IMPLEMENTATION_PLAN.md`](./v2.4_IMPLEMENTATION_PLAN.md) | SHIPPED 2026-05-13 as v2.4.0 (Phase A/B/C + B-001..B-010 live-debug bugs) |
| [`v2.5_IMPLEMENTATION_PLAN.md`](./v2.5_IMPLEMENTATION_PLAN.md) | SHIPPED 2026-05-19 as v2.5.0 + v2.5.1 (see retrospective above) |

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
