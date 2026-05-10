# TDPilot v2.1.4

[![CI](https://github.com/dreamrec/TDPilot_deepseekv4/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/dreamrec/TDPilot_deepseekv4/actions/workflows/ci.yml)
[![npm](https://img.shields.io/npm/v/tdpilot-dpsk4?label=npm)](https://www.npmjs.com/package/tdpilot-dpsk4)
[![downloads](https://img.shields.io/npm/dm/tdpilot-dpsk4?label=downloads)](https://www.npmjs.com/package/tdpilot-dpsk4)
[![license](https://img.shields.io/badge/license-MIT-blue)](https://github.com/dreamrec/TDPilot_deepseekv4/blob/main/LICENSE)
[![MCP tools](https://img.shields.io/badge/MCP%20tools-103-blueviolet)](https://github.com/dreamrec/TDPilot_deepseekv4/blob/main/docs/MANUAL.md)

AI copilot for TouchDesigner — 103 tools for full live control via MCP, with technique memory, knowledge corpus, POPx inspection, project lifecycle control, focus + locations, hint injection, component notes, and custom parameter authoring.

## Quick start

Add to your MCP desktop client config:

```json
{
  "mcpServers": {
    "touchdesigner": {
      "command": "npx",
      "args": ["-y", "tdpilot-dpsk4"]
    }
  }
}
```

That's it. On first run it installs `uv` and downloads the server automatically.

Useful local commands:

```bash
tdpilot-dpsk4 doctor
tdpilot-dpsk4 init --client claude-desktop
```

**TouchDesigner side:** Drop `tdpilot-dpsk4.tox` into `/local` (persists across project opens).

For full docs, setup guides, and the .tox component: **[github.com/dreamrec/TDPilot_deepseekv4](https://github.com/dreamrec/TDPilot_deepseekv4)**
