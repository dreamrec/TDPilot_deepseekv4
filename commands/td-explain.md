---
description: Explain the current TD project's structure, errors, and purpose
---

Note: this command only runs in the Claude Code plugin variant of TDPilot.
It is not available in the standalone `.tox` chat.

Use the `td_` MCP tools to characterize the current TouchDesigner project. If
the user supplied a path, use it; otherwise default to `/project1`.

1. Call `td_get_nodes` on the scope path to map the node tree.
2. Call `td_get_errors` on the same scope to surface broken or warning nodes.
3. Call `td_describe_surface` to characterize what the project actually does
   (signal flow, output type, dominant operator families).

Then render a structured summary with these four sections:

- **Project structure** — operator families present, depth, notable COMPs.
- **Active errors** — failing nodes by path, error class, severity. "None" if clean.
- **What this project does** — one-paragraph plain-English description of the pipeline.
- **Suggested next steps** — 2–4 concrete actions (fix error X, optimize Y, add Z).

Keep it concise. The user wants to understand the project in 30 seconds.
