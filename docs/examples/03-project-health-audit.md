# Project health audit — open an existing .toe and find what's broken before it costs you a render

**Prerequisites:** TouchDesigner 2025.30000+, TDPilot v2.4.0+ (standalone .tox or Claude Code plugin).

**Setup:** An existing `.toe` project already loaded in TouchDesigner — ideally one you've inherited, or one you've been editing for a few hours and have stopped trusting. The agent will inspect what's there, not build anything destructive.

## The prompts

Paste these into the chat, one at a time. Wait for each turn to finish before sending the next.

1. `Run td_audit_project on /project1 and summarize: total node count, error count, instability flags, cooking-hotspot nodes. Don't fix anything yet — just inventory.`
2. `Use td_tool_batch to run these inspections in parallel: td_get_errors recursive on /project1, td_cooking_info on the slowest 10 nodes from the audit, td_get_state_vector for FPS and health, td_threading_status, td_get_server_metrics. Then collapse the output into a single status block.`
3. `Of the errors found, which are engine-level (broken refs, type mismatches) vs which are warnings I can ignore? Specifically flag any 'Not enough sources specified' on feedbackTOPs — those are static-analyzer artifacts unless the screenshot is black.`
4. `Screenshot the top 3 leaf TOPs (the ones with display flag or that look like final outputs). If any are solid black or unchanged across two screenshots 1 second apart, flag them as silently dead.`
5. `For any moviefileoutTOP in the project: confirm the output file path is writable, FPS > 0, and the source TOP is non-black. We don't want to start a recording and discover 4 minutes in that it's writing zeros.`
6. `Detect instability: td_detect_instability on the project, surface any oscillating cook times or memory growth.`
7. `Write the audit findings to td_memory_save with tags ["audit","health"] so I can replay this inspection batch on the next project.`

## Expected tool sequence

The agent should call these tools in roughly this order (variations are fine — what matters is the rough shape):

- `td_audit_project` — single high-level pass: node count, error count, hotspots
- `td_tool_batch` wrapping:
  - `td_get_errors` (path=`/project1`, recursive=true)
  - `td_cooking_info` on the slow nodes
  - `td_get_state_vector` — FPS, snapshots, health, license tier
  - `td_threading_status` — main-thread blockage check
  - `td_get_server_metrics` — MCP server health
- `td_get_info` — build / license tier for codec & resolution-cap context
- `td_screenshot` xN — leaf TOPs, taken once, then again 1s later for the dead-output check
- `td_get_params` on any moviefileoutTOP — verify `file` path, `fps`, `playmode`
- `td_detect_instability` — oscillation / memory growth heuristics
- `td_memory_save` — persist the audit recipe for replay

## The result

A single status block: clean error count vs warnings to ignore, FPS reading, hotspot list (cook time descending), leaf-TOP screenshot grid, any silently-dead outputs called out by name, license tier and any codec/resolution caps that affect this project, and an instability summary. Nothing in the project has been modified — `td_audit_project` and the batched inspection tools are all read-only.

<!-- TODO: screenshot -->

## Variations to try

- `Now also walk the device tree under /project1 and surface any operators using deprecated parameter names (TD 2025.32820 unified pattern matching — older * patterns still work but should be flagged).`
- `Compare the current state vector to a snapshot from earlier today using td_diff_snapshots — what changed?`
- `Run the audit on /local instead of /project1 to inspect a specific containerCOMP in isolation.`
