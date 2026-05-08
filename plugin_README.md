# TDPilot v1.8.1 — TouchDesigner AI Assistant Plugin

This plugin installs the **DPSK4 (Claude Code CLI) variant** of TDPilot.
Two variants ship in the same repo:

| Variant | This plugin? | Install | Best for |
|---|---|---|---|
| **TDPilot DPSK4** (Claude Code MCP) | ✅ yes | `/plugin install tdpilot-dpsk4@…` | 103 tools, full Claude Code plugin ecosystem |
| **TDPilot API** (standalone .tox) | ❌ separate path | drag `td_component/tdpilot_API.tox` into TD | self-contained, no CLI, in-TD chat |

For the standalone .tox path, see the [main README](https://github.com/dreamrec/TDPilot_deepseekv4#readme) and the [MANUAL](https://github.com/dreamrec/TDPilot_deepseekv4/blob/main/docs/MANUAL.md). Both variants coexist in the same TD project (different ports + COMP names + config dirs).

---

TDPilot v1.7.0 provides 103 MCP tools for live control of TouchDesigner projects from Claude Code, optimized for DeepSeek v4. The bundled `.tox` includes a one-button installer (drag-drop into TD, click "Bootstrap All", done) — no manual setup script.

## Components

### MCP Server
- **touchdesigner-dpsk4** — Connects to TDPilot DPSK4 MCP server via `npx tdpilot-dpsk4` (stdio transport)

### Skills
- **tdpilot-dpsk4-core** — Core patching discipline: 103-tool reference, node layout, color coding, expressions, error verification, visual checks, technique memory, knowledge corpus, v1.1 features (custom parameters, project lifecycle, POP inspection)
- **tdpilot-dpsk4-production** — Production-safe workflow: staged edits, undo blocks, snapshots, completion gates, failure protocol
- **popx-touchdesigner** — POPX workflow skill for 59 GPU-accelerated operators. References must be built locally from your own licensed POPx copy (see `references/BUILD.md`)

### Commands
- **/td-check** — Run a comprehensive health check on the current TD project
- **/td-snapshot** — Create a safety snapshot of the current scene

## Setup

### Prerequisites
- TouchDesigner running with TDPilot MCP component loaded
- Node.js installed (for `npx tdpilot-dpsk4`)

### Environment
The MCP server connects to TouchDesigner via HTTP/WebSocket:
- `TD_MCP_HOST` — default `127.0.0.1`
- `TD_MCP_PORT` — default `9985`
- `TD_MCP_WS_PORT` — default `9986`

### Loading TDPilot in TouchDesigner

**Recommended (persistent across projects):**
1. Open TouchDesigner
2. Drag-and-drop `td_component/tdpilot-dpsk4.tox` into the `/local` container
3. The MCP server starts automatically and persists across project opens

**Alternative (setup script):**
```python
# In TD Textport — auto-installs into /local
exec(open("/path/to/TDPilot/setup_mcp_in_td.py").read(), globals(), globals())
```

**Per-project install:**
Import `td_component/tdpilot-dpsk4.tox` directly into your project root.

The TOX file is included in this plugin under `td_component/tdpilot-dpsk4.tox`.

## Usage

Once installed, TDPilot skills activate automatically whenever you mention TouchDesigner, TD, TOPs, CHOPs, SOPs, or any TD-related topic. Use `/td-check` for quick health checks and `/td-snapshot` before major changes.

## v1.1 Features
- `td_custom_parameters` — Declarative custom parameter pages on COMPs
- `td_project_lifecycle` — Save/load/undo/redo/undo-blocks
- `td_pop_inspect` — POP-native data inspection and attribute sampling
- Structured JSON results from `td_exec_python`
