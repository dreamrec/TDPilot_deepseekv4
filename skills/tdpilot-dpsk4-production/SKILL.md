---
name: tdpilot-dpsk4-production
description: >
  Production-grade TouchDesigner MCP workflow for TDPilot DPSK4 v2.5.0 (DeepSeek v4 optimized, 109 tools):
  staged edits with undo blocks, rollback safety via snapshots, token-efficient
  diagnostics, strict completion gates, tool approval gates for destructive
  operations, activity log + journal hints for loop detection. For core patching
  discipline (layout, error checking, parameter authoring, POP inspection,
  technique memory), see tdpilot-dpsk4-core.
---

# TDPilot DPSK4 Production v2.5.2 (TD 2025.32820)

## Use This Skill When
- The user asks for reliable, production-safe network edits.
- The task affects live performance, show-critical logic, or many nodes.
- The user asks for "stable", "ship-ready", "realistic", or "production" workflows.

## Non-Negotiable Output Contract
- Make small, reversible edit batches wrapped in undo blocks.
- Keep token usage controlled (no continuous image payloads unless explicitly approved).
- End every meaningful mutation task with verification evidence.
- Report unresolved risks explicitly.

## DeepSeek v4: Parallel Dispatch Mandate
When a task involves multiple independent subtasks (e.g., `td_get_errors` + `td_screenshot`, or code research + documentation lookup), spawn parallel subagents with `run_in_background: true`. DeepSeek v4 has a single model tier — control parallelism through background dispatch. Keep agent prompts self-contained (agents have no conversation context). For code-writing or complex analysis, run agents in the foreground so you can verify results.

## Production Workflow

### 1) Preflight and Scope Lock
- Call `td_get_info` and `td_get_capabilities`.
- Inspect only the target scope first (`td_get_nodes`, `td_get_node_detail`, `td_get_params`).
- For comprehensive overview: `td_get_state_vector` returns project, timeline, health, performance, events, monitoring, safety, snapshots, and jobs in one call.
- Confirm exact root path and objective before mutation.

### 2) Safety Baseline
- Create a rollback point with `td_snapshot_scene`.
- For risky parameters, set bounds first with `td_set_param_bounds`.
- If scene health is unknown, run `td_detect_instability` before large edits.
- Start an undo block: `td_project_lifecycle({ action: "start_undo_block", name: "description" })`.

### 3) Mutation Strategy
- Apply edits in batches of one structural step: create → connect → parameterize → validate.
- Prefer deterministic tools over `td_exec_python`. Use `td_custom_parameters` for param pages, `td_pop_inspect` for POP data.
- Use `td_exec_python` only when no direct tool path exists.

### 4) Completion Gates (Must Pass)
- No unacknowledged critical errors in `td_get_errors` on affected root.
- Visual output verified via `td_screenshot` for any render-chain change.
- Performance remains acceptable for the stated context (`td_cooking_info`).
- Snapshot/rollback path exists and is documented.
- End the undo block: `td_project_lifecycle({ action: "end_undo_block" })`.
- Final response includes: changed scope, verification evidence, and residual risks.

## Failure Protocol
- On unsafe drift or rising errors:
  1. Pause timeline if needed (`td_emergency_stabilize` or `td_timeline_set`).
  2. Restore with `td_restore_snapshot` or `td_project_lifecycle({ action: "undo" })`.
  3. Report root cause and smallest next safe step.

## Handoff Format
- `Scope`: exact root/components changed.
- `Actions`: structural and parameter edits made.
- `Validation`: error + visual verification evidence.
- `Rollback`: snapshot id and/or undo block name, restore instructions.
- `Risks`: what is still uncertain or deferred.
