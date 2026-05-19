# Audit 2026-05-19 — Follow-Ups Plan

This doc tracks the items deferred from the 2026-05-19 fresh repo audit. The
audit itself ran against `main` at v2.5.3 (commits `7fea6fa` → `365bf4e`) and
surfaced 1 Critical, 4 High, 2 Medium security findings plus 3 architectural
debt items.

**✅ Merged to `main`** via PR [#53](https://github.com/dreamrec/TDPilot_deepseekv4/pull/53), squash-commit `6a9aabe`, on 2026-05-19. All 7 CI gates green at merge time. Both `.tox` files rebuilt in TouchDesigner 2025.32820 against the audit-fix edits and bundled into the PR:

| Finding | Severity | Status | Original branch commit |
|---|---|---|---|
| D-1..D-4 doc drift | — | ✅ on main | `30b7c20` |
| H-3 OCR path sandbox | High | ✅ on main | `30b7c20` |
| Bidirectional schema↔handler parity test | — | ✅ on main | `30b7c20` |
| C-1 MCP auth default-secure (autostart.py) | Critical | ✅ on main + .tox rebuilt | `727282b` + `b004baf` |
| H-1 snapshot path traversal | High | ✅ on main + .tox rebuilt | `727282b` + `b004baf` |
| H-2 approval-gate truthiness | High | ✅ on main + .tox rebuilt | `727282b` + `b004baf` |
| H-4 Authmode env-var bypass | High | ✅ on main + .tox rebuilt | `727282b` + `b004baf` |
| M-2 /set-authmode lockout | Medium | ✅ on main + .tox rebuilt | `727282b` + `b004baf` |

The two CI freshness gates (`check_tox_freshness.py` /
`check_tox_api_freshness.py`) are green on `main`. The seven version
manifests still read `2.5.3`; bumping them to `2.5.4` is a separate
ritual (touches files baked into both `.tox` source-hash sets, so
would require another rebuild before tag + release).

---

## Section A — Deferred audit items (callbacks/ byte-frozen)

Both items live inside the PR-16 split-package `td_component/callbacks/` whose
composed output is byte-pinned to a baseline fixture at
`tests/fixtures/mcp_webserver_callbacks_v1.8.2_baseline.py`. Editing any file
in that package fails `tests/test_composer_byte_equivalence.py` until the
baseline is refreshed.

### A.1 — C-1 part B: MCP-side origin allowlist
**Severity:** Critical (defense-in-depth)
**File:** `td_component/callbacks/router.py:60-90` (where Sec-Fetch-Site
already lives)
**Why:** Part A (commit `727282b`, autostart.py) inverts the MCP auth default
to secure. Part B adds a same-origin allowlist mirroring
`td_component/tdpilot_api_web_callbacks.py:_allowed_origin` so a malicious
**browser tab** running on the same machine can't drive the MCP webserverDAT
even if the user has explicitly opted into `TDPILOT_ENABLE_AUTH_BYPASS=1`.
**Approach:**
1. Add `_allowed_origin(request_origin)` to `callbacks/router.py` (port the
   logic from `tdpilot_api_web_callbacks.py:_allowed_origin`).
2. Enforce it as the FIRST check in `onHTTPRequest`, BEFORE auth — same
   ordering as the chat-pipe side.
3. Refresh baseline: `cd /tmp && uv run python -c "
   from td_component.callbacks import _composer
   open('.../tests/fixtures/mcp_webserver_callbacks_v1.8.2_baseline.py','wb')
   .write(_composer.compose_bytes())"`
4. Run `pytest tests/test_composer_byte_equivalence.py` — green.
5. TD-side rebuild of MCP `.tox`.

### A.2 — M-1: traceback redaction
**Severity:** Medium
**File:** `td_component/callbacks/router.py:108-116`
**Why:** The 500 path returns `traceback.format_exc()` verbatim, leaking
`$HOME` paths and internal module names to any caller that can reach an
error response. The chat-pipe-side already has
`td_component/tdpilot_api_config.py:redact_paths` (lines 178-208).
**Approach:**
1. Either port `redact_paths` into `callbacks/_header.py` (composed into the
   same flat namespace as router.py), OR gate the traceback on
   auth-required — only callers who already authenticated see tracebacks.
2. Same baseline-refresh + .tox-rebuild flow as A.1.

**Recommendation:** bundle A.1 + A.2 into a single follow-up PR (one
baseline regen, one .tox rebuild, two related changes).

---

## Section B — Architectural refactors (multi-week)

These were flagged by the audit's code-quality + architecture passes. They
are NOT one-session fixes; each needs its own plan.

### B.1 — Extract `td_shared/` package (~1 week + parity CI)
**Source-of-truth:** audit architecture report — "Two parallel
implementations of the entire agent stack".
**Problem:** ~4,000 LOC silently forked between `src/td_mcp/` and
`td_component/tdpilot_api_*.py`:

| Subsystem | `src/td_mcp/` | `td_component/` | Notes |
|---|---|---|---|
| Macros | `macros/engine.py`, `loader.py`, `models.py`, `templates.py` (821 LOC) | `tdpilot_api_macros.py:14` (816 LOC) | "Ported from" comment exists |
| Schemas | `models/` + Pydantic | `tdpilot_api_schema_defs.py` (2047 LOC) | Drift caused v2.5.1 chat-pipe gap |
| Cycle detector | `events/cycle_detector.py` (?) | `tdpilot_api_cycle_detector.py` | Silent fork |
| Activity log | `observability/activity_log.py` (227 LOC) | `tdpilot_api_activity_log.py` (303 LOC) | ONLY pair with parity tests |
| Tracing | `observability/traces.py` (384) | `tdpilot_api_tracing.py` (74) | Silent fork |
| Patches | `patch/*.py` | `tdpilot_api_patches.py` | Silent fork |
| Approval policy | `safety/manager.py` | `tdpilot_api_approval.py` | Silent fork |
| Recovery/rollback | `?` | `tdpilot_api_rollback*.py` | Silent fork |

**Why extract**: every silent fork is encoded future-bug; the v2.5.1
chat-pipe `td_get_traces` alias gap is the canonical example.

**Constraint**: `td_component/` runs inside TD's restricted Python (no
stdlib subprocess, limited imports). The shared module must compile +
exec cleanly under both runtimes. That means:
- No Pydantic (TD's bundled Python doesn't ship it; current dual-import
  pattern is exactly because of this)
