# Anti-Patterns — Things That Look Right and Aren't

A consolidated catalog of traps. Each one looks fine on first read and breaks on first run. Cross-referenced from the core SKILL.md and other reference files.

When you catch yourself about to do one of these, STOP and apply the fix.

---

## Don't: Destroy and recreate same-named nodes in one Python block

```python
# WRONG — produces "Invalid OP object" errors
op('/project1/feedback').destroy()
op('/project1').create(feedbackTOP, 'feedback')
op('/project1/feedback').par.top = op('over')   # crash — old ref still in scope
```

`destroy()` marks the node for deletion **at end of cook**; the second-line `create()` happens before the deletion completes, so the new node has the same name but the next reference resolves to the half-dead old node.

**Fix:** Split into two separate MCP calls — one to destroy, one to create. Or, simpler: use `td_delete_node` followed by `td_create_node` (separate tool calls = separate cook boundaries).

---

## Don't: Use Lag CHOP or Filter CHOP for spectrum smoothing

Both CHOPs operate in timeslice mode. Feed them a 256-sample-per-cook AudioSpectrum and they expand it to 2400+ samples per cook downstream. The CHOP-to-TOP becomes a wall of stale data; the shader sees noise.

**Fix:** Smooth in the GLSL shader via a feedback texture (`mix(prevFrame, currentSample, attackRate)`). See `audio-reactive-glsl.md`.

---

## Don't: Use `top.save(path)` for animation captures

Each call captures the current GPU texture. In a loop you get N copies of the same frame.

**Fix:** `moviefileoutTOP`. Frame-locked to cook. See `recording-and-export.md`.

---

## Don't: Hardcode `/project1/...` paths inside script callbacks (Python)

```python
# WRONG — breaks when the COMP is reused or moved
def onValueChange(par, prev):
    op('/project1/myCOMP/target').par.value = par.val
```

The moment someone copies the COMP into another project or relocates it, the callback can't find `/project1/myCOMP/target` anymore.

**Fix:** Use `me.parent()` or `parent()` to find the host:

```python
def onValueChange(par, prev):
    parent().op('target').par.value = par.val
```

**Note:** This rule applies to Python callbacks only (`executeDAT`, `scriptOp`, panel callbacks). For parameter **expressions** (the `.expr` strings inside parameter fields), absolute paths are often required — see SKILL.md §9.

---

## Don't: Trust `td_get_errors == 0` as a render-success signal

`td_get_errors` only catches engine-level errors (broken refs, type mismatches). It does NOT catch:

- Empty geometry inside a geo COMP (default POP `torus1`, scale=0, etc.)
- Camera frustum miss
- Material assignment missing
- Instances at NaN positions
- Output that's solid black for any of a dozen render-pipeline reasons

**Fix:** After every render-chain build, `td_screenshot(path=<final_top>)` and visually verify before claiming success. SKILL.md §11 has the full check.

---

## Don't: Wire a Feedback TOP's output back to its input

It cycles, TD's static analyzer flags it (sometimes), and even when it works the wiring is fragile.

**Fix:** Use `feedbackTOP.par.top` (the OP reference parameter). Output flows forward in the visible network; the parameter closes the loop. See `glsl-idioms.md` for the explicit pattern.

---

## Don't: Forget `viewer = True` on new test COMPs

Without the viewer flag, red-bordered TD errors aren't visible in the network editor. `td_get_errors` may or may not catch them depending on whether they're engine-level.

**Fix:** Bake `op(test_comp).viewer = True` into every new test build.

---

## Don't: Pass strings to reference-style params

```python
# WRONG — silent-null guard catches this, but the lesson is the same
td_set_params({"instanceop": "/project1/source"})  # path string
```

`instanceop`, `material`, `camera`, `lights`, `geometry` need real OP references, not path strings.

**Fix:** `td_exec_python` with `op(target).par.instanceop = op(source_path)`. The reference resolves at the assignment site, not at use site.

---

## Don't: Default to `td_exec_python` when a native tool exists

The native tools (`td_create_node`, `td_set_params`, `td_connect_nodes`, `td_custom_parameters`, `td_pop_inspect`) are validated, batched into proper undo blocks, and surface clear errors. `td_exec_python` is the escape hatch — fast but it bypasses all the safety.

**Fix:** Reach for `td_exec_python` only when no native tool covers the operation. Document why when you do.

---

## Don't: Assume `outputresolution` will exceed 1280×1280 on Non-Commercial

Non-Commercial TouchDesigner silently clamps every TOP to 1280×1280. The user sees stretched output in the recorded file with no error.

**Fix:** Confirm license tier with `td_get_info` before promising a 4K render. See `recording-and-export.md`.

---

## Don't: Set `.expr` without setting `.mode`

```python
# WRONG — expression assigned but mode not updated; parameter still uses .val
op('node').par.scrollspeed.expr = "absTime.seconds * 0.3"
```

The expression sits dormant. The parameter still reads its raw `.val`, so the animation is frozen.

**Fix:**

```python
op('node').par.scrollspeed.expr = "absTime.seconds * 0.3"
op('node').par.scrollspeed.mode = ParMode.EXPRESSION
```

---

## Don't: Skip `td_get_hints` on unfamiliar operator types

Operator-specific quirks (default child nodes, required wiring, parameter mode interactions) are surfaced by `td_get_hints` and not by raw operator docs. Skipping the hints call to "save a tool round-trip" trades 1 tool call for 5+ debugging cycles.

**Fix:** Call `td_get_hints` before building. See SKILL.md §0 rule 5.

---

## Don't: Trust a single "Not enough sources specified" warning as a failure

TD's static analyzer flags cyclic dependencies (Feedback TOP, some POP setups) as warnings before first cook. After first cook, output often stabilizes and the warning becomes benign.

**Fix:** `td_screenshot` the output. If the image is real (non-black, varying) and the file size > a few KB, the chain is rendering. The warning is a static-analyzer artifact, not a runtime failure. SKILL.md §11 covers this for Feedback TOP specifically.

---

## Don't: Build a render chain without snapshotting first

If something goes wrong mid-build (a parameter writes NaN, a wiring inversion, a stuck expression), restoring to a known-good state is the fastest recovery. Without a snapshot, you're rebuilding from memory.

**Fix:** `td_snapshot_scene` before every render-chain build, every destructive refactor, every "let me try something." See `references/advanced-workflows.md` for snapshot patterns.

---

## See Also

- SKILL.md §0 — Mandatory STOP Rules (the preflight discipline these traps violate)
- SKILL.md §11 — Render Pipeline Pitfalls (deeper coverage of the render-chain traps)
- `audio-reactive-glsl.md` — full audio-reactive chain with empirically verified magic numbers
- `glsl-idioms.md` — GLSL TOP time, inputs, feedback wiring
- `recording-and-export.md` — codec license matrix, pre-recording checklist
- `advanced-workflows.md` — snapshots, safety system, optimization
