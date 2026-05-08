```
████████╗██████╗ ██████╗ ██╗██╗      ██████╗ ████████╗
╚══██╔══╝██╔══██╗██╔══██╗██║██║     ██╔═══██╗╚══██╔══╝
   ██║   ██║  ██║██████╔╝██║██║     ██║   ██║   ██║
   ██║   ██║  ██║██╔═══╝ ██║██║     ██║   ██║   ██║
   ██║   ██████╔╝██║     ██║███████╗╚██████╔╝   ██║
   ╚═╝   ╚═════╝ ╚═╝     ╚═╝╚══════╝ ╚═════╝    ╚═╝
```

# TDPilot — DeepSeek v4 · v1.8.0

[![CI](https://github.com/dreamrec/TDPilot_deepseekv4/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dreamrec/TDPilot_deepseekv4/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/tdpilot-dpsk4?label=npm)](https://www.npmjs.com/package/tdpilot-dpsk4)
[![downloads](https://img.shields.io/npm/dm/tdpilot-dpsk4?label=downloads)](https://www.npmjs.com/package/tdpilot-dpsk4)
[![license](https://img.shields.io/badge/license-MIT-blue)](./LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](./pyproject.toml)
[![tools](https://img.shields.io/badge/tools-91%20%28standalone%29%20%C2%B7%20103%20%28CLI%29-blueviolet)](./docs/MANUAL.md)
[![TouchDesigner](https://img.shields.io/badge/TouchDesigner-2025.30000%2B-ff6200)](https://derivative.ca)
[![DeepSeek](https://img.shields.io/badge/DeepSeek-v4-00a86b)](https://deepseek.com)

An AI assistant that lives inside TouchDesigner. It can inspect your network, build new operators, wire them up, debug errors, take screenshots, remember things between sessions, replay successful patterns, surface relevant memories before each turn, batch tool calls, recover from failures with actionable hints, and survive long conversations via context compaction.

> **v1.7.0 · TouchDesigner build 2025.32820 (May 2026) support.** New operator knowledge for Trace POP, Triangulate POP, Layer Mix TOP, Render Simple TOP, NVIDIA RTX Video TOP, ST2110 In/Out, Pan Tilt CHOP, the DMX POP pipeline, and more. Existing cards (Render TOP, Movie File In, Constant TOP, Noise TOP) refreshed for the build's new params (3D textures, 2D arrays, render pulse, 4D noise derivatives, KTX2). Migration trap: **Polygonize POP is now 3D-only — for 2D inputs use Trace POP.** ZED operators now route through a central ZED TOP. See `td_get_release_delta` for the full release card. Build scripts now pass `encoding="utf-8"` so the Textport rebuild path works on macOS regardless of locale.

There are two ways to run it. Pick whichever fits — they coexist in the same TD project if you want both.

| | **Standalone .tox** | **Claude Code CLI** |
|---|---|---|
| **Install effort** | Drag one file in, paste a key. | Install Claude Code, install the plugin, configure MCP. |
| **Where chat lives** | Browser tab + a panel inside TD. | Your Claude Code terminal. |
| **Tools** | 91 curated for in-TD use | 103 (full surface) |
| **Best for** | Live performance, quick patches, demos, "no setup" use | Heavy multi-file projects, long sessions, full Claude Code ecosystem |
| **TD port** | 9987 | 9985 + 9986 |
| **Config dir** | `~/.tdpilot-api/` | `~/.tdpilot-dpsk4/` |

**New here?** Start with the standalone — it's working in under two minutes. Read on, or jump straight to [`docs/MANUAL.md`](docs/MANUAL.md) for the deep reference.

---

## Standalone .tox — first-time install (Mac, Windows, Linux)

### What you need

- TouchDesigner 2025.30000 or newer.
- A DeepSeek API key. Get one at [platform.deepseek.com](https://platform.deepseek.com/) — pay-as-you-go, no subscription. v4-flash is cheap; v4-pro is fine for serious work.
- A modern browser. Chrome, Edge, Firefox, Safari — any of them work.

### Step-by-step

**1. Drop the .tox into your project.**
Open TouchDesigner. Drag `td_component/tdpilot_API.tox` from this repo into your network. It lands at `/project1/tdpilot_API` — a purple containerCOMP about 900×600.

**2. Paste your API key.**
Click on the COMP to open its parameters (right side panel, or hit `p`). On the **API** page, find `Api Key`, paste your DeepSeek key, then click `Save Key to ~/.tdpilot-api/`. The key is written to `~/.tdpilot-api/config.json` with restricted permissions on macOS/Linux.

You only do this once — TDPilot reads the key from that file on every load.

**3. Open the chat.**
Click `Open Chat in Browser`. A new tab opens at `http://127.0.0.1:9987/` with the chat UI. The same chat is also rendered inside the COMP's panel — viewer toggle on the COMP shows it natively in TD.

![TDPilot chat UI on first load](docs/images/chat.png)

**4. Say hi.**
Type something simple — "what's the project's FPS?" or "list the operators in /project1". The agent answers, calls tools as needed, shows them inline, and ends with a text reply. The chime at the end means it's done.

That's it. You're chatting with TouchDesigner.

### What the parameters control

The COMP has three parameter pages:

**API page** — model + budget settings. Most you can leave alone:

| Param | Default | What it does |
|---|---|---|
| `Api Key` | empty | Your DeepSeek key. |
| `Model` | `deepseek-v4-pro` | Pro tier model. Smarter, slightly slower. |
| `Flash Model Name` | `deepseek-v4-flash` | Flash tier. Cheaper, snappier on lookup-style prompts. |
| `Model Tier` | `auto` | `auto` routes per turn (long/code-heavy → pro, short lookups → flash). Or pin `flash` / `pro` manually. |
| `Max Tokens` | `32768` | Output budget per call. Crank to 384000 if you want long-form replies. |
| `Temperature` | `0.7` | Sampling temperature. Lower = more deterministic. |
| `Turn Budget` | `100` | Max tool-use rounds before the loop hard-stops. |
| `Sound on completion` | on | Plays a chime when the agent finishes a turn. Toggle off if you're recording or in a quiet space. |
| `Sound Volume` | `0.7` | 0.0 (silent) to 1.0 (loud). |
| `Auto-open chat in browser on load` | on | Opens `http://127.0.0.1:9987/` automatically when the .tox loads. |

**Chat page** — the controls you reach for during a session:

| Param | What it does |
|---|---|
| `Send` | Pulse to send the current message. Same as the Send button in the browser. |
| `Stop` | Pulse to interrupt the agent mid-turn. |
| `Reset` | Pulse to clear the conversation and start fresh. |

**Status page** — read-only:

| Param | What it shows |
|---|---|
| `Status` | `idle` / `thinking` / `tool` / `error` |
| `Last Tool` | The last tool the agent called. |
| `Active Model` | Which tier (flash/pro) the auto router picked for the most recent turn. |

### Sound

The completion chime is on by default. macOS plays `Glass.aiff`, Linux uses `paplay`, Windows uses the built-in beep API. Volume is the `Sound Volume` slider on the API page. To kill it entirely, untoggle `Sound on completion`.

### Browser chat

Default URL: `http://127.0.0.1:9987/`. The chat reconnects automatically if you reload the page or close and reopen the tab. Multiple tabs can follow the same session — open the URL on a phone or second monitor and they all stream the same conversation. The `Stop` button in the browser does the same thing as the COMP's `Stop` parameter.

If the browser tab shows nothing on first load, give it a second — the embedded webserver takes a frame to start. If it stays blank, click `Open Chat in Browser` once on the COMP to force a fresh open.

---

## Claude Code CLI — first-time install

If you already use Claude Code and want the full 103-tool surface plus the plugin marketplace ecosystem:

**1. Install Claude Code.**
Follow the [Claude Code install docs](https://docs.claude.com/en/docs/claude-code).

**2. Configure DeepSeek as your backend.**
Set `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` and `ANTHROPIC_MODEL=deepseek-v4-pro` in your environment.

**3. Install the plugin.** One of these three:

```
/plugin marketplace add dreamrec/TDPilot_deepseekv4
/plugin install tdpilot-dpsk4@dreamrec-TDPilot_deepseekv4
```

```
npx tdpilot-dpsk4 plugin-install
```

Or paste this `.mcp.json` block into your project root:

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

**4. Drop the bridge .tox into TouchDesigner.**
Drag `td_component/tdpilot-dpsk4.tox` into your TD `/local` container. Or run the auto-installer panel inside the .tox.

**5. Talk to TD from your Claude Code terminal.**
The 103 tools, 3 skills (`tdpilot-dpsk4-core`, `tdpilot-dpsk4-production`, `popx-touchdesigner`), and 2 slash commands (`/td-check`, `/td-snapshot`) are now available.

---

## Standalone vs CLI — when to pick which

Both variants run on the same DeepSeek backend and share the same TD-side handler layer. The difference is **where the chat lives** and **how much surface you want**.

**Pick the standalone .tox if:**
- You want to chat *while* you patch — alt-tab between the browser and your TD viewport.
- You're doing live performance and need the agent in the same window as your visuals.
- You're showing it to someone — drag-drop demo with no terminal involved.
- You want zero CLI dependencies. No Node, no uv, no Claude Code install.
- You like the chat panel rendering inside TD's UI itself.

**Pick the Claude Code CLI if** you're already using Claude Code, want long sessions across TD + non-TD code, or need any of the 15 tools that only exist on the CLI:

| Category | What the CLI exclusively gives you | Tools |
|---|---|---|
| **Typed patch sessions** | Plan a multi-step build, dry-run it, apply it, validate, generate variations — wrapped in a typed `PatchPlan` value with atomic rollback on failure. | `td_plan_patch`, `td_preflight_patch`, `td_patch_apply`, `td_patch_validate`, `td_patch_variations` |
| **Streaming output** | Push live TOP frames over WebSocket to a client at controllable FPS. | `td_stream_top`, `td_stop_stream_top` |
| **Continuous vision monitoring** | Watch a TOP over time — alpha coverage, luminance, dominant color, ROI diff between frames. | `td_monitor_visual`, `td_capture_and_analyze` |
| **Visual optimization** | Suggest improvements to a render based on weighted objectives (stability vs complexity). | `td_optimize_visual` |
| **Advanced memory** | Auto-extract a recipe from a live network, replay it elsewhere, promote project techniques to global. | `td_memory_learn`, `td_memory_replay`, `td_memory_promote` |
| **Macros & planning advanced** | Author macros from templates programmatically. | `td_create_macro` |
| **Slash commands** | One-keystroke `/td-check` (project health) and `/td-snapshot` (safety snapshot). | `/td-check`, `/td-snapshot` |
| **Plugin marketplace + skills** | Auto-activating skills for TD work; full Claude Code skill ecosystem available alongside. | `tdpilot-dpsk4-core`, `tdpilot-dpsk4-production`, `popx-touchdesigner` |

**Now in BOTH variants** (Tier 1+2 ports landed in the standalone): official-docs lookup (5 tools), TD 2025 native introspection (6 tools), recommendations (3 tools), server introspection (3 tools), audit/validate utilities (2 tools), memory export/import/favorite (3 tools).

**Standalone-exclusive runtime improvements** (shipped post-1.6.11, see [CHANGELOG](CHANGELOG.md)):

| Capability | What it does |
|---|---|
| **Cache-stable dynamic context** | Volatile per-turn state (memory / knowledge / recipes indexes) lives in a synthetic message slot so the system prompt prefix stays byte-stable — DeepSeek's auto-cache hits at ~50× discount |
| **SQLite/FTS corpus support** | `knowledge_search` / `td_search_official_docs` now work against `*brain.db` files installed via `npx tdpilot-dpsk4 brains add <corpus>`, alongside the legacy `pages.jsonl` path |
| **Pre-turn retrieval injection** | Top memory / recipe / knowledge hits surface as ambient context before each turn, no tool round-trip needed |
| **Trigger-based skill loading** | A user message containing `popx` / `slow` / `fps` / etc. auto-loads the matching skill body for the rest of the session |
| **Trust-tier-aware results** | Every search hit carries `trust_tier` (`official` > `bundled` > `personal` > `community` > `transcript` > `experimental`); the agent weights evidence and validates community/transcript hits before claiming behaviour as fact |
| **Severity-tracked validation hints** | High-severity mutations (create_node, exec_python, …) without a follow-up `td_get_errors` get a soft nudge in the chat — informational, never blocks |
| **Failure recovery hints** | 10 known error patterns ("Unknown operator type", "THREAD CONFLICT", 401, "corpus not installed", …) attach an actionable `recovery_hint` so the agent doesn't retry the same failed call 3× |
| **`tool_batch`** | Run up to 8 independent tool calls in one round trip instead of N — saves model→server→model latency on chained reads |
| **Per-turn observability traces** | `~/.tdpilot-api/traces/<YYYY-MM-DD>.jsonl` captures timing + tool calls + outcomes per turn (user text + args hashed for privacy); read via `td_get_recent_traces` |
| **Conversation compaction** | At 20+ messages the oldest portion summarises into one synthetic assistant message; recent 10 turns kept verbatim with their original thinking-block signatures intact; full history forensically preserved at `~/.tdpilot-api/history/` |
| **First-run wizard** | The chat panel polls `/firstrun` and renders a 3-step quickstart checklist (paste key → install brain → save first memory) until completion |
| **Doctor `--live`** | `python3 scripts/doctor_live.py [--deep]` probes webserver health, key validity, brain inventory, memory + user-tool dirs |

The standalone has 91 tools that cover the everyday inspect → build → wire → verify loop, plus persistent memory, knowledge corpus, recipes, snapshots, subagents (parallel fan-out), multi-model routing (auto/flash/pro), macros, user-pluggable tools (drop a `.py` in `~/.tdpilot-api/tools/`), official-docs lookup against the derivative corpus, TD 2025 runtime introspection (Python env, threading, color pipeline), and project-audit + recipe-validation utilities.

**Run both at the same time.** The two .tox files coexist in the same TD project — different ports, different config dirs, different COMP names. Standalone in the browser for quick chat, CLI in the terminal for heavy work.

---

## Repository layout

```
td_component/         TouchDesigner-side source (textDATs baked into the .tox)
  tdpilot_API.tox     Standalone .tox binary
  tdpilot-dpsk4.tox   CLI-bridge .tox binary
  build_tdpilot_api_tox.py   Build script for the standalone .tox
src/td_mcp/           DPSK4 MCP server (Python, 103 tools)
skills/               Claude Code skills (CLI plugin)
tests/                pytest suite (1122 tests + 12 agent-eval skeletons)
  agent_evals/        Live-integration evals (run with `pytest -m agent_eval`)
scripts/              Build + maintenance scripts
  doctor_live.py      Install doctor for the standalone (--deep probes DeepSeek)
  sync_counts.py      Keep README + MANUAL tool counts in sync with TOOL_SCHEMAS
  _chunk_schema_v1.py Shared chunk schema helpers used by every brain builder
docs/MANUAL.md        Full user manual (parameters, tools, troubleshooting, security)
docs/CHUNK_SCHEMA.md  Canonical chunk record format for brain.db files
docs/images/          Drop your own screenshots here
```

## Tests

```
uv run --extra dev pytest tests/
```

## License

MIT
