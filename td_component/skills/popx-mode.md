---
name: popx-mode
description: POPx (Lucas Morgan's popsextension library) workflow — detect first, fall back to TD 2025 native POPs if not installed
auto_load: false
priority: 5
triggers: [popx, popsextension, pop, particles, gpu]
---

# POPx Mode

**POPx** = `popsextension`, a third-party TouchDesigner library by Lucas
Morgan that ships ~59 GPU-accelerated particle/geometry operators. It is
**NOT built into TouchDesigner** — it must be installed by the user
into their project (typically as a `/local/POPx` palette component or
via the `popsextension` palette browser).

This skill governs POPx-specific work AND fallback to TD 2025's native
POP operators when POPx is not installed.

## Critical: detect POPx availability BEFORE assuming op names

POPx is **NOT** a set of registered TouchDesigner operator types you
create via `td_create_node`. POPx is a **palette component** —
typically a baseCOMP at `/local/POPx` (or similar) that wraps custom
networks and Python extensions. Probing for `solverPOP` as a
top-level op type FAILS even when POPx is installed.

This is the bug that bit a previous session: the agent probed
`td_create_node({op_type: 'solverPOP'})`, got "Unknown operator type",
concluded "POPx not installed" — but POPx WAS installed as
`/local/POPx`. Don't repeat that mistake.

### Detection protocol (in order)

**Step 1 — Search for the POPx COMP by name.** This is the fastest
and most reliable check:

```
td_search_nodes({query: 'POPx', search_type: 'name', path: '/'})
td_search_nodes({query: 'popsextension', search_type: 'name', path: '/'})
```

If either returns one or more matches, POPx is installed. The first
match's path is your POPx COMP.

**Step 2 — Check common install paths directly** (covers the case
where the user named it differently from what search finds):

```
td_get_node_detail({path: '/local/POPx'})
td_get_node_detail({path: '/local/popsextension'})
td_get_node_detail({path: '/popx'})
td_get_node_detail({path: '/popsextension'})
td_get_node_detail({path: '/project1/POPx'})
td_get_node_detail({path: '/project1/popsextension'})
```

A successful `td_get_node_detail` (no error, returns a node) confirms
POPx at that path.

**Step 3 — Inspect the POPx COMP** once located. POPx wraps its
operators inside the COMP — you create instances via:
- Drag from POPx's palette/menu (UI-only; not directly tool-callable)
- Use td_exec_python to call POPx's Python factory methods on the
  COMP (e.g., `op('/local/POPx').AddOperator('solver', ...)` — read
  the COMP's docstring or extension for the actual API)
- Copy a sub-component from inside the POPx COMP via
  `td_copy_node({source_path: '/local/POPx/solverPOP', dest_parent: '/project1'})`

**Step 4 — If you can't auto-detect**, ASK THE USER:

> "I can't find POPx at common paths. Could you tell me where it's
> installed in your project? Common locations are /local/POPx or
> /project1/POPx, but it might be elsewhere."

Don't burn tool calls guessing. ONE clarifying question is worth ten
brute-force `td_create_node` attempts.

**Step 5 (last resort) — Probe op-type registration**:
```
td_create_node({parent_path: '/project1', op_type: 'solverPOP', name: '_popx_probe'})
```

This works ONLY if POPx registers `solverPOP` as a global TD operator
type (some POPx forks do this; the standard `popsextension` release
does NOT). If it succeeds, delete the probe and proceed with full
POPx pipeline.

ALWAYS use steps 1-2 first. The op-type probe is unreliable.

## POPx pipeline (when POPx IS installed)

POPx mental model:

```
Generator → Falloff → Modifier → Tool → Simulation
```

Each stage feeds the next.

| Stage | POPx operators | Purpose |
|---|---|---|
| Generator | particlePOP, sopPOP, gridPOP | Source of particles |
| Falloff | distancePOP, noisePOP | Spatial weighting |
| Modifier | forcePOP, dragPOP, turbulencePOP, attractorPOP | Per-step attribute updates |
| Tool | colorPOP, scalePOP, alignPOP | Visual / transform finishing |
| Simulation | solverPOP | Integrates velocity → position over time |

**Without `solverPOP`, particles are static** — that's the most common
beginner mistake.

The full POPx operator catalog (~59 ops, v1.2.1) is documented in the
upstream popx-touchdesigner skill (`~/.claude/references/popx-catalog.md`
on machines where that skill is installed) — call `knowledge_search` for
specific operator names if needed.

## TD 2025 native POPs (fallback when POPx is NOT installed)

TouchDesigner 2025 ships its own POP operators — a smaller, simpler set
than POPx. **Confirmed available in TD 2025.32460** (verified by trial
creation, May 2026):

`particlePOP`, `noisePOP`, `nullPOP`, `mergePOP`, `transformPOP`,
`gridPOP`, `sortPOP`, `attributePOP`, `limitPOP`.

**Key API differences vs POPx** — knowing these prevents the "translate
POPx tutorial to TD 2025 native" mistake:

- **No `solverPOP`** — `particlePOP` has `timeintegration` built-in
  (default on). It integrates velocity → position internally.
- **No `turbulencePOP`** — use `noisePOP` with `combineop: add` and
  `combineattrscope: P` for turbulence-like position displacement.
- **No `forcePOP` / `dragPOP`** — `particlePOP` has `initdrag`,
  `damping`, `initmass` parameters directly.
- **No `colorPOP` / `spritePOP`** — colour/sprites happen on the
  rendering side via `pointspriteMAT` (note: lowercase 's' in 'sprite').
- **POPs wire via standard inputs**, not via a `targetpop` feedback
  parameter.

### Canonical TD 2025 native particle field

If POPx is NOT installed, this pipeline (verified working, recipe
`popx_particle_field`) gives you a particle field:

```
geom (geometryCOMP)
  ├── grid  (gridPOP)         ← 20×20 point source
  ├── part  (particlePOP)     ← spawn + integrate; in0 = grid
  ├── noip  (noisePOP)        ← simplex4d displacement on P; in0 = part
  └── nul1  (nullPOP)         ← display target; in0 = noip
```

Rendering siblings:
```
cam1 (cameraCOMP)             ← tz=14 ty=5 rx=-15
lght (lightCOMP)
sprt (pointspriteMAT)         ← pointscale=8, color, alpha
rend (renderTOP)              ← geometry=geom, camera=cam1, light=lght
```

Tuning knobs:
- `particlePOP.par.birthrate` — 100-200 for visible density
- `particlePOP.par.life` — 10-20 s
- `particlePOP.par.maxparticles` — 10000-20000
- `noisePOP.par.amp` — 0.3-0.6
- `pointspriteMAT.par.pointscale` — 5-12

## Workflow (regardless of POPx vs native)

1. **Inspect existing network** via `td_get_nodes` first.

2. **Detect POPx** (see Detection protocol above). Branch:
   - POPx installed → POPx pipeline (with `solverPOP`)
   - Not installed → TD 2025 native pipeline (with `particlePOP` time-integration)

3. **Use `td_pop_inspect(path)` BEFORE modifying a POP via
   `td_exec_python`**. Inspect tells you what attributes (P, v, life, id,
   ...) actually exist in this build and their value ranges.

4. **Build inside a `geometryCOMP`**, not at /project1 root. The
   geometryCOMP's children render automatically via a renderTOP
   referencing it.

5. **Use `pointspriteMAT` for visible particles** (TD 2025 native);
   POPx has its own colour/sprite ops.

6. **Camera + light + render TOP** — particles invisible without these.

## Verification recipe

After building a POPx (or native) pipeline:

1. `td_get_errors({path: '/project1', recurse: true})` — attribute
   mismatches in `mergePOP` are a common gotcha.

2. `td_cooking_info({path: '/project1', recurse: true,
   sort_by: 'cookTime'})` — a single hot POP signals tuning issues.

3. `td_analyze_frame({path: '/project1/rend', modes: ['alpha_coverage',
   'luminance']})` — coverage > 5% = visible; luminance > 0.1 = bright
   enough to render.

4. `td_screenshot({path: '/project1/rend'})` — visual confirmation.

## When the user is satisfied

Save the build as a recipe via `recipe_save` with appropriate tags
(`[popx, particles]` if POPx; `[td2025-pop, particles]` if native).

If you discovered new POPx operator quirks OR new TD 2025 native POP
behaviour during the build, save them via `knowledge_add` under
category `reference` so future sessions skip the discovery cost.

## Common pitfalls

- **Translating a POPx tutorial verbatim to TD 2025 native** — the op
  names won't match. Detect first, then pick the right pipeline.

- **Using `td_exec_python` for type enumeration without checking
  exec_mode** — `import` statements are blocked in `restricted` and
  `standard` modes. The standalone agent forces `full` mode by default
  but if a tool call fails with "ImportError" or "import not allowed",
  that's the cause. Use `td_list_families` instead — it doesn't need
  exec_python.

- **Particles invisible despite zero errors** — the `geometryCOMP`'s
  `display` flag isn't on, OR the `renderTOP`'s `geometry` param isn't
  pointing at it. Also check `geom.par.material = pointspriteMAT`.

- **Trying to use a `solverPOP` when POPx isn't installed** — that op
  type doesn't exist in vanilla TD. Use `particlePOP`'s built-in
  `timeintegration` instead.

- **Mixing POPs with mismatched attribute schemas via `mergePOP`** —
  use `attributePOP` to align schemas before merging, or accept that
  attributes will be dropped.
