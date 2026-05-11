# AGENTS.md

Operating rules for AI coding agents (Claude, Codex, Copilot, Cursor, etc.) and human contributors who'd benefit from the same context.

This file is the **development-time** counterpart to [`README.md`](./README.md) (user-facing) and the runtime [`skills/tdpilot-dpsk4-core/SKILL.md`](./skills/tdpilot-dpsk4-core/SKILL.md) (how the in-TD agent operates the live network). AGENTS.md captures the **non-obvious rules for changing the repo** — version sync, the two `.tox` binaries, the release flow, DeepSeek's prefix-cache contract, and the TouchDesigner-specific traps that have already cost real debug time. Read sections that match what you're about to touch; you don't have to read top-to-bottom.

---

## TL;DR — fresh-agent kit

If you're walking in cold, internalize these before touching anything:

- **Package name is `tdpilot-dpsk4`** — never `tdpilot` (parent fork). Pinned by [`tests/test_release_critical_names.py`](./tests/test_release_critical_names.py).
- **Two `.tox` binaries**, two source-file lists, two CI freshness gates. Edits to source files baked into either `.tox` require a rebuild in a running TouchDesigner — CI rejects stale `.tox` until you rebuild.
- **Seven version manifests** must move in lockstep. `scripts/check_versions.py` is the enforcer.
- **After `git push origin vX.Y.Z`, run `gh release create vX.Y.Z`** — otherwise `release-assets.yml` never fires and `.mcpb` / `.plugin` artifacts don't land on the Release page.
- **Pre-commit local sweep** (always, in order):
  ```bash
  uv run pytest tests/ --ignore=tests/agent_evals          # 1695+ tests
  uv run --extra dev ruff format --check src tests scripts td_component
  uv run --extra dev ruff check src tests scripts td_component
  uv run python scripts/check_versions.py
  uv run python scripts/check_tox_freshness.py
  uv run python scripts/check_tox_api_freshness.py
  ```
- **DeepSeek's Anthropic-compat endpoint requires `thinking` blocks to be echoed back** in subsequent turns. Stripping them returns HTTP 400. Only strip `reasoning_content` SUB-KEYS. See `_strip_reasoning` in [`td_component/tdpilot_api_agent.py`](./td_component/tdpilot_api_agent.py).
- **Inside the `tdpilot_API` chat-pipe code, `parent()` is the COMP, NOT the project root.** Always use absolute paths like `op('/project1')` to escape the COMP scope.

---

## Project overview

This repo is the **DPSK4 (DeepSeek v4) fork** of TDPilot — a TouchDesigner AI assistant. Two distinct artifacts ship from the same codebase:

| Variant | `.tox` file | Audience | How it's driven |
|---|---|---|---|
| **MCP server** | `td_component/tdpilot-dpsk4.tox` | Claude Code / Claude Desktop / Cursor users | `npx tdpilot-dpsk4` runs a stdio MCP server that talks to the running TD via HTTP on port 9985 |
| **Standalone chat-pipe** | `td_component/tdpilot_API.tox` | Direct-in-TD users with a DeepSeek key | An HTML chat panel served from a `webserverDAT` on port 9987; the agent loop runs entirely inside TD, no MCP client needed |

Both `.tox` files load simultaneously in the same TD process when both are present in `/project1`. They share the same Python source tree under `td_component/` (some files are baked into both binaries) but have independent source-hash gates so freshness is tracked per-variant.