- No `subprocess`, `pathlib.Path.symlink_to` follow-up syscalls
- No third-party deps
- Only stdlib

**Phase 1 (smallest, lowest-risk):** schema definitions. Move
`TOOL_SCHEMAS` + `TOOL_TO_HANDLER` + `INTERNAL_ONLY_TOOL_NAMES` from
`td_component/tdpilot_api_schema_defs.py` and `tdpilot_api_schema_map.py`
into `td_shared/schemas.py`. The MCP server already imports the Pydantic
models from `src/td_mcp/models/_legacy.py`; convert these to plain
dicts so both runtimes use the same dict-based schema list. CI parity
gate enforces both surfaces stay in sync.

**Phase 2:** cycle detector + args_hash canonicalisation (deep-canonical
sort logic from B-010). Pure stdlib, easy port.

**Phase 3:** macros models (`macros/models.py` only — engine stays
specialized per runtime because it touches different APIs).

**Phase 4:** activity log hashing (the public-facing ring API stays in
each runtime; only the hash + record canonical form moves to shared).

**Phase 5:** patch sentinels + approval policy table.

CI parity gate: a new test in `tests/test_td_shared_parity.py` that
imports each `td_shared.*` module BOTH as `td_shared.X` (the MCP-server
path) AND as `X` (the TD restricted-mode path simulating the
single-namespace composed-textDAT pattern), and asserts the runtime
behavior matches.

### B.2 — Decompose `_loop` (~2 days)
**Source-of-truth:** audit code-quality report — "401-line `_loop`
method holds all 3 v2.5.x bug sites".
**File:** `td_component/tdpilot_api_agent.py:858-1259`.
**Approach:** extract 4-5 named helpers:
- `_dispatch_tool_use_batch(tool_uses) -> list[dict]`
- `_handle_cycle_detect(tu, count, tool_uses)` (the v2.5.2/3 hot spot)
- `_apply_screenshot_strip(results_block, pending_image_blocks)`
- `_inject_journal_hints(results_block, batch_meta)`
- `_finalize_turn(rollback_guard, hint_text)`

