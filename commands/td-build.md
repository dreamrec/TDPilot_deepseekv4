---
description: Scaffold a starting structure from a template name or freeform description
---

Note: this command only runs in the Claude Code plugin variant of TDPilot.
It is not available in the standalone `.tox` chat.

Accept one of two argument shapes:

- **Template name** — a known recipe or macro (e.g. `feedback-loop`,
  `audio-reactive`, `extension-comp`). Resolve via `recipe_replay` first, then
  fall back to `macro_load` if no recipe matches.
- **Freeform description** — natural-language brief (e.g. "a basic GLSL
  pipeline with mouse uniforms"). Match the description against the live
  recipe/macro lists and pick the closest. If nothing fits, build from scratch
  with `td_create_node` + `td_connect_nodes`.

For the live list of valid template names, the user should run `recipe_list`
and `macro_list` — the available set changes as new templates are added.

Recommended flow:

1. Take a snapshot first with `td_snapshot_scene` (name it `pre-build`) so the
   user can roll back if the scaffold isn't what they wanted.
2. Build the structure under `/project1` unless the user gave a different path.
3. Report what was created: node count, entry/exit operators, parameters worth
   tweaking. End with "Restore with `td_restore_snapshot pre-build` if needed."
