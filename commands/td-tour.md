---
description: 30-second capability tour for first-time TDPilot users
---

Note: this command only runs in the Claude Code plugin variant of TDPilot.
It is not available in the standalone `.tox` chat.

Auto-run a 5-step tour to show the user what TDPilot can do. Narrate each step
in one short sentence before calling the tool, and one short sentence after
with the result. Do not ask for confirmation between steps — run them all.

1. **Health check** — call `td_audit_project` to confirm the session is alive
   and the project is in a clean state.
2. **Create a node** — call `td_create_node` to drop a harmless test operator
   (a `constantCHOP` or `noiseTOP`) into `/project1`. Pick a unique name like
   `tdpilot_tour_demo`.
3. **Screenshot it** — call `td_screenshot` on the node from step 2 so the user
   sees vision works.
4. **Save a memory** — call `memory_save` with a "hello from tour" entry so the
   user sees the memory system is real and persistent.
5. **List recipes** — call `recipe_list` so the user sees the recipe library is
   available for higher-level workflows.

After step 5, give a one-line wrap-up: "TDPilot has 104 tools, vision, memory,
and recipes. Try `/td-explain` or `/td-build` next."

Leave the demo node in place — the user can delete it with `td_delete_node`.
