# TDPilot v1.6.13 Manual

The full reference. Read the [README](../README.md) first if you haven't installed yet — this manual assumes you've got either the standalone .tox or the Claude Code plugin running.

## Contents

- [First-time setup (standalone .tox)](#first-time-setup-standalone-tox)
- [First-time setup (Claude Code CLI)](#first-time-setup-claude-code-cli)
- [Standalone vs CLI — practical differences](#standalone-vs-cli--practical-differences)
- [Parameters explained](#parameters-explained)
- [Sound](#sound)
- [Browser chat](#browser-chat)
- [Standalone-only behaviours](#standalone-only-behaviours)
- [Tools](#tools)
- [Persistent state on disk](#persistent-state-on-disk)
- [User-pluggable tools](#user-pluggable-tools)
- [Install doctor](#install-doctor)
- [Building the .tox from source](#building-the-tox-from-source)
- [Working loop](#working-loop)
- [Troubleshooting](#troubleshooting)
- [Security](#security)

---

## First-time setup (standalone .tox)

You need TouchDesigner 2025.30000+, a [DeepSeek API key](https://platform.deepseek.com/), and a browser. That's everything.

**1. Drop the .tox into your project.**
Drag `td_component/tdpilot_API.tox` into your TD network. It lands at `/project1/tdpilot_API` — a purple containerCOMP, panel size 900×600.

**2. Open the COMP's parameter panel.**
Click the COMP, then press `p` (or use the right-side parameter pane). You'll see three pages: **API**, **Chat**, **Status**.

**3. Paste your API key.**
On the **API** page, find `Api Key`. Paste your DeepSeek key into the field. Then click `Save Key to ~/.tdpilot-api/`. The key gets written to `~/.tdpilot-api/config.json` with mode 0600 on macOS/Linux. On Windows, the file is plaintext — see [Security](#security).

You only do this once. From now on TDPilot reads the key from that file.

**4. Open the chat.**
Click `Open Chat in Browser`. A new browser tab opens at `http://127.0.0.1:9987/` showing the chat UI:

![TDPilot chat UI on first load](images/chat.png)

The same chat is rendered inside the COMP's own panel — toggle the COMP's viewer flag if you want the chat in TD instead of (or alongside) the browser.

**5. Try it.**
Type something. Good first prompts:
- "what's the current FPS?"
- "list the operators in /project1"
- "search the knowledge base for feedback loops"
- "create a noiseTOP at /project1 named test_noise"

Hit `enter` to send, `shift+enter` for newline. The agent thinks, calls tools, shows them inline, and ends with a text reply. The completion chime tells you the turn is done.

That's the entire setup. Everything below is reference.

---

## First-time setup (Claude Code CLI)

Heavier path — for people who already use Claude Code or want the full 103-tool surface.

**1. Install Claude Code.** [Docs](https://docs.claude.com/en/docs/claude-code).

**2. Point it at DeepSeek.** Set in your shell:
```
export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic
export ANTHROPIC_MODEL=deepseek-v4-pro
```

**3. Install the plugin.** One of:

```
/plugin marketplace add dreamrec/TDPilot_deepseekv4
/plugin install tdpilot-dpsk4@dreamrec-TDPilot_deepseekv4
```

Or via npm:

```
npx tdpilot-dpsk4 plugin-install
```

Or paste this into your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "touchdesigner-dpsk4": {
      "command": "uv",
      "args": ["run", "--directory", "${workspaceFolder}", "tdpilot-dpsk4"],
      "env": {
        "TD_MCP_HOST": "127.0.0.1",
        "TD_MCP_PORT": "9985",
        "TD_MCP_WS_PORT": "9986"
      }
    }
  }
}
```

**4. Drag the bridge .tox into TD.**
Drag `td_component/tdpilot-dpsk4.tox` into `/local`. The .tox has its own one-button installer panel — click `Bootstrap All` and it wires everything up.

**5. Talk to TD from your Claude Code session.** The 103 tools, three skills (`tdpilot-dpsk4-core`, `tdpilot-dpsk4-production`, `popx-touchdesigner`), and the `/td-check` + `/td-snapshot` slash commands are now available.

---

## Standalone vs CLI — practical differences

Both run on the same DeepSeek backend and share the TD-side handler layer. The choice is about **where the chat lives** and **how big a surface you want**.

### How they feel different

| | Standalone .tox | Claude Code CLI |
|---|---|---|
| **Where you type** | Browser tab or in-TD panel | Your terminal |
| **Setup time** | ~2 min (drag + paste key) | ~10 min (Claude Code + plugin + bridge .tox) |
| **Dependencies** | TouchDesigner + a browser | TouchDesigner + Node.js + uv + Claude Code |
| **Tool surface** | 90 tools | 103 tools |
| **Multi-file projects** | TD-only | Anything Claude Code touches (TD + Python + Markdown + …) |
| **Session length** | Lighter — one focused conversation per turn | Heavier — long sessions across many files |
| **Collaboration** | Multi-tab — open the chat URL on a phone or second monitor | Terminal stays with one user |
| **Slash commands** | None | `/td-check`, `/td-snapshot` |
| **Skills** | Bundled (popx-mode, performance-mode) | Bundled + the full Claude Code skill ecosystem |
| **Resource cost** | One TD process, one DeepSeek connection | Adds a Python MCP server + Claude Code's context |

### Capability matrix

| Capability | Standalone | CLI | Notes |
|---|:---:|:---:|---|
| Inspect (info, nodes, params, errors, search) | ✅ | ✅ | Same handler layer |
| Build & wire (create, connect, copy, rename, delete) | ✅ | ✅ | Same handler layer |
| Edit content (set params, set content, pulse, custom params) | ✅ | ✅ | Same handler layer |
| Diagnostics (screenshot, CHOP/POP/SOP, exec_python) | ✅ | ✅ | Same handler layer |
| Project lifecycle (timeline, save, undo) | ✅ | ✅ | Same handler layer |
| Persistent memory (BM25 over markdown) | ✅ | ✅ | Same store |
| Knowledge corpus (auto-discovered + bundled) | ✅ | ✅ | Same store |
| Recipes (save / replay) | ✅ | ✅ | Same format |
| Skills system | ✅ | ✅ | Bundled + user |
| Patch sessions (begin / commit / rollback) | ✅ | ✅ | Same undo-block model |
| Snapshots (`.toe` save / list) | ✅ | ✅ | Same `~/.tdpilot-*/snapshots/` |
| Subagents (parallel fan-out) | ✅ | — | Standalone only |
| Multi-model routing (auto/flash/pro) | ✅ | — | Standalone only |
| Macro engine (5 bundled templates) | ✅ | — | Standalone only |
| User-pluggable tools (`~/.tdpilot-api/tools/*.py`) | ✅ | — | Standalone only |
| **Official-docs lookup** (`td_search_official_docs`, `td_get_operator_doc`, `td_get_param_help`, `td_lookup_snippets`, `td_lookup_palette_component`) | ✅ | ✅ | Both — searches the auto-discovered `derivative` corpus |
| **TD 2025 native introspection** (`td_python_env_status`, `td_threading_status`, `td_logger_status`, `td_tdresources_inspect`, `td_component_standardize`, `td_color_pipeline`) | ✅ | ✅ | Both — read-only Python/TD probes |
| **Project audit** (`td_audit_project`) | ✅ | ✅ | Both — recursive subtree audit |
| **Recipe validation** (`td_validate_recipe`) | ✅ | ✅ | Both — pre-save sanity check |
| **Recommendations** (`td_recommend_official_component`, `td_find_official_example`, `td_explain_better_way`) | ✅ | ✅ | Both — LLM-flavored docs surfacing |
| **Server introspection** (`td_get_server_metrics`, `td_describe_surface`, `td_get_capabilities`) | ✅ | ✅ | Both — runtime metrics + tool surface description |
| **Memory advanced — export/import/favorite** (`memory_export`, `memory_import`, `memory_favorite`) | ✅ | partial | Standalone has all three; CLI has the analogous `td_memory_export`/`import`/`favorite` plus `td_memory_learn`/`replay`/`promote` |
| **Memory advanced — learn/replay/promote** (`td_memory_learn`, `td_memory_replay`, `td_memory_promote`) | — | ✅ | CLI only — recipe extraction + cross-project promotion |
| **Typed patch-session API** (`td_plan_patch` → `td_preflight_patch` → `td_patch_apply` → `td_patch_validate` → `td_patch_variations`) | — | ✅ | CLI only — atomic multi-step builds with typed rollback |
| **Macro authoring** (`td_create_macro`) | — | ✅ | CLI only — programmatic macro construction |
| **Streaming TOP output** (`td_stream_top`, `td_stop_stream_top`) | — | ✅ | CLI only — interactive workflow doesn't need this |
| **Continuous vision monitoring** (`td_monitor_visual`, `td_capture_and_analyze`) | — | ✅ | CLI only — for unattended autonomous agents |
| **Visual optimization** (`td_optimize_visual`) | — | ✅ | CLI only — multi-pass batch workflow |
| **Slash commands** (`/td-check`, `/td-snapshot`) | — | ✅ | Claude Code plugin |
| **Tool count** | 90 | 103 | — |
| **Where chat lives** | Browser tab + in-TD panel | Your terminal | — |
| **Setup time** | ~2 min | ~10 min | — |
| **Dependencies** | TouchDesigner + browser | TD + Node.js + uv + Claude Code | — |

### When to pick which

| If you want to… | Pick |
|---|---|
| chat *while* you patch — alt-tab between viewport and chat | Standalone |
| show TDPilot to someone with no CLI setup | Standalone |
| follow the chat on a phone or second monitor | Standalone |
| run with zero terminal dependencies | Standalone |
| use TD inside a longer Claude Code session that touches non-TD code | CLI |
| use the typed patch-session API for atomic multi-step builds | CLI |
| use `/td-check` / `/td-snapshot` slash commands | CLI |
| stream TOP output continuously to a client | CLI |
| query the official TD docs corpus from the agent | CLI |

### Run them both

The two `.tox` files coexist in the same TD project. Different ports (9987 vs 9985/9986), different config dirs (`~/.tdpilot-api/` vs `~/.tdpilot-dpsk4/`), different COMP names (`tdpilot_API` vs `tdpilot`). Drop them both in. Standalone for chat-while-patching, CLI when you go heavy.

---

## Parameters explained

Three pages on the standalone COMP:

### API page

| Param | Default | What it does |
|---|---|---|
| `Api Key` | empty | Your DeepSeek key. |
| `Save Key to ~/.tdpilot-api/` | pulse | Persists the key. Mode 0600 on POSIX; plaintext on Windows. |
| `Model` | `deepseek-v4-pro` | Pro tier model name. |
| `Base URL` | `https://api.deepseek.com/anthropic` | The Anthropic-compat endpoint. |
| `Model Tier` | `auto` | `auto` routes per turn (long/code-heavy → pro, short lookups → flash). Or pin `flash` / `pro`. |
| `Flash Model Name` | `deepseek-v4-flash` | Flash tier model name. |
| `Max Tokens (out, max 384000)` | `32768` | Output budget per call. |
| `Temperature` | `0.7` | Sampling temperature. |
| `Turn Budget (tool rounds)` | `100` | Max tool-use rounds before the loop hard-stops. Prevents runaway agents. |
| `Sound on completion` | on | Plays a chime when the agent finishes a turn. |
| `Sound Volume (0-1)` | `0.7` | Chime volume. |
| `Auto-open chat in browser on load` | on | Opens `http://127.0.0.1:9987/` when the .tox loads. |
| `Open Chat in Browser` | pulse | Opens the chat URL now. |
| `Reload Config` | pulse | Re-reads `~/.tdpilot-api/` (config, memories, knowledge, recipes, user tools). Use this after dropping a new user tool or editing config.json. |

### Chat page

| Param | What it does |
|---|---|
| `Message` | Type a message here as an alternative to the browser. |
| `Send` | Pulse to send. Same as Send button in the browser. |
| `Stop` | Pulse to interrupt the active turn. |
| `Reset` | Pulse to clear the conversation and start fresh. |

### Status page (read-only)

| Param | What it shows |
|---|---|
| `Status` | `idle` / `thinking` / `tool` / `error` |
| `Last Tool` | The last tool the agent invoked. |
| `Active Model` | Which tier (flash/pro) the auto router picked for the most recent turn. |

---

## Sound

The completion chime fires once at the end of every turn. macOS plays `/System/Library/Sounds/Glass.aiff` via `afplay`, Linux uses `paplay`, Windows uses the `winsound` API.

- **To disable**: untoggle `Sound on completion` on the API page.
- **To adjust volume**: `Sound Volume` (0.0–1.0).
- **To change the macOS sound**: any of `Glass.aiff`, `Hero.aiff`, `Ping.aiff` work — edit `td_component/tdpilot_api_extension.py` if you want a different one.

The sound is fire-and-forget on a daemon thread, so it never blocks the agent or the cook thread.

---

## Browser chat

The chat lives at `http://127.0.0.1:9987/` and is also rendered inside the COMP's panel via a webRenderTOP.

**What you see:**

![TDPilot chat UI on first load](images/chat.png)

- Top-left: `TDPILOT_API` branding.
- Top-right: `[ CONNECTED ]` WebSocket status. If it goes red the WS dropped — usually fixes itself on reload.
- Center: pixel-art logo and the keyboard hint (initial state). Once you start chatting, this area becomes the conversation transcript.
- Bottom: input bar with `SEND` button. `enter` to send, `shift+enter` for newline.

**Multi-tab streaming.** Open the URL on a second device or another browser — both tabs follow the same conversation in real time via WebSocket fan-out. Useful for showing what the agent is doing on a second monitor or phone while you keep the TD viewport on the main display.

**Stop button.** Appears top-right while the agent is running. Same effect as pulsing the `Stop` parameter on the COMP — interrupts mid-turn cleanly.

**If the chat shows nothing on first load:** the embedded webserver takes a frame to start. Click `Open Chat in Browser` on the COMP once to force a fresh open. If it stays blank, drop the .tox out and back in (the `executor` DAT needs a force-cook to register `onFrameStart`).

---

## Standalone-only behaviours

The standalone runtime ships several agent-quality features that don't exist in the CLI variant. They run automatically — no parameter to flip — and they're additive: they layer on top of the model's tool calls without replacing them.

### Cache-stable dynamic context

The system prompt is byte-stable across the entire session. Volatile per-turn state (Memory Index, Knowledge Index, Recipes Index) lives in a synthetic `[[TDPILOT_CONTEXT]]` message that prepends to the conversation history just before each API call but is NOT persisted in the agent's `messages` array. Net effect:

- DeepSeek's auto-cache hits at ~50× discount on cached input tokens.
- Saving a memory mid-session shows up in the next turn's context without rebuilding the system prompt.
- The dynamic-context block is built on the cook thread and snapshotted; the worker thread never touches TD globals.

### Pre-turn retrieval injection

Every `start_turn(user_text)` runs cheap local retrieval (`memory_recall` + `recipe_recall` + `knowledge_search`) and prepends the top hits to the dynamic context, sorted by score, capped at 4 hits / ~800 tokens. The agent sees what it already knows about the prompt without having to spend a tool round-trip on a generic "search anything" probe first.

### Trigger-based skill loading

Skills carry a `triggers:` list in their frontmatter. When a user message contains a trigger keyword (e.g. `popx`, `slow`, `fps`), the runtime auto-loads the matching skill body for the rest of the session and emits a `hint`-role event in the chat ("Auto-loaded skill 'popx-mode'"). Re-triggering is idempotent. `Reset` clears the activation set.

### Trust-tier-aware results

Every search match (BM25 in-memory or FTS5 SQLite) carries a `trust_tier` field — one of `official`, `bundled`, `personal`, `community`, `transcript`, `experimental`. The system prompt instructs the model to weight evidence by tier: official docs answer facts; community / transcript hits suggest approaches and require validation via `td_get_errors` / `td_screenshot` / `td_get_operator_doc` before being claimed as fact.

### Severity-tracked validation hints

The runtime classifies every tool call as high / medium / low severity (`td_create_node` = high, `td_set_params` = medium, `td_get_info` = low). At turn end, if any high-severity mutation went out without a follow-up validator (`td_get_errors` / `td_audit_project` / `td_validate_recipe` / `patch_validate`), the chat shows a soft `hint`-role nudge: "You modified the network … without validating." Soft signal only — never blocks the conversation.

### Failure recovery hints

When a tool returns `{"error": ...}` whose message matches one of 10 registered patterns, the dispatcher attaches a `recovery_hint` field with an actionable next step. The agent sees both the error and the hint, so it routes differently on the next turn instead of retrying the same failed call.

| Error pattern | Hint suggests |
|---|---|
| `Unknown operator type` | `td_list_families` or `td_search_official_docs` for valid type names |
| `401` / `Unauthorized` / `API key invalid` | Pulse `Save Key to ~/.tdpilot-api/` with a fresh key |
| `Path not found` / `No node at path` | `td_get_nodes(path='/parent_path')` to check the parent |
| `THREAD CONFLICT` / `outside the main thread` | Don't return raw `op()` from `td_exec_python` — stringify first |
| `corpus.*not installed` / `brain.db.*not found` | `npx tdpilot-dpsk4 brains add <corpus>` |
| `recipe.*invalid` / `unknown tool.*replay` | `td_validate_recipe` before saving |
| `malformed MATCH expression` / `fts5: syntax error` | Retry with simple alphanumeric terms |
| `Module .* not found` / `No module named` | Likely a stale .tox; rebuild |
| `Permission denied` / `read-only file system` / `EACCES` | Check `~/.tdpilot-api/` ownership |
| `timed out` / `TimeoutError` | Narrower query, check Cooking Info for stuck operators |

Patterns are deliberately narrow to avoid false positives. New patterns can be added in `td_component/tdpilot_api_recovery.py`.

### `tool_batch`

Run up to 8 independent tool calls in one round trip. The agent uses this for chained reads ("get info + get capabilities + get errors") so it pays one model→server→model cycle instead of N. Failed sub-calls don't abort the batch — each result reports its own success/error. Sub-calls execute SEQUENTIALLY on the cook thread because TD's Python API isn't thread-safe; the win is LLM round-trip latency, not per-tool latency.

```jsonc
// Schema (callers don't need to write this — the agent does)
{
  "tool": "tool_batch",
  "input": {
    "calls": [
      {"tool": "td_get_info", "args": {}},
      {"tool": "td_get_capabilities", "args": {}},
      {"tool": "td_get_errors", "args": {"path": "/project1"}}
    ]
  }
}
```

Returns `{ok, count, results: [{tool, ok, result, error, elapsed_ms}, ...]}`. Nested `tool_batch` calls are rejected.

### Per-turn observability traces

Every completed turn writes one JSONL line to `~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl`:

```jsonc
{
  "ts": "2026-05-07T10:32:18.000Z",
  "session_id": "3de5f9469904",
  "turn_id": "ab12cd34ef56",
  "user_text_hash": "ab12cd34ef56",   // 12-char SHA-256 prefix; raw text NEVER stored
  "model_tier": "auto",
  "model_used": "deepseek-v4-flash",
  "tool_calls": [
    {"name": "td_get_info", "args_hash": "xx12...", "latency_ms": 12, "ok": true, "error": ""}
  ],
  "outcome": "done",                   // done / error / interrupted
  "duration_ms": 1842,
  "total_tokens": 0
}
```

Files rotate by day. Files older than 30 days are pruned at `Tracer` init. Disk writes happen on a daemon thread so the cook thread never blocks.

The agent (or you) can read recent records via the `td_get_recent_traces({limit: N})` tool — newest first, max 200, walks back up to 7 days. Privacy: raw user text and tool args are SHA-256-hashed (12-char prefix) at write time so the file never holds prompt content.

### Conversation compaction

When the message history exceeds the threshold (default 20 messages), the runtime summarises the oldest portion into a single synthetic assistant message and keeps the most recent 10 messages verbatim. The synthetic message is **text-only** — no `thinking` block — because we can't fabricate a valid signature for one. Recent messages keep their original API-issued thinking blocks (with valid signatures) intact, so DeepSeek's "thinking-must-echo-back" contract is preserved.

The summary captures: user-prompt count, first/last user goals (truncated at 200 chars), tool calls deduplicated and sorted by frequency, error count. Token-frugal — the model gets enough signal to maintain rough continuity without paying full-fidelity context cost.

Forensic preservation: every compaction event appends one JSON record to `~/.tdpilot-api/history/<session_id>.jsonl` containing the entire sliced batch BEFORE compaction. A future `td_history_recall(query)` tool will let the agent re-read this when needed.

Disable by setting `compaction_threshold` to 0 in `~/.tdpilot-api/config.json`.

### Verify Setup pulse

Callable from the TouchDesigner Textport:

```python
mod('tdpilot_api_extension').get_extension(parent()).OnVerifySetupPulse()
```

Runs the same check registry as [`scripts/doctor_live.py`](#install-doctor) and returns a JSON-serialisable dict `{ok, results, fail, warn}`. Use this when you want a one-call install sanity check from inside TD.

### First-run wizard

When the standalone is loaded by a fresh-install user (no API key, no memories saved, no external brains), the welcome panel renders a 3-step quickstart checklist. The chat HTML polls `GET /firstrun` every 4s to update step-completion state; once all three boxes are ticked the wizard auto-dismisses.

The `/firstrun` endpoint returns:

```jsonc
{
  "is_first_run": true,
  "has_api_key": false,
  "has_memory": false,
  "has_brains": false,
  "next_steps": [
    {"name": "paste_api_key",  "label": "...", "done": false},
    {"name": "install_brain",  "label": "...", "done": false, "optional": true},
    {"name": "first_memory",   "label": "...", "done": false}
  ]
}
```

Also returned under `caps["first_run"]` in `td_get_capabilities`.

---

## Tools

90 tools across 17 categories. Full schemas in `td_component/tdpilot_api_schema_defs.py`.

| Category | Count | Examples |
|---|---|---|
| Inspect | 9 | `td_get_info`, `td_get_nodes`, `td_get_node_detail`, `td_get_params`, `td_get_errors`, `td_search_nodes`, `td_pop_inspect` |
| Build & wire | 9 | `td_create_node`, `td_delete_node`, `td_copy_node`, `td_rename_node`, `td_connect_nodes`, `td_disconnect`, `td_get_connections` |
| Edit content | 4 | `td_set_params`, `td_set_content`, `td_pulse_param`, `td_custom_parameters` |
| Diagnostics | 6 | `td_screenshot`, `td_chop_data`, `td_geometry_data`, `td_cooking_info`, `td_analyze_frame`, `td_exec_python` |
| Python help | 2 | `td_python_help`, `td_python_classes` |
| Project lifecycle | 4 | `td_timeline`, `td_timeline_set`, `td_project_lifecycle`, `td_subscribe`/`td_unsubscribe` |
| Memory | 5 | `memory_save`, `memory_get`, `memory_list`, `memory_recall`, `memory_delete` |
| Knowledge | 4 | `knowledge_search`, `knowledge_get`, `knowledge_list`, `knowledge_add` |
| Recipes | 5 | `recipe_save`, `recipe_get`, `recipe_list`, `recipe_recall`, `recipe_replay` |
| Skills | 3 | `skill_list`, `skill_get`, `skill_load` |
| Patch sessions | 6 | `snapshot_save`, `snapshot_list`, `patch_begin`, `patch_validate`, `patch_commit`, `patch_rollback` |
| Subagents | 5 | `spawn_subagent`, `subagent_status`, `subagent_wait`, `subagent_cancel`, `subagent_list` |
| Macros & user tools | 5 | `macro_list`, `macro_get`, `macro_run`, `tool_list_user`, `tool_validate` |
| Memory advanced | 3 | `memory_export`, `memory_import`, `memory_favorite` |
| Recipe validation | 1 | `td_validate_recipe` |
| Official-docs lookup | 5 | `td_search_official_docs`, `td_get_operator_doc`, `td_get_param_help`, `td_lookup_snippets`, `td_lookup_palette_component` |
| Recommendations | 3 | `td_recommend_official_component`, `td_find_official_example`, `td_explain_better_way` |
| TD 2025 native | 7 | `td_python_env_status`, `td_threading_status`, `td_logger_status`, `td_tdresources_inspect`, `td_color_pipeline`, `td_component_standardize`, `td_audit_project` |
| Server introspection | 3 | `td_get_server_metrics`, `td_describe_surface`, `td_get_capabilities` |
| Tool batch | 1 | `tool_batch` (parallel-dispatch wrapper for up to 8 independent reads) |
| Observability | 1 | `td_get_recent_traces` (read recent per-turn JSONL records) |

The CLI variant adds 15 more tools — see [Standalone vs CLI](#standalone-vs-cli--practical-differences) for the gap.

---

## Persistent state on disk

```
~/.tdpilot-api/
  config.json        API key + setting overrides (mode 0600 on POSIX)
  memory/            Markdown memory files indexed by BM25
    MEMORY.md        Auto-generated index, refreshed on every memory_save
  knowledge/         User knowledge entries (merged with bundled)
  recipes/           Saved recipes (replayable tool sequences)
  skills/            User skills (merged with bundled, auto-loaded on triggers)
  tools/             User tools — drop a *.py file with SCHEMA + handle()
  snapshots/         Saved .toe snapshots from snapshot_save
  macros/            User macro templates (merged with the 5 bundled)
  traces/            Phase 4.1 — per-turn observability JSONL, rotated daily
    YYYY-MM-DD.jsonl   one record per completed/interrupted turn (30-day retention)
  history/           Phase 4.3 — full pre-compaction message history
    <session>.jsonl    one JSON record per compaction event
```

Every directory is created lazily on first use. You can copy this whole folder to another machine — your memories, recipes, knowledge, and tools come with you. (Skip `traces/` and `history/` if you don't want forensic data on the destination — they're regenerated on use.)

### Sharing external corpora

If you have a `data/normalized/<corpus>/pages.jsonl` folder (built by the dpsk4 brain tooling), you can share it with anyone who installed TDPilot from the public repo. The agent auto-discovers external corpora at three known roots:

```
~/.tdpilot/data/normalized/<corpus>/
~/.tdpilot-api/data/normalized/<corpus>/
~/.tdpilot-dpsk4/data/normalized/<corpus>/
```

**On Windows** the home dir resolves to `C:\Users\<username>\` so the path is:

```
C:\Users\<username>\.tdpilot\data\normalized\<corpus>\
```

Drop the folder, pulse `Reload Config` on the COMP (standalone) or restart the CLI server. The corpus shows up under `knowledge_search` and `knowledge_get`. mtime-based caching means repeated reads are cheap; the cache invalidates automatically when the source file changes.

---

## User-pluggable tools

Drop a Python file in `~/.tdpilot-api/tools/`, click `Reload Config` on the COMP, and the agent picks up your tool alongside the built-ins.

Minimal example — `~/.tdpilot-api/tools/td_count_operators.py`:

```python
SCHEMA = {
    "name": "td_count_operators",
    "description": "Count operators under a path with an optional family filter.",
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "/project1"},
            "family": {"type": "string"},
        },
    },
}

def handle(args):
    path = args.get("path", "/project1")
    root = op(path)  # op() is a TD global injected by the loader
    if root is None:
        return {"error": f"Path not found: {path}"}
    counts = {}
    for child in root.children:
        fam = str(getattr(child, "family", "OP")).upper()
        counts[fam] = counts.get(fam, 0) + 1
    return {"ok": True, "total": sum(counts.values()), "by_family": counts}
```

Rules:
- `SCHEMA` must be a dict with `name`, `description`, `input_schema` (a JSON Schema object). The validator rejects malformed schemas at load time.
- `handle(args) -> dict` — runs on TD's cook thread. `op()`, `parent()`, `project`, `absTime`, `tdu`, `td`, `ui`, `app` are all available.
- **Don't** use threading, asyncio, or blocking I/O in `handle()` — it stalls the cook thread at 60 FPS.
- Filenames starting with `_` are skipped, so `_helpers.py` is fine for shared code.
- A user tool with the same `name` as a built-in **wins** — the built-in is suppressed for that runtime.

To dry-validate without registering, ask the agent to call `tool_validate({"path": "td_count_operators.py"})`.

---

## Install doctor

`scripts/doctor_live.py` probes the most common "why isn't it working" failure modes against a running standalone. Run it from your project root:

```
python3 scripts/doctor_live.py             # offline checks only
python3 scripts/doctor_live.py --deep      # also probe DeepSeek with the saved key
python3 scripts/doctor_live.py --json      # machine-readable output
python3 scripts/doctor_live.py --url http://127.0.0.1:9988/   # alt port
```

Five always-on checks plus one optional deep probe:

| Check | Pass condition | If fail |
|---|---|---|
| `webserver_up` | `GET /health` returns 200 | "Drag the .tox out and back in to restart the webserver" |
| `api_key_set` | `~/.tdpilot-api/config.json` has non-empty `api_key` | "Pulse Save Key on the COMP" |
| `external_brains` | At least one corpus discoverable (jsonl OR brain.db) | (warn) "Run `npx tdpilot-dpsk4 brains add derivative`" |
| `memory_dir` | Readable + writable | "Check ownership of `~/.tdpilot-api/memory/`" |
| `user_tools` | Every `.py` compiles cleanly | Names the offending file + line |
| `api_key_valid` (`--deep`) | DeepSeek probe ≠ 401 | "Generate a fresh key at platform.deepseek.com" |

Exit code: 0 if no `fail` results, 1 otherwise. `warn` doesn't fail the run. The same registry is callable from the TouchDesigner Textport via `mod('tdpilot_api_extension').get_extension(parent()).OnVerifySetupPulse()` — see [Verify Setup pulse](#verify-setup-pulse) for the in-TD flow.

---

## Building the .tox from source

Edit the source files in `td_component/`, then rebuild via TD's Textport:

```python
import os
os.environ["TD_MCP_REPO_ROOT"] = "/ABS/PATH/TDPilot_deepseekv4"
path = os.path.join(os.environ["TD_MCP_REPO_ROOT"],
                    "td_component", "build_tdpilot_api_tox.py")
exec(compile(open(path).read(), path, "exec"), globals(), globals())
build_and_export()
```

Output: `td_component/tdpilot_API.tox` is overwritten in place. The build also installs a fresh COMP at `/project1/tdpilot_API` so you can immediately test the new build.

---

## Working loop

The same discipline applies to both variants:

1. **Inspect first.** Read state with `td_get_info`, `td_get_nodes`, `td_get_params`, `td_get_errors` before touching anything.
2. **Check memory.** Search saved memories and the knowledge corpus before rebuilding from scratch — the agent often already knows how to do what you're asking.
3. **Build in small steps.** Create one node, wire it, set params, verify. Don't batch ten edits before checking.
4. **Verify.** `td_get_errors` after each change. `td_screenshot` or `td_analyze_frame` to confirm visuals.
5. **Snapshot before risky changes.** `snapshot_save` (standalone) or `td_snapshot_scene` (CLI). Restore on regression.
6. **Save what worked.** `recipe_save` / `memory_save` (standalone) or `td_memory_learn` / `td_memory_save` (CLI) for reusable patterns.

For CLI typed patch sessions (`td_plan_patch` → `td_preflight_patch` → `td_patch_apply` → `td_patch_validate`), the patch is wrapped in an undo block so the whole sequence rolls back atomically on failure.

---

## Troubleshooting

### Standalone .tox

| Symptom | Cause | Fix |
|---|---|---|
| Chat panel stuck on "thinking…" | `executor` DAT didn't force-cook | Drop the .tox out and back in. The build script and `onServerStart` both call `executor.cook(force=True)`. |
| Chat panel blank | webRenderTOP loaded `file://` | Click `Open Chat in Browser` once. Subsequent loads use `http://127.0.0.1:9987/` directly. |
| WebSocket disconnects | Live-edited a callback DAT | The WS client registry lives in `comp.storage`, so reloads don't drop clients. If they do, click `Reset` in the chat. |
| `ImportError: tdpilot_api_bm25` (or `_schema_defs`, `_schema_map`, `_batch`, `_recovery`, `_tracing`, `_compaction`) | .tox is older than the source files | Rebuild via the Textport snippet under [Building the .tox from source](#building-the-tox-from-source). |
| 401 from DeepSeek | Key not loaded | Paste the key into `Api Key`, click `Save Key to ~/.tdpilot-api/`, then `Reload Config`. |
| Agent's response is truncated | Hit `Max Tokens` ceiling | Bump `Max Tokens` (max 384000). |
| `Active Model` always shows `pro` | Model Tier is pinned to pro | Set `Model Tier` to `auto` for cost-saving routing. |
| Quickstart wizard stuck on a checked step | `/firstrun` poll returned a stale state | Hard-refresh the browser tab (Cmd-Shift-R). The poll runs every 4s while the welcome panel is visible. |
| Validation hint fires on every turn | Agent IS calling high-severity tools and never validating | This is the intended nudge from Phase 1.3. Add a `td_get_errors` call after mutations OR ignore the hint — it never blocks. |
| `THREAD CONFLICT` dialog from TD | Worker thread touched a TD global | Check the trace at `~/.tdpilot-api/traces/<today>.jsonl` for the offending tool name; usually a user tool that returned `op()` instead of `str(op)`. |
| Conversation 400s after ~20 messages | DeepSeek rejected the post-compaction prefix | Check `~/.tdpilot-api/history/<session>.jsonl` exists (compaction archived). If yes, the issue is downstream of compaction — file an issue with the trace + history. As a workaround, set `compaction_threshold: 0` in `config.json` to disable compaction entirely. |
| `corpus … not installed` despite a `*brain.db` on disk | Corpus dir name doesn't match the brain.db's `meta.brain_id` | Run `python3 scripts/doctor_live.py` — the `external_brains` check lists what's discoverable. Rename the dir to match. |

### Claude Code CLI

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot connect to TouchDesigner` at port 9985 | Bridge .tox not loaded | Drag `td_component/tdpilot-dpsk4.tox` into `/local`. |
| Tools missing from the MCP surface | Stale plugin install | `/plugin uninstall tdpilot-dpsk4` then reinstall. |
| `400 thinking blocks must be passed back` | DeepSeek requires `thinking` content blocks to round-trip in message history | Update to current — the agent keeps `thinking` blocks and only strips `reasoning_content` sub-keys. |
| `td_create_node` creates a node named "None" | Agent didn't supply `name` | The dispatcher's `_adapt_create_node` already drops null names; if you see this, the adapter chain is bypassed — file an issue. |

---

## Security

- API keys live in `~/.tdpilot-api/config.json` (standalone) or `~/.tdpilot-dpsk4/` (CLI), restricted to user-readable mode (`0600`) on POSIX. Windows ACL hardening is the user's responsibility — the standalone logs a notice on save.
- The standalone agent's exec mode is forced to `full` for the local user — same person owns the TD process and the API key, so there's no second-party security boundary to defend. User-pluggable tools execute as part of the running TD process; the trust boundary is "the user's home directory", same model as VS Code extensions or `.bashrc`.
- The CLI server defaults to `restricted` exec mode and blocks `os` / `subprocess` / file I/O. `standard` mode adds 14 safe data-transform imports (`json`, `math`, `re`, `datetime`, …). `full` mode lifts all guards — only when you trust both the client and the TD project.
- The TD-side webserver listens on `127.0.0.1` by default. Bind to a non-localhost address only if you understand the implications and have configured TLS + auth.