Each helper takes explicit arguments; no shared mutable state. The
outer `_loop` becomes a 50-line orchestrator. Codex reviews + targeted
regression tests get cheap.

### B.3 — Split `tool_registry.py` (~3 days)
**Source-of-truth:** audit architecture report — "2168-line god-import,
imported by 30 files".
**File:** `src/td_mcp/tool_registry.py`.
**Approach:** Move the singleton `mcp = FastMCP(...)` into a tiny
`src/td_mcp/_mcp_instance.py` consumed everywhere. The current barrel
of input models lives in `models/_legacy.py` (1348 LOC, marked
TODO(tech-debt) by author) — split into per-domain modules to match
the per-domain `registry/tools_*.py` layout. The `_forward()` HTTP
dispatch + audit hooks split into their own helpers. End state:
`tool_registry.py` becomes a 200-line orchestrator that just wires
the registry tools onto the singleton.

Dependency on B.1: defer until td_shared/ lands so the registry can
import from there cleanly.

---

## Section C — Mock-evals scenario coverage (recommended, ~half day)

**Source-of-truth:** audit code-quality report — "the 12 mock-replay
tests already run by default in CI, but the scenarios don't cover
cycle-detect, rollback, or alias resolution".

The PR-20 fixture-replay infra already exists at `tests/_mock_deepseek.py`
+ `tests/agent_evals_mock/_eval_harness.py`. Adding new scenarios is
incremental:

1. **Cycle-detect orphan tool_use** (v2.5.2 regression class) — fixture
   that triggers `CycleDetected` on the 3rd identical `td_get_info`
   call, asserts the persisted transcript has paired `tool_result`
   blocks (no orphan `tool_use_id`).
2. **Rollback-hint preservation** (v2.5.3 regression class) — fixture
   that triggers `AutoRollbackGuard.__exit__` mid-batch, asserts the
   synthetic `tool_result` carries the rollback hint text.
3. **Alias dispatch** (v2.5.1 regression class) — fixture that calls
   `td_get_traces`, asserts the chat-pipe routes to
   `handle_get_recent_traces` (the dispatcher alias).
4. **Tool approval timeout** (v2.5.0 new boundary) — fixture that hits
   a destructive tool, doesn't click-through within 30 s, asserts the
   tool returns `denied_by_approval`.
5. **Hybrid retrieval threshold** (Bug 8 regression class) — fixture
   with a short instruction-shaped memory entry, asserts BM25 does NOT
   over-match and the agent does NOT side-effect.

These are the scenarios that **would have caught the v2.5.1/2/3 cascade
pre-merge**. The mock harness runs in ~3 s on CI; adding 5 scenarios
should add ~15 s total. Worth doing before v2.6 ships.

---

## Section D — Bootstrap context for a future session

When picking this plan up cold:

1. **Read** [`AGENTS.md`](../../AGENTS.md) + [`docs/plans/README.md`](./README.md).
2. **Verify state**: `git log --oneline origin/main..` should show this
   audit-fix branch's commits if not yet merged, OR be empty if merged.
3. **Verify .tox state**: `uv run python scripts/check_tox_freshness.py &&
   uv run python scripts/check_tox_api_freshness.py` should both PASS. If
   either FAILS, the Phase-4 .tox rebuild has not happened yet — handle
   that BEFORE starting any Section A/B work.
4. **Pick a section**:
   - Section A: small, bounded, ~1 day total. Good first pickup.
   - Section B: each item ~1-2 weeks. Coordinate with v2.6 / v2.7 roadmap.
   - Section C: ~half day, no .tox impact, high regression-prevention ROI.

Per the v2.5 retrospective in [`docs/plans/README.md`](./README.md), the
codebase has a healthy pattern of:
- Single-PR scope per phase
- Pre-merge CI green on lint + 3 Python versions + 2 install-parses
- Live-debug verification on a rebuilt `.tox` before tag
- Per-release CHANGELOG entry + version-file lockstep

Follow that cadence for these items.

---

**Audit report (full)**: see this branch's commit messages on `30b7c20`
+ `727282b` for the per-finding detail. The original audit narrative
lives in the chat transcript that produced this branch.