The dpsk4 fork was forked from `dreamrec/TDPilot` (the parent fork) around v1.6.11 and renamed: npm package `tdpilot` → `tdpilot-dpsk4`, Python entrypoint `tdpilot` → `tdpilot-dpsk4`, repo `dreamrec/TDPilot` → `dreamrec/TDPilot_deepseekv4`, marketplace `dreamrec-TDPilot` → `dreamrec-TDPilot_deepseekv4`. **The rename has bled in places multiple times** — see [Critical naming pins](#critical-naming-pins) below.

---

## Critical naming pins

Every "release-critical" name. If you see any of the left-hand strings in a new file or comment, you're either looking at parent-fork residue or about to ship a bug. Codex review caught four such cases on PR #30; the pin tests at [`tests/test_release_critical_names.py`](./tests/test_release_critical_names.py) enforce these going forward.

| Concept | ❌ Parent fork | ✅ This fork |
|---|---|---|
| npm package | `tdpilot` | `tdpilot-dpsk4` |
| Python entrypoint (`pyproject.toml [project.scripts]`) | `tdpilot` | `tdpilot-dpsk4` |
| GitHub repo slug | `dreamrec/TDPilot` | `dreamrec/TDPilot_deepseekv4` |
| Claude Code marketplace | `dreamrec-TDPilot` | `dreamrec-TDPilot_deepseekv4` |
| Plugin name (inside marketplace) | `tdpilot` | `tdpilot-dpsk4` |
| Config dir (post v2.1.3) | `~/.tdpilot-api/` (legacy fallback) | `~/.tdpilot-dpsk4/api/` (new default) |
| MCP env file | — | `~/.tdpilot-dpsk4/.tdpilot-dpsk4.env` |

When you write a new script, workflow, or doc that mentions any of these, **read the package name from `npm/package.json` or `pyproject.toml` dynamically rather than hard-coding**. The `npm-publish.yml` workflow does this via `node -p "require('./npm/package.json').name"`; mirror the pattern.

---

## Repository layout

```
src/td_mcp/             MCP server (Python) — the `npx tdpilot-dpsk4` entrypoint
  registry/               per-domain tool modules (23 files, ~103 tools)
  release_gates.py        EXPECTED_MIN_TOOL_COUNT (single source of truth)
  memory/                 technique + knowledge + snapshot + preference stores
  knowledge/              bundled corpus index + docsbrain
  hints/                  hint-injection orchestrator + packs/
  patch/                  typed patch sessions (begin/preview/apply/rollback)
  safety/                 param-bounds + emergency-stabilize
  vision/                 TOP capture + monitor + stream
  jobs/                   long-running job manager
  events/                 EV_* event bus + WS fan-out

td_component/           TD-bound code (gets baked into one or both .tox files)
  tdpilot-dpsk4.tox       MCP-server tox (binary; rebuilt via build_export_mcp_tox.py)
  tdpilot_API.tox         Chat-pipe tox    (binary; rebuilt via build_tdpilot_api_tox.py)
  .tox-source-hash.json       hash of dpsk4 tox sources (checked by check_tox_freshness.py)
  .tox-api-source-hash.json   hash of API tox sources   (checked by check_tox_api_freshness.py)
  callbacks/              split package baked into mcp_webserver_callbacks textDAT
                          (post-PR-16; replaces the old 3149-line god module)
  tdpilot_api_*.py        chat-pipe modules (agent loop, runtime, dispatcher,
                          schemas, recipes, skills, subagents, etc.)
  tdpilot_dpsk4_startup.py   dpsk4-side startup (env file load, install bootstrap)

skills/                 Runtime operational discipline (NOT contribution rules)
  tdpilot-dpsk4-core/     core patching discipline (layout, errors, screenshots, …)
  tdpilot-dpsk4-production/ production-safe edit patterns (snapshots, undo blocks)
  popx-touchdesigner/     POPx-specific workflow

tests/                  1695+ tests; agent_eval marker excluded by default
scripts/                CI gates (check_*.py) + maintenance + live-TD harnesses
.github/workflows/      ci.yml, npm-publish.yml, release-assets.yml
```

The split between `src/td_mcp/` and `td_component/` matters: code in `src/td_mcp/` runs in the MCP server Python process (outside TD); code in `td_component/` runs **inside TouchDesigner's bundled Python**, which is restricted (no stdlib subprocess, limited filesystem access depending on `exec_mode`).

---

## Setup & development environment

```bash
# Python 3.10, 3.11, or 3.12 (declared >= 3.10 in pyproject.toml).
# CI runs all three. Use uv for everything.
uv sync --extra dev
uv run pytest tests/ --ignore=tests/agent_evals     # 1695+ pass, ~20s
uv run --extra dev ruff format --check src tests scripts td_component
uv run --extra dev ruff check src tests scripts td_component
```

You **don't** need TouchDesigner running for unit/integration tests — the chat-pipe code is structured so the agent/runtime/dispatcher layers can be exercised with mocks (see `tests/_mock_deepseek.py`, `tests/_mock_dispatcher.py`). TD is only needed for:
- `.tox` rebuilds (must run inside TD's Python)
- live-TD end-to-end (`scripts/full_td_mcp_e2e.py`, `scripts/runtime_stress_matrix.py`)
- The `agent_eval` test class (deselected from default `pytest` runs)

---

## Build & test commands

```bash
# Full unit + integration suite (no TD required):
uv run pytest tests/ --ignore=tests/agent_evals

# Single file / pattern:
uv run pytest tests/test_v214_codex_followups.py -v
uv run pytest -k "resolve_model" -v

# Format + lint:
uv run --extra dev ruff format src tests scripts td_component
uv run --extra dev ruff format --check src tests scripts td_component
uv run --extra dev ruff check src tests scripts td_component

# Static gates (also run by CI):
uv run python scripts/check_versions.py              # 7-file version sync
uv run python scripts/check_tox_freshness.py         # dpsk4 .tox
uv run python scripts/check_tox_api_freshness.py     # API .tox
uv run python scripts/check_release_gates.py         # EXPECTED_MIN_TOOL_COUNT
uv run python scripts/sync_counts.py --check         # tool-count drift across docs
./scripts/check_no_personal_paths.sh                 # absolute-path leak guard
```

---

## The two `.tox` files (read in full — most agent friction lives here)

`.tox` files are TouchDesigner **binary** component bundles. Editing source files that get **baked into** a `.tox` doesn't change the running TD until the `.tox` is rebuilt **inside a running TouchDesigner session** (the build script uses TD-only Python APIs).

The repo guards against shipping mismatched sources/binaries via two hash files. CI fails if either hash drifts from the live sources.

### tdpilot-dpsk4.tox — MCP-server tox

- **Built by**: [`td_component/build_export_mcp_tox.py`](./td_component/build_export_mcp_tox.py)
- **Source files baked in**: see `_TOX_SOURCE_FILES` in that script — the `callbacks/` split package + `event_emitter.py` + `ws_callbacks.py` + `tdpilot_dpsk4_startup.py` + `installer.py` + `installer_exec.py` + `autostart.py` + `renderer.py` + `state_cache.py` + the two build scripts themselves
- **Source-hash gate**: [`td_component/.tox-source-hash.json`](./td_component/.tox-source-hash.json) (`scripts/check_tox_freshness.py`)

### tdpilot_API.tox — Chat-pipe tox

- **Built by**: [`td_component/build_tdpilot_api_tox.py`](./td_component/build_tdpilot_api_tox.py)
- **Source files baked in**: see `_API_TOX_SOURCE_FILES` in that script — every `td_component/tdpilot_api_*.py` (~30 files), the `callbacks/` split package, `tdpilot_api_chat.html`, and the same `event_emitter.py` + `ws_callbacks.py` etc. that `dpsk4.tox` bakes
- **Source-hash gate**: [`td_component/.tox-api-source-hash.json`](./td_component/.tox-api-source-hash.json) (`scripts/check_tox_api_freshness.py`)

**Overlap matters**: files in `td_component/callbacks/` and `td_component/callbacks/handlers/` are baked into **both** `.tox` files. Editing one of them stales BOTH gates — you need to rebuild BOTH `.tox` binaries.

### When you must rebuild

Run `scripts/check_tox_freshness.py` and `scripts/check_tox_api_freshness.py` after every edit. If either reports stale, rebuild. The script tells you which gate failed and which `.tox` to rebuild.

You CANNOT skip this. There's no "comment-only" exception — `# noqa: <RULE>` annotations on a baked file bump the source hash the same as real code. (Lesson learned: the v1.9.0 ruff-rule re-enable PR shipped a "comment only" edit to a baked file and the .tox-staleness CI gate immediately rejected the PR.)

### Canonical rebuild recipe (paste verbatim into TD Textport, ONE LINE AT A TIME)

```python
import os
repo = '<absolute-path-to-this-repo>'
os.environ['TD_MCP_REPO_ROOT'] = repo
runfile = os.path.join(repo, 'td_component', 'build_tdpilot_api_tox.py'); exec(compile(open(runfile, encoding="utf-8").read(), runfile, "exec"), globals(), globals())
runfile = os.path.join(repo, 'td_component', 'build_export_mcp_tox.py'); exec(compile(open(runfile, encoding="utf-8").read(), runfile, "exec"), globals(), globals())
```

**Why this exact form?** Four footguns this snippet avoids:

1. **TD's macOS Python opens files as ASCII by default.** The TDPilot source has `─` (U+2500) box-drawing chars in section comments; reading without `encoding='utf-8'` raises `UnicodeDecodeError`.
2. **Textport runs single-line statements only.** Multi-line `with`/`def`/`class` after a `...` continuation prompt is parsed wrong and eats the next statement as `SyntaxError`. The single-line `compile()`-then-execute form sidesteps the continuation-prompt bug entirely.
3. **`TD_MCP_REPO_ROOT` is required.** Without it, the build script's `_guess_repo_root()` falls back to scanning `~/Desktop`, `~/Documents`, etc. — and may pick the wrong repo if multiple TDPilot checkouts exist.
4. **Marker-file path drift.** `_is_repo_root()` validates a candidate root by checking for a marker file. If a refactor moves the marker, the env-var path gets rejected with `RuntimeError: Could not auto-detect repo root` even when set correctly. After ANY refactor in `td_component/`, grep for `_is_repo_root` / `_MARKER_FILES` and confirm the marker still points at a real file.

DO NOT paste the build-script docstring examples — those still recommend the broken `with open(runfile) as f:` form. Use the recipe above.

---

## Version manifests (eight artifacts in lockstep)

`scripts/check_versions.py` is the canonical enforcer. It reads `src/td_mcp/__init__.py::__version__` as the source of truth and asserts every other file matches:

| File | Field |
|---|---|
| `pyproject.toml` | `version = "X.Y.Z"` |
| `src/td_mcp/__init__.py` | `__version__ = "X.Y.Z"` |
| `.claude-plugin/plugin.json` | `version` |
| `.claude-plugin/marketplace.json` | `plugins[0].version` (drives the Claude Code "Update" button) |
| `npm/package.json` | `version` (drives `npx tdpilot-dpsk4`) |
| `mcp/manifest.json` | `version` |
| `td_component/callbacks/_header.py` | `API_VERSION = "X.Y.Z"` (baked into BOTH `.tox` files) |
| **GitHub repo description** | NOT in any file — set via the GitHub web UI or `gh repo edit` |

The `API_VERSION` bump in `_header.py` is what forces a `.tox` rebuild on every version bump. If you're patching CI / scripts / workflows / docs only (no runtime change), **skip the version bump entirely** — see PR #31 as the reference for a version-stable hygiene patch.

---

## Release flow (the 12-step ritual)

Used for every release since v2.1.3. Memorize this — drift in any step shows up later as a user-visible breakage.

1. **Branch off fresh `origin/main`** — never from a leftover feature branch. Reviewer's PR #30 review flagged this in v2.1.5: their stale local checkout caused confusion.
   ```bash
   git fetch origin main
   git checkout -b claude/v<X.Y.Z>-<headline-slug> origin/main
   ```
2. **Bump 7 version files** (excluding the GitHub repo description, which `gh repo edit` handles last):
   ```bash
   # edit each manifest manually, OR use sed if confident; check_versions enforces sync
   uv run python scripts/check_versions.py    # must report "in sync at vX.Y.Z"
   ```
3. **Add a CHANGELOG entry** at the top of `CHANGELOG.md` (newest first). Section header pattern: `## X.Y.Z - YYYY-MM-DD`.
4. **Update the README "just shipped" banner** + the "What's new since v1.5.x" table at the bottom.
5. **If you edited any file in `_TOX_SOURCE_FILES` or `_API_TOX_SOURCE_FILES`** (or bumped `API_VERSION` in `_header.py`): rebuild both `.tox` files inside TD using the canonical recipe above. Then:
   ```bash
   uv run python scripts/check_tox_freshness.py        # must report fresh
   uv run python scripts/check_tox_api_freshness.py    # must report fresh
   ```
6. **Local pre-commit sweep**:
   ```bash
   uv run pytest tests/ --ignore=tests/agent_evals
   uv run --extra dev ruff format --check src tests scripts td_component
   uv run --extra dev ruff check src tests scripts td_component
   ```
7. **Commit** with the canonical message style (HEREDOC + `Co-Authored-By` per the user's global instructions):
   ```bash
   git add -u
   git commit -m "release: X.Y.Z — <headline>

   <2-3 paragraphs explaining what changed and why>

   Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
   ```
8. **Push + open PR**:
   ```bash
   git push -u origin claude/v<X.Y.Z>-<headline-slug>
   gh pr create --title "vX.Y.Z — <headline>" --body "<...>"
   ```
9. **Watch CI**. Expect 6 checks: `lint`, `Install script parse-check (macos-latest)`, `Install script parse-check (windows-latest)`, `test (3.10)`, `test (3.11)`, `test (3.12)`. All must be green. The `lint` job is the strictest — it runs ruff check + ruff format --check + check_versions + check_tox_freshness + check_tox_api_freshness + check_no_personal_paths.
10. **Squash-merge** when CI is green:
    ```bash
    gh pr merge <PR-N> --squash --delete-branch
    ```
    `gh` will print `fatal: 'main' is already used by worktree at <...>` — **this is harmless**, the merge succeeded on GitHub. The error is `gh` trying to do a local checkout that conflicts with our worktree setup. Clean up the remote feature branch separately:
    ```bash
    git push origin --delete claude/v<X.Y.Z>-<headline-slug>
    ```
11. **Tag the squash-merge commit** + **create the GitHub Release** (this is the load-bearing step that `git push origin <tag>` alone DOES NOT do — `release-assets.yml` only fires on `release: published`):
    ```bash
    git fetch origin main
    MERGE_SHA=$(gh pr view <PR-N> --json mergeCommit --jq '.mergeCommit.oid')
    git tag -a vX.Y.Z -m "Release vX.Y.Z — <headline>" "$MERGE_SHA"
    git push origin vX.Y.Z

    # Extract THIS version's CHANGELOG section into a notes file:
    awk '/^## X\.Y\.Z/,/^## /' CHANGELOG.md | sed '$d' > /tmp/release-notes.md
    gh release create vX.Y.Z --title "vX.Y.Z — <headline>" --notes-file /tmp/release-notes.md
    ```
    `release-assets.yml` will then run for ~30s and attach `tdpilot.mcpb` + `tdpilot.plugin` to the Release page.
12. **Verify both distribution channels caught up**:
    ```bash
    # npm publish auto-fires on tag push (npm-publish.yml triggers on push: tags: v*.*.*)
    curl -s https://registry.npmjs.org/tdpilot-dpsk4/latest | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])'
    # Should print X.Y.Z. If it prints the previous version, the npm workflow failed —
    # ALWAYS RETRY ONCE before assuming a binding issue:
    gh workflow run "Publish to npm" -f tag=vX.Y.Z
    # 404s on first try are usually transient OIDC hiccups that recover on retry.
    # Only audit the npmjs.com Trusted Publisher binding if retry ALSO 404s.

    gh release view vX.Y.Z --json assets --jq '.assets[].name'
    # Expect: tdpilot.mcpb, tdpilot.plugin
    ```

A release isn't "shipped" until step 12 reports both green.

---

## DeepSeek-specific operating rules

DeepSeek v4's Anthropic-compat endpoint (`base_url=https://api.deepseek.com/anthropic`) has two non-obvious requirements that the agent loop must respect:

### `thinking` content blocks MUST be echoed back

If you strip `type: "thinking"` content blocks from `Agent.messages` before the next API call, DeepSeek returns:
> HTTP 400: `The content[].thinking in the thinking mode must be passed back to the API.`

This is the **opposite** of `reasoning_content` (OpenAI format), which DeepSeek's compat layer ALSO returns but which MUST be stripped from sub-keys (it's a free-form scratchpad, not a required round-trip). The two fields look superficially similar and need opposite handling. The canonical implementation is `_strip_reasoning` in [`td_component/tdpilot_api_agent.py`](./td_component/tdpilot_api_agent.py):

- **KEEPS** `thinking` + `redacted_thinking` blocks as-is.
- **STRIPS** `reasoning_content` SUB-KEYS only (preserves the rest of the block).

If you refactor this function: do not "clean up" thinking blocks. The tests in `tests/test_tdpilot_api_agent.py::test_strip_reasoning_*` pin the contract.

### Prefix-cache stability is load-bearing

DeepSeek's auto-cache discounts cached input tokens ~50× (with the 75%-off promo through 2026-05-31, the effective cost gap is ~3× but the latency win is even larger). The cache hits only when the **prefix is byte-identical** across turns.

This forces a discipline in `td_component/tdpilot_api_runtime.py`:
- `build_system_prompt()` MUST return a byte-identical string across every turn in a session. Anything that varies turn-to-turn (memory index, knowledge index, recipes index) lives in `build_dynamic_context()` instead.
- `build_dynamic_context()` returns a synthetic `[user, assistant]` pair prepended to each API call's message history — **but is NOT persisted to `Agent.messages`**. The cache prefix sees the volatile state on each call without the conversation history itself losing cache stability.
- The `Phase 0.1 contract` comment in `build_system_prompt()` is the canonical reference. Read it before adding anything to the system prompt.

### Model-tier routing

The agent supports three tiers (`auto` / `flash` / `pro`) via the COMP's `Modeltier` parameter. The `auto` heuristic scores user_text (length > 300, pro-leaning verbs, code fences, ≥2 tool keywords) and picks `pro` at score ≥ 2, else `flash`.

**Per-turn override** (post-v2.1.3): if the user writes "use pro", "use the pro model", "switch to pro", "force pro", "pro model", or `deepseek-v4-pro` (mirror set for `flash`), the override fires BEFORE the tier-pin and BEFORE the auto heuristic. Pro wins on ties. See `_PRO_OVERRIDE_RE` / `_FLASH_OVERRIDE_RE` in [`td_component/tdpilot_api_agent.py`](./td_component/tdpilot_api_agent.py) and pin tests in `tests/test_tdpilot_api_agent.py::test_resolve_model_*`.

The override is **per-turn only** — the next turn falls back to the configured tier. False-positive guards via `\b…\b` word boundaries prevent `professional` / `prompt` / `flashlight` from triggering it.

---

## Security model

Three layers, all enforced at the chat-pipe webserver layer ([`td_component/tdpilot_api_web_callbacks.py`](./td_component/tdpilot_api_web_callbacks.py)):

1. **Origin allowlist**: only `http://127.0.0.1:<port>` / `http://localhost:<port>` / `http://[::1]:<port>` (or empty/`null` origin = same-origin / non-browser tool) passes. ALWAYS enforced — including in insecure mode (the post-v2.1.3 fix).
2. **Per-launch session token** (`X-TDPilot-Token` header): server-injected into the served chat HTML at GET `/` time. Required on every non-bootstrap HTTP route AND on the WebSocket handshake URL (`?t=<token>`). Browsers can't read this cross-origin even with a permissive iframe policy because we never emit `Access-Control-Allow-Origin: *`.
3. **`Sec-Fetch-Site` rejection**: modern browsers send `Sec-Fetch-Site: cross-site` on cross-origin fetches; the server 403s anything that isn't `same-origin` or `none`.

### `TDPILOT_API_INSECURE` escape hatch

Set in the TD process env, this env var **bypasses ONLY the token check** (not origin / Sec-Fetch-Site / `Content-Type: application/json` for browser callers). It exists for external scripting from local tooling (curl, Python `requests`) that has no `Origin` header to fail the origin check. The chat-pipe shows a loud textport banner on every COMP load when insecure-mode is active.

### `EXEC_MODE` (the `td_exec_python` sandbox)

Four levels, controlled by `TD_MCP_EXEC_MODE`: `off` / `restricted` / `standard` / `full`.
- `restricted` strips builtins (no `print`, no `Exception`, no `import`).
- `standard` allows a small allowlist (json, math, re, datetime, …) and blocks `subprocess` / `socket` / `os.system` / etc.
- `full` is unrestricted.

**Critical interaction with insecure mode (post-v2.1.3)**: the API tox normally sets `EXEC_MODE=full` at COMP load for agent-introspection convenience. But when `TDPILOT_API_INSECURE=1`, that combination = drive-by RCE from any browser tab the user has open. So [`td_component/tdpilot_api_extension.py::_build_runtime`](./td_component/tdpilot_api_extension.py) clamps `EXEC_MODE` to `restricted` whenever insecure-mode is on. The user can opt back into full by also setting `TDPILOT_API_ALLOW_INSECURE_FULL_EXEC=1` (documented as "only safe in trusted single-user dev sandboxes").

### MCP shared-secret auth

The MCP-server `.tox` (port 9985) uses a separate auth path: `TD_MCP_SHARED_SECRET` (sent as `Authorization: Bearer <secret>` header) is checked against the value in the TD process env. The canonical place to store the secret is `~/.tdpilot-dpsk4/.tdpilot-dpsk4.env`; `auth_bootstrap.py` reads it at server startup. `autostart.py` no longer wipes the secret since v2.1.2 (set `TDPILOT_DISABLE_AUTH_BYPASS=1` to opt into persistent auth that survives TD restart).

---

## TouchDesigner-specific gotchas

Every one of these has cost real debug time. Encode in any new TD-touching code.

### Textport rules

- **Single-line statements only.** Multi-line `with` / `def` / `class` blocks after a `...` continuation prompt get parsed wrong and the next statement is eaten as `SyntaxError`. Always use the one-line `compile()`-then-execute form for paste-friendly scripts.
- **Never call `time.sleep` on TD's main thread.** It blocks cooks, invalidating any "wait then measure absTime/frame" diagnostic. For "do X after N frames" use a force-cooked executeDAT (see below) or a CHOP-driven delay, NOT `time.sleep`.

### Cook thread vs worker thread

- All `op()` access, parameter writes, and CHOP/DAT/TOP reads MUST happen on the **cook thread**. The chat-pipe agent runs on a worker thread; calls into TD are routed through `CookThreadDispatcher` (see [`td_component/tdpilot_api_runtime.py`](./td_component/tdpilot_api_runtime.py)).
- The dispatcher uses an event-queue pattern: the worker pushes a request, the cook thread drains it via `pump_dispatcher()` on `onFrameStart`, and the worker blocks on a `threading.Event` until the cook thread completes. **A paused TD pauses all dispatch** — `start_turn()` emits a textport hint when `me.time.play=False` so the 60s tool-call timeouts don't look like the agent is wedged.
- `tool_batch` and `recipe_replay` handlers already run on the cook thread, so they bypass the dispatcher (which would deadlock waiting for itself) and use `ext.runtime.raw_dispatcher` directly.

### Module reloading

- **State that must survive textDAT reload belongs in `comp.storage`**, NOT module-level variables. The canonical case is the WebSocket client registry (`_STORAGE_KEY_CLIENTS`): a textDAT edit reloads the module, but `comp.fetch(_STORAGE_KEY_CLIENTS, ...)` keeps the live client set across the reload. See [`td_component/tdpilot_api_web_callbacks.py::_ws_clients`](./td_component/tdpilot_api_web_callbacks.py) for the canonical pattern.
- **Bind modules with `sys.modules[name] = ...` (NOT `setdefault`)**. After a COMP rebuild, `setdefault` keeps the OLD modules from the previous build — `from tdpilot_api_runtime import AgentRuntime` then resolves to the STALE class while every other piece of state (the new COMP, its new DATs) is fresh. Symptom: stuck "thinking…" forever and no LLM response. See `_ensure_module_path` in [`td_component/tdpilot_api_extension.py`](./td_component/tdpilot_api_extension.py).

### Operator quirks

- **`executeDAT` needs `dat.cook(force=True)` once after programmatic creation** — otherwise `onFrameStart` / `onStart` callbacks never fire. The build scripts apply this when assembling the COMP.
- **`op.run(code, delayFrames=N)` callbacks can be silently dropped in TD 2025+.** Don't use it as a per-frame scheduler. Use force-cooked executeDATs instead, OR push events into the runtime queue and process them on `onFrameStart`.
- **`webRenderTOP` MUST load HTML via `http://`, not `file://`.** Chromium blocks WebSocket connections from a `file://` origin even when the WS server is on localhost. The chat-pipe avoids this by serving its own HTML from the same `webserverDAT` the WS lives on (one `http://127.0.0.1:9987/` origin for both).
- **`webserverDAT.onServerStart` is the reliable ".tox finished loading" hook** for both build-script-driven installs AND drag-drop installs. The textDAT's `onCreate`/`onStart` paths are less reliable — `onServerStart` fires consistently across both install paths.

### Render pipeline (TD 2025+)

- **`geometryCOMP` defaults to a POP-family `torus1` inside, not a SOP.** Setting `geo.par.instanceop` to a SOP outside and expecting the inside POP torus to be instanced produces no visible geometry. Fix: delete the default POP torus, create a SOP shape inside (`sphereSOP`, `boxSOP`), set `render=True` + `display=True`.
- **Reference-style params (`instanceop`, `material`, `camera`, `lights`, `geometry`) need real OP refs, not strings.** `td_set_params({'instanceop': '../noise'})` returns `success=False` with "did not resolve". Use `td_exec_python` with `op(target_path).par.instanceop = op(source_path)` for reliable assignment. The silent-null guard in [`src/td_mcp/registry/tools_graph.py`](./src/td_mcp/registry/tools_graph.py) (v1.5.2/v1.5.3) catches this for both single and plural list reference styles (`OPS`/`COMPS`/`OPLIST` for `renderTOP.cameras/lights/geometry`).
- **Always set `viewer=True` on test/debug COMPs.** Without it, red-bordered TD errors aren't visible in the network editor and `td_get_errors == 0` becomes a false greenlight.
- **`td_get_errors == 0` is NOT a render-success signal.** It catches engine-level errors (broken refs, type mismatches) but NOT: empty geometry, scale=0, camera frustum miss, unrendered SOPs, broken material assignment, instances at NaN. After every render-chain build, `td_screenshot` the output and visually verify it isn't black/uniform.

### `feedbackTOP` canonical pattern

The Derivative reference demo's exact wiring:

```
src ──┬──► fb (in 0)              [seed]
      ├──► over (in 0 = BG)       [fresh frame, NOT feedback's output]
      └──► dryWetMix (in 0 = dry) [optional dry-path crossfade]

fb → level → over (in 1 = OVERLAY) → dryWetMix (in 1 = wet) → out

fb.par.top      = over            ← mid-chain compositor, NOT final out
level.opacity   = 0.9 (Post page) ← THIS is the trail decay, NOT brightness1
level.brightness1 = 1.0
over.size       = "input1"        ← sizes output from the overlay (level) input
```

Critical details:
- `src` is a **trifurcation** (feedback seed + over BG + dry path).
- `over1` takes `src` on input 0 (background) and `level` on input 1 (overlay) — **not reversed**.
- Trail decay happens via `level.opacity` on the **Post page**, not `brightness1`.
- `fb.par.top` points at the compositor (`over`), NOT the final `out`.

**"Not enough sources specified" is a TD static-analysis warning**, NOT a runtime "this won't render" error. TD's runtime cycle resolver often handles the chain fine. Screenshot the output before assuming the error means the render is broken. A 1280×720 chain that "errors" but produces a 25+ KB JPEG with real variation is rendering correctly.

### Expressions — the #1 source of broken expression refs

- **Relative vs absolute paths.** Expressions inside a COMP cannot reach nodes outside with `op('name')`. Use `op('/project1/name')` for absolute paths. This is the #1 source of expression errors and the most common cause of "the build looks right but the param doesn't update".
- **Menu parameters: `.par.ParamName.eval()`, not bracket notation.**
- **Expression mode: after assigning `.expr`, always set `.mode = ParMode.EXPRESSION`.** Otherwise the param sits in `CONSTANT` mode and the expression string is treated as a literal.
- **Time-driven**: `absTime.seconds` for smooth animation, `absTime.frame` for frame-locked.

### POPx specifics (when POPX is installed)

If a project uses POPX (community POP extension), the mental model is `Generator → Falloff → Modifier → Tool → Simulation`. See [`skills/popx-touchdesigner/SKILL.md`](./skills/popx-touchdesigner/SKILL.md) and the catalog at `~/.claude/references/popx-catalog.md` (in user's home, not in this repo). POPX operators are NOT TD-native — they're loaded from `/POPX_<version>/` and the operator types use the family suffix `POPX` rather than `POP`.

---

## PR conventions

- **Branch name**: `claude/<short-slug>-<headline>` (matches existing pattern; PR list stays scannable).
- **Commit message style** (the user's global instructions require this):
  - HEREDOC-wrapped multi-line message.
  - Title: `<area>: X.Y.Z — <one-line headline>` (e.g. `release: 2.1.5 — Codex P2 follow-up on v2.1.4`) or `ci: …` / `fix: …` / `docs: …` for non-release commits.
  - Trailing `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>` line.
- **Always squash-merge**. Match the existing release-history style.
- **Use `gh` CLI for everything** (`gh pr create`, `gh pr merge --squash`, `gh release create`, `gh workflow run`). The worktree-conflict warning from `gh pr merge` is harmless — see step 10 of the release flow.
- **Codex review cadence**: after every release merge, the `chatgpt-codex-connector` bot leaves an automated review with P1/P2 findings. Pattern that's emerged across v2.1.3/4/5/PR#31: each release tends to surface 1-2 real edge regressions in its own fixes. **Don't dismiss these reviews** — they catch real bugs. Ship the followup as a separate small PR (no version bump needed if it's CI/scripts/docs only — see PR #31 as the reference).

---

## Cross-references

- [`README.md`](./README.md) — user-facing intro + install path
- [`CHANGELOG.md`](./CHANGELOG.md) — release history, deeply detailed; the canonical source of truth for what shipped when
- [`docs/MANUAL.md`](./docs/MANUAL.md) — long-form user manual
- [`skills/tdpilot-dpsk4-core/SKILL.md`](./skills/tdpilot-dpsk4-core/SKILL.md) — runtime operational discipline for the in-TD agent. Reads as "how to operate TD when you're the agent at runtime" — NOT "how to contribute to this repo". Distinct concerns.
- [`skills/tdpilot-dpsk4-production/SKILL.md`](./skills/tdpilot-dpsk4-production/SKILL.md) — production-safe edit patterns (undo blocks, snapshots, strict completion gates) — also RUNTIME discipline.
- [`skills/popx-touchdesigner/SKILL.md`](./skills/popx-touchdesigner/SKILL.md) — POPx workflow.
- [`tests/test_release_critical_names.py`](./tests/test_release_critical_names.py) — machine-enforceable backstop for the [Critical naming pins](#critical-naming-pins) section.

---

## Meta: this file is itself pinnable

When you find yourself adding a rule that another agent ALSO needs to know, add it here. The pattern that's working:

1. Encounter a non-obvious gotcha during real work.
2. Fix the immediate symptom.
3. Encode the rule in AGENTS.md with a file:line citation and the specific failure mode it prevents.
4. If the rule is mechanically checkable, add a pin test to `tests/test_release_critical_names.py` (or a sibling).

Steps 3-4 are what keep AGENTS.md a living, useful artifact rather than aspirational doc rot.
